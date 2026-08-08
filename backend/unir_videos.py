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
import re
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
MAX_SEGUNDOS_CLIP = 90    # tope duro por clip en el unificador (backend, red de seguridad
                          # ademas de la validacion que ya hace el HTML antes de subir)

# Ruta real del binario de FFmpeg en este entorno (Railway no lo deja en el
# PATH del sistema como "ffmpeg" a secas, así que usamos el que trae
# empaquetado la propia librería imageio_ffmpeg, la misma que usa moviepy).
try:
    import imageio_ffmpeg
    FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_BIN = "ffmpeg"

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
            [FFMPEG_BIN, "-version"],
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

def obtener_orientacion(ruta_video):
    """Comprueba si el vídeo es vertical (retrato) u horizontal (paisaje).
    FFmpeg reporta el tamaño "en bruto" del sensor y, aparte, si hay que
    rotarlo para verse bien (metadato displaymatrix). Si la rotación es de
    90 o 270 grados, el ancho y el alto reales están invertidos."""
    try:
        result = subprocess.run(
            [FFMPEG_BIN, "-i", ruta_video],
            capture_output=True, text=True
        )
        m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", result.stderr)
        if not m:
            return "horizontal"
        ancho, alto = int(m.group(1)), int(m.group(2))

        rot = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees", result.stderr)
        if rot:
            grados = abs(float(rot.group(1))) % 180
            if 80 <= grados <= 100:
                ancho, alto = alto, ancho

        return "vertical" if alto > ancho else "horizontal"
    except Exception:
        return "horizontal"


def tiene_audio(ruta_video):
    """Comprueba si el archivo tiene alguna pista de audio."""
    try:
        result = subprocess.run(
            [FFMPEG_BIN, "-i", ruta_video],
            capture_output=True, text=True
        )
        return "Audio:" in result.stderr
    except Exception:
        return False


def obtener_duracion(ruta_video):
    """Obtiene la duración de un vídeo en segundos.
    Usa ffmpeg (no ffprobe, que puede no estar disponible en este entorno)
    leyendo la línea 'Duration: HH:MM:SS.xx' que ffmpeg escribe en stderr."""
    try:
        result = subprocess.run(
            [FFMPEG_BIN, "-i", ruta_video],
            capture_output=True, text=True
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", result.stderr)
        if not m:
            return 0
        horas, minutos, segundos = m.groups()
        return int(horas) * 3600 + int(minutos) * 60 + float(segundos)
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

def params_calidad(orientacion="horizontal"):
    """Devuelve los parámetros de FFmpeg según la calidad elegida.
    Si el vídeo es vertical, invierte ancho/alto del objetivo para no
    encoger el contenido dentro de un marco horizontal."""
    resoluciones = {
        "1080p": (1920, 1080),
        "720p":  (1280, 720),
        "original": None
    }
    dims = resoluciones.get(RESOLUCION, (1920, 1080))
    if dims and orientacion == "vertical":
        dims = (dims[1], dims[0])
    res = f"{dims[0]}:{dims[1]}" if dims else None

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
    clips_demasiado_largos = []
    for ruta in videos_rutas:
        if os.path.exists(ruta):
            duracion = obtener_duracion(ruta)
            if duracion and duracion > MAX_SEGUNDOS_CLIP:
                clips_demasiado_largos.append((os.path.basename(ruta), duracion))
                continue
            videos.append({"ruta": ruta, "nombre": os.path.basename(ruta), "duracion": duracion})

    if clips_demasiado_largos:
        detalle = ", ".join(f"{nombre} ({int(dur)}s)" for nombre, dur in clips_demasiado_largos)
        log(f"Clips rechazados por superar {MAX_SEGUNDOS_CLIP}s: {detalle}", "⚠")
        raise ValueError(
            f"Cada clip debe durar máximo {MAX_SEGUNDOS_CLIP} segundos. "
            f"Estos superan el límite: {detalle}. Redúcelos primero con la herramienta de reducir vídeo."
        )

    if not videos:
        raise ValueError("No se encontraron vídeos válidos")

    duracion_total = sum(v["duracion"] for v in videos)
    log(f"{len(videos)} vídeos · {int(duracion_total//60)}m {int(duracion_total%60)}s", "✅")

    ruta_final = ruta_salida

    # 6. Normalizar vídeos — misma resolución y codec
    print()
    log("Normalizando vídeos para unirlos...", "⚙")
    orientacion = obtener_orientacion(videos[0]["ruta"])
    res, crf, preset = params_calidad(orientacion)

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
            FFMPEG_BIN, "-y",
            "-i", v["ruta"],
            "-vf", vf,
            "-c:v", "libx264",
            "-threads", "2",
            "-crf", crf,
            "-preset", preset,
            "-c:a", "aac",
            "-ar", "44100",
            "-ac", "2",
            ruta_norm
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            log(f"  Tiempo agotado normalizando {v['nombre']} (mas de 180s)", "⚠")
            continue
        if result.returncode != 0:
            log(f"  Error normalizando {v['nombre']} (codigo {result.returncode}):", "⚠")
            print(f"--- STDERR completo de ffmpeg para {v['nombre']} ---")
            print(result.stderr)
            print("--- fin stderr ---")
            continue

        videos_norm.append(ruta_norm)

    if not videos_norm:
        log("No se pudo procesar ningún vídeo", "❌")
        raise RuntimeError("No se pudo procesar ningún vídeo")

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
        FFMPEG_BIN, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", lista_path,
        "-c", "copy",
        "-loglevel", "error",
        ruta_unido
    ]

    try:
        result = subprocess.run(cmd_concat, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        log("Tiempo agotado uniendo vídeos (mas de 120s)", "❌")
        raise RuntimeError("Tiempo agotado uniendo vídeos")
    if result.returncode != 0:
        log(f"Error uniendo vídeos: {result.stderr[-300:]}", "❌")
        raise RuntimeError(f"Error uniendo vídeos: {result.stderr[-300:]}")

    log("Vídeos unidos correctamente", "✅")

    # 9. Añadir música de fondo
    print()
    if ruta_musica:
        log(f"Añadiendo música · volumen {int(VOLUMEN_MUSICA*100)}%...", "🎵")

        duracion_video = obtener_duracion(ruta_unido)

        if tiene_audio(ruta_unido):
            # El vídeo unido tiene su propio sonido: lo bajamos y lo mezclamos con la música
            filtro = (
                f"[0:a]volume={VOLUMEN_VIDEO}[voz];"
                f"[1:a]volume={VOLUMEN_MUSICA},aloop=loop=-1:size=2e+09[music];"
                f"[voz][music]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            )
        else:
            # El vídeo unido no tiene pista de audio propia (clips mudos) - solo música
            filtro = f"[1:a]volume={VOLUMEN_MUSICA},aloop=loop=-1:size=2e+09[aout]"

        cmd_musica = [
            FFMPEG_BIN, "-y",
            "-i", ruta_unido,
            "-i", ruta_musica,
            "-filter_complex", filtro,
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-threads", "2",
            "-b:a", "192k",
        ]
        # Duración explícita en vez de fiarnos de -shortest, que con el bucle
        # infinito de la música (aloop=loop=-1) puede no cortar el proceso a tiempo
        if duracion_video and duracion_video > 0:
            cmd_musica += ["-t", str(duracion_video)]
        else:
            cmd_musica += ["-shortest"]
        cmd_musica += ["-loglevel", "error", ruta_final]

        musica_ok = False
        try:
            result = subprocess.run(cmd_musica, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                log(f"Error añadiendo música · guardando sin música: {result.stderr[-200:]}", "⚠")
                shutil.copy2(ruta_unido, ruta_final)
            else:
                musica_ok = True
        except subprocess.TimeoutExpired:
            log("Tiempo agotado añadiendo música (mas de 120s) · guardando sin música", "⚠")
            shutil.copy2(ruta_unido, ruta_final)

        if musica_ok:
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


# ═══════════════════════════════════════════════════════
#  REDUCTOR DE VÍDEO INDIVIDUAL (no une nada, solo comprime)
# ═══════════════════════════════════════════════════════

MAX_SEGUNDOS_REDUCTOR = 240  # 4 minutos
SEGUNDOS_CORTE_CALIDAD = 90  # por debajo de esto: 1080p · por encima: 720p mas comprimido

def reducir_video(ruta_entrada, ruta_salida=None, carpeta_temp=None):
    """
    Reduce el peso de UN vídeo sin tocar su duración (ni un segundo de recorte).
    Regla de calidad:
      - Menos de 90s  -> 1080p, calidad alta (crf 20)
      - 90s a 4 min    -> 720p, calidad mas comprimida (crf 26) para controlar el peso
    Devuelve la ruta del vídeo reducido.
    """
    if not verificar_ffmpeg():
        raise RuntimeError("FFmpeg no disponible en el servidor")

    if not os.path.exists(ruta_entrada):
        raise ValueError("El vídeo no existe")

    duracion = obtener_duracion(ruta_entrada)
    if duracion and duracion > MAX_SEGUNDOS_REDUCTOR:
        raise ValueError(
            f"El vídeo dura {int(duracion // 60)} min. El máximo para reducir son 4 minutos."
        )

    if not carpeta_temp:
        carpeta_temp = "/tmp/bookeo_reductor_temp"
    os.makedirs(carpeta_temp, exist_ok=True)

    if not ruta_salida:
        ruta_salida = os.path.join(carpeta_temp, "video_reducido.mp4")

    orientacion = obtener_orientacion(ruta_entrada)

    if duracion and duracion >= SEGUNDOS_CORTE_CALIDAD:
        # Clip mas largo (hasta 4 min): bajar mas la calidad para controlar el peso
        dims = (1280, 720)
        crf = "26"
        preset = "fast"
        log(f"Reductor: {duracion:.0f}s -> 720p, compresion mas agresiva", "⚙")
    else:
        # Clip corto: mantener buena calidad, ya pesa poco de por si
        dims = (1920, 1080)
        crf = "20"
        preset = "medium"
        log(f"Reductor: {duracion:.0f}s -> 1080p, calidad alta", "⚙")

    if orientacion == "vertical":
        dims = (dims[1], dims[0])
    res = f"{dims[0]}:{dims[1]}"
    vf = f"scale={res}:force_original_aspect_ratio=decrease,pad={res}:(ow-iw)/2:(oh-ih)/2,setsar=1"

    cmd = [
        FFMPEG_BIN, "-y",
        "-i", ruta_entrada,
        "-vf", vf,
        "-c:v", "libx264",
        "-threads", "2",
        "-crf", crf,
        "-preset", preset,
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-ac", "2",
        "-movflags", "+faststart",
        ruta_salida
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Tiempo agotado reduciendo el vídeo (más de 240s)")

    if result.returncode != 0:
        log(f"Error reduciendo vídeo (codigo {result.returncode}): {result.stderr[-300:]}", "⚠")
        raise RuntimeError(f"Error de FFmpeg reduciendo el vídeo: {result.stderr[-300:]}")

    peso_antes = os.path.getsize(ruta_entrada) / (1024 * 1024)
    peso_despues = os.path.getsize(ruta_salida) / (1024 * 1024)
    log(f"Reductor: {peso_antes:.1f}MB -> {peso_despues:.1f}MB (misma duración: {duracion:.0f}s)", "✅")

    return ruta_salida


if __name__ == "__main__":
    # Solo para pruebas locales — en Railway se llama via FastAPI
    print("Uso: from bookeo_unir_videos import unir_videos")
    print("     ruta = unir_videos(videos_rutas=[...], ruta_musica='...', ruta_salida='...')")
