"""
╔══════════════════════════════════════════════════════╗
║         BOOKEO · Unir vídeos con música             ║
║              Backend Railway · Versión definitiva    ║
╚══════════════════════════════════════════════════════╝

Une los vídeos del cliente en uno solo y añade música de fondo.
FFmpeg se instala automáticamente en Railway via moviepy.
Se llama desde main.py (FastAPI) cuando el cliente usa el unificador.

Función principal: unir_videos(videos_rutas, ruta_musica, ruta_salida)
"""

import os
import sys
import subprocess
import datetime
import shutil
from pathlib import Path

# ═══════════════════════════════════════════════════════
#  CONFIGURACIÓN — valores por defecto del servidor
# ═══════════════════════════════════════════════════════

VOLUMEN_MUSICA  = 0.30   # música al 30%
VOLUMEN_VIDEO   = 0.50   # audio original al 50%
CALIDAD         = "media"
RESOLUCION      = "1080p"

# ═══════════════════════════════════════════════════════
#  UTILIDADES
# ═══════════════════════════════════════════════════════

def log(msg, emoji="→"):
    hora = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{hora}] {emoji} {msg}")

def verificar_ffmpeg():
    """Comprueba que FFmpeg está disponible en Railway."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            version = result.stdout.split('\n')[0]
            log(f"FFmpeg encontrado · {version[:40]}", "✅")
            return True
    except FileNotFoundError:
        pass
    log("FFmpeg no disponible en este entorno", "❌")
    return False

def obtener_duracion(ruta_video):
    """Obtiene la duración de un vídeo en segundos."""
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            ruta_video
        ], capture_output=True, text=True)
        return float(result.stdout.strip())
    except Exception:
        return 0

def cargar_videos(carpeta):
    """Carga y ordena los vídeos por nombre (que suele ser por fecha)."""
    exts = {".mp4", ".mov", ".avi", ".m4v", ".mkv", ".wmv", ".MP4", ".MOV"}
    videos = []
    for f in sorted(Path(carpeta).iterdir()):
        if f.suffix in exts:
            duracion = obtener_duracion(str(f))
            videos.append({
                "ruta": str(f),
                "nombre": f.name,
                "duracion": duracion
            })
    return videos

def cargar_musica(carpeta):
    """Coge el primer archivo de música de la carpeta."""
    exts = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".MP3", ".M4A"}
    for f in sorted(Path(carpeta).iterdir()):
        if f.suffix in exts:
            log(f"Música encontrada: {f.name}", "🎵")
            return str(f)
    return None

# ═══════════════════════════════════════════════════════
#  CONFIGURACIÓN DE CALIDAD
# ═══════════════════════════════════════════════════════

def params_calidad():
    """Devuelve los parámetros de FFmpeg según la calidad elegida."""
    resoluciones = {
        "1080p": "1920:1080",
        "720p":  "1280:720",
        "original": None
    }
    res = resoluciones.get(RESOLUCION, "1920:1080")

    calidades = {
        "alta":  {"crf": "18", "preset": "slow"},
        "media": {"crf": "23", "preset": "medium"},
        "baja":  {"crf": "28", "preset": "fast"},
    }
    cal = calidades.get(CALIDAD, calidades["media"])

    return res, cal["crf"], cal["preset"]

# ═══════════════════════════════════════════════════════
#  PROCESO PRINCIPAL
# ═══════════════════════════════════════════════════════

def unir_videos(videos_rutas, ruta_musica=None, ruta_salida=None, carpeta_temp=None):
    """
    Función principal llamada por main.py (FastAPI).

    videos_rutas  → lista de rutas absolutas de los vídeos del cliente
    ruta_musica   → ruta del MP3 elegido (None = sin música)
    ruta_salida   → ruta donde guardar el vídeo final
    carpeta_temp  → carpeta temporal para archivos intermedios

    Devuelve: ruta del vídeo final generado
    """
    if not verificar_ffmpeg():
        raise RuntimeError("FFmpeg no disponible en el servidor")

    if not carpeta_temp:
        carpeta_temp = "/tmp/bookeo_videos_temp"
    os.makedirs(carpeta_temp, exist_ok=True)

    if not ruta_salida:
        ruta_salida = os.path.join(carpeta_temp, "bookeo_video_final.mp4")

    log(f"Iniciando unión de {len(videos_rutas)} vídeos", "🎬")

    # Cargar vídeos desde rutas
    videos = []
    for ruta in videos_rutas:
        if os.path.exists(ruta):
            duracion = obtener_duracion(ruta)
            videos.append({"ruta": ruta, "nombre": os.path.basename(ruta), "duracion": duracion})

    if not videos:
        raise ValueError("No se encontraron vídeos válidos")

    duracion_total = sum(v["duracion"] for v in videos)
    log(f"{len(videos)} vídeos · {int(duracion_total//60)}m {int(duracion_total%60)}s", "✅")

    ruta_final = ruta_salida

    # 6. Normalizar vídeos — misma resolución y codec
    print()
    log("Normalizando vídeos para unirlos...", "⚙")
    res, crf, preset = params_calidad()

    videos_norm = []
    for i, v in enumerate(videos):
        log(f"  Procesando {i+1}/{len(videos)} · {v['nombre']}", "")
        ruta_norm = os.path.join(carpeta_temp, f"video_{i:03d}_norm.mp4")

        # Filtro de escala según resolución elegida
        if res:
            vf = f"scale={res}:force_original_aspect_ratio=decrease,pad={res}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        else:
            vf = "setsar=1"

        cmd = [
            "ffmpeg", "-y",
            "-i", v["ruta"],
            "-vf", vf,
            "-c:v", "libx264",
            "-crf", crf,
            "-preset", preset,
            "-c:a", "aac",
            "-ar", "44100",
            "-ac", "2",
            "-loglevel", "error",
            ruta_norm
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log(f"  Error normalizando {v['nombre']}: {result.stderr[-200:]}", "⚠")
            continue

        videos_norm.append(ruta_norm)

    if not videos_norm:
        log("No se pudo procesar ningún vídeo", "❌")
        sys.exit(1)

    log(f"{len(videos_norm)} vídeos normalizados correctamente", "✅")

    # 7. Crear lista de concatenación
    lista_path = os.path.join(carpeta_temp, "lista_videos.txt")
    with open(lista_path, "w") as f:
        for ruta in videos_norm:
            f.write(f"file '{ruta}'\n")

    # 8. Unir vídeos
    print()
    log("Uniendo vídeos...", "🔗")
    ruta_unido = os.path.join(carpeta_temp, "video_unido.mp4")

    cmd_concat = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", lista_path,
        "-c", "copy",
        "-loglevel", "error",
        ruta_unido
    ]

    result = subprocess.run(cmd_concat, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"Error uniendo vídeos: {result.stderr[-300:]}", "❌")
        sys.exit(1)

    log("Vídeos unidos correctamente", "✅")

    # 9. Añadir música de fondo
    print()
    if ruta_musica:
        log(f"Añadiendo música · volumen {int(VOLUMEN_MUSICA*100)}%...", "🎵")

        cmd_musica = [
            "ffmpeg", "-y",
            "-i", ruta_unido,
            "-i", ruta_musica,
            "-filter_complex",
            # Baja el audio original del vídeo + mezcla con música de fondo
            # Solo se aplica cuando hay música activa (nunca toca el audio si no hay música)
            f"[0:a]volume={VOLUMEN_VIDEO}[voz];"
            f"[1:a]volume={VOLUMEN_MUSICA},aloop=loop=-1:size=2e+09[music];"
            f"[voz][music]amix=inputs=2:duration=first:dropout_transition=2[aout]",
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            "-loglevel", "error",
            ruta_final
        ]

        result = subprocess.run(cmd_musica, capture_output=True, text=True)

        if result.returncode != 0:
            log(f"Error añadiendo música · guardando sin música: {result.stderr[-200:]}", "⚠")
            # Fallback — guardar sin música
            shutil.copy2(ruta_unido, ruta_final)
        else:
            log("Música añadida correctamente", "✅")
    else:
        # Sin música — copiar directamente
        shutil.copy2(ruta_unido, ruta_final)
        log("Vídeo guardado sin música", "✅")

    # 10. Limpiar temporales
    shutil.rmtree(carpeta_temp, ignore_errors=True)

    # 11. Info del resultado
    tamaño = os.path.getsize(ruta_final) / (1024*1024)
    duracion_final = obtener_duracion(ruta_final)
    log(f"Vídeo final: {int(duracion_final//60)}m {int(duracion_final%60)}s · {tamaño:.1f} MB", "✅")
    log(f"Guardado en: {ruta_final}", "📁")

    return ruta_final


if __name__ == "__main__":
    # Solo para pruebas locales — en Railway se llama via FastAPI
    print("Uso: from bookeo_unir_videos import unir_videos")
    print("     ruta = unir_videos(videos_rutas=[...], ruta_musica='...', ruta_salida='...')")
