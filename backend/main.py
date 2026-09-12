"""
Bookeo · Backend unificador de vídeos
Despliega en Railway · Python 3.11+

Endpoints:
  POST /crear-pedido/propuestas →  sube fotos+vídeos a Drive, analiza con IA, devuelve 2 portadas
  POST /crear-pedido/confirmar  →  lanza el CÁLCULO de páginas a Celery (fase rápida, editor)
  POST /crear-pedido/subir-foto →  sube al servidor una foto añadida a mitad de edición
  WS   /ws/editor/{pedido_id}   →  recibe cada página según se calcula, para el editor
  POST /crear-pedido/finalizar  →  recibe la estructura ya editada por el cliente, genera el PDF final
  GET  /estado-tarea/{tarea_id} →  consulta el progreso de cualquiera de las dos fases
  GET  /ver-pdf/{tarea_id}      →  sirve el PDF final SOLO para visionado online (nunca la URL real de R2)
  POST /merge              →  recibe hasta 5 vídeos + música → devuelve MP4
  POST /reducir-video       →  recibe 1 vídeo (máx. 4 min) → devuelve el mismo vídeo comprimido
  GET  /auth/google/iniciar   →  inicia login de Google Drive del cliente
  GET  /auth/google/callback  →  recibe el token, obtiene el email, crea/identifica al cliente
  GET  /health              →  healthcheck para Railway
"""

import os
import io
import json
import traceback
import asyncio
from starlette.concurrency import run_in_threadpool
import uuid
import base64
import tempfile
import shutil
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
from pathlib import Path
from typing import Optional
from pydantic import BaseModel

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, ImageOps

from moviepy.editor import (
    VideoFileClip,
    concatenate_videoclips,
    AudioFileClip,
    CompositeAudioClip,
)

from subir_drive import procesar_video
from google_auth import generar_url_autorizacion, intercambiar_codigo_por_token_y_email
from crear_libro_railway import generar_propuestas_portada, leer_fecha, preparar_fotos_ordenadas
from supabase_client import obtener_o_crear_cliente, guardar_refresh_token_cliente, obtener_cliente_drive, guardar_datos_contacto_cliente, guardar_marketing_cliente, guardar_libro
from unir_videos import unir_videos as unir_videos_ffmpeg
from unir_videos import reducir_video as reducir_video_ffmpeg
from r2_storage import subir_a_r2
import redis.asyncio as redis_asyncio

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

# ═══════════════════════════════════════════════════════
#  GELATO — mapeo formato interno -> producto real de Gelato
# ═══════════════════════════════════════════════════════
# El UID de cada producto se saca de la ficha de Gelato (Product Catalog ->
# Photo Books -> elegir tamaño/papel/acabado -> te da este identificador).
# Solo tengo confirmado el de 20x20 (el que me pasaste). Los de 21x28 y
# 28x28 son PLACEHOLDER - hay que sacarlos igual que el primero, desde la
# web de Gelato, y pegarlos aqui antes de conectar el pedido de verdad.
GELATO_PRODUCTOS = {
    "2020": {
        "uid": "photobooks-hardcover_pf_200x200-mm-8x8-inch_pt_170-gsm-65lb-coated-silk_cl_4-4_ccl_4-4_bt_glued-left_ct_matt-lamination_prt_1-0_cpt_130-gsm-65-lb-cover-coated-silk_ver",
        "precio": 26.90,
        "etiqueta": "20×20 cm",
    },
    "2128": {
        "uid": "PENDIENTE_SACAR_DE_GELATO_21x28",
        "precio": 32.60,
        "etiqueta": "21×28 cm",
    },
    "2828": {
        "uid": "PENDIENTE_SACAR_DE_GELATO_28x28",
        "precio": 46.50,
        "etiqueta": "28×28 cm",
    },
}

app = FastAPI(title="Bookeo Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

FONTS_DIR = Path(__file__).parent / "fonts"
FONTS_DIR.mkdir(exist_ok=True)
app.mount("/fonts", StaticFiles(directory=str(FONTS_DIR)), name="fonts")

PEDIDOS_EN_PROCESO: dict = {}

# Máximo de llamadas simultáneas a la API de Claude para generar
# propuestas de portada. Con más de 4 en cola, los siguientes esperan
# su turno en vez de saturar la API o el servicio web.
SEMAFORO_IA = asyncio.Semaphore(4)

MUSIC_DIR = Path(__file__).parent / "music"
MUSIC_DIR.mkdir(exist_ok=True)

GENRE_FILES: dict[str, str] = {
    "romantica":   "romantica.mp3",
    "boda":        "boda.mp3",
    "aniversario": "aniversario.mp3",
    "bebe":        "bebe.mp3",
    "infantil":    "infantil.mp3",
    "familiar":    "familiar.mp3",
    "mascota":     "mascota.mp3",
    "cumpleanos":  "cumpleanos.mp3",
    "graduacion":  "graduacion.mp3",
    "comunion":    "comunion.mp3",
    "viaje":       "viaje.mp3",
    "aventura":    "aventura.mp3",
    "verano":      "verano.mp3",
    "reforma":     "reforma.mp3",
    "cinematica":  "cinematica.mp3",
    "corporativa": "corporativa.mp3",
}

MUSIC_VOLUME = 0.28


def foto_a_base64(ruta, max_lado=1000, calidad=82):
    """Lee una foto del disco y la devuelve como JPEG base64 reducido,
    listo para mostrar en el editor Fabric.js del navegador."""
    try:
        img = Image.open(ruta).convert("RGB")
        img = ImageOps.exif_transpose(img)
        if max(img.size) > max_lado:
            ratio = max_lado / max(img.size)
            nuevo_tam = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(nuevo_tam, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=calidad)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        return f"data:image/jpeg;base64,{b64}"
    except Exception as e:
        print(f"[DEBUG] Error generando base64 de {ruta}: {e}")
        return None


def serializar_fotos(fotos):
    """Convierte datetime a texto ISO para poder mandarlo a Celery en JSON."""
    resultado = []
    for f in fotos:
        f_copia = dict(f)
        f_copia["fecha"] = f["fecha"].isoformat()
        resultado.append(f_copia)
    return resultado


@app.get("/health")
def health():
    return {"status": "ok", "service": "bookeo-backend"}


# ═══════════════════════════════════════════════════════
#  GOOGLE DRIVE — LOGIN OAUTH DEL CLIENTE
# ═══════════════════════════════════════════════════════

@app.get("/auth/google/iniciar")
def auth_google_iniciar():
    url = generar_url_autorizacion()
    return RedirectResponse(url)


@app.get("/auth/google/callback")
def auth_google_callback(code: str = None, error: str = None):
    if error:
        return {"ok": False, "error": f"Google devolvió un error: {error}"}
    if not code:
        return {"ok": False, "error": "No se recibió el parámetro 'code' de Google"}

    try:
        refresh_token, email = intercambiar_codigo_por_token_y_email(code)
        cliente_id = obtener_o_crear_cliente(email)
        guardar_refresh_token_cliente(cliente_id, refresh_token, email=email)
    except Exception as e:
        return {"ok": False, "error": f"Fallo procesando el login de Google: {e}"}

    return RedirectResponse(
        f"https://mibookeo.es/creador.html?drive_ok=1&cliente_id={cliente_id}&email={quote(email)}"
    )


# ═══════════════════════════════════════════════════════
#  FASE A — SUBIR FOTOS/VÍDEOS + PROPUESTAS DE PORTADA
# ═══════════════════════════════════════════════════════

@app.post("/crear-pedido/propuestas")
async def crear_pedido_propuestas(
    fotos: list[UploadFile] = File(...),
    videos: list[UploadFile] = File(default=[]),
    titulo: str = Form(...),
    nombre_cliente: str = Form(...),
    cliente_id: str = Form(...),
    pedido_id: str = Form(...),
    formato: str = Form("2128"),
    orientacion: str = Form("v"),
    packs_extra: int = Form(0),
    sin_capitulos: bool = Form(False),
    desde_cero: bool = Form(False),
):
    """
    OJO: antes esta petición se quedaba abierta hasta un minuto entero
    (subir fotos a R2, subir vídeos a Drive, esperar turno y llamar a la
    IA, todo DENTRO de esta misma petición HTTP) - muy sensible a que el
    móvil del cliente perdiera cobertura un instante durante esa espera.
    Ahora esa parte lenta (Drive + IA) se movió a una tarea de Celery.

    OJO 2 - IMPORTANTE: el servicio web y el Worker son DOS CONTENEDORES
    DISTINTOS en Railway y NO comparten disco. La primera versión de este
    cambio guardaba las fotos en el /tmp de ESTE proceso y le pasaba esas
    rutas locales a la tarea de Celery - que corre en el Worker, con su
    propio /tmp vacío, así que al intentar abrir esas rutas no encontraba
    nada ("No such file or directory"). Por eso esta petición SÍ sube las
    fotos/vídeos a R2 ella misma (es la parte rápida, de red, que ya
    hacía bien en paralelo) - lo que se le pasa a la tarea de Celery son
    las CLAVES de R2, no rutas de disco; el Worker las descarga a SU
    propio disco antes de trabajar con ellas (ver generar_propuestas_task).
    """
    print(f"[DEBUG] Petición recibida: pedido_id={pedido_id}, cliente_id={cliente_id}, sin_capitulos={sin_capitulos}, desde_cero={desde_cero}")

    datos_drive = obtener_cliente_drive(cliente_id)
    print(f"[DEBUG] Datos Drive obtenidos de Supabase: {bool(datos_drive)}")

    if not datos_drive or not datos_drive.get("google_refresh_token"):
        raise HTTPException(
            status_code=400,
            detail="No se encontró la conexión de Google Drive para este cliente. Conecta Drive de nuevo."
        )
    google_refresh_token = datos_drive["google_refresh_token"]

    work_dir_subida = Path(tempfile.mkdtemp(prefix=f"bookeo_subida_{pedido_id}_"))

    try:
        def _guardar_y_subir(upload_file):
            dest = work_dir_subida / upload_file.filename
            with dest.open("wb") as f:
                shutil.copyfileobj(upload_file.file, f)
            clave_r2 = subir_a_r2(str(dest), pedido_id, upload_file.filename)
            return {"nombre": upload_file.filename, "clave_r2": clave_r2}

        fotos_meta = []
        errores = []
        with ThreadPoolExecutor(max_workers=16) as pool:
            futuros = {pool.submit(_guardar_y_subir, f): f.filename for f in fotos}
            for fut in as_completed(futuros):
                try:
                    fotos_meta.append(fut.result())
                except Exception as e:
                    print(f"[DEBUG] ERROR subiendo foto {futuros[fut]} a R2: {e}")
                    errores.append(futuros[fut])
        if errores:
            raise HTTPException(
                status_code=500,
                detail=f"Error subiendo {len(errores)} foto(s) al almacenamiento temporal: {', '.join(errores[:5])}"
            )

        videos_meta = [_guardar_y_subir(v) for v in videos]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error subiendo los archivos: {e}")
    finally:
        shutil.rmtree(work_dir_subida, ignore_errors=True)

    print(f"[DEBUG] {len(fotos_meta)} fotos y {len(videos_meta)} vídeos subidos a R2 - lanzando tarea de propuestas")

    from celery_worker import generar_propuestas_task

    tarea_datos = {
        "pedido_id": pedido_id,
        "fotos_meta": fotos_meta,
        "videos_meta": videos_meta,
        "titulo": titulo,
        "nombre_cliente": nombre_cliente,
        "cliente_id": cliente_id,
        "google_refresh_token": google_refresh_token,
        "formato": formato,
        "orientacion": orientacion,
        "packs_extra": packs_extra,
        "sin_capitulos": sin_capitulos,
        "desde_cero": desde_cero,
    }

    tarea = generar_propuestas_task.delay(tarea_datos)
    return {"ok": True, "tarea_id": tarea.id, "pedido_id": pedido_id}


# ═══════════════════════════════════════════════════════
#  FASE B — CONFIRMAR PORTADA → LANZAR TAREA A CELERY
# ═══════════════════════════════════════════════════════

@app.post("/crear-pedido/confirmar")
async def crear_pedido_confirmar(
    pedido_id: str = Form(...),
    portada_foto: Optional[str] = Form(None),
    portada_titulo: Optional[str] = Form(None),
    portada_subtitulo: Optional[str] = Form(None),
    portada_editor_json: Optional[str] = Form(None),
    portada_foto_custom: Optional[UploadFile] = File(None),
    portada_fotos_blanco: list[UploadFile] = File(default=[]),
):
    datos = PEDIDOS_EN_PROCESO.get(pedido_id)
    if not datos:
        raise HTTPException(status_code=404, detail="Pedido no encontrado o expirado. Vuelve a subir tus fotos.")

    editor = None
    if portada_editor_json:
        try:
            editor = json.loads(portada_editor_json)
        except Exception as e:
            print(f"[DEBUG] portada_editor_json invalido, se ignora: {e}")
            editor = None

    foto_personalizada_ruta = None
    foto_personalizada_r2 = None
    if portada_foto_custom and portada_foto_custom.filename:
        work_dir = Path(datos["work_dir"])
        nombre_custom = f"portada_custom_{portada_foto_custom.filename}"
        dest = work_dir / nombre_custom
        with dest.open("wb") as f:
            shutil.copyfileobj(portada_foto_custom.file, f)
        foto_personalizada_ruta = str(dest)
        try:
            foto_personalizada_r2 = subir_a_r2(str(dest), pedido_id, nombre_custom)
        except Exception as e:
            print(f"[DEBUG] ERROR subiendo portada personalizada a R2: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Error subiendo la portada personalizada al almacenamiento temporal: {e}"
            )

    # Modo blanco: puede haber varias fotos, cada una emparejada por
    # "nombre" con su posición/tamaño en portada_editor_json.fotos_blanco.
    # Se suben todas a R2 igual que la personalizada de arriba - luego
    # preparar_datos_libro() las refresca en el worker que corresponda,
    # igual que ya hace con foto_personalizada_ruta.
    fotos_blanco_archivos = {}
    for f in portada_fotos_blanco:
        if not f.filename:
            continue
        work_dir = Path(datos["work_dir"])
        dest = work_dir / f.filename
        with dest.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        try:
            clave_r2 = subir_a_r2(str(dest), pedido_id, f.filename)
        except Exception as e:
            print(f"[DEBUG] ERROR subiendo foto en blanco de portada '{f.filename}' a R2: {e}")
            continue
        fotos_blanco_archivos[f.filename] = {"ruta": str(dest), "r2": clave_r2}

    portada_elegida = {
        "foto": portada_foto if portada_foto else None,
        "titulo": portada_titulo or datos["titulo"],
        "subtitulo": portada_subtitulo or "",
        "editor": editor,
        "foto_personalizada_ruta": foto_personalizada_ruta,
        "foto_personalizada_r2": foto_personalizada_r2,
        "fotos_blanco_archivos": fotos_blanco_archivos,
    }

    work_dir = Path(datos["work_dir"])
    carpeta_sal = str(work_dir / "salida")

    from celery_worker import calcular_paginas_libro

    tarea_datos = {
        "pedido_id": pedido_id,
        "diseño": datos["diseño"],
        "fotos": serializar_fotos(datos["fotos"]),
        "videos_rutas": datos["videos_rutas"],
        "qr_urls": datos["qr_urls"],
        "portada_elegida": portada_elegida,
        "nombre_cliente": datos["nombre_cliente"],
        "carpeta_sal": carpeta_sal,
        "carpeta_temp": datos["carpeta_temp"],
        "formato": datos["formato"],
        "orientacion": datos["orientacion"],
        "caso_reparto": datos["caso_reparto"],
        "paginas_objetivo": datos["paginas_objetivo"],
        "fotos_r2": datos.get("fotos_r2", {}),
        "videos_r2": datos.get("videos_r2", {}),
        "desde_cero": datos.get("desde_cero", False),
        "packs_extra": datos.get("packs_extra", 0),
        "cliente_id": datos.get("cliente_id"),
    }

    tarea = calcular_paginas_libro.delay(tarea_datos)

    # OJO: a diferencia de antes, NO se borra PEDIDOS_EN_PROCESO aqui.
    # Hace falta mas adelante, cuando el cliente termine de editar y llame
    # a /crear-pedido/finalizar, para generar el PDF de verdad con los
    # mismos datos (fotos, portada, formato...). Se guarda tambien
    # 'tarea_datos' tal cual para no tener que reconstruirlo dos veces.
    datos["portada_elegida"] = portada_elegida
    datos["carpeta_sal"] = carpeta_sal
    datos["tarea_datos_base"] = tarea_datos

    return {"ok": True, "tarea_id": tarea.id, "pedido_id": pedido_id}


# ═══════════════════════════════════════════════════════
#  EDITOR — WEBSOCKET: reenvía cada página según se calcula
# ═══════════════════════════════════════════════════════

@app.websocket("/ws/editor/{pedido_id}")
async def ws_editor(websocket: WebSocket, pedido_id: str):
    """
    El editor (Fabric.js, en el navegador del cliente) se conecta aquí justo
    después de llamar a /crear-pedido/confirmar. Cada página que
    'calcular_paginas_libro' publica en Redis (canal bookeo:editor:{pedido_id})
    se reenvía tal cual por este WebSocket, en cuanto llega. Se cierra solo
    cuando llega el mensaje 'completo' o 'error', o si el cliente se desconecta.
    """
    await websocket.accept()
    canal = f"bookeo:editor:{pedido_id}"
    clave_snapshot = f"bookeo:editor:snapshot:{pedido_id}"
    cliente_redis = redis_asyncio.from_url(REDIS_URL)
    pubsub = cliente_redis.pubsub()

    try:
        # Nos suscribimos ANTES de comprobar nada, para no perdernos ningún
        # mensaje que se publique justo mientras hacemos la comprobación.
        await pubsub.subscribe(canal)

        # Si el cálculo ya terminó (es rápido, puede haber acabado antes de
        # que el navegador llegue a abrir este WebSocket), hay una "foto
        # fija" guardada en Redis - la mandamos entera de golpe y ya está,
        # no hace falta esperar mensajes en directo que nunca van a llegar.
        snapshot_bruto = await cliente_redis.get(clave_snapshot)
        if snapshot_bruto:
            snapshot = json.loads(snapshot_bruto)
            total = snapshot.get("total_paginas", 0)
            for indice, pagina in enumerate(snapshot.get("paginas", [])):
                await websocket.send_text(json.dumps({
                    "tipo": "pagina", "pedido_id": pedido_id,
                    "indice": indice, "total_paginas": total, "pagina": pagina,
                }))
            await websocket.send_text(json.dumps({
                "tipo": "completo", "pedido_id": pedido_id, "total_paginas": total,
                "AW": snapshot.get("AW"), "AH": snapshot.get("AH"),
                "titulo": snapshot.get("titulo"), "subtitulo": snapshot.get("subtitulo"),
                "fotos": snapshot.get("fotos", []),
                "videos": snapshot.get("videos", []),
                "formato": snapshot.get("formato"),
                "packs_extra": snapshot.get("packs_extra", 0),
                "desde_cero": snapshot.get("desde_cero", False),
                "tipo_libro": snapshot.get("tipo_libro"),
            }))
            return

        # Si no hay foto fija todavía, el cálculo sigue en marcha - escuchamos
        # los mensajes en directo segun van llegando.
        async for mensaje in pubsub.listen():
            if mensaje.get("type") != "message":
                continue

            data = mensaje["data"]
            if isinstance(data, bytes):
                data = data.decode("utf-8")

            await websocket.send_text(data)

            try:
                contenido = json.loads(data)
                if contenido.get("tipo") in ("completo", "error"):
                    break
            except Exception:
                pass

    except WebSocketDisconnect:
        print(f"[DEBUG /ws/editor] Cliente desconectado, pedido_id={pedido_id}")
    except Exception as e:
        print(f"[DEBUG /ws/editor] ERROR: {e}")
        try:
            await websocket.send_text(json.dumps({"tipo": "error", "pedido_id": pedido_id, "error": str(e)}))
        except Exception:
            pass
    finally:
        try:
            await pubsub.unsubscribe(canal)
            await pubsub.close()
        except Exception:
            pass
        try:
            await cliente_redis.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════
#  EDITOR — SUBIR UN VÍDEO NUEVO MIENTRAS SE EDITA
# ═══════════════════════════════════════════════════════

class DatosContacto(BaseModel):
    correo: str
    nombre: Optional[str] = None
    telefono: Optional[str] = None


@app.post("/crear-pedido/datos-contacto")
async def crear_pedido_datos_contacto(datos: DatosContacto):
    """
    Guarda (o actualiza) el nombre/teléfono del cliente en la tabla
    'clientes', buscándolo por correo. NO guarda dirección de envío ni de
    factura - eso solo se usa para mandarlo a Gelato al fabricar el
    pedido, no se guarda en Supabase. Se llama desde pago.html al
    rellenar el formulario.
    """
    try:
        guardar_datos_contacto_cliente(datos.correo, datos.nombre, datos.telefono)
        return {"ok": True}
    except Exception as e:
        print(f"[DEBUG] ERROR guardando datos de contacto en Supabase: {e}")
        raise HTTPException(status_code=500, detail=f"Error guardando los datos: {e}")


class DatosMarketing(BaseModel):
    correo: str
    marketing: bool = False


@app.post("/crear-pedido/marketing")
async def crear_pedido_marketing(datos: DatosMarketing):
    """
    Guarda si el cliente acepta recibir ofertas/novedades por correo. Se
    llama desde confirmar-pedido.html al marcar (o desmarcar) esa casilla.
    """
    try:
        guardar_marketing_cliente(datos.correo, datos.marketing)
        return {"ok": True}
    except Exception as e:
        print(f"[DEBUG] ERROR guardando marketing en Supabase: {e}")
        raise HTTPException(status_code=500, detail=f"Error guardando marketing: {e}")


class DatosLibro(BaseModel):
    libro_id: str
    titulo: Optional[str] = None
    tipo_libro: Optional[str] = None
    estado: Optional[str] = None
    unidad_url: Optional[str] = None
    tarea_id: Optional[str] = None
    editor_url: Optional[str] = None


@app.post("/crear-pedido/guardar-libro")
async def crear_pedido_guardar_libro(datos: DatosLibro):
    """
    Guarda (o actualiza) la fila de este libro en la tabla 'libros'. El
    "pedido_id" que se usa en el resto del backend (uno por cada álbum
    creado) es, en tu esquema, el identificador del LIBRO - la fila de
    'pedidos' (con cliente, precio total, Stripe, Gelato...) se crea más
    adelante, al pagar de verdad, agrupando los libros de ese pedido.
    """
    try:
        libro_id = datos.libro_id
        fila = datos.dict(exclude={"libro_id", "tarea_id"}, exclude_none=True)

        # El cliente ya no maneja la URL real del PDF en ningún momento
        # (ver /ver-pdf/{tarea_id} y el visor online) - aquí, en el único
        # sitio donde de verdad hace falta guardar el archivo real para
        # que producción pueda mandarlo a imprenta, se resuelve el
        # tarea_id -> pdf_url real DENTRO del backend, sin que pase nunca
        # por el navegador del cliente.
        if datos.tarea_id and not fila.get("unidad_url"):
            try:
                from celery_worker import app as celery_app
                resultado = celery_app.AsyncResult(datos.tarea_id)
                if resultado.state == "SUCCESS":
                    r = resultado.result or {}
                    if r.get("pdf_url"):
                        fila["unidad_url"] = r.get("pdf_url")
            except Exception as e:
                print(f"[DEBUG] No se pudo resolver tarea_id->pdf_url para {libro_id}: {e}")

        guardar_libro(libro_id, fila)
        return {"ok": True}
    except Exception as e:
        print(f"[DEBUG] ERROR guardando libro en Supabase: {e}")
        raise HTTPException(status_code=500, detail=f"Error guardando el libro: {e}")


@app.post("/crear-pedido/subir-foto")
async def crear_pedido_subir_foto(
    pedido_id: str = Form(...),
    foto: UploadFile = File(...),
):
    """
    El editor llama aquí en cuanto el cliente añade una foto NUEVA desde
    el panel de fotos (subida directa desde el móvil/galería, no una de
    las que ya estaban en el pedido desde el principio).

    OJO - por qué existe este endpoint: antes, una foto añadida así solo
    se leía en local en el navegador (FileReader → base64) para poder
    verla en el editor, pero nunca se subía al servidor de verdad. El
    backend no tenía forma de saber que esa foto existía, así que al
    generar el PDF final la foto se descartaba en silencio (huecos vacíos
    en la página). Este endpoint sube la foto de verdad a R2 y la
    registra en el pedido - tanto en la lista viva de fotos como en la
    copia fija que se tomó al confirmar la portada (tarea_datos_base),
    porque /crear-pedido/finalizar usa esa copia fija, no la lista viva -
    sin actualizar las dos, la foto se seguiría perdiendo igual.
    """
    datos = PEDIDOS_EN_PROCESO.get(pedido_id)
    if not datos:
        raise HTTPException(
            status_code=404,
            detail="Pedido no encontrado o expirado. Vuelve a empezar desde el creador."
        )

    try:
        work_dir = Path(datos["work_dir"])
        work_dir.mkdir(parents=True, exist_ok=True)
        nombre_archivo = f"nueva_{uuid.uuid4().hex[:8]}_{foto.filename}"
        dest = work_dir / nombre_archivo
        with dest.open("wb") as f:
            shutil.copyfileobj(foto.file, f)

        clave_r2 = subir_a_r2(str(dest), pedido_id, nombre_archivo)

        fecha, fuente = leer_fecha(str(dest))
        nueva_entrada = {"ruta": str(dest), "fecha": fecha, "nombre": nombre_archivo, "fuente_fecha": fuente}

        datos.setdefault("fotos", []).append(nueva_entrada)
        datos.setdefault("fotos_r2", {})[nombre_archivo] = clave_r2

        if "tarea_datos_base" in datos:
            entrada_serializada = dict(nueva_entrada)
            entrada_serializada["fecha"] = fecha.isoformat()
            datos["tarea_datos_base"].setdefault("fotos", []).append(entrada_serializada)
            datos["tarea_datos_base"].setdefault("fotos_r2", {})[nombre_archivo] = clave_r2

        return {"ok": True, "nombre": nombre_archivo}

    except Exception as e:
        print(f"[DEBUG /crear-pedido/subir-foto] ERROR: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error subiendo la foto: {e}")


@app.post("/crear-pedido/subir-video")
async def crear_pedido_subir_video(
    pedido_id: str = Form(...),
    video: UploadFile = File(...),
):
    """
    El editor llama aquí cuando el cliente añade un vídeo nuevo desde el
    panel de vídeos (botón "Añadir vídeo"). Sube el vídeo al Drive del
    cliente (misma carpeta del pedido que ya existe) y devuelve la URL
    pública real - así el QR que se pinte en el editor ya es el definitivo,
    no uno provisional que haya que sustituir más tarde.
    """
    datos = PEDIDOS_EN_PROCESO.get(pedido_id)
    if not datos:
        raise HTTPException(
            status_code=404,
            detail="Pedido no encontrado o expirado. Vuelve a empezar desde el creador."
        )

    cliente_id = datos.get("cliente_id")
    if not cliente_id:
        raise HTTPException(status_code=400, detail="No se encontró el cliente de este pedido.")

    datos_drive = obtener_cliente_drive(cliente_id)
    if not datos_drive or not datos_drive.get("google_refresh_token"):
        raise HTTPException(
            status_code=400,
            detail="No se encontró la conexión de Google Drive para este cliente. Conecta Drive de nuevo."
        )

    try:
        work_dir = Path(datos["work_dir"])
        work_dir.mkdir(parents=True, exist_ok=True)
        dest = work_dir / video.filename
        with dest.open("wb") as f:
            shutil.copyfileobj(video.file, f)

        url, file_id = procesar_video(
            ruta_local=str(dest),
            nombre_archivo=video.filename,
            cliente_id=cliente_id,
            pedido_id=pedido_id,
            refresh_token_cliente=datos_drive["google_refresh_token"],
            nombre_album=datos.get("titulo"),
        )

        return {"ok": True, "qr_url": url}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[DEBUG /crear-pedido/subir-video] ERROR: {e}")
        print(traceback.format_exc())
        # subir_drive.py ya traduce el error de "sin espacio en el Drive
        # del cliente" a un mensaje claro en español (RuntimeError) - si
        # es ese caso, se manda tal cual; para cualquier otro fallo
        # inesperado se mantiene el prefijo técnico de siempre.
        detalle = str(e) if isinstance(e, RuntimeError) else f"Error subiendo el vídeo a Drive: {e}"
        raise HTTPException(status_code=400, detail=detalle)


# ═══════════════════════════════════════════════════════
#  FASE FINAL — EL CLIENTE TERMINÓ DE EDITAR → PDF DE VERDAD
# ═══════════════════════════════════════════════════════

@app.post("/crear-pedido/finalizar")
async def crear_pedido_finalizar(
    pedido_id: str = Form(...),
    estructura_editada_json: str = Form(...),
    packs_extra: int = Form(None),
):
    """
    El editor llama aquí cuando el cliente termina (mueve fotos, añade
    texto...) y confirma/paga. Genera el PDF final de verdad usando
    exactamente la estructura que el cliente dejó en el editor.
    """
    datos = PEDIDOS_EN_PROCESO.get(pedido_id)
    if not datos or "tarea_datos_base" not in datos:
        raise HTTPException(
            status_code=404,
            detail="Pedido no encontrado o expirado. Vuelve a empezar desde el creador."
        )

    try:
        estructura_editada = json.loads(estructura_editada_json)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"La estructura editada no es JSON válido: {e}")

    from celery_worker import generar_libro

    tarea_datos = dict(datos["tarea_datos_base"])
    tarea_datos["estructura_editada"] = estructura_editada

    # OJO: 'paginas_objetivo' en tarea_datos_base es el que se fijó la
    # PRIMERA vez (al elegir portada), antes de que el cliente tocara nada
    # en el editor. Si desde entonces usó "+2 páginas" o "Quitar 2
    # páginas", el número de packs habrá cambiado - sin esto, el PDF
    # final seguía exigiendo el número de páginas ANTIGUO (por la
    # verificación de crear_libro_railway.py que fuerza el número exacto
    # de páginas de contenido), así que una página recién quitada por el
    # cliente volvía a aparecer de la nada en el PDF, o una recién añadida
    # se recortaba. packs_extra es opcional (Form(None)) para no romper
    # una versión vieja del editor que todavía no lo mande - en ese caso
    # se sigue usando el de tarea_datos_base, igual que antes.
    if packs_extra is not None:
        tarea_datos["packs_extra"] = packs_extra
        tarea_datos["paginas_objetivo"] = 30 + (packs_extra * 2)

    # OJO: el dibujado de la cubierta (portada+lomo+contraportada) usa
    # 'portada_elegida' - un dato que se fijó UNA sola vez, al elegir la
    # portada la primera vez, y que se queda tal cual guardado en
    # PEDIDOS_EN_PROCESO. Si el cliente usa "Editar portada" más tarde
    # (cambia título, color, fuente, foto...), ese cambio se guarda en
    # paginas[0]["editor"] dentro de estructura_editada - pero antes NADA
    # copiaba ese cambio de vuelta a 'portada_elegida', así que el PDF
    # final (y el lomo, que saca su fuente/color del título) seguía
    # dibujando la portada tal cual estaba la PRIMERA vez, ignorando por
    # completo cualquier edición posterior. Aquí se sincroniza antes de
    # generar, para que el PDF final refleje siempre la ÚLTIMA versión.
    portada_pagina = next((p for p in estructura_editada if p.get("tipo") == "portada"), None)
    if portada_pagina and portada_pagina.get("editor"):
        portada_elegida_actual = dict(tarea_datos.get("portada_elegida") or {})
        portada_elegida_actual["editor"] = portada_pagina["editor"]
        # OJO: la FOTO de portada no hace falta sincronizarla aquí - el
        # dibujado de la cubierta ya lee pagina["fotos"][0]["ruta"]
        # directamente de estructura_editada (se resuelve igual que
        # cualquier otra foto del libro), 'portada_elegida.foto' solo se
        # usa en la fase inicial de cálculo de estructura, no aquí.
        tarea_datos["portada_elegida"] = portada_elegida_actual

    tarea = generar_libro.delay(tarea_datos)

    # OJO: antes aquí se borraba PEDIDOS_EN_PROCESO[pedido_id] justo
    # después de lanzar la tarea - pensado para "esto ya se usó, se
    # limpia". Pero el cliente puede perfectamente volver al editor
    # después de revisar el PDF, tocar algo más, y pulsar "Guardar y
    # finalizar" otra vez para el MISMO libro (antes de pagar) - la
    # segunda vez ya no encontraba los datos base y fallaba con "Pedido
    # no encontrado o expirado". Se deja en memoria para que se pueda
    # regenerar tantas veces como haga falta mientras siga editando.
    return {"ok": True, "tarea_id": tarea.id, "pedido_id": pedido_id}


@app.get("/estado-tarea/{tarea_id}")
def estado_tarea(tarea_id: str):
    from celery_worker import app as celery_app
    resultado = celery_app.AsyncResult(tarea_id)

    # OJO: si el resultado guardado en el backend de Celery está mal
    # formado (p.ej. una tarea vieja que dejó un estado FAILURE con un
    # meta que no era una excepción de verdad - ver comentario en
    # celery_worker.py), CUALQUIER acceso a resultado.state/.info/.result
    # puede lanzar una excepción al intentar decodificarlo, tumbando este
    # endpoint con un 500 en vez de devolver un error legible. Con esto
    # de por medio, en el peor de los casos el cliente ve "hubo un error"
    # y puede reintentar, en vez de quedarse con la pantalla colgada para
    # siempre sin ningún mensaje.
    try:
        estado = resultado.state
    except Exception as e:
        return {"estado": "error", "detalle": f"No se pudo leer el estado de la tarea: {e}"}

    if estado == "PENDING":
        return {"estado": "esperando"}
    elif estado == "PROGRESS":
        try:
            info = resultado.info
        except Exception:
            info = None
        return {"estado": info.get("estado", "generando") if info else "generando"}
    elif estado == "SUCCESS":
        r = resultado.result or {}
        if r.get("es_propuestas"):
            # Resultado de generar_propuestas_task (fase A: análisis IA +
            # 2 propuestas de portada). Se guarda en PEDIDOS_EN_PROCESO
            # aquí, en el primer sondeo que ve el resultado listo - antes
            # esto se guardaba directamente dentro de la propia petición
            # HTTP de /crear-pedido/propuestas (que ya no hace este
            # trabajo, ver ese endpoint); Celery corre en un proceso
            # aparte y no puede escribir directamente en el diccionario en
            # memoria de este proceso web, así que hay que hacerlo aquí,
            # cuando main.py sí puede escribirlo.
            pedido_id = r.get("pedido_id")
            if pedido_id and pedido_id not in PEDIDOS_EN_PROCESO:
                PEDIDOS_EN_PROCESO[pedido_id] = {
                    "diseño": r["diseño"],
                    "fotos": r["fotos"],
                    "videos_rutas": r["videos_rutas"],
                    "qr_urls": r["qr_urls"],
                    "titulo": r["titulo"],
                    "nombre_cliente": r["nombre_cliente"],
                    "cliente_id": r["cliente_id"],
                    "work_dir": r["work_dir"],
                    "carpeta_temp": r["carpeta_temp"],
                    "formato": r["formato"],
                    "orientacion": r["orientacion"],
                    "packs_extra": r.get("packs_extra", 0),
                    "sin_capitulos": r.get("sin_capitulos", False),
                    "desde_cero": r.get("desde_cero", False),
                    "caso_reparto": r["caso_reparto"],
                    "paginas_objetivo": r["paginas_objetivo"],
                    "fotos_r2": r.get("fotos_r2", {}),
                    "videos_r2": r.get("videos_r2", {}),
                }

            fotos_dict = {f["nombre"]: f["ruta"] for f in r["fotos"]}
            portada_opciones_con_foto = []
            for op in r["portada_opciones"]:
                op_copia = dict(op)
                ruta_foto = fotos_dict.get(op.get("foto"))
                op_copia["foto_base64"] = foto_a_base64(ruta_foto) if ruta_foto else None
                portada_opciones_con_foto.append(op_copia)

            return {
                "estado": "propuestas_listas",
                "pedido_id": pedido_id,
                "tipo": (r["diseño"] or {}).get("tipo"),
                "formato": r["formato"],
                "orientacion": r["orientacion"],
                "portada_opciones": portada_opciones_con_foto,
                "videos_fallidos": r.get("videos_fallidos", []),
            }
        elif "pdf_url" in r:
            # OJO: antes se devolvía aquí el pdf_url real de R2 tal cual,
            # y el front lo pintaba como enlace directo (target="_blank")
            # - cualquiera con ese enlace podía descargarse el PDF listo
            # para imprimir sin pasar por Bookeo. Ahora solo se devuelve
            # un identificador de visionado (el propio tarea_id) - el PDF
            # real nunca sale del backend, se sirve desde /ver-pdf/ para
            # visionado online, y de ahí solo se puede ver, no descargar
            # directamente con un enlace público.
            return {"estado": "listo", "ver_pdf_id": tarea_id}
        else:
            # Resultado de calcular_paginas_libro (fase de calculo del editor)
            return {"estado": "paginas_calculadas", "total_paginas": r.get("total_paginas")}
    elif estado == "FAILURE":
        try:
            detalle = str(resultado.info)
        except Exception as e:
            detalle = f"La tarea falló (no se pudo leer el detalle del error: {e})"
        return {"estado": "error", "detalle": detalle}
    else:
        return {"estado": estado}


@app.get("/ver-pdf/{tarea_id}")
def ver_pdf(tarea_id: str, descargar: bool = False):
    """
    Sirve el PDF final. Por defecto SOLO para visionado online (lo consume
    el visor de pdf.js del front, que lo pinta en un <canvas>) - nunca se
    manda al navegador la URL real de R2, así que no hay ningún enlace que
    el cliente pueda copiar/abrir para descargarse directamente el archivo
    listo para imprimir antes de pagar.

    Con descargar=1 (usado SOLO en el botón de descarga que aparece
    después de confirmar/pagar el pedido) se sirve como "attachment" para
    que el navegador ofrezca guardarlo. OJO: mientras no haya pago real
    (Stripe) integrado, esto no comprueba que el pedido esté pagado de
    verdad - en cuanto se conecte el pago real, este endpoint debe
    verificar aquí el estado del pedido antes de permitir descargar=1.
    """
    from celery_worker import app as celery_app
    resultado = celery_app.AsyncResult(tarea_id)
    if resultado.state != "SUCCESS":
        raise HTTPException(status_code=404, detail="El PDF todavía no está listo o la tarea no existe.")

    r = resultado.result or {}
    pdf_url = r.get("pdf_url")
    if not pdf_url:
        raise HTTPException(status_code=404, detail="Esta tarea no tiene ningún PDF asociado.")

    try:
        resp = requests.get(pdf_url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo recuperar el PDF: {e}")

    return Response(
        content=resp.content,
        media_type="application/pdf",
        headers={
            # "inline" (no "attachment") - el navegador no ofrece
            # descargarlo solo por la cabecera; el visor de pdf.js del
            # front además nunca expone esta URL como enlace clicable.
            # Con descargar=1 se cambia a "attachment" para el botón de
            # descarga posterior al pago.
            "Content-Disposition": 'attachment; filename="mibookeo.pdf"' if descargar else "inline",
            "Cache-Control": "no-store",
        },
    )


# ═══════════════════════════════════════════════════════
#  MERGE DE VÍDEOS (ya existente)
# ═══════════════════════════════════════════════════════

@app.post("/merge")
async def merge_videos(
    video_1: Optional[UploadFile] = File(None),
    video_2: Optional[UploadFile] = File(None),
    video_3: Optional[UploadFile] = File(None),
    video_4: Optional[UploadFile] = File(None),
    video_5: Optional[UploadFile] = File(None),
    music_file:  Optional[UploadFile] = File(None),
    music_genre: Optional[str]       = Form(None),
):
    uploaded = [v for v in [video_1, video_2, video_3, video_4, video_5] if v is not None]
    if len(uploaded) < 2:
        raise HTTPException(status_code=400, detail="Se necesitan al menos 2 vídeos.")

    work_dir = Path(tempfile.mkdtemp(prefix="bookeo_"))

    try:
        video_paths = []
        for i, upload in enumerate(uploaded):
            ext = Path(upload.filename).suffix or ".mp4"
            dest = work_dir / f"video_{i+1}{ext}"
            with dest.open("wb") as f:
                shutil.copyfileobj(upload.file, f)
            video_paths.append(str(dest))

        music_path: Optional[str] = None

        if music_file and music_file.filename:
            music_ext = Path(music_file.filename).suffix or ".mp3"
            music_dest = work_dir / f"user_music{music_ext}"
            with music_dest.open("wb") as f:
                shutil.copyfileobj(music_file.file, f)
            music_path = str(music_dest)

        elif music_genre and music_genre in GENRE_FILES:
            candidate = MUSIC_DIR / GENRE_FILES[music_genre]
            if candidate.exists():
                music_path = str(candidate)

        carpeta_temp_merge = str(work_dir / "temp_merge")
        output_path = work_dir / "bookeo_output.mp4"

        ruta_final = unir_videos_ffmpeg(
            videos_rutas=video_paths,
            ruta_musica=music_path,
            ruta_salida=str(output_path),
            carpeta_temp=carpeta_temp_merge,
        )

        return FileResponse(
            path=ruta_final,
            media_type="video/mp4",
            filename="bookeo-video.mp4",
            background=_cleanup_task(work_dir),
        )

    except HTTPException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    except Exception as e:
        print(f"[DEBUG /merge] ERROR: {e}")
        print(traceback.format_exc())
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Error interno: {e}")


from starlette.background import BackgroundTask

def _cleanup_task(directory: Path) -> BackgroundTask:
    def _cleanup():
        shutil.rmtree(directory, ignore_errors=True)
    return BackgroundTask(_cleanup)


# ═══════════════════════════════════════════════════════
#  REDUCTOR DE VÍDEO INDIVIDUAL (no une, solo comprime)
# ═══════════════════════════════════════════════════════

@app.post("/reducir-video")
async def reducir_video_endpoint(
    video: UploadFile = File(...),
):
    work_dir = Path(tempfile.mkdtemp(prefix="bookeo_reductor_"))

    try:
        ext = Path(video.filename).suffix or ".mp4"
        dest = work_dir / f"original{ext}"
        with dest.open("wb") as f:
            shutil.copyfileobj(video.file, f)

        carpeta_temp_reductor = str(work_dir / "temp_reductor")
        output_path = work_dir / "video_reducido.mp4"

        ruta_final = await run_in_threadpool(
            reducir_video_ffmpeg,
            str(dest), str(output_path), carpeta_temp_reductor
        )

        return FileResponse(
            path=ruta_final,
            media_type="video/mp4",
            filename="bookeo-video-reducido.mp4",
            background=_cleanup_task(work_dir),
        )

    except ValueError as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    except Exception as e:
        print(f"[DEBUG /reducir-video] ERROR: {e}")
        print(traceback.format_exc())
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Error interno: {e}")