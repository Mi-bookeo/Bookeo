"""
Bookeo · Backend unificador de vídeos
Despliega en Railway · Python 3.11+

Endpoints:
  POST /merge         →  recibe hasta 5 vídeos + música → devuelve MP4
  POST /crear-pedido  →  recibe fotos+vídeos → sube a Drive → genera PDF del libro
  GET  /health        →  healthcheck para Railway
"""

import os
import uuid
import tempfile
import shutil
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

# ── MoviePy ──
from moviepy.editor import (
    VideoFileClip,
    concatenate_videoclips,
    AudioFileClip,
    CompositeAudioClip,
)

# ── Bookeo: subida a Drive y creación del libro ──
from subir_drive import procesar_video
from crear_libro_railway import crear_libro

app = FastAPI(title="Bookeo Backend", version="1.0.0")

# ── CORS: permite llamadas desde tu dominio ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # ← cambia por tu dominio en producción
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ── Carpeta de músicas automáticas (súbelas a /music/ en Railway) ──
MUSIC_DIR = Path(__file__).parent / "music"
MUSIC_DIR.mkdir(exist_ok=True)

# Mapeo género → archivo MP3
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


@app.get("/health")
def health():
    return {"status": "ok", "service": "bookeo-backend"}


# ═══════════════════════════════════════════════════════
#  CREAR PEDIDO — sube vídeos a Drive + genera el libro
# ═══════════════════════════════════════════════════════

@app.post("/crear-pedido")
async def crear_pedido(
    fotos: list[UploadFile] = File(...),
    videos: list[UploadFile] = File(default=[]),
    titulo: str = Form(...),
    nombre_cliente: str = Form(...),
    pedido_id: str = Form(...),
    carpeta_drive_id: Optional[str] = Form(None),
):
    work_dir = Path(tempfile.mkdtemp(prefix=f"bookeo_pedido_{pedido_id}_"))
    carpeta_temp = work_dir / "temp"
    carpeta_temp.mkdir(exist_ok=True)

    try:
        # 1. Guardar fotos en disco
        fotos_rutas = []
        for foto in fotos:
            dest = work_dir / foto.filename
            with dest.open("wb") as f:
                shutil.copyfileobj(foto.file, f)
            fotos_rutas.append(str(dest))

        # 2. Guardar vídeos en disco
        videos_rutas = []
        for video in videos:
            dest = work_dir / video.filename
            with dest.open("wb") as f:
                shutil.copyfileobj(video.file, f)
            videos_rutas.append(str(dest))

        # 3. Subir cada vídeo a Drive y construir qr_urls
        qr_urls = {}
        carpeta_id_actual = carpeta_drive_id
        for ruta_video in videos_rutas:
            nombre_archivo = Path(ruta_video).name
            url, file_id, carpeta_id_actual = procesar_video(
                ruta_local=ruta_video,
                nombre_archivo=nombre_archivo,
                pedido_id=pedido_id,
                carpeta_id_existente=carpeta_id_actual,
            )
            qr_urls[nombre_archivo] = url

        # IMPORTANTE: guarda carpeta_id_actual en Supabase (tabla pedidos)
        # aquí, para que el visor reutilice la misma carpeta si el cliente
        # añade más vídeos después. (Pendiente de conectar Supabase.)

        # 4. Generar el PDF del libro
        ruta_pdf = crear_libro(
            fotos_rutas=fotos_rutas,
            videos_rutas=videos_rutas,
            titulo=titulo,
            nombre_cliente=nombre_cliente,
            qr_urls=qr_urls,
            carpeta_sal=str(work_dir / "salida"),
            carpeta_temp=str(carpeta_temp),
        )

        return {"ok": True, "pdf": ruta_pdf, "carpeta_drive_id": carpeta_id_actual}

    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Error creando el pedido: {e}")


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


# ── Limpieza asíncrona del directorio temporal ──
from starlette.background import BackgroundTask

def _cleanup_task(directory: Path) -> BackgroundTask:
    def _cleanup():
        shutil.rmtree(directory, ignore_errors=True)
    return BackgroundTask(_cleanup)
