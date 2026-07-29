"""
Bookeo · Backend unificador de vídeos
Despliega en Railway · Python 3.11+

Endpoints:
  POST /crear-pedido/propuestas →  sube fotos+vídeos a Drive, analiza con IA, devuelve 2 portadas
  POST /crear-pedido/confirmar  →  lanza la generación del PDF a Celery, devuelve tarea_id
  GET  /estado-tarea/{tarea_id} →  consulta el progreso de la generación
  POST /merge              →  recibe hasta 5 vídeos + música → devuelve MP4
  GET  /auth/google/iniciar   →  inicia login de Google Drive del cliente
  GET  /auth/google/callback  →  recibe el token, obtiene el email, crea/identifica al cliente
  GET  /health              →  healthcheck para Railway
"""

import os
import uuid
import tempfile
import shutil
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

from moviepy.editor import (
    VideoFileClip,
    concatenate_videoclips,
    AudioFileClip,
    CompositeAudioClip,
)

from subir_drive import procesar_video
from google_auth import generar_url_autorizacion, intercambiar_codigo_por_token_y_email
from crear_libro_railway import generar_propuestas_portada
from supabase_client import obtener_o_crear_cliente, guardar_refresh_token_cliente, obtener_cliente_drive

app = FastAPI(title="Bookeo Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

PEDIDOS_EN_PROCESO: dict = {}

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
        f"https://mibookeo.es/creador.html?drive_ok=1&cliente_id={cliente_id}"
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
):
    print(f"[DEBUG] Petición recibida: pedido_id={pedido_id}, cliente_id={cliente_id}")

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
        for foto in fotos:
            dest = work_dir / foto.filename
            with dest.open("wb") as f:
                shutil.copyfileobj(foto.file, f)
            fotos_rutas.append(str(dest))
        print(f"[DEBUG] {len(fotos_rutas)} fotos guardadas en disco")

        videos_rutas = []
        for video in videos:
            dest = work_dir / video.filename
            with dest.open("wb") as f:
                shutil.copyfileobj(video.file, f)
            videos_rutas.append(str(dest))
        print(f"[DEBUG] {len(videos_rutas)} vídeos guardados en disco")

        qr_urls = {}
        for ruta_video in videos_rutas:
            nombre_archivo = Path(ruta_video).name
            print(f"[DEBUG] Subiendo vídeo a Drive: {nombre_archivo}")
            url, file_id = procesar_video(
                ruta_local=ruta_video,
                nombre_archivo=nombre_archivo,
                cliente_id=cliente_id,
                pedido_id=pedido_id,
                refresh_token_cliente=google_refresh_token,
                nombre_album=titulo,
            )
            qr_urls[nombre_archivo] = url
        print(f"[DEBUG] Vídeos subidos a Drive: {len(qr_urls)}")

        print(f"[DEBUG] Llamando a generar_propuestas_portada...")
        resultado = generar_propuestas_portada(
            fotos_rutas, videos_rutas, titulo_cliente=titulo, formato=formato,
            orientacion=orientacion, packs_extra=packs_extra
        )
        print(f"[DEBUG] Propuestas generadas correctamente")

        PEDIDOS_EN_PROCESO[pedido_id] = {
            "diseño": resultado["diseño"],
            "fotos": resultado["fotos"],
            "videos_rutas": videos_rutas,
            "qr_urls": qr_urls,
            "titulo": titulo,
            "nombre_cliente": nombre_cliente,
            "work_dir": str(work_dir),
            "carpeta_temp": str(carpeta_temp),
            "formato": resultado["formato"],
            "orientacion": resultado["orientacion"],
            "packs_extra": packs_extra,
            "caso_reparto": resultado["caso_reparto"],
            "paginas_objetivo": resultado["paginas_objetivo"],
        }

        return {
            "ok": True,
            "pedido_id": pedido_id,
            "tipo": resultado["diseño"].get("tipo"),
            "portada_opciones": resultado["portada_opciones"],
        }

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
):
    datos = PEDIDOS_EN_PROCESO.get(pedido_id)
    if not datos:
        raise HTTPException(status_code=404, detail="Pedido no encontrado o expirado. Vuelve a subir tus fotos.")

    portada_elegida = {
        "foto": portada_foto if portada_foto else None,
        "titulo": portada_titulo or datos["titulo"],
        "subtitulo": portada_subtitulo or "",
    }

    work_dir = Path(datos["work_dir"])
    carpeta_sal = str(work_dir / "salida")

    from celery_worker import generar_libro

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
    }

    tarea = generar_libro.delay(tarea_datos)

    del PEDIDOS_EN_PROCESO[pedido_id]

    return {"ok": True, "tarea_id": tarea.id, "pedido_id": pedido_id}


@app.get("/estado-tarea/{tarea_id}")
def estado_tarea(tarea_id: str):
    from celery_worker import app as celery_app
    resultado = celery_app.AsyncResult(tarea_id)

    if resultado.state == "PENDING":
        return {"estado": "esperando"}
    elif resultado.state == "PROGRESS":
        return {"estado": "generando"}
    elif resultado.state == "SUCCESS":
        return {"estado": "listo", "pdf": resultado.result.get("pdf")}
    elif resultado.state == "FAILURE":
        return {"estado": "error", "detalle": str(resultado.info)}
    else:
        return {"estado": resultado.state}


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
            video_paths.append(dest)

        clips = []
        for path in video_paths:
            try:
                clip = VideoFileClip(str(path))
                if clips:
                    w, h = clips[0].size
                    if clip.size != (w, h):
                        clip = clip.resize((w, h))
                clips.append(clip)
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"Error leyendo vídeo: {e}")

        final_video = concatenate_videoclips(clips, method="compose")

        music_path: Optional[Path] = None

        if music_file and music_file.filename:
            music_ext = Path(music_file.filename).suffix or ".mp3"
            music_path = work_dir / f"user_music{music_ext}"
            with music_path.open("wb") as f:
                shutil.copyfileobj(music_file.file, f)

        elif music_genre and music_genre in GENRE_FILES:
            candidate = MUSIC_DIR / GENRE_FILES[music_genre]
            if candidate.exists():
                music_path = candidate

        if music_path and music_path.exists():
            try:
                bg_audio = AudioFileClip(str(music_path))
                total_duration = final_video.duration

                if bg_audio.duration < total_duration:
                    loops = int(total_duration / bg_audio.duration) + 1
                    from moviepy.editor import concatenate_audioclips
                    bg_audio = concatenate_audioclips([bg_audio] * loops)

                bg_audio = bg_audio.subclip(0, total_duration).volumex(MUSIC_VOLUME)

                if final_video.audio is not None:
                    video_audio = final_video.audio.volumex(0.50)
                    mixed = CompositeAudioClip([
                        video_audio,
                        bg_audio,
                    ])
                    final_video = final_video.set_audio(mixed)
                else:
                    final_video = final_video.set_audio(bg_audio)

            except Exception:
                pass

        output_path = work_dir / "bookeo_output.mp4"
        final_video.write_videofile(
            str(output_path),
            codec="libx264",
            audio_codec="aac",
            temp_audiofile=str(work_dir / "temp_audio.m4a"),
            remove_temp=True,
            logger=None,
            fps=clips[0].fps or 30,
        )

        for c in clips:
            c.close()
        final_video.close()

        return FileResponse(
            path=str(output_path),
            media_type="video/mp4",
            filename="bookeo-video.mp4",
            background=_cleanup_task(work_dir),
        )

    except HTTPException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Error interno: {e}")


from starlette.background import BackgroundTask

def _cleanup_task(directory: Path) -> BackgroundTask:
    def _cleanup():
        shutil.rmtree(directory, ignore_errors=True)
    return BackgroundTask(_cleanup)