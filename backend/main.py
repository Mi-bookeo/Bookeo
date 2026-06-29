"""
Bookeo · Backend unificador de vídeos
Despliega en Railway · Python 3.11+

Endpoints:
  POST /merge  →  recibe hasta 5 vídeos + música → devuelve MP4
  GET  /health →  healthcheck para Railway
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

app = FastAPI(title="Bookeo Video Merger", version="1.0.0")

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
# Descarga estas pistas de YouTube Audio Library (gratuitas, sin derechos)
# y guárdalas en la carpeta /music/ con estos nombres
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

# Volumen de la música de fondo (0.0 – 1.0)
MUSIC_VOLUME = 0.28


@app.get("/health")
def health():
    return {"status": "ok", "service": "bookeo-video-merger"}


@app.post("/merge")
async def merge_videos(
    video_1: Optional[UploadFile] = File(None),
    video_2: Optional[UploadFile] = File(None),
    video_3: Optional[UploadFile] = File(None),
    video_4: Optional[UploadFile] = File(None),
    video_5: Optional[UploadFile] = File(None),
    music_file:  Optional[UploadFile] = File(None),   # música subida por el usuario
    music_genre: Optional[str]       = Form(None),    # género automático
):
    # ── Recoger vídeos en orden ──
    uploaded = [v for v in [video_1, video_2, video_3, video_4, video_5] if v is not None]
    if len(uploaded) < 2:
        raise HTTPException(status_code=400, detail="Se necesitan al menos 2 vídeos.")

    # ── Directorio temporal de trabajo ──
    work_dir = Path(tempfile.mkdtemp(prefix="bookeo_"))

    try:
        # Guardar vídeos en disco
        video_paths = []
        for i, upload in enumerate(uploaded):
            ext = Path(upload.filename).suffix or ".mp4"
            dest = work_dir / f"video_{i+1}{ext}"
            with dest.open("wb") as f:
                shutil.copyfileobj(upload.file, f)
            video_paths.append(dest)

        # ── Cargar clips ──
        clips = []
        for path in video_paths:
            try:
                clip = VideoFileClip(str(path))
                # Normalizar resolución al primer vídeo
                if clips:
                    w, h = clips[0].size
                    if clip.size != (w, h):
                        clip = clip.resize((w, h))
                clips.append(clip)
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"Error leyendo vídeo: {e}")

        # ── Concatenar ──
        final_video = concatenate_videoclips(clips, method="compose")

        # ── Música ──
        music_path: Optional[Path] = None

        if music_file and music_file.filename:
            # Música subida por el usuario
            music_ext = Path(music_file.filename).suffix or ".mp3"
            music_path = work_dir / f"user_music{music_ext}"
            with music_path.open("wb") as f:
                shutil.copyfileobj(music_file.file, f)

        elif music_genre and music_genre in GENRE_FILES:
            # Música automática por género
            candidate = MUSIC_DIR / GENRE_FILES[music_genre]
            if candidate.exists():
                music_path = candidate
            # Si el archivo no existe en /music/, se continúa sin música

        if music_path and music_path.exists():
            try:
                bg_audio = AudioFileClip(str(music_path))
                total_duration = final_video.duration

                # Hacer loop si la música es más corta que el vídeo
                if bg_audio.duration < total_duration:
                    loops = int(total_duration / bg_audio.duration) + 1
                    from moviepy.editor import concatenate_audioclips
                    bg_audio = concatenate_audioclips([bg_audio] * loops)

                bg_audio = bg_audio.subclip(0, total_duration).volumex(MUSIC_VOLUME)

                if final_video.audio is not None:
                    # Audio original del vídeo al 50% · música al 28%
                    video_audio = final_video.audio.volumex(0.50)
                    mixed = CompositeAudioClip([
                        video_audio,
                        bg_audio,
                    ])
                    final_video = final_video.set_audio(mixed)
                else:
                    final_video = final_video.set_audio(bg_audio)

            except Exception:
                pass  # Si falla la música, continuar sin ella

        # ── Exportar ──
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

        # Cerrar clips
        for c in clips:
            c.close()
        final_video.close()

        # ── Devolver fichero ──
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
