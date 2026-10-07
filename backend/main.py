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
from zoneinfo import ZoneInfo
import io
import json
import math
import traceback
import asyncio
import datetime
import hmac
import hashlib
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

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, Response, HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, ImageOps

from moviepy.editor import (
    VideoFileClip,
    concatenate_videoclips,
    AudioFileClip,
    CompositeAudioClip,
)

from subir_drive import procesar_video, obtener_servicio_drive
from google_auth import generar_url_autorizacion, intercambiar_codigo_por_token_y_email
from crear_libro_railway import generar_propuestas_portada, leer_fecha, preparar_fotos_ordenadas, extraer_fotograma, FORMATOS_MM
from supabase_client import obtener_o_crear_cliente_por_drive, guardar_refresh_token_cliente, obtener_cliente_drive, guardar_datos_contacto_cliente, guardar_marketing_cliente, guardar_libro, crear_cupones_cliente, validar_cupon, marcar_cupon_usado, crear_o_actualizar_pedido_inicial, marcar_pedido_pagado, borrar_refresh_token_cliente, guardar_valoracion
from unir_videos import unir_videos as unir_videos_ffmpeg
from unir_videos import reducir_video as reducir_video_ffmpeg
from r2_storage import subir_a_r2, generar_url_descarga, descargar_de_r2
import redis.asyncio as redis_asyncio

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

# Clave para comprobar que el aviso de envío (/webhooks/gelato) viene de
# verdad de Gelato - se pone en su panel (Developer > Webhooks > este
# webhook > Authorization) como un header con este mismo nombre y valor.
GELATO_WEBHOOK_HEADER = "X-Bookeo-Gelato-Key"
GELATO_WEBHOOK_CLAVE = os.environ.get("GELATO_WEBHOOK_CLAVE", "")

# Clave sencilla para el panel interno "admin-repetir.html" - esa página
# manda correos de verdad a cualquier dirección, así que no puede quedar
# abierta a quien encuentre la URL. Pon esta misma clave como variable de
# entorno en Railway y como valor por defecto en admin-repetir.html.
ADMIN_REPETIR_CLAVE = os.environ.get("ADMIN_REPETIR_CLAVE", "")

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

from admin_panel import router as admin_panel_router, registrar_manejadores
app.include_router(admin_panel_router)
registrar_manejadores(app)

FONTS_DIR = Path(__file__).parent / "fonts"
FONTS_DIR.mkdir(exist_ok=True)
app.mount("/fonts", StaticFiles(directory=str(FONTS_DIR)), name="fonts")

PEDIDOS_EN_PROCESO: dict = {}

# ═══════════════════════════════════════════════════════
#  COPIA DE SEGURIDAD DE LOS PEDIDOS EN CURSO (Redis)  -  FASE 1
# ═══════════════════════════════════════════════════════
# PEDIDOS_EN_PROCESO (arriba) vive en la memoria de esta copia de la web:
# si Railway la reinicia (p.ej. al desplegar), se pierde y el cliente que
# estaba a mitad de pedido ve "Pedido no encontrado o expirado". Para
# evitarlo, cada vez que un pedido se crea o cambia se guarda TAMBIÉN una
# copia en Redis, y solo si el pedido NO está en memoria se recupera de ahí.
#
# REGLAS (para que no pueda romper nada de lo que ya funciona):
#  - La memoria sigue siendo la fuente principal. Redis solo se LEE cuando
#    el pedido no está en memoria (antes eso era un 404 seguro).
#  - Ninguna función de este bloque lanza excepciones: si Redis falla o el
#    pedido no se puede serializar, solo se escribe un aviso en el log y
#    todo sigue exactamente igual que antes.
#  - Si Redis falla, se pausa 30 s para no ir esperando en cada petición.
#  - OJO: esto NO permite tener 2 copias de la web a la vez (cada copia
#    seguiría con su propia memoria "principal"). Eso sería la fase 2
#    (Redis como fuente principal).
import time as _time
import threading as _threading

PEDIDO_REDIS_TTL_SEGUNDOS = 7 * 24 * 3600   # = los 7 días que viven las fotos en R2
PEDIDO_REDIS_MAX_BYTES = 5_000_000          # un pedido normal ocupa decenas de KB
_redis_pedidos_cliente = None
_redis_pedidos_pausado_hasta = 0.0
_redis_pedidos_lock = _threading.Lock()


def _clave_pedido_redis(pedido_id):
    return f"bookeo:pedido:{pedido_id}"


def _redis_pedidos():
    """Cliente Redis (síncrono) o None si no hay que usarlo ahora mismo."""
    global _redis_pedidos_cliente
    if _time.time() < _redis_pedidos_pausado_hasta:
        return None
    if _redis_pedidos_cliente is None:
        if not REDIS_URL:
            return None
        import redis as redis_sync
        _redis_pedidos_cliente = redis_sync.from_url(
            REDIS_URL, socket_connect_timeout=2, socket_timeout=2
        )
    return _redis_pedidos_cliente


def _redis_pedidos_fallo(e, accion):
    global _redis_pedidos_cliente, _redis_pedidos_pausado_hasta
    _redis_pedidos_pausado_hasta = _time.time() + 30
    _redis_pedidos_cliente = None  # se recrea la conexión al volver a intentarlo
    print(f"[DEBUG] Redis (copia de pedidos): fallo al {accion}: {e} - se pausa 30 s, el pedido sigue funcionando desde memoria")


def _json_pedido_default(o):
    # Las fotos llevan su fecha como datetime; JSON no lo entiende, así que
    # se marca para poder devolverlo a datetime al recuperar el pedido.
    if isinstance(o, datetime.datetime):
        return {"__datetime__": o.isoformat()}
    raise TypeError(f"tipo no serializable: {type(o).__name__}")


def _json_pedido_hook(d):
    if len(d) == 1 and "__datetime__" in d:
        return datetime.datetime.fromisoformat(d["__datetime__"])
    return d


def _guardar_pedido_sync(pedido_id, datos):
    """Copia el pedido a Redis. Nunca lanza excepciones."""
    try:
        with _redis_pedidos_lock:
            cliente = _redis_pedidos()
            if cliente is None:
                return
            contenido = None
            for _ in range(3):
                try:
                    contenido = json.dumps(datos, default=_json_pedido_default)
                    break
                except RuntimeError:
                    # el diccionario cambió mientras se serializaba: se reintenta
                    _time.sleep(0.02)
                except (TypeError, ValueError) as e:
                    print(f"[DEBUG] Redis (copia de pedidos): el pedido {pedido_id} no se puede serializar, no se guarda copia: {e}")
                    return
            if contenido is None:
                print(f"[DEBUG] Redis (copia de pedidos): no se pudo serializar el pedido {pedido_id} tras 3 intentos, no se guarda copia")
                return
            if len(contenido) > PEDIDO_REDIS_MAX_BYTES:
                print(f"[DEBUG] Redis (copia de pedidos): el pedido {pedido_id} es demasiado grande ({len(contenido)} bytes), no se guarda copia")
                return
            cliente.set(_clave_pedido_redis(pedido_id), contenido, ex=PEDIDO_REDIS_TTL_SEGUNDOS)
    except Exception as e:
        _redis_pedidos_fallo(e, "guardar")


def _leer_pedido_redis_sync(pedido_id):
    """Devuelve el pedido guardado en Redis, o None. Nunca lanza excepciones."""
    try:
        cliente = _redis_pedidos()
        if cliente is None:
            return None
        bruto = cliente.get(_clave_pedido_redis(pedido_id))
    except Exception as e:
        _redis_pedidos_fallo(e, "leer")
        return None
    if not bruto:
        return None
    try:
        return json.loads(bruto, object_hook=_json_pedido_hook)
    except Exception as e:
        print(f"[DEBUG] Redis (copia de pedidos): copia del pedido {pedido_id} ilegible, se ignora: {e}")
        return None


async def guardar_pedido_async(pedido_id, datos):
    """Llamar después de crear o modificar un pedido. No espera ni rompe nada si Redis falla."""
    try:
        await run_in_threadpool(_guardar_pedido_sync, pedido_id, datos)
    except Exception as e:
        print(f"[DEBUG] Redis (copia de pedidos): error inesperado guardando {pedido_id}: {e}")


def obtener_pedido(pedido_id):
    """Igual que PEDIDOS_EN_PROCESO.get(pedido_id), pero si no está en memoria intenta recuperarlo de Redis."""
    datos = PEDIDOS_EN_PROCESO.get(pedido_id)
    if datos:
        return datos
    restaurado = _leer_pedido_redis_sync(pedido_id)
    if restaurado:
        print(f"[DEBUG] Pedido {pedido_id} recuperado de Redis (la web se había reiniciado)")
        # setdefault: si otra petición lo recuperó a la vez, se comparte el mismo diccionario
        return PEDIDOS_EN_PROCESO.setdefault(pedido_id, restaurado)
    return None


async def obtener_pedido_async(pedido_id):
    datos = PEDIDOS_EN_PROCESO.get(pedido_id)
    if datos:
        return datos
    restaurado = await run_in_threadpool(_leer_pedido_redis_sync, pedido_id)
    if restaurado:
        print(f"[DEBUG] Pedido {pedido_id} recuperado de Redis (la web se había reiniciado)")
        return PEDIDOS_EN_PROCESO.setdefault(pedido_id, restaurado)
    return None


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


@app.post("/repetir-pedido/preparar-subida")
async def repetir_pedido_preparar_subida():
    """
    Paso 1 de 2 para 'repetir-pedido.html'. Petición pequeña y rápida:
    solo genera un id nuevo y un enlace para que el NAVEGADOR suba el
    PDF DIRECTAMENTE a R2 (sin pasar por este servidor) - los PDF de
    libros reales pesan fácilmente más de 100MB, y Railway tiene un
    límite práctico de subida (y un tope duro de 5 min por petición) que
    R2 no tiene. Así el archivo grande nunca toca Railway.
    """
    from r2_storage import generar_url_subida
    libro_id = uuid.uuid4().hex
    clave_r2 = f"{libro_id}/original.pdf"
    url_subida = generar_url_subida(clave_r2, expira_segundos=3600)
    return {"ok": True, "pedido_id": libro_id, "url_subida": url_subida, "clave_r2": clave_r2}


class RepetirPedidoAnalizar(BaseModel):
    pedido_id: str
    clave_r2: str


@app.post("/repetir-pedido/analizar-pdf")
async def repetir_pedido_analizar_pdf(datos: RepetirPedidoAnalizar):
    """
    Paso 2 de 2. El navegador ya subió el PDF directo a R2 (ver
    /repetir-pedido/preparar-subida) - aquí lo DESCARGAMOS nosotros
    (servidor a servidor, rápido, sin depender de la conexión del
    cliente) solo para detectar el formato por el tamaño real de página
    (comparándolo contra FORMATOS_MM, que ya incluye el sangrado) y
    contar las páginas. El PDF se queda donde ya está en R2, listo para
    usarse como el libro más de cara al resto del sistema (pago,
    ticket/factura, correo de confirmación) - sin pasar por Celery,
    porque el PDF ya existe.

    OJO - el precio SOLO tiene en cuenta el formato y las páginas
    detectadas con la fórmula de abajo (33 páginas de PDF = 0 packs
    extra) - si algún día esa fórmula cambia (por ejemplo, si cambias el
    número de páginas base del libro), hay que actualizarla aquí.
    """
    libro_id = datos.pedido_id
    work_dir = Path(tempfile.gettempdir()) / f"bookeo_repetido_{libro_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    ruta_local = work_dir / "original.pdf"

    try:
        descargar_de_r2(datos.clave_r2, str(ruta_local))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se encontró el PDF subido ({e}). Vuelve a intentarlo desde el principio.")

    try:
        from pypdf import PdfReader
        lector = PdfReader(str(ruta_local))
        paginas = len(lector.pages)
        caja = lector.pages[0].mediabox
        ancho_mm = float(caja.width) / 2.8346457
        alto_mm = float(caja.height) / 2.8346457
        # En los PDF de Bookeo la página 1 es la CUBIERTA entera (contraportada
        # + lomo + portada en un solo spread, mucho más ancha que el libro),
        # así que su tamaño no coincide con ningún formato. El formato se
        # detecta también con la página 2, que ya es una página interior.
        medidas_paginas = [(ancho_mm, alto_mm)]
        if paginas > 1:
            caja2 = lector.pages[1].mediabox
            medidas_paginas.append((float(caja2.width) / 2.8346457, float(caja2.height) / 2.8346457))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el PDF ({e}). Prueba con el archivo original que descargaste.")

    # Compara contra los 3 formatos reales, con un margen de +-4mm por si
    # el PDF viene con alguna diferencia leve de exportación. Admite
    # también el ancho/alto invertidos (orientación horizontal del 21x28).
    formato_detectado = None
    for ancho_pag, alto_pag in medidas_paginas:
        for clave, (aw_mm, ah_mm) in FORMATOS_MM.items():
            for a, b in ((ancho_pag, alto_pag), (alto_pag, ancho_pag)):
                if abs(a - aw_mm) <= 4 and abs(b - ah_mm) <= 4:
                    formato_detectado = clave
                    break
            if formato_detectado:
                break
        if formato_detectado:
            break

    if not formato_detectado:
        raise HTTPException(
            status_code=400,
            detail=f"No hemos reconocido el formato de este PDF (página de {ancho_mm:.0f}×{alto_mm:.0f} mm). Escríbenos a info@mibookeo.es y te ayudamos a mano.",
        )

    # 33 páginas de PDF = 0 packs extra (30 de contenido + portada + las 2
    # guardas en blanco). Cada pack extra son 2 páginas de contenido más,
    # así que cada 2 páginas de PDF de más sobre esas 33 es 1 pack. Se
    # redondea hacia arriba para no cobrar de menos si el PDF viene con
    # una página suelta de diferencia.
    PAGINAS_BASE_PDF = 33
    packs_extra = max(0, math.ceil((paginas - PAGINAS_BASE_PDF) / 2))

    # Mismo precio por pack que ya usa pago.html (inferirFormato) - si
    # ese precio cambia algún día, hay que tocarlo en los dos sitios.
    PACK_PRICE = {"2020": 0.50, "2128": 0.60, "2828": 0.80}
    info_formato = GELATO_PRODUCTOS[formato_detectado]
    precio_final = round(info_formato["precio"] + packs_extra * PACK_PRICE[formato_detectado], 2)

    return {
        "ok": True,
        "pedido_id": libro_id,
        "formato": formato_detectado,
        "etiqueta_formato": info_formato["etiqueta"],
        "precio": precio_final,
        "paginas": paginas,
        "packs": packs_extra,
        "aw": FORMATOS_MM[formato_detectado][0],
        "ah": FORMATOS_MM[formato_detectado][1],
        "pdf_r2": datos.clave_r2,
    }


@app.post("/admin/enviar-repetir-pedido")
async def admin_enviar_repetir_pedido(correo: str = Form(...), clave: str = Form(...)):
    """
    Para 'admin-repetir.html' - panel interno, solo para ti. Manda a mano
    el correo con el enlace a repetir-pedido.html a un cliente concreto
    que haya pedido repetir su álbum. Protegido con ADMIN_REPETIR_CLAVE
    para que nadie más pueda mandar correos desde aquí si encontrara la
    URL de la página.
    """
    if not ADMIN_REPETIR_CLAVE or clave != ADMIN_REPETIR_CLAVE:
        raise HTTPException(status_code=403, detail="Clave incorrecta")
    try:
        from resend_client import enviar_correo_repetir_pedido
        enviar_correo_repetir_pedido(correo)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo enviar el correo: {e}")
    return {"ok": True}


@app.post("/webhooks/gelato")
async def webhook_gelato(request: Request):
    """
    Gelato manda aquí un aviso automático cada vez que el estado de un
    pedido cambia (evento 'order_status_updated') - lo configuras tú en
    su panel (Developer > Webhooks) apuntando a esta URL. Cuando el
    estado es 'shipped', se manda el correo de seguimiento al cliente
    (enviar_correo_envio, que ya estaba escrito pero sin usar hasta
    ahora).

    Protegido con una clave: en el panel de Gelato, al configurar este
    webhook, en 'Authorization' se pone un header llamado
    GELATO_WEBHOOK_HEADER con el mismo valor que GELATO_WEBHOOK_CLAVE
    aquí - sin eso, cualquiera que adivinara esta URL y un numero_pedido
    real podría forzar el envío anticipado de ese correo.

    Se devuelve 200 en cuanto se reconoce el aviso (aunque el estado no
    sea 'shipped' y no haya nada que hacer), para que Gelato no lo
    reintente sin necesidad - solo se devuelve error si algo interno
    falla de verdad, para que sus 3 reintentos automáticos puedan
    ayudar.
    """
    if not GELATO_WEBHOOK_CLAVE or request.headers.get(GELATO_WEBHOOK_HEADER) != GELATO_WEBHOOK_CLAVE:
        raise HTTPException(status_code=403, detail="Clave de webhook incorrecta o no configurada")

    evento = await request.json()
    estado = evento.get("fulfillmentStatus")
    numero_pedido = evento.get("orderReferenceId")

    if estado != "shipped" or not numero_pedido:
        return {"ok": True, "ignorado": True}

    try:
        from supabase_client import obtener_pedido_por_numero, marcar_aviso_envio_enviado
        pedido = obtener_pedido_por_numero(numero_pedido)
        if not pedido:
            print(f"[DEBUG] Webhook de Gelato: no se encontró el pedido {numero_pedido}")
            return {"ok": True, "encontrado": False}
        if pedido.get("aviso_envio_enviado"):
            return {"ok": True, "ya_enviado": True}  # aviso repetido de Gelato - no se manda dos veces

        # El seguimiento va dentro de items[0].fulfillments[0] - se coge
        # el primero: aunque el pedido lleve varios libros, normalmente
        # se envían juntos en el mismo paquete con un único seguimiento.
        items = evento.get("items") or []
        fulfillments = (items[0].get("fulfillments") or [{}]) if items else [{}]
        primero = fulfillments[0] if fulfillments else {}

        from resend_client import enviar_correo_envio
        enviar_correo_envio(
            correo=pedido.get("correo"),
            numero_pedido=numero_pedido,
            transportista=primero.get("shipmentMethodName") or "tu transportista",
            numero_seguimiento=primero.get("trackingCode") or "",
            url_seguimiento=primero.get("trackingUrl"),
            titulo_libro=pedido.get("titulo_libro"),
        )
        marcar_aviso_envio_enviado(numero_pedido)
        return {"ok": True}
    except Exception as e:
        print(f"[DEBUG] ERROR procesando el aviso de envío de Gelato ({numero_pedido}): {e}")
        from resend_client import enviar_alerta_interna
        enviar_alerta_interna("Falló el correo de envío (aviso de Gelato)", str(e), numero_pedido)
        raise HTTPException(status_code=500, detail="Error interno procesando el aviso")


@app.get("/health")
def health():
    return {"status": "ok", "service": "bookeo-backend"}


# ═══════════════════════════════════════════════════════
#  GOOGLE DRIVE — LOGIN OAUTH DEL CLIENTE
# ═══════════════════════════════════════════════════════

@app.get("/auth/google/iniciar")
def auth_google_iniciar(pedido_id: str = None):
    # pedido_id solo llega cuando se pide la conexión desde dentro del
    # editor (ver abrirModalDriveDesconectado() en editor.html). Si no
    # llega (login normal desde el creador), se manda el valor fijo
    # "creador" como state - nunca None, para que la librería de Google
    # no invente uno aleatorio por su cuenta (ver el porqué en
    # generar_url_autorizacion).
    url = generar_url_autorizacion(state=pedido_id or "creador")
    return RedirectResponse(url)


# ═══════════════════════════════════════════════════════
#  GOOGLE DRIVE — AUTORIZACIÓN ÚNICA DE LA CUENTA DE NEGOCIO
#  (para tickets/facturas - no confundir con la de arriba)
# ═══════════════════════════════════════════════════════
# Solo hace falta visitar /auth/google-negocio/iniciar UNA vez, con tu
# propia cuenta de Google (la del negocio) - al terminar, el callback te
# enseña el refresh_token en pantalla para que lo copies a mano como
# variable de entorno GOOGLE_NEGOCIO_REFRESH_TOKEN en Railway. Después
# de copiarlo, esta URL ya no hace falta volver a visitarla salvo que el
# token deje de funcionar algún día.

@app.get("/auth/google-negocio/iniciar")
def auth_google_negocio_iniciar():
    from google_auth import generar_url_autorizacion_negocio
    return RedirectResponse(generar_url_autorizacion_negocio())


@app.get("/auth/google-negocio/callback")
def auth_google_negocio_callback(code: str = None, error: str = None):
    if error or not code:
        return HTMLResponse(f"<h3>Error autorizando la cuenta de negocio: {error or 'sin código'}</h3>")
    from google_auth import intercambiar_codigo_negocio
    try:
        refresh_token = intercambiar_codigo_negocio(code)
    except Exception as e:
        return HTMLResponse(f"<h3>Error obteniendo el token: {e}</h3>")
    # Se muestra en pantalla UNA vez - nunca se guarda solo, hay que
    # copiarlo a mano a Railway. No sale en ningún log ni se manda a
    # ningún sitio.
    return HTMLResponse(f"""
      <h3>✅ Cuenta de negocio autorizada</h3>
      <p>Copia este valor y guárdalo en Railway (en <b>Bookeo</b> y en <b>booKeo-Worker</b>) como variable:</p>
      <p><code>GOOGLE_NEGOCIO_REFRESH_TOKEN</code></p>
      <textarea style="width:100%;height:80px;">{refresh_token}</textarea>
      <p style="color:#888;">Esta pantalla no vuelve a mostrar este valor - si la pierdes, repite este mismo proceso para generar uno nuevo.</p>
    """)


def pagina_error_amistosa(volver_a="creador"):
    """
    Página de error genérica para cuando algo falla a mitad del login de
    Google (o cualquier otro flujo que redirige al navegador en vez de
    llamar a un JSON). Antes se le enseñaba al cliente el JSON crudo del
    error - esto le da algo legible, con un botón para volver.
    """
    destino = "editor.html" if volver_a == "editor" else "creador.html"
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Bookeo</title>
      <style>
        body {{ font-family:-apple-system,system-ui,sans-serif; background:#f5f2ea; margin:0;
                min-height:100vh; display:flex; align-items:center; justify-content:center;
                text-align:center; padding:24px; box-sizing:border-box; }}
        .caja {{ max-width:340px; }}
        p.emoji {{ font-size:2.5rem; margin:0 0 12px; }}
        p.titulo {{ font-weight:800; color:#1a1a2e; font-size:1.05rem; margin:0 0 8px; }}
        p.texto {{ color:#7a7568; font-size:0.9rem; line-height:1.5; margin:0 0 22px; }}
        a.boton {{ display:inline-block; padding:12px 28px; background:#7db898; color:#fff;
                   border-radius:10px; font-weight:700; text-decoration:none; font-size:0.9rem; }}
      </style>
    </head>
    <body>
      <div class="caja">
        <p class="emoji">😕</p>
        <p class="titulo">Vaya, algo ha fallado</p>
        <p class="texto">Nuestro equipo ya lo sabe y lo está revisando. Vuelve atrás e inténtalo de nuevo en un momento.</p>
        <a class="boton" href="https://mibookeo.es/{destino}">Volver</a>
      </div>
    </body>
    </html>
    """)


@app.get("/auth/google/callback")
def auth_google_callback(code: str = None, error: str = None, state: str = None):
    volver_a = "editor" if (state and state != "creador") else "creador"
    if error:
        from resend_client import enviar_alerta_interna
        enviar_alerta_interna("Google devolvió un error en el login de Drive", f"error={error}, state={state}")
        return pagina_error_amistosa(volver_a)
    if not code:
        from resend_client import enviar_alerta_interna
        enviar_alerta_interna("Login de Drive sin 'code'", f"No se recibió el parámetro 'code' de Google (state={state})")
        return pagina_error_amistosa(volver_a)

    # OJO: intercambiar_codigo_por_token_y_email() habla por HTTP con los
    # servidores de Google - un corte de conexión puntual (visto ya dos
    # veces con mensajes distintos: "ConnectionTerminated" y "Server
    # disconnected", ambos de la misma familia - la conexión se corta a
    # mitad de la petición) tiraba TODO el login sin ningún reintento,
    # obligando al cliente a empezar de cero. Se reintenta una vez más
    # antes de rendirse - pero solo si el fallo tiene toda la pinta de ser
    # de RED (conexión/timeout/servidor desconectado), nunca si es que
    # Google rechazó el propio código (p.ej. "invalid_grant" porque ya se
    # usó, o porque expiró) - reintentar ESO con el mismo código no
    # serviría de nada, un código de un solo uso no se puede reutilizar
    # aunque la conexión fuera perfecta.
    ultimo_error = None
    for intento in range(2):
        try:
            refresh_token, email = intercambiar_codigo_por_token_y_email(code)
            # OJO: se busca/crea por el correo de DRIVE (google_drive_email),
            # nunca por el correo principal (email) - ver el porqué en
            # obtener_o_crear_cliente_por_drive(). Si el cliente ya había
            # rellenado antes sus datos de envío con OTRO correo, esto
            # sigue siendo un cliente nuevo aparte hasta que conecte los
            # dos - eso se resuelve del otro lado, en /crear-pedido/
            # datos-contacto, pasándole este mismo cliente_id.
            cliente_id = obtener_o_crear_cliente_por_drive(email)
            guardar_refresh_token_cliente(cliente_id, refresh_token, email=email)
            # "creador" (o vacío, por si acaso) = login normal desde
            # creador.html, como siempre. Cualquier otro valor = venimos
            # de una reconexión pedida desde dentro del editor, y ESE
            # valor es el pedido_id al que hay que volver - el editor ya
            # se encargó de guardar el estado en sessionStorage antes de
            # salir, así que al cargar lo recupera tal cual.
            if state and state != "creador":
                return RedirectResponse(
                    f"https://mibookeo.es/editor.html?pedido_id={quote(state)}&drive_ok=1"
                )
            return RedirectResponse(
                f"https://mibookeo.es/creador.html?drive_ok=1&cliente_id={cliente_id}&email={quote(email)}"
            )
        except Exception as e:
            ultimo_error = e
            mensaje = str(e).lower()
            parece_fallo_de_red = any(
                pista in mensaje for pista in (
                    "connectionterminated", "connection", "disconnected", "timeout",
                    "timed out", "reset", "temporarily",
                )
            )
            parece_codigo_rechazado = any(
                pista in mensaje for pista in ("invalid_grant", "invalid_request", "expired")
            )
            if parece_codigo_rechazado or not parece_fallo_de_red or intento == 1:
                break

    print(f"[auth/google/callback] Fallo procesando login de Google: {ultimo_error}")
    traceback.print_exc()
    from resend_client import enviar_alerta_interna
    enviar_alerta_interna("Falló el login de Google Drive", f"{ultimo_error}\n\nstate={state}")
    return pagina_error_amistosa(volver_a)


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
    OJO: esto se probó a mover a una tarea de Celery para no mantener la
    conexión abierta tanto rato, pero el Worker es un contenedor
    DISTINTO al servicio web y no comparte disco - varias partes del
    resto del flujo (crear_pedido_confirmar incluido) siguen asumiendo
    que pueden leer/escribir en el work_dir de este pedido directamente,
    y eso se rompía en cuanto ese work_dir vivía en el Worker en vez de
    aquí. Revertido a síncrono, que es como llevaba funcionando bien
    desde el principio - las propuestas (rápidas, unos 20-40s) se quedan
    en el servicio web; el libro completo (la parte de verdad pesada)
    sigue en el Worker, como ya estaba antes de tocar nada de esto.
    """
    print(f"[DEBUG] Petición recibida: pedido_id={pedido_id}, cliente_id={cliente_id}, sin_capitulos={sin_capitulos}, desde_cero={desde_cero}")

    # OJO: Google Drive solo hace falta de verdad si hay algún vídeo que
    # subir ahí - exigirlo igual cuando no hay ningún vídeo era fricción
    # sin ningún motivo que el cliente pudiera entender. El navegador ya
    # dejaba avanzar sin conectar Drive en ese caso, pero aquí, en el
    # servidor, se seguía exigiendo siempre - así que al final la petición
    # fallaba igualmente. Ahora coincide con lo que ya hace el navegador.
    google_refresh_token = None
    if videos:
        datos_drive = obtener_cliente_drive(cliente_id)
        print(f"[DEBUG] Datos Drive obtenidos de Supabase: {bool(datos_drive)}")

        if not datos_drive or not datos_drive.get("google_refresh_token"):
            raise HTTPException(
                status_code=400,
                detail="No se encontró la conexión de Google Drive para este cliente. Conecta Drive de nuevo."
            )
        google_refresh_token = datos_drive["google_refresh_token"]

    work_dir = Path(tempfile.mkdtemp(prefix=f"bookeo_pedido_{pedido_id}_"))
    carpeta_temp = work_dir / "temp"
    carpeta_temp.mkdir(exist_ok=True)

    try:
        fotos_rutas = []
        rutas_y_nombres = []
        for foto in fotos:
            dest = work_dir / foto.filename
            with dest.open("wb") as f:
                shutil.copyfileobj(foto.file, f)
            fotos_rutas.append(str(dest))
            rutas_y_nombres.append((str(dest), foto.filename))

        videos_rutas = []
        for video in videos:
            dest = work_dir / video.filename
            with dest.open("wb") as f:
                shutil.copyfileobj(video.file, f)
            videos_rutas.append(str(dest))
        print(f"[DEBUG] {len(fotos_rutas)} fotos y {len(videos_rutas)} vídeos guardados en disco")

        # -- SUBIDA (R2+Drive) Y LA IA, EN PARALELO --
        # Las dos cosas que más tardan son independientes entre sí: la IA
        # solo necesita las fotos/vídeos YA guardados en disco (arriba),
        # no que ya estén subidos a ningún sitio. Antes se hacía todo en
        # fila (subir todo, LUEGO preguntar a la IA), sumando los dos
        # tiempos - ahora se lanzan a la vez con asyncio.gather() y se
        # tarda lo que tarde el más lento de los dos, no la suma. Menos
        # tiempo con la conexión del cliente abierta = menos probabilidad
        # de que un corte de red a medias la tire ("Failed to fetch").
        # Sigue siendo la misma petición HTTP de siempre, sin Celery ni
        # nada en segundo plano - solo cambia el orden interno de qué se
        # espera cuándo.
        def _subir_todo_sync():
            # Función síncrona normal (nada de async aquí dentro) para
            # que run_in_threadpool() la pueda mandar entera a un hilo
            # aparte - así el "hilo principal" de FastAPI queda libre
            # mientras tanto para atender el run_in_threadpool() de la
            # IA a la vez, en vez de quedarse bloqueado aquí primero.
            def _subir_una_foto(item):
                ruta, nombre = item
                return nombre, subir_a_r2(ruta, pedido_id, nombre)

            fotos_r2_claves = {}
            errores_r2 = []
            with ThreadPoolExecutor(max_workers=16) as pool:
                futuros = {pool.submit(_subir_una_foto, item): item for item in rutas_y_nombres}
                for fut in as_completed(futuros):
                    ruta, nombre = futuros[fut]
                    try:
                        nombre_ok, clave_r2 = fut.result()
                        fotos_r2_claves[nombre_ok] = clave_r2
                    except Exception as e:
                        print(f"[DEBUG] ERROR subiendo foto {nombre} a R2: {e}")
                        errores_r2.append(nombre)

            if errores_r2:
                raise RuntimeError(
                    f"Error subiendo {len(errores_r2)} foto(s) al almacenamiento temporal: {', '.join(errores_r2[:5])}"
                )
            print(f"[DEBUG] {len(fotos_rutas)} fotos subidas a R2 (en paralelo)")

            # Los vídeos se suben de 3 en 3 a la vez (antes, uno detrás de otro):
            # la mayor parte del tiempo se va esperando a la red, así que varios a
            # la vez acaban antes. Más de 3 no compensa y gasta memoria.
            videos_r2_claves = {}

            def _subir_un_video_r2(ruta_video):
                nombre = Path(ruta_video).name
                return nombre, subir_a_r2(ruta_video, pedido_id, nombre)

            errores_videos_r2 = []
            if videos_rutas:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    futuros_r2 = {pool.submit(_subir_un_video_r2, r): r for r in videos_rutas}
                    for fut in as_completed(futuros_r2):
                        nombre = Path(futuros_r2[fut]).name
                        try:
                            nombre_ok, clave_r2 = fut.result()
                            videos_r2_claves[nombre_ok] = clave_r2
                        except Exception as e:
                            print(f"[DEBUG] ERROR subiendo vídeo {nombre} a R2: {e}")
                            errores_videos_r2.append(str(e))
            if errores_videos_r2:
                raise RuntimeError(f"Error subiendo vídeos al almacenamiento temporal: {errores_videos_r2[0]}")
            print(f"[DEBUG] {len(videos_rutas)} vídeos subidos a R2")

            qr_urls = {}
            thumb_urls = {}
            videos_fallidos = []  # [{"nombre": ..., "motivo": ...}] - para avisar al cliente sin tirar todo el pedido
            carpeta_temp_thumbs = Path(carpeta_temp) / "miniaturas_video"

            def _subir_un_video_drive(ruta_video):
                """Devuelve (nombre, url, miniatura, motivo_del_fallo); nunca lanza."""
                nombre_archivo = Path(ruta_video).name
                print(f"[DEBUG] Subiendo vídeo a Drive: {nombre_archivo}")
                try:
                    url, file_id = procesar_video(
                        ruta_local=ruta_video,
                        nombre_archivo=nombre_archivo,
                        cliente_id=cliente_id,
                        pedido_id=pedido_id,
                        refresh_token_cliente=google_refresh_token,
                        nombre_album=titulo,
                    )
                except Exception as e:
                    # OJO: si UN vídeo falla (p.ej. el Drive del cliente sin
                    # espacio), se aísla ese vídeo (sin su QR) y se sigue con
                    # el resto - el pedido continúa con normalidad, y se avisa
                    # de cuáles fallaron y por qué en la respuesta final.
                    print(f"[DEBUG] Vídeo '{nombre_archivo}' no se pudo subir a Drive: {e}")
                    return nombre_archivo, None, None, str(e)

                # Miniatura real (un fotograma del propio vídeo) solo para
                # que se distinga uno de otro en la galería del editor -
                # nunca para el PDF ni el QR. Si falla, no pasa nada, el
                # vídeo ya está subido y usable igualmente.
                thumb = None
                try:
                    ruta_frame = extraer_fotograma(ruta_video, str(carpeta_temp_thumbs))
                    if ruta_frame:
                        nombre_thumb = f"thumb_{Path(nombre_archivo).stem}.jpg"
                        clave_r2_thumb = subir_a_r2(ruta_frame, pedido_id, nombre_thumb)
                        thumb = generar_url_descarga(clave_r2_thumb, expira_segundos=3600 * 24)
                except Exception as e:
                    print(f"[DEBUG] No se pudo generar miniatura del vídeo '{nombre_archivo}': {e}")
                return nombre_archivo, url, thumb, None

            resultados_drive = {}
            if videos_rutas:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    futuros_drive = {pool.submit(_subir_un_video_drive, r): r for r in videos_rutas}
                    for fut in as_completed(futuros_drive):
                        ruta = futuros_drive[fut]
                        try:
                            resultados_drive[ruta] = fut.result()
                        except Exception as e:  # no debería pasar (la función no lanza), por si acaso
                            resultados_drive[ruta] = (Path(ruta).name, None, None, str(e))
            # Se recogen en el MISMO orden en que se mandaron, igual que antes.
            for ruta_video in videos_rutas:
                nombre_archivo, url, thumb, motivo = resultados_drive[ruta_video]
                if motivo is not None:
                    videos_fallidos.append({"nombre": nombre_archivo, "motivo": motivo})
                    continue
                qr_urls[nombre_archivo] = url
                if thumb:
                    thumb_urls[nombre_archivo] = thumb
            print(f"[DEBUG] Vídeos subidos a Drive: {len(qr_urls)} de {len(videos_rutas)}")

            return fotos_r2_claves, videos_r2_claves, qr_urls, thumb_urls, videos_fallidos

        async def _analizar():
            if desde_cero:
                # "Crear desde cero": sin IA. Se listan y ordenan las fotos por
                # fecha (igual que haría analizar_con_ia antes de analizar nada)
                # pero no se llama a Claude en absoluto - ni para agrupar en
                # capítulos ni para proponer portada. Es justo lo que pide este
                # modo: el cliente decide todo a mano en el editor.
                print(f"[DEBUG] Modo 'desde cero' - sin IA, solo se ordenan las fotos por fecha")
                fotos_ordenadas = await run_in_threadpool(preparar_fotos_ordenadas, fotos_rutas)
                P = 30 + (packs_extra * 2)
                return {
                    "diseño": {},
                    "fotos": fotos_ordenadas,
                    "portada_opciones": [],
                    "formato": formato,
                    "orientacion": orientacion,
                    "paginas_objetivo": P,
                    "caso_reparto": "A",
                }
            async with SEMAFORO_IA:
                print(f"[DEBUG] Esperando turno para llamar a la IA (maximo 4 a la vez)...")
                print(f"[DEBUG] Llamando a generar_propuestas_portada...")
                return await run_in_threadpool(
                    generar_propuestas_portada,
                    fotos_rutas, videos_rutas, titulo_cliente=titulo, formato=formato,
                    orientacion=orientacion, packs_extra=packs_extra, sin_capitulos=sin_capitulos
                )

        try:
            (fotos_r2_claves, videos_r2_claves, qr_urls, thumb_urls, videos_fallidos), resultado = await asyncio.gather(
                run_in_threadpool(_subir_todo_sync), _analizar()
            )
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))
        print(f"[DEBUG] Propuestas generadas correctamente")

        PEDIDOS_EN_PROCESO[pedido_id] = {
            "diseño": resultado["diseño"],
            "fotos": resultado["fotos"],
            "videos_rutas": videos_rutas,
            "qr_urls": qr_urls,
            "thumb_urls": thumb_urls,
            "titulo": titulo,
            "nombre_cliente": nombre_cliente,
            "cliente_id": cliente_id,
            "work_dir": str(work_dir),
            "carpeta_temp": str(carpeta_temp),
            "formato": resultado["formato"],
            "orientacion": resultado["orientacion"],
            "packs_extra": packs_extra,
            "sin_capitulos": sin_capitulos,
            "desde_cero": desde_cero,
            "caso_reparto": resultado["caso_reparto"],
            "paginas_objetivo": resultado["paginas_objetivo"],
            "fotos_r2": fotos_r2_claves,
            "videos_r2": videos_r2_claves,
        }
        await guardar_pedido_async(pedido_id, PEDIDOS_EN_PROCESO[pedido_id])

        fotos_dict = {f["nombre"]: f["ruta"] for f in resultado["fotos"]}
        portada_opciones_con_foto = []
        for op in resultado["portada_opciones"]:
            op_copia = dict(op)
            ruta_foto = fotos_dict.get(op.get("foto"))
            op_copia["foto_base64"] = foto_a_base64(ruta_foto) if ruta_foto else None
            portada_opciones_con_foto.append(op_copia)

        return {
            "ok": True,
            "pedido_id": pedido_id,
            "tipo": resultado["diseño"].get("tipo"),
            "formato": resultado["formato"],
            "orientacion": resultado["orientacion"],
            "portada_opciones": portada_opciones_con_foto,
            "videos_fallidos": videos_fallidos,
        }

    except HTTPException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    except Exception as e:
        print(f"[DEBUG] ERROR: {e}")
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Error generando propuestas: {e}")


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
    datos = await obtener_pedido_async(pedido_id)
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
        work_dir.mkdir(parents=True, exist_ok=True)  # por si el pedido se recuperó de Redis tras un reinicio y la carpeta ya no existe
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
        work_dir.mkdir(parents=True, exist_ok=True)  # ver nota arriba
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
        "thumb_urls": datos.get("thumb_urls", {}),
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
    await guardar_pedido_async(pedido_id, datos)

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
    # Si el navegador ya conoce el cliente_id (porque conectó Google
    # Drive antes, en esta misma sesión), lo manda aquí - así se
    # actualiza esa MISMA fila con el correo de los datos de envío, en
    # vez de crear un cliente nuevo aparte por tener un correo distinto
    # al de Drive.
    cliente_id: Optional[str] = None


@app.post("/crear-pedido/datos-contacto")
async def crear_pedido_datos_contacto(datos: DatosContacto):
    """
    Guarda (o actualiza) el nombre/teléfono del cliente en la tabla
    'clientes'. NO guarda dirección de envío ni de factura - eso solo se
    usa para mandarlo a Gelato al fabricar el pedido, no se guarda en
    Supabase. Se llama desde pago.html al rellenar el formulario.

    Devuelve el cliente_id (nuevo o el mismo que ya tenía) para que el
    navegador lo recuerde - así, si más adelante conecta Google Drive en
    esta misma sesión, esa conexión puede completar esta MISMA fila en
    vez de crear una aparte.
    """
    try:
        cliente_id = guardar_datos_contacto_cliente(datos.correo, datos.nombre, datos.telefono, cliente_id=datos.cliente_id)
        return {"ok": True, "cliente_id": cliente_id}
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


def _ahora_madrid():
    """Hora actual de Madrid (el servidor de Railway va en UTC, así que el
    nº de pedido BK-AAAAMMDD-HHMMSS salía con 2 h menos en verano). Si por lo
    que sea no hay base de zonas horarias, se queda como antes (hora del
    servidor) en vez de romper la creación del pedido."""
    try:
        return datetime.datetime.now(ZoneInfo("Europe/Madrid"))
    except Exception:
        return datetime.datetime.now()


def _nuevo_numero_pedido():
    """Número de pedido con la forma BK-AAAAMMDD-XXX: fecha de hoy (hora de
    Madrid) + 3 caracteres al azar (sin 0/O/1/I para que no se confundan al
    leerlos o dictarlos). Con solo 3 caracteres dos pedidos del mismo día
    podrían coincidir, y como al guardar se ACTUALIZA la fila si el número ya
    existe, el segundo pisaría los datos del primero - por eso se comprueba
    antes en 'pedidos' que el número esté libre y, si no, se saca otro."""
    import secrets
    alfabeto = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    fecha = _ahora_madrid().strftime("%Y%m%d")
    numero = None
    for _ in range(10):
        numero = f"BK-{fecha}-" + "".join(secrets.choice(alfabeto) for _ in range(3))
        try:
            from supabase_client import obtener_pedido_por_numero
            if not obtener_pedido_por_numero(numero):
                return numero
        except Exception:
            # Si no se puede comprobar (Supabase no responde), se usa tal cual:
            # es mejor dejar pagar que bloquear el pedido.
            return numero
    return numero


PRECIO_ENVIO = 2.90  # mismo importe y misma regla (gratis desde 2 libros) que ya usa pago.html - si cambia, tocar los dos sitios


def _precio_por_ejemplar(libro):
    """
    Precio de UN solo ejemplar de ese libro: el 'precio' de la línea (que ya
    lleva la cantidad incluida) dividido entre la cantidad. El descuento de un
    cupón se calcula siempre sobre un ejemplar del libro más caro, nunca sobre
    todos los ejemplares de la línea.
    """
    cantidad = libro.cantidad if libro.cantidad and libro.cantidad > 0 else 1
    return libro.precio / cantidad


def coste_envio(libros):
    """Cuánto se cobra de envío para este carrito - 0 si ya venía gratis
    de por sí (2 libros físicos o más), sin necesidad de ningún cupón."""
    total_libros_fisicos = sum((l.cantidad or 1) for l in libros)
    return 0.0 if total_libros_fisicos >= 2 else PRECIO_ENVIO


class LibroDelPedido(BaseModel):
    titulo: Optional[str] = None
    cantidad: Optional[int] = 1
    tarea_id: Optional[str] = None
    pedido_id: Optional[str] = None  # el id de ESE libro en la tabla 'libros' (no confundir con el nº de pedido)
    formato: Optional[str] = None  # '2020'/'2128'/'2828' - hace falta para saber si un cupón de amigo (solo mediano/grande) se puede aplicar aquí
    precio: Optional[float] = None  # precio de ESTE libro en concreto (cantidad incluida) - para saber cuál es "el más caro" del carrito
    paginas: Optional[int] = None  # 30 (base) + 2 por cada pack extra - va a libros.paginas al confirmarse el pago
    pdf_r2: Optional[str] = None  # solo en pedidos de "repetir libro" (ver /repetir-pedido/subir-pdf): la clave de R2 donde ya está el PDF que subió el cliente, sin pasar por Celery


class DatosEnvioPedido(BaseModel):
    nombre: Optional[str] = None
    correo: Optional[str] = None
    telefono: Optional[str] = None
    direccion: Optional[str] = None
    ciudad: Optional[str] = None
    codigo_postal: Optional[str] = None
    provincia: Optional[str] = None
    # OJO: estos campos ya se recogían en pago.html (el desplegable de
    # "¿Quieres factura?") pero, al no estar declarados aquí, Pydantic
    # los descartaba en silencio antes de llegar a Stripe - sin esto, la
    # factura nunca podría generarse con los datos fiscales reales.
    quiere_factura: Optional[bool] = False
    factura_nif: Optional[str] = None
    factura_razon_social: Optional[str] = None
    factura_direccion: Optional[str] = None
    factura_ciudad: Optional[str] = None
    factura_codigo_postal: Optional[str] = None
    factura_provincia: Optional[str] = None


class ConfirmarPagoSimulado(BaseModel):
    libros: list[LibroDelPedido]
    envio: DatosEnvioPedido
    total: float
    codigo_cupon: Optional[str] = None  # opcional - si se pasa, se vuelve a validar y a calcular el descuento aquí mismo, nunca se confía en un total ya descontado que mande el navegador
    cliente_id: Optional[str] = None  # el cliente del PEDIDO entero (no de cada libro) - para poder generarle sus cupones al confirmar el pago
    cantidad_libros: Optional[int] = None  # total de EJEMPLARES del pedido (copias incluidas, no solo diseños distintos) - va a pedidos.cantidad_libros


class ValidarCuponRequest(BaseModel):
    codigo: str
    libros: list[LibroDelPedido]  # se necesita el formato/precio de cada uno para elegir el más caro


# ═══════════════════════════════════════════════════════
#  STRIPE (modo test) - crear sesión de pago, webhook, estado
# ═══════════════════════════════════════════════════════
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
URL_SITIO = os.environ.get("URL_SITIO", "https://www.mibookeo.es")


def _verificar_firma_stripe(payload_bytes: bytes, sig_header: str, secret: str) -> bool:
    """
    Verificación manual de la firma del webhook de Stripe (HMAC-SHA256
    sobre "timestamp.payload"), sin depender de la librería oficial de
    Stripe - así no hace falta añadir una dependencia nueva al proyecto.
    Es exactamente el algoritmo que Stripe documenta para hacerlo a mano.
    """
    if not secret:
        # Sin STRIPE_WEBHOOK_SECRET configurada no se puede verificar de
        # verdad - se deja pasar SOLO para las primeras pruebas en local;
        # en producción esta variable tiene que estar puesta siempre.
        print("[DEBUG] AVISO: STRIPE_WEBHOOK_SECRET no configurada, webhook sin verificar firma")
        return True
    try:
        partes = dict(p.split("=", 1) for p in sig_header.split(","))
        timestamp = partes["t"]
        firma_recibida = partes["v1"]
    except Exception:
        return False
    mensaje_firmado = f"{timestamp}.".encode() + payload_bytes
    firma_esperada = hmac.new(secret.encode(), mensaje_firmado, hashlib.sha256).hexdigest()
    return hmac.compare_digest(firma_esperada, firma_recibida)


def _crear_sesion_stripe(numero_pedido, libros, envio, total, cupon_info=None, cliente_id=None,
                          codigo_cupon=None, descuento_importe=None, envio_gratis_cupon=False):
    """
    Llama directamente a la API REST de Stripe (sin su SDK) para crear
    una sesión de Checkout. Los datos del pedido (libros + envío) van en
    'metadata' - Stripe los guarda tal cual y los devuelve en el evento
    del webhook cuando el pago se completa, así el webhook no depende de
    nada que el propio navegador del cliente pueda manipular.

    cupon_info: opcional - {"fila_id":..., "tipo": "propio"|"amigo",
    "descuento_importe": float} ya calculado y VALIDADO en el servidor
    (ver crear_pedido_iniciar_pago) - se manda también en metadata para
    que el webhook sepa qué cupón canjear cuando el pago se confirme.

    OJO: metadata de Stripe limita cada valor a 500 caracteres - con
    pedidos de pocos libros (el caso normal) entra de sobra; si algún día
    hay carritos con muchos libros o títulos muy largos, esto habría que
    revisarlo (por ejemplo guardando el pedido en Supabase primero y
    pasando solo su id).
    """
    libros_json = json.dumps([
        {"titulo": l.titulo, "cantidad": l.cantidad, "tarea_id": l.tarea_id, "pedido_id": l.pedido_id,
         "formato": l.formato, "precio": l.precio, "paginas": l.paginas, "pdf_r2": l.pdf_r2}
        for l in libros
    ])
    envio_json = json.dumps(envio.dict())
    if len(libros_json) > 490 or len(envio_json) > 490:
        raise HTTPException(
            status_code=400,
            detail="El pedido tiene demasiados libros o datos de envío muy largos para procesarlo así - avisa para revisarlo."
        )

    payload = {
        "mode": "payment",
        "success_url": f"{URL_SITIO}/confirmar-pedido.html?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{URL_SITIO}/pago.html",
        "customer_email": envio.correo,
        # Tarjeta (con Apple Pay/Google Pay integrados dentro - Stripe los
        # detecta solo según el dispositivo/navegador, no hace falta nada
        # más para esos dos) + Bizum como método aparte. OJO: Bizum
        # además hay que activarlo a mano una vez en el panel de Stripe
        # (Ajustes → Métodos de pago) - si no está activado ahí, Stripe
        # rechaza la sesión aunque se pida aquí.
        "payment_method_types[0]": "card",
        "payment_method_types[1]": "bizum",
        "line_items[0][price_data][currency]": "eur",
        "line_items[0][price_data][product_data][name]": f"Pedido Bookeo {numero_pedido}",
        "line_items[0][price_data][unit_amount]": int(round(total * 100)),
        "line_items[0][quantity]": 1,
        "metadata[numero_pedido]": numero_pedido,
        "metadata[libros]": libros_json,
        "metadata[envio]": envio_json,
        # Se copian estos dos también al PaymentIntent (no solo a la
        # sesión) - checkout.session.expired sí trae la sesión entera con
        # su metadata, pero payment_intent.payment_failed (tarjeta
        # rechazada) llega como un objeto PaymentIntent aparte, que NO
        # hereda la metadata de la sesión a menos que se le ponga aquí
        # explícitamente. Sin esto, ese aviso no sabría a qué pedido ni a
        # qué correo pertenece.
        "payment_intent_data[metadata][numero_pedido]": numero_pedido,
        "payment_intent_data[metadata][correo]": envio.correo or "",
    }
    if cupon_info:
        payload["metadata[cupon_fila_id]"] = str(cupon_info["fila_id"])
        payload["metadata[cupon_tipo]"] = cupon_info["tipo"]
        # Para poder mostrar la línea de descuento en el ticket/factura -
        # el código lo escribe el propio cliente, así que se manda tal
        # cual (no hay nada sensible en enseñarlo en su propio ticket).
        if codigo_cupon:
            payload["metadata[cupon_codigo]"] = codigo_cupon
        if descuento_importe is not None:
            payload["metadata[descuento_importe]"] = str(descuento_importe)
        if envio_gratis_cupon:
            payload["metadata[envio_gratis_cupon]"] = "1"
    if cliente_id:
        payload["metadata[cliente_id]"] = cliente_id
    resp = requests.post(
        "https://api.stripe.com/v1/checkout/sessions",
        auth=(STRIPE_SECRET_KEY, ""),
        data=payload,
        timeout=15,
    )
    if resp.status_code >= 400:
        print(f"[DEBUG] Error creando sesión de Stripe: {resp.status_code} {resp.text}")
        raise HTTPException(status_code=502, detail="No se pudo iniciar el pago con Stripe")
    return resp.json()


@app.post("/validar-cupon")
async def validar_cupon_endpoint(datos: ValidarCuponRequest):
    """
    Consulta si un código es válido - NO lo marca como usado ni lo borra
    (eso solo pasa al confirmarse el pago de verdad, ver el webhook). El
    descuento se aplica SOLO al libro de mayor precio del carrito, nunca
    a la compra completa - así, si hay varios libros, el cliente ve el
    descuento reflejado justo bajo ese libro en concreto.
    """
    if not datos.libros:
        raise HTTPException(status_code=400, detail="No hay libros en el pedido")

    libros_con_precio = [l for l in datos.libros if l.precio is not None and l.formato]
    if not libros_con_precio:
        raise HTTPException(status_code=400, detail="Faltan el formato o el precio de los libros del pedido")

    # El libro más caro se decide por el precio de UN ejemplar, y el descuento
    # se aplica solo a ese ejemplar (aunque lleve varios en la misma línea).
    libro_mas_caro = max(libros_con_precio, key=_precio_por_ejemplar)
    resultado = validar_cupon(datos.codigo, libro_mas_caro.formato)
    if not resultado:
        raise HTTPException(status_code=400, detail="Código no válido")

    descuento_importe = round(_precio_por_ejemplar(libro_mas_caro) * resultado["descuento"] / 100, 2)
    # Si el código lleva envío gratis (columna 'envio_gratis' en
    # 'promociones'), se quita lo que se estuviera cobrando de envío -
    # si ya era gratis por llevar 2+ libros, esto no cambia nada.
    envio_quitado = coste_envio(datos.libros) if resultado.get("envio_gratis") else 0.0
    return {
        "ok": True,
        "descuento_pct": resultado["descuento"],
        "descuento_importe": descuento_importe,
        "envio_gratis": bool(resultado.get("envio_gratis")),
        "envio_quitado": envio_quitado,
        "tarea_id_aplicado": libro_mas_caro.tarea_id,
        # Si el libro lleva varios ejemplares, el mensaje del carrito avisa de que el
        # descuento va solo en uno.
        "titulo_libro_aplicado": (
            f"{libro_mas_caro.titulo or 'tu libro'} (1 ejemplar)"
            if libro_mas_caro.cantidad and libro_mas_caro.cantidad > 1 else libro_mas_caro.titulo
        ),
    }


@app.post("/crear-pedido/iniciar-pago")
async def crear_pedido_iniciar_pago(datos: ConfirmarPagoSimulado):
    """
    Crea la sesión de pago de Stripe (modo test por ahora) y devuelve la
    URL a la que el navegador tiene que redirigir. El pedido NO se marca
    como confirmado aquí - eso solo lo hace el webhook cuando Stripe
    avisa de verdad de que el pago se ha completado (ver /webhooks/stripe).
    """
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe no está configurado en este entorno (falta STRIPE_SECRET_KEY)")
    if not datos.envio.correo:
        raise HTTPException(status_code=400, detail="Falta el correo del cliente")

    total_final = datos.total
    cupon_info = None
    if datos.codigo_cupon:
        # Se vuelve a validar y a calcular el descuento AQUÍ, en el
        # servidor - nunca se confía en un total ya descontado que venga
        # del navegador, porque esa petición se podría manipular y
        # aplicarse un descuento sin tener ningún código válido de verdad.
        libros_con_precio = [l for l in datos.libros if l.precio is not None and l.formato]
        if libros_con_precio:
            libro_mas_caro = max(libros_con_precio, key=_precio_por_ejemplar)
            resultado = validar_cupon(datos.codigo_cupon, libro_mas_caro.formato)
            if resultado:
                descuento_importe = round(_precio_por_ejemplar(libro_mas_caro) * resultado["descuento"] / 100, 2)
                envio_gratis_cupon = bool(resultado.get("envio_gratis"))
                envio_quitado = coste_envio(datos.libros) if envio_gratis_cupon else 0.0
                total_final = round(datos.total - descuento_importe - envio_quitado, 2)
                cupon_info = {"fila_id": resultado["fila_id"], "tipo": resultado["tipo"]}

    numero_pedido = _nuevo_numero_pedido()
    sesion = _crear_sesion_stripe(
        numero_pedido, datos.libros, datos.envio, total_final, cupon_info=cupon_info, cliente_id=datos.cliente_id,
        codigo_cupon=datos.codigo_cupon, descuento_importe=(descuento_importe if cupon_info else None),
        envio_gratis_cupon=(envio_gratis_cupon if cupon_info else False),
    )

    # Se deja constancia YA (sin pagar todavía) en la tabla 'pedidos' -
    # es lo que la tarea programada de "7 días sin pagar" necesita para
    # poder encontrar este pedido si nunca llega a completarse. Si el
    # cliente sí paga, el webhook de Stripe (más abajo) actualiza esta
    # misma fila con fecha_pago.
    try:
        print(f"[DEBUG] Creando pedido inicial {numero_pedido} con cliente_id={datos.cliente_id!r}")
        crear_o_actualizar_pedido_inicial(numero_pedido, {
            "cliente_id": datos.cliente_id,
            "precio": total_final,
            "correo": datos.envio.correo,
            "titulo_libro": datos.libros[0].titulo if datos.libros else None,
            "cantidad_libros": datos.cantidad_libros,
        })
    except Exception as e:
        print(f"[DEBUG] ERROR creando la fila inicial de 'pedidos' para {numero_pedido} (cliente_id recibido: {datos.cliente_id!r}): {e}")

    return {"ok": True, "url": sesion["url"], "numero_pedido": numero_pedido}


@app.get("/pedido/estado")
def pedido_estado(session_id: str):
    """
    El navegador llama aquí al volver de Stripe (success_url) para saber
    si el pago se completó de verdad - NUNCA se fía solo de haber vuelto
    a esta URL (eso se podría manipular), siempre se vuelve a preguntar
    a Stripe directamente por el estado real de esa sesión.
    """
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe no está configurado en este entorno")
    resp = requests.get(
        f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
        auth=(STRIPE_SECRET_KEY, ""),
        timeout=15,
    )
    if resp.status_code >= 400:
        raise HTTPException(status_code=404, detail="No se encontró ese pedido")
    sesion = resp.json()
    metadata = sesion.get("metadata") or {}
    try:
        libros = json.loads(metadata.get("libros", "[]"))
    except Exception:
        libros = []
    return {
        "ok": True,
        "pagado": sesion.get("payment_status") == "paid",
        "numero_pedido": metadata.get("numero_pedido"),
        "libros": libros,
        "total_texto": f"{(sesion.get('amount_total') or 0) / 100:.2f}".replace(".", ",") + " €",
    }


class ValoracionRequest(BaseModel):
    estrellas: int
    comentario: Optional[str] = None
    nombre: Optional[str] = None
    correo: Optional[str] = None
    numero_pedido: Optional[str] = None


@app.post("/enviar-valoracion")
async def enviar_valoracion(datos: ValoracionRequest):
    """
    Llamado desde valorar.html. Todas las valoraciones (1-5 estrellas) se
    guardan en la tabla 'valoraciones' para tener histórico. Además, las
    de 4-5 estrellas NO generan ningún correo interno - esas van directas
    a Trustpilot desde la propia página. Solo las de 1-3 estrellas se
    avisan a info@mibookeo.es, para poder atenderlas en privado.
    """
    if datos.estrellas < 1 or datos.estrellas > 5:
        raise HTTPException(status_code=400, detail="La valoración tiene que ser de 1 a 5 estrellas")

    try:
        guardar_valoracion(
            estrellas=datos.estrellas,
            comentario=datos.comentario,
            nombre=datos.nombre,
            correo=datos.correo,
            numero_pedido=datos.numero_pedido,
        )
    except Exception as e:
        print(f"[DEBUG] ERROR guardando valoración en Supabase: {e}")

    if datos.estrellas < 4:
        try:
            from resend_client import enviar_correo_valoracion_interna
            enviar_correo_valoracion_interna(
                estrellas=datos.estrellas,
                comentario=datos.comentario,
                nombre=datos.nombre,
                correo=datos.correo,
                numero_pedido=datos.numero_pedido,
            )
        except Exception as e:
            print(f"[DEBUG] ERROR enviando valoración interna: {e}")
            raise HTTPException(status_code=500, detail="No se pudo enviar la valoración, inténtalo de nuevo")

    return {"ok": True}


@app.post("/webhooks/stripe")
async def webhook_stripe(request: Request):
    """
    Aquí es donde Stripe avisa DE VERDAD de que un pago se ha completado
    (o ha fallado) - esta es la única fuente de verdad del sistema sobre
    si un pedido está pagado, nunca un botón que pulse el propio cliente.
    """
    cuerpo = await request.body()
    firma = request.headers.get("stripe-signature", "")
    # Todo el procesado (Gelato, correos, ticket y factura a Drive, Supabase...)
    # son llamadas LENTAS y bloqueantes. Hechas aquí dentro, en una función
    # "async", congelan el servidor entero mientras duran: ningún otro cliente
    # puede ver su libro ni aplicar un cupón, y la página del propio cliente que
    # acaba de pagar tampoco carga. Mandadas a un hilo, el servidor sigue
    # atendiendo a los demás. El procesado en sí es EXACTAMENTE el de siempre.
    return await run_in_threadpool(_procesar_webhook_stripe, cuerpo, firma)


def _procesar_webhook_stripe(cuerpo, firma):
    """
    Procesa el aviso de Stripe ya leído (cuerpo y firma). Es una función normal
    (no async) a propósito: se ejecuta en un hilo desde webhook_stripe().
    """
    if not _verificar_firma_stripe(cuerpo, firma, STRIPE_WEBHOOK_SECRET):
        raise HTTPException(status_code=400, detail="Firma de Stripe inválida")

    try:
        evento = json.loads(cuerpo)
    except Exception:
        raise HTTPException(status_code=400, detail="Cuerpo del webhook no es JSON válido")

    tipo = evento.get("type")
    print(f"[DEBUG] Webhook de Stripe recibido: {tipo}")

    if tipo == "checkout.session.completed":
        sesion = evento["data"]["object"]
        metadata = sesion.get("metadata") or {}
        numero_pedido = metadata.get("numero_pedido", "BK-DESCONOCIDO")

        # Stripe puede avisar más de una vez del mismo pago. Si este pedido ya
        # se procesó, se ignora el aviso entero - si no, se crearía otro
        # pedido en Gelato (y se pagaría dos veces la impresión).
        if numero_pedido != "BK-DESCONOCIDO":
            try:
                from supabase_client import reclamar_procesado_pago
                if not reclamar_procesado_pago(numero_pedido, sesion.get("id")):
                    print(f"[DEBUG] Aviso DUPLICADO de Stripe para {numero_pedido}: ya estaba procesado, se ignora")
                    return {"ok": True, "duplicado": True}
            except Exception as e:
                # Si no se puede comprobar, se procesa como siempre: mejor eso
                # que dejar un pago sin su pedido.
                print(f"[DEBUG] No se pudo comprobar si {numero_pedido} ya estaba procesado ({e}); se procesa igualmente")

        try:
            libros = json.loads(metadata.get("libros", "[]"))
            envio = json.loads(metadata.get("envio", "{}"))
        except Exception as e:
            print(f"[DEBUG] ERROR leyendo metadata del pedido {numero_pedido}: {e}")
            from resend_client import enviar_alerta_interna
            enviar_alerta_interna("No se pudo leer la metadata del pedido pagado", str(e), numero_pedido)
            libros, envio = [], {}

        # Se deja constancia en Supabase de cada libro como "confirmado" -
        # esto es lo que antes hacía guardarLibrosEnSupabase() en el
        # navegador ANTES de pagar; ahora se hace aquí, en el servidor,
        # DESPUÉS de que Stripe confirme el pago de verdad - así un
        # cliente que cierra la pestaña a mitad no deja libros marcados
        # como confirmados sin haber pagado.
        for l in libros:
            libro_id = l.get("pedido_id")
            if not libro_id:
                continue
            try:
                guardar_libro(libro_id, {
                    "titulo": l.get("titulo") or "Mi álbum",
                    "estado": "confirmado",
                    "url_editor": f"{URL_SITIO}/editor.html?pedido_id={libro_id}",
                    "formato": l.get("formato"),
                    "paginas": l.get("paginas"),
                })
            except Exception as e:
                print(f"[DEBUG] No se pudo marcar el libro {libro_id} como confirmado: {e}")

            # Ya está pagado y el PDF final ya existe - las fotos y vídeos
            # de origen que se usaron para montarlo ya no hacen falta en
            # R2 (están "horneados" dentro del PDF). Se borran para no
            # pagar almacenamiento de más, dejando SOLO el PDF final vivo
            # (ese se borra solo a los 7 días, ver generar_url_descarga).
            try:
                tarea_id_libro = l.get("tarea_id")
                clave_r2_pdf = None
                if tarea_id_libro:
                    from celery_worker import app as celery_app
                    resultado_libro = celery_app.AsyncResult(tarea_id_libro)
                    if resultado_libro.state == "SUCCESS":
                        clave_r2_pdf = (resultado_libro.result or {}).get("clave_r2_pdf")
                elif l.get("pdf_r2"):
                    # Pedido de "repetir libro" (ver /repetir-pedido/subir-pdf) -
                    # el PDF no lo genera Celery, ya estaba subido de antes.
                    clave_r2_pdf = l.get("pdf_r2")
                from r2_storage import borrar_carpeta_pedido_r2
                borrar_carpeta_pedido_r2(libro_id, excluir_claves={clave_r2_pdf} if clave_r2_pdf else None)
            except Exception as e:
                print(f"[DEBUG] No se pudo limpiar R2 del libro {libro_id}: {e}")

        # Se manda un ENLACE de descarga de cada PDF (por su tarea_id), no
        # el archivo adjunto - los PDF a 300 PPI pueden pesar más de los
        # 40MB que Resend permite por correo, así que adjuntarlos hacía
        # que el correo entero se rechazase. El enlace ya viene de
        # celery_worker con validez de 7 días (ver generar_libro), que es
        # lo mismo que se le dice al cliente en el propio correo.
        enlaces_pdf = []
        from celery_worker import app as celery_app
        for l in libros:
            tarea_id = l.get("tarea_id")
            if not tarea_id:
                if l.get("pdf_r2"):
                    # Pedido de "repetir libro" - el PDF ya está en R2, se
                    # genera el enlace directo sin pasar por Celery.
                    try:
                        pdf_url = generar_url_descarga(
                            l["pdf_r2"], expira_segundos=3600 * 24 * 7,
                            nombre_descarga=f"{(l.get('titulo') or 'mi_libro').replace(' ', '_')}.pdf",
                        )
                        enlaces_pdf.append({"titulo": l.get("titulo") or "Mi álbum", "url": pdf_url, "pedido_id": l.get("pedido_id")})
                    except Exception as e:
                        print(f"[DEBUG] No se pudo generar el enlace del PDF repetido de '{l.get('titulo')}': {e}")
                continue
            try:
                resultado = celery_app.AsyncResult(tarea_id)
                if resultado.state != "SUCCESS":
                    continue
                r = resultado.result or {}
                pdf_url = r.get("pdf_url")
                if not pdf_url:
                    continue
                enlaces_pdf.append({"titulo": l.get("titulo") or "Mi álbum", "url": pdf_url, "pedido_id": l.get("pedido_id")})
            except Exception as e:
                print(f"[DEBUG] No se pudo obtener el enlace del PDF de '{l.get('titulo')}' ({tarea_id}): {e}")

        # Un único pedido de producción en Gelato para TODO el pedido de
        # Bookeo, aunque lleve varios libros (cada uno como su propio
        # 'item' dentro de ese mismo pedido) - así encaja con
        # pedidos.gelato_id (uno por pedido, no uno por libro). Se arma
        # a partir de enlaces_pdf (mismo PDF que ya se manda al cliente
        # por correo), cruzado con 'libros' para sacar formato/cantidad.
        try:
            from gelato_client import crear_pedido_gelato
            libros_para_gelato = []
            for l in libros:
                url_libro = next((e["url"] for e in enlaces_pdf if e.get("pedido_id") == l.get("pedido_id")), None)
                if not url_libro:
                    continue  # ese libro no tiene PDF listo todavía (o falló arriba) - no se manda a Gelato sin PDF
                libros_para_gelato.append({
                    "formato": l.get("formato"),
                    "cantidad": l.get("cantidad", 1),
                    "pdf_url": url_libro,
                    # Páginas interiores imprimibles (30 + 2 por pack extra).
                    # En los pedidos de "repetir libro" ese dato no es fiable
                    # (viene del PDF subido), así que ahí no se manda.
                    "paginas_gelato": None if l.get("pdf_r2") else l.get("paginas"),
                })
            if libros_para_gelato:
                respuesta_gelato = crear_pedido_gelato(
                    numero_pedido=numero_pedido, cliente_id=metadata.get("cliente_id"),
                    libros=libros_para_gelato, envio=envio,
                )
                gelato_id = respuesta_gelato.get("id")
                if gelato_id:
                    from supabase_client import marcar_pedido_gelato
                    marcar_pedido_gelato(numero_pedido, gelato_id)
            else:
                raise RuntimeError("Ningún libro de este pedido tenía un PDF listo para mandar a Gelato")
        except Exception as e:
            print(f"[DEBUG] ERROR creando el pedido en Gelato ({numero_pedido}): {e}")
            from resend_client import enviar_alerta_interna
            enviar_alerta_interna("Falló la creación del pedido en Gelato", str(e), numero_pedido)

        correo_cliente = envio.get("correo") or (sesion.get("customer_details") or {}).get("email")

        # Se marca el pedido como pagado DE VERDAD justo aquí - es lo
        # único que la tarea programada de "valoración a los 7 días"
        # necesita para saber cuándo empezar a contar (desde la
        # confirmación del pedido, no desde la entrega).
        try:
            marcar_pedido_pagado(numero_pedido, stripe_id=sesion.get("id"))
        except Exception as e:
            print(f"[DEBUG] ERROR marcando el pedido {numero_pedido} como pagado en Supabase: {e}")
            from resend_client import enviar_alerta_interna
            enviar_alerta_interna("No se pudo marcar un pedido como pagado en Supabase", str(e), numero_pedido)

        # (Esto va ANTES del correo de confirmación para poder adjuntarlo a él.)
        # TICKET (siempre) y FACTURA (solo si se pidió con NIF/razón
        # social) - se generan y se suben a la carpeta del mes que
        # corresponda en el Drive del negocio. Un fallo aquí no debe
        # impedir que el resto del pedido (correo, cupones) siga
        # funcionando, por eso va en su propio try/except aparte.
        # PDF del ticket (y de la factura, si la hay) para adjuntarlos al correo de
        # confirmación. Se define AQUÍ, fuera del try, para que el correo salga
        # igualmente - sin adjunto - si algo de lo de abajo falla.
        adjuntos_ticket = []
        try:
            import tempfile
            from facturacion_pdf import generar_pdf_ticket, generar_pdf_factura
            from facturacion_drive import subir_documento
            from supabase_client import siguiente_numero_ticket, siguiente_numero_factura

            hoy = datetime.date.today()
            total_venta = (sesion.get("amount_total") or 0) / 100
            libros_para_pdf = [
                {"titulo": l.get("titulo"), "cantidad": l.get("cantidad", 1),
                 "formato": l.get("formato"), "precio": l.get("precio")}
                for l in libros
            ]
            descuento_importe_meta = metadata.get("descuento_importe")
            descuento_pdf = None
            if descuento_importe_meta:
                try:
                    descuento_pdf = {"codigo": metadata.get("cupon_codigo"), "importe": float(descuento_importe_meta)}
                except ValueError:
                    descuento_pdf = None
            # Mismo cálculo que ya hace pago.html (PRECIO_ENVIO = 2.90,
            # gratis a partir de 2 libros contando todas las unidades del
            # carrito) - si ese importe cambia algún día, hay que tocarlo
            # en los dos sitios.
            total_libros_fisicos = sum(l.get("cantidad", 1) for l in libros)
            envio_gratis_por_cupon = metadata.get("envio_gratis_cupon") == "1"
            envio_importe_pdf = 0.0 if (total_libros_fisicos >= 2 or envio_gratis_por_cupon) else PRECIO_ENVIO

            # Dirección de envío del cliente para el ticket: "Calle, 31500 Tudela (Navarra)".
            cp_ciudad = f"{envio.get('codigo_postal') or ''} {envio.get('ciudad') or ''}".strip()
            if cp_ciudad and envio.get("provincia"):
                cp_ciudad += f" ({envio.get('provincia')})"
            direccion_cliente = ", ".join(x for x in (envio.get("direccion"), cp_ciudad) if x) or None

            numero_ticket = siguiente_numero_ticket()
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                generar_pdf_ticket(tmp.name, numero_ticket, numero_pedido, hoy, correo_cliente, libros_para_pdf, total_venta,
                                   descuento=descuento_pdf, envio_importe=envio_importe_pdf, nombre_cliente=envio.get("nombre"),
                                   direccion_cliente=direccion_cliente)
                with open(tmp.name, "rb") as f_pdf:
                    adjuntos_ticket.append({"filename": f"Ticket_{numero_ticket}.pdf", "content_bytes": f_pdf.read()})
                subir_documento(tmp.name, f"{numero_ticket}.pdf", "ticket", hoy)

            if envio.get("quiere_factura"):
                numero_factura = siguiente_numero_factura(hoy.year)
                datos_cliente_factura = {
                    "nif": envio.get("factura_nif"),
                    "razon_social": envio.get("factura_razon_social"),
                    "direccion": envio.get("factura_direccion"),
                    "ciudad": envio.get("factura_ciudad"),
                    "codigo_postal": envio.get("factura_codigo_postal"),
                    "provincia": envio.get("factura_provincia"),
                }
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    generar_pdf_factura(tmp.name, numero_factura, numero_pedido, hoy, datos_cliente_factura, libros_para_pdf, total_venta,
                                         descuento=descuento_pdf, envio_importe=envio_importe_pdf)
                    with open(tmp.name, "rb") as f_pdf:
                        adjuntos_ticket.append({"filename": f"Factura_{numero_factura}.pdf", "content_bytes": f_pdf.read()})
                    subir_documento(tmp.name, f"{numero_factura}.pdf", "factura", hoy)
        except Exception as e:
            print(f"[DEBUG] ERROR generando/subiendo ticket o factura del pedido {numero_pedido}: {e}")
            from resend_client import enviar_alerta_interna
            enviar_alerta_interna("Falló la generación del ticket o factura de un pedido", str(e), numero_pedido)

        try:
            from resend_client import enviar_correo_confirmacion_pedido
            total_texto = f"{(sesion.get('amount_total') or 0) / 100:.2f}".replace(".", ",") + " €"
            enviar_correo_confirmacion_pedido(
                correo=envio.get("correo") or sesion.get("customer_details", {}).get("email"),
                numero_pedido=numero_pedido,
                libros=[{"titulo": l.get("titulo", "Mi álbum"), "cantidad": l.get("cantidad", 1)} for l in libros],
                total_texto=total_texto,
                envio=envio,
                pdf_enlaces=enlaces_pdf,
                adjuntos=adjuntos_ticket or None,
            )
        except Exception as e:
            print(f"[DEBUG] ERROR enviando correo de confirmación tras pago Stripe ({numero_pedido}): {e}")
            from resend_client import enviar_alerta_interna
            enviar_alerta_interna("Falló el correo de confirmación de un pedido pagado", str(e), numero_pedido)

        # Si en esta compra se usó un cupón, se canjea AHORA (nunca antes,
        # nunca al "aplicarlo" en pantalla) - así un cliente que aplica un
        # código y luego no llega a pagar no deja ese cupón gastado sin
        # que nadie lo haya usado de verdad.
        cupon_fila_id = metadata.get("cupon_fila_id")
        cupon_tipo = metadata.get("cupon_tipo")
        if cupon_fila_id and cupon_tipo:
            try:
                resultado_canje = marcar_cupon_usado(cupon_fila_id, cupon_tipo)
                # Si era un código de recomendación, quien lo regaló recibe
                # ahora su cupón de recompensa del 20%.
                if resultado_canje:
                    from resend_client import enviar_correo_recompensa_recomendacion
                    enviar_correo_recompensa_recomendacion(
                        correo=resultado_canje["correo"],
                        codigo_recompensa=resultado_canje["codigo_recompensa"],
                    )
            except Exception as e:
                print(f"[DEBUG] ERROR canjeando el cupón {cupon_fila_id} ({numero_pedido}): {e}")
                from resend_client import enviar_alerta_interna
                enviar_alerta_interna("Falló el canje de un cupón", str(e), numero_pedido)

        # Se generan (o renuevan) los dos cupones propios de ESTE cliente
        # para su próximo pedido - de fidelidad (10%) y de recomendación
        # (15%, para regalar). Todavía no se le mandan por correo aquí: se
        # guardan listos para cuando se dispare el correo de valoración a
        # los 7 días de la entrega (necesita la tarea programada, ver
        # memoria del proyecto).
        cliente_id_pedido = metadata.get("cliente_id")
        if correo_cliente and cliente_id_pedido:
            try:
                crear_cupones_cliente(cliente_id_pedido, correo_cliente)
            except Exception as e:
                print(f"[DEBUG] ERROR generando cupones para {cliente_id_pedido} ({numero_pedido}): {e}")
                from resend_client import enviar_alerta_interna
                enviar_alerta_interna("No se pudieron generar los cupones de un cliente", str(e), numero_pedido)


        # TODO (ya apuntado en la memoria del proyecto): en cuanto se
        # conecte Gelato, aquí es donde se lanzaría el pedido de
        # producción y se borrarían las fotos/vídeos temporales de R2.
        # Si Gelato RECHAZA el pedido (formato incorrecto, PDF inválido,
        # fallo de su API...), avisar con:
        #   from resend_client import enviar_alerta_interna
        #   enviar_alerta_interna("Gelato ha rechazado un pedido", detalle_del_error, numero_pedido)

    elif tipo in ("payment_intent.payment_failed", "checkout.session.expired"):
        objeto = evento.get("data", {}).get("object", {})
        metadata_fallo = objeto.get("metadata") or {}
        numero_pedido_fallo = metadata_fallo.get("numero_pedido")
        correo_fallo = metadata_fallo.get("correo")
        if not correo_fallo:
            # checkout.session.expired trae la metadata de la sesión
            # entera (igual que checkout.session.completed) - ahí el
            # correo va anidado dentro de 'envio' (JSON), no como campo
            # suelto como sí se hizo para el PaymentIntent.
            try:
                correo_fallo = json.loads(metadata_fallo.get("envio", "{}")).get("correo")
            except Exception:
                pass
        correo_fallo = correo_fallo or (objeto.get("customer_details") or {}).get("email")
        motivo = None
        if tipo == "payment_intent.payment_failed":
            # El motivo real (tarjeta rechazada, fondos insuficientes...)
            # viene dentro de last_payment_error, si Stripe lo informó.
            error = objeto.get("last_payment_error") or {}
            motivo = error.get("message")

        print(f"[DEBUG] Pago fallido/expirado ({tipo}): pedido={numero_pedido_fallo} correo={correo_fallo}")

        if numero_pedido_fallo and correo_fallo:
            try:
                from resend_client import enviar_correo_pago_fallido
                enviar_correo_pago_fallido(
                    correo=correo_fallo,
                    numero_pedido=numero_pedido_fallo,
                    url_reintentar=f"{URL_SITIO}/pago.html",
                    motivo=motivo,
                )
            except Exception as e:
                print(f"[DEBUG] ERROR enviando correo de pago fallido ({numero_pedido_fallo}): {e}")
                from resend_client import enviar_alerta_interna
                enviar_alerta_interna("Falló el correo de aviso de pago fallido", str(e), numero_pedido_fallo)
        else:
            print(f"[DEBUG] No se pudo mandar el correo de pago fallido - falta numero_pedido o correo en la metadata")

    return {"ok": True}


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


@app.post("/crear-pedido/confirmar-pago-simulado")
async def confirmar_pago_simulado(datos: ConfirmarPagoSimulado):
    """
    OJO: esto es SOLO para probar que el correo de confirmación (con el
    PDF adjunto y el resumen) funciona de verdad, MIENTRAS Stripe no está
    conectado todavía. Simula que el pago se ha completado y dispara el
    correo - cuando se conecte Stripe de verdad, este disparo tiene que
    pasar al webhook de Stripe (evento checkout.session.completed), NUNCA
    quedarse en un botón que pulsa el propio cliente sin ninguna
    verificación real de pago de por medio.
    """
    from resend_client import enviar_correo_confirmacion_pedido
    from celery_worker import app as celery_app

    if not datos.envio.correo:
        raise HTTPException(status_code=400, detail="Falta el correo del cliente")

    # Nº de pedido de prueba - cuando haya pago real, esto debe venir de
    # una fila de verdad en la tabla 'pedidos' de Supabase (creada en el
    # webhook de Stripe), no generarse aquí a partir de la fecha/hora.
    numero_pedido = _nuevo_numero_pedido()

    # Enlace de descarga de cada PDF (por su tarea_id) - no adjunto, por
    # el mismo motivo que en el webhook real: puede pesar más de los
    # 40MB que Resend admite por correo.
    enlaces_pdf = []
    for l in datos.libros:
        if not l.tarea_id:
            continue
        try:
            resultado = celery_app.AsyncResult(l.tarea_id)
            if resultado.state != "SUCCESS":
                print(f"[DEBUG] Libro '{l.titulo}' (tarea {l.tarea_id}) sin PDF listo todavía - se omite del correo")
                continue
            r = resultado.result or {}
            pdf_url = r.get("pdf_url")
            if not pdf_url:
                continue
            enlaces_pdf.append({"titulo": l.titulo or "Mi álbum", "url": pdf_url})
        except Exception as e:
            print(f"[DEBUG] No se pudo obtener el enlace del PDF de '{l.titulo}' ({l.tarea_id}): {e}")

    try:
        enviar_correo_confirmacion_pedido(
            correo=datos.envio.correo,
            numero_pedido=numero_pedido,
            libros=[{"titulo": l.titulo or "Mi álbum", "cantidad": l.cantidad or 1} for l in datos.libros],
            total_texto=f"{datos.total:.2f}".replace(".", ",") + " €",
            envio=datos.envio.dict(),
            pdf_enlaces=enlaces_pdf,
        )
    except Exception as e:
        print(f"[DEBUG] ERROR enviando correo de confirmación: {e}")
        raise HTTPException(status_code=500, detail=f"No se pudo enviar el correo de confirmación: {e}")

    return {"ok": True, "numero_pedido": numero_pedido, "pdfs_adjuntados": len(adjuntos)}


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
    datos = await obtener_pedido_async(pedido_id)
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

        await guardar_pedido_async(pedido_id, datos)

        # OJO: antes esto solo devolvía 'nombre' - el navegador nunca se
        # enteraba de la ruta real en disco donde se guardó la foto, así
        # que cualquier página que acabara usando esta foto (una nueva
        # añadida a mitad de edición, o la foto de una portada reeditada)
        # se quedaba con un hueco sin "ruta" en su propio JSON. Al
        # regenerar el PDF final, el backend no encontraba el archivo y
        # fallaba con un error de foto - ahora se devuelve también.
        return {"ok": True, "nombre": nombre_archivo, "ruta": str(dest)}

    except Exception as e:
        print(f"[DEBUG /crear-pedido/subir-foto] ERROR: {e}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error subiendo la foto: {e}")


@app.get("/crear-pedido/{pedido_id}/drive-estado")
def crear_pedido_drive_estado(pedido_id: str):
    """
    Comprobación rápida (sin subir ningún archivo) de si el cliente de
    este pedido tiene Drive conectado DE VERDAD. El editor la llama justo
    ANTES de dejar elegir un vídeo - así, si no está conectado, se avisa
    al instante sin hacer subir el archivo entero primero para enterarse
    (que es lo que pasaba antes: se veía "Failed to fetch" en vez del
    aviso, porque la subida se cortaba a mitad antes de llegar a
    comprobar nada).

    OJO: no basta con mirar si hay un refresh_token guardado en Supabase
    - si el cliente revocó el acceso en Google (o caducó), el token
    seguiría "existiendo" en la base de datos pero ya no valdría para
    nada. Por eso aquí se intenta usar de verdad contra la API de Google
    (creds.refresh) - si falla, se trata exactamente igual que si nunca
    hubiera estado conectado, y de paso se borra ese token muerto de
    Supabase para no arrastrarlo.
    """
    datos = obtener_pedido(pedido_id)
    if not datos:
        return {"conectado": False}
    cliente_id = datos.get("cliente_id")
    if not cliente_id:
        return {"conectado": False}
    datos_drive = obtener_cliente_drive(cliente_id)
    if not datos_drive or not datos_drive.get("google_refresh_token"):
        return {"conectado": False}

    try:
        obtener_servicio_drive(datos_drive["google_refresh_token"])
        return {"conectado": True}
    except Exception as e:
        print(f"[DEBUG /drive-estado] Token de Drive inválido para cliente {cliente_id}: {e}")
        borrar_refresh_token_cliente(cliente_id)
        return {"conectado": False}


@app.get("/crear-pedido/video-listo")
def crear_pedido_video_listo(pedido_id: str, file_id: str):
    """
    El editor pregunta aquí, cada pocos segundos, si Google Drive ya ha terminado de
    preparar un vídeo recién subido para poder reproducirlo (mientras tanto, al
    abrirlo, Drive enseña "este archivo de vídeo aún se está procesando"). Drive
    solo informa de las medidas y la duración del vídeo (videoMediaMetadata) cuando
    ya lo ha procesado. Es una función normal (no async) para que FastAPI la mande
    a un hilo: habla con Google y no debe bloquear al resto del servidor.
    """
    datos = obtener_pedido(pedido_id)
    if not datos:
        raise HTTPException(status_code=404, detail="Pedido no encontrado o expirado.")
    cliente_id = datos.get("cliente_id")
    datos_drive = obtener_cliente_drive(cliente_id) if cliente_id else None
    if not datos_drive or not datos_drive.get("google_refresh_token"):
        raise HTTPException(status_code=400, detail="No se encontró la conexión de Google Drive de este cliente.")
    try:
        servicio = obtener_servicio_drive(datos_drive["google_refresh_token"])
        info = servicio.files().get(fileId=file_id, fields="id, videoMediaMetadata").execute()
        return {"ok": True, "listo": bool(info.get("videoMediaMetadata"))}
    except Exception as e:
        print(f"[DEBUG /crear-pedido/video-listo] {e}")
        raise HTTPException(status_code=502, detail="No se pudo consultar a Google Drive.")


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
    datos = await obtener_pedido_async(pedido_id)
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

        # IMPORTANTE: tanto guardar el vídeo como subirlo a Drive son operaciones
        # BLOQUEANTES (pueden tardar minutos con un vídeo grande). Esta función es
        # "async", así que si se hacen directamente aquí congelan el servidor entero
        # mientras duran: ningún otro cliente puede ver su libro ni pagar hasta que
        # termine. Mandadas a un hilo (run_in_threadpool) el servidor sigue atendiendo
        # a los demás con normalidad.
        def _guardar_en_disco():
            with dest.open("wb") as f:
                shutil.copyfileobj(video.file, f)
        await run_in_threadpool(_guardar_en_disco)

        url, file_id = await run_in_threadpool(
            procesar_video,
            ruta_local=str(dest),
            nombre_archivo=video.filename,
            cliente_id=cliente_id,
            pedido_id=pedido_id,
            refresh_token_cliente=datos_drive["google_refresh_token"],
            nombre_album=datos.get("titulo"),
        )

        # Miniatura real del vídeo (un fotograma de verdad, no una foto
        # prestada de la página) - se extrae con OpenCV (ya se usa igual
        # para elegir el mejor fotograma cuando una página solo tiene
        # vídeo y no foto) y se sube a R2 igual que las fotos, con una URL
        # firmada temporal (mismo mecanismo que ya usan). Si algo de esto
        # falla, no debe romper la subida del vídeo en sí - el cliente ya
        # tiene su vídeo y su QR, solo se queda sin miniatura bonita.
        thumb_url = None
        try:
            carpeta_temp_thumb = work_dir / "miniaturas_video"
            ruta_frame = await run_in_threadpool(extraer_fotograma, str(dest), str(carpeta_temp_thumb))
            if ruta_frame:
                nombre_thumb = f"thumb_{Path(video.filename).stem}.jpg"
                clave_r2_thumb = subir_a_r2(ruta_frame, pedido_id, nombre_thumb)
                thumb_url = generar_url_descarga(clave_r2_thumb, expira_segundos=3600 * 24)
        except Exception as e:
            print(f"[DEBUG] No se pudo generar miniatura del vídeo '{video.filename}': {e}")

        return {"ok": True, "qr_url": url, "thumb_url": thumb_url, "file_id": file_id}

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
    datos = await obtener_pedido_async(pedido_id)
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
        if "pdf_url" in r:
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
        # stream=True: el PDF NO se carga entero en la memoria del servidor (un
        # libro puede pesar decenas de MB); se va pasando al navegador por trozos
        # a medida que llega de R2.
        resp = requests.get(pdf_url, timeout=(10, 30), stream=True)
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo recuperar el PDF: {e}")

    cabeceras = {
        # "inline" (no "attachment") - el navegador no ofrece
        # descargarlo solo por la cabecera; el visor de pdf.js del
        # front además nunca expone esta URL como enlace clicable.
        # Con descargar=1 se cambia a "attachment" para el botón de
        # descarga posterior al pago.
        "Content-Disposition": 'attachment; filename="mibookeo.pdf"' if descargar else "inline",
        "Cache-Control": "no-store",
    }
    # Con el tamaño total el navegador puede calcular lo que falta por descargar.
    # (Solo si R2 no lo comprime: con compresión el tamaño no coincidiría.)
    longitud = resp.headers.get("Content-Length")
    if longitud and not resp.headers.get("Content-Encoding"):
        cabeceras["Content-Length"] = longitud

    def trozos():
        try:
            for trozo in resp.iter_content(chunk_size=1024 * 1024):
                if trozo:
                    yield trozo
        finally:
            resp.close()

    return StreamingResponse(trozos(), media_type="application/pdf", headers=cabeceras)


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