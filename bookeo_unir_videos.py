"""
╔══════════════════════════════════════════════════════╗
║         BOOKEO · Unir vídeos con música             ║
║              Script de prueba MVP                    ║
╚══════════════════════════════════════════════════════╝

Une todos los vídeos de una carpeta en uno solo
y añade una música de fondo a volumen suave.

REQUIERE:
  FFmpeg instalado en el Mac:
  → Descarga desde: https://ffmpeg.org/download.html
  → O instala con Homebrew: brew install ffmpeg

CÓMO USAR:
  1. Edita la sección CONFIGURACIÓN con tus rutas
  2. En la terminal: python3 bookeo_unir_videos.py
  3. El vídeo final aparece en ~/Desktop/Bookeo_Videos/
"""

import os
import sys
import subprocess
import datetime
import shutil
from pathlib import Path

# ═══════════════════════════════════════════════════════
#  CONFIGURACIÓN — EDITA ESTO ANTES DE EJECUTAR
# ═══════════════════════════════════════════════════════

CARPETA_VIDEOS  = "/Users/a/Documents/videos-prueba"
CARPETA_MUSICA  = "/Users/a/Documents/musica-prueba"
NOMBRE_SALIDA   = "bookeo_video_prueba.mp4"

# Volumen de la música de fondo (0.0 = silencio · 1.0 = volumen original)
# 0.30 = música al 30% · recomendado
VOLUMEN_MUSICA  = 0.30

# Volumen del audio original de los vídeos cuando hay música activa
# 0.50 = voz/sonido del vídeo al 50% · deja espacio a la música sin taparla
# 1.0  = sin cambios (solo si los vídeos no tienen sonido importante)
VOLUMEN_VIDEO   = 0.50

# Calidad del vídeo final
# "alta"   → mejor calidad · archivo más grande
# "media"  → equilibrio calidad/tamaño · recomendado
# "baja"   → archivo pequeño · para pruebas rápidas
CALIDAD         = "media"

# Resolución máxima del vídeo final
# "original" → mantiene la resolución de los vídeos
# "1080p"    → Full HD · 1920x1080
# "720p"     → HD · 1280x720 · archivo más pequeño
RESOLUCION      = "1080p"

# ═══════════════════════════════════════════════════════
#  UTILIDADES
# ═══════════════════════════════════════════════════════

def log(msg, emoji="→"):
    hora = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{hora}] {emoji} {msg}")

def verificar_ffmpeg():
    """Comprueba que FFmpeg está instalado."""
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
    log("FFmpeg no encontrado", "❌")
    print()
    print("  Instala FFmpeg con uno de estos métodos:")
    print()
    print("  Opción A — Homebrew (recomendado):")
    print("    /bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"")
    print("    brew install ffmpeg")
    print()
    print("  Opción B — Descarga directa:")
    print("    https://ffmpeg.org/download.html → macOS")
    print()
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

def main():
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║       BOOKEO · Unir vídeos con música           ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    # 1. Verificar FFmpeg
    if not verificar_ffmpeg():
        sys.exit(1)

    # 2. Verificar carpetas
    if not os.path.exists(CARPETA_VIDEOS):
        log(f"No se encuentra la carpeta de vídeos: {CARPETA_VIDEOS}", "❌")
        sys.exit(1)

    if not os.path.exists(CARPETA_MUSICA):
        log(f"No se encuentra la carpeta de música: {CARPETA_MUSICA}", "❌")
        sys.exit(1)

    # 3. Cargar vídeos
    print()
    log("Buscando vídeos...", "🎬")
    videos = cargar_videos(CARPETA_VIDEOS)

    if not videos:
        log("No se encontraron vídeos en la carpeta", "❌")
        sys.exit(1)

    duracion_total = sum(v["duracion"] for v in videos)
    log(f"{len(videos)} vídeos encontrados · duración total: {int(duracion_total//60)}m {int(duracion_total%60)}s", "✅")
    for v in videos:
        log(f"  · {v['nombre']} ({int(v['duracion'])}s)", "")

    # 4. Cargar música
    print()
    log("Buscando música...", "🎵")
    ruta_musica = cargar_musica(CARPETA_MUSICA)
    if not ruta_musica:
        log("No se encontró música en la carpeta · el vídeo se generará sin música", "⚠")

    # 5. Carpeta de salida
    carpeta_sal = os.path.join(os.path.expanduser("~/Desktop"), "Bookeo_Videos")
    os.makedirs(carpeta_sal, exist_ok=True)
    carpeta_temp = os.path.join(carpeta_sal, "temp")
    os.makedirs(carpeta_temp, exist_ok=True)

    ruta_final = os.path.join(carpeta_sal, NOMBRE_SALIDA)

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

    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║                  ¡LISTO! ✅                     ║")
    print("╚══════════════════════════════════════════════════╝")
    print()
    print(f"  Vídeos unidos   : {len(videos_norm)}")
    print(f"  Duración final  : {int(duracion_final//60)}m {int(duracion_final%60)}s")
    print(f"  Tamaño          : {tamaño:.1f} MB")
    print(f"  Resolución      : {RESOLUCION}")
    print(f"  Calidad         : {CALIDAD}")
    if ruta_musica:
        print(f"  Música          : {os.path.basename(ruta_musica)} · {int(VOLUMEN_MUSICA*100)}% volumen")
        print(f"  Audio vídeo     : bajado al {int(VOLUMEN_VIDEO*100)}% para dejar espacio a la música")
    print()
    print(f"  📁 Guardado en  : {ruta_final}")
    print()
    print("  Abre el archivo para revisar el resultado.")
    print("  Si la música está muy alta/baja cambia VOLUMEN_MUSICA")
    print("  y vuelve a ejecutar el script.")
    print()

if __name__ == "__main__":
    main()
