"""
╔══════════════════════════════════════════════════════╗
║           BOOKEO MVP · Creador de libros             ║
║         mibookeo.es · Versión 3.3                    ║
╚══════════════════════════════════════════════════════╝
"""

import os, io, sys, json, base64, datetime, re, math
import resource
import gc
import shutil
from pypdf import PdfWriter
from pathlib import Path
from r2_storage import descargar_de_r2


def log_memoria(etiqueta):
    """Chivato de memoria: registra el pico de RAM acumulado por el proceso
    hasta este punto, para poder ver en los logs justo qué foto/paso estaba
    en marcha cuando se dispara un salto grande."""
    try:
        uso_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(f"[MEM] {etiqueta}: {uso_mb:.0f} MB (pico acumulado del proceso)")
    except Exception:
        pass

from PIL import Image, ExifTags, ImageStat, ImageOps
from reportlab.lib.pagesizes import mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

import anthropic

try:
    import qrcode
    QRCODE_OK = True
except ImportError:
    QRCODE_OK = False

try:
    import cv2
    import numpy as np
    OPENCV_OK = True
    _cas_test = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if _cas_test.empty():
        OPENCV_OK = False
        print("OpenCV: clasificador de caras no encontrado")
except ImportError:
    OPENCV_OK = False
    print("OpenCV no disponible")

CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ═══ Fuentes personalizadas (portada) ═══
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

FUENTES_DISPONIBLES = {
    "Aclonica": "Aclonica-Regular.ttf",
    "GochiHand": "GochiHand-Regular.ttf",
    "GrandHotel": "GrandHotel-Regular.ttf",
    "JustAnotherHand": "JustAnotherHand-Regular.ttf",
    "LuckiestGuy": "LuckiestGuy-Regular.ttf",
    "NerkoOne": "NerkoOne-Regular.ttf",
    "Notable": "Notable-Regular.ttf",
    "OrbitronRegular": "Orbitron-Regular.ttf",
    "OrbitronBold": "Orbitron-Bold.ttf",
    "OrbitronBlack": "Orbitron-Black.ttf",
    "PermanentMarker": "PermanentMarker-Regular.ttf",
}

for _nombre_fuente, _archivo_fuente in FUENTES_DISPONIBLES.items():
    try:
        pdfmetrics.registerFont(TTFont(_nombre_fuente, os.path.join(_FONTS_DIR, _archivo_fuente)))
    except Exception as _e:
        print(f"[fuentes] No se pudo registrar {_nombre_fuente}: {_e}")

MG = 10
GAP = 3
QR_MM = 22
QR_URL_PRUEBA = ""
MIN_PPI_OK = 180
MIN_PPI_BEST = 250

CV = (125/255, 184/255, 152/255)
CO = (26/255, 26/255, 46/255)
CCL = (0.96, 0.94, 0.90)

FORMATOS_MM = {
    "2020": (208, 208),
    "2828": (288, 288),
    "2128": (218, 288),
}


def obtener_medidas(formato="2128", orientacion="v"):
    ancho, alto = FORMATOS_MM.get(formato, FORMATOS_MM["2128"])
    if formato == "2128" and orientacion == "h":
        ancho, alto = alto, ancho
    return ancho, alto


def log(msg, e="->"):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {e} {msg}")


def set_negro(c):
    c.setFillColorCMYK(0.60, 0.60, 0.60, 1.0)


def leer_fecha(ruta):
    nombre = os.path.basename(ruta)
    try:
        img = Image.open(ruta)
        exif = img._getexif()
        if exif:
            for tid, val in exif.items():
                if ExifTags.TAGS.get(tid) in ["DateTimeOriginal", "DateTime", "DateTimeDigitized"] and isinstance(val, str):
                    try:
                        f = datetime.datetime.strptime(val.strip(), "%Y:%m:%d %H:%M:%S")
                        if f.year > 2000:
                            return f, "exif"
                    except Exception:
                        pass
    except Exception:
        pass
    for pat in [r'(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})',
                r'IMG-(\d{4})(\d{2})(\d{2})-WA\d+',
                r'(\d{4})(\d{2})(\d{2})']:
        m = re.search(pat, nombre)
        if m:
            try:
                g = m.groups()
                a, me, d = int(g[0]), int(g[1]), int(g[2])
                h = int(g[3]) if len(g) > 3 else 12
                mi = int(g[4]) if len(g) > 4 else 0
                if 2000 <= a <= 2030 and 1 <= me <= 12 and 1 <= d <= 31:
                    return datetime.datetime(a, me, d, h, mi), "nombre"
            except Exception:
                pass
    return datetime.datetime.fromtimestamp(os.path.getmtime(ruta)), "modificacion"


def detectar_caras(ruta):
    if not OPENCV_OK:
        return None
    try:
        img_pil = Image.open(ruta)
        MAX_LADO = 1600
        # Pedir al decodificador JPEG que ya descomprima en pequeño de entrada,
        # en vez de descomprimir la foto entera (puede ser 50-100+ megapíxeles
        # en móviles modernos) y reducirla después
        try:
            img_pil.draft("RGB", (MAX_LADO, MAX_LADO))
        except Exception:
            pass
        img_pil = img_pil.convert("RGB")
        img_pil = ImageOps.exif_transpose(img_pil)
        iw_original, ih_original = img_pil.size
        if max(img_pil.size) > MAX_LADO:
            ratio = MAX_LADO / max(img_pil.size)
            nuevo_tam = (max(1, int(img_pil.size[0] * ratio)), max(1, int(img_pil.size[1] * ratio)))
            img_pil = img_pil.resize(nuevo_tam, Image.LANCZOS)
        # Factor para volver a convertir las coordenadas detectadas en la
        # miniatura al tamaño real de la foto original (antes no se hacía
        # y el recorte centrado en la cara salía descuadrado)
        factor_escala = iw_original / img_pil.size[0]
        cv_img = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
        gris = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
        gris_eq = cv2.equalizeHist(gris)
        cas = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        det = cas.detectMultiScale(gris_eq, scaleFactor=1.05, minNeighbors=4, minSize=(20, 20))
        if len(det) == 0:
            det = cas.detectMultiScale(gris_eq, scaleFactor=1.1, minNeighbors=3, minSize=(15, 15))
        if len(det) == 0:
            return None
        caras = det.tolist()
        x1 = min(c[0] for c in caras)
        y1 = min(c[1] for c in caras)
        x2 = max(c[0] + c[2] for c in caras)
        y2 = max(c[1] + c[3] for c in caras)
        return {
            "x1": x1 * factor_escala, "y1": y1 * factor_escala,
            "x2": x2 * factor_escala, "y2": y2 * factor_escala,
            "iw": iw_original, "ih": ih_original, "n": len(caras)
        }
    except Exception:
        return None


def recortar_con_caras(ruta, w_mm, h_mm):
    try:
        DPI_OBJETIVO = 300
        # Margen x1.6 sobre lo que hace falta a 300ppp, para tener suficiente
        # imagen de sobra al recortar sin perder nitidez
        target_w_px = max(400, int((w_mm / 25.4) * DPI_OBJETIVO * 1.6))
        target_h_px = max(400, int((h_mm / 25.4) * DPI_OBJETIVO * 1.6))

        img = Image.open(ruta)
        # Igual que en detectar_caras: pedir al decodificador que ya
        # descomprima en un tamaño reducido, en vez de descomprimir la foto
        # entera (puede haber picos enormes de memoria con móviles modernos)
        try:
            img.draft("RGB", (target_w_px, target_h_px))
        except Exception:
            pass
        img = img.convert("RGB")
        img = ImageOps.exif_transpose(img)
        iw, ih = img.size
        ratio_z = w_mm / h_mm
        ratio_i = iw / ih

        caras = detectar_caras(ruta)
        if caras and caras.get("iw"):
            # detectar_caras calcula sus coordenadas sobre su propia versión
            # reducida de la foto; aquí puede que hayamos abierto una versión
            # reducida distinta (draft), así que reescalamos proporcionalmente
            escala_draft = iw / caras["iw"]
            x1_s = caras["x1"] * escala_draft
            y1_s = caras["y1"] * escala_draft
            x2_s = caras["x2"] * escala_draft
            y2_s = caras["y2"] * escala_draft
            cx_cara = (x1_s + x2_s) / 2
            cy_cara = (y1_s + y2_s) / 2
            cara_h = y2_s - y1_s
            margen_arriba = int(cara_h * 1.5)
            y_minimo_visible = max(0, y1_s - margen_arriba)
            cx = cx_cara
        else:
            cx = iw / 2
            y_minimo_visible = 0
            x1_s = y1_s = x2_s = y2_s = 0

        if ratio_i > ratio_z:
            nw = int(ih * ratio_z)
            nw = min(nw, iw)
            x0 = int(cx - nw / 2)
            x0 = max(0, min(x0, iw - nw))
            recorte = (x0, 0, x0 + nw, ih)
        else:
            nh = int(iw / ratio_z)
            nh = min(nh, ih)
            if caras:
                y0_ideal = y_minimo_visible
                y0 = max(0, min(y0_ideal, ih - nh))
                if y0 > y1_s - int(cara_h * 0.3):
                    y0 = max(0, y1_s - int(cara_h * 1.5))
                    y0 = max(0, min(y0, ih - nh))
                if y0 + nh < y2_s:
                    y0 = max(0, int(cy_cara - nh * 0.45))
                    y0 = max(0, min(y0, ih - nh))
            else:
                y0 = int(ih / 2 - nh / 2)
                y0 = max(0, min(y0, ih - nh))
            recorte = (0, y0, iw, y0 + nh)

        img = img.crop(recorte)
        return img
    except Exception as e:
        log(f"Error recortando {os.path.basename(ruta)}: {e}", "!")
        try:
            img = Image.open(ruta).convert("RGB")
            return ImageOps.exif_transpose(img)
        except Exception:
            return None


def ppi(ruta, w_mm, h_mm):
    try:
        img = Image.open(ruta)
        pw, ph = img.size
        return min(pw / (w_mm / 25.4), ph / (h_mm / 25.4))
    except Exception:
        return 300


def foto_zona(c, ruta, x, y, w, h, check_ppi=True):
    if not ruta or not os.path.exists(ruta):
        log(f"Foto no encontrada: {ruta}", "!")
        return
    log_memoria(f"antes de {os.path.basename(ruta)}")
    img = recortar_con_caras(ruta, w, h)
    if img is None:
        return
    log_memoria(f"tras recortar {os.path.basename(ruta)}")

    # Reducir la foto ya recortada a la resolución real que necesita imprenta
    # (300 ppp del hueco donde va) - antes se incrustaba a resolución completa
    # del móvil aunque el hueco de la página fuera pequeño, gastando memoria
    # y peso final sin ninguna ganancia de calidad visible impresa.
    DPI_OBJETIVO = 300
    ancho_max_px = max(1, int((w / 25.4) * DPI_OBJETIVO))
    alto_max_px = max(1, int((h / 25.4) * DPI_OBJETIVO))
    if img.width > ancho_max_px or img.height > alto_max_px:
        img.thumbnail((ancho_max_px, alto_max_px), Image.LANCZOS)

    iw, ih = img.size
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    buf.seek(0)
    rl = ImageReader(buf)
    img.close()
    log_memoria(f"despues de {os.path.basename(ruta)}")
    xm, ym, wm, hm = x * mm, y * mm, w * mm, h * mm
    c.saveState()
    p2 = c.beginPath()
    p2.rect(xm, ym, wm, hm)
    c.clipPath(p2, stroke=0)
    ri = iw / ih
    rz = wm / hm
    if ri > rz:
        hd = hm
        wd = hm * ri
        xd = xm + (wm - wd) / 2
        yd = ym
    else:
        wd = wm
        hd = wm / ri
        xd = xm
        yd = ym + (hm - hd) / 2
    c.drawImage(rl, xd, yd, wd, hd)
    c.restoreState()


def dibujar_qr_sobre_foto(c, x_foto, y_foto, w_foto, h_foto, url, ruta_foto=None):
    s = QR_MM * mm
    mg_qr = 2 * mm
    qx = (x_foto + w_foto) * mm - s - mg_qr
    qy = y_foto * mm + mg_qr
    color_pts = CO
    color_fondo = (0.96, 0.94, 0.90)
    if ruta_foto and os.path.exists(ruta_foto):
        try:
            img = Image.open(ruta_foto).convert("RGB")
            iw, ih = img.size
            zona = img.crop((int(iw * 0.75), 0, iw, int(ih * 0.25)))
            lum = sum(ImageStat.Stat(zona).mean[:3]) / 3
            if lum < 100:
                color_pts = (1.0, 1.0, 1.0)
                color_fondo = (0.06, 0.06, 0.12)
        except Exception:
            pass
    pad = 1.5 * mm
    c.setFillColorRGB(*color_fondo)
    c.roundRect(qx - pad, qy - pad, s + pad * 2, s + pad * 2, 1.5 * mm, fill=1, stroke=0)
    if QRCODE_OK:
        try:
            def hex_col(t):
                return "#{:02x}{:02x}{:02x}".format(int(t[0] * 255), int(t[1] * 255), int(t[2] * 255))
            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=1)
            qr.add_data(url)
            qr.make(fit=True)
            qri = qr.make_image(fill_color=hex_col(color_pts), back_color=hex_col(color_fondo))
            qri = qri.resize((200, 200), Image.LANCZOS)
            buf = io.BytesIO()
            qri.save(buf, "PNG")
            buf.seek(0)
            c.drawImage(ImageReader(buf), qx, qy, s, s, mask='auto')
        except Exception:
            pass


def wm(c, aw, ah):
    c.saveState()
    c.setFillColorRGB(0.55, 0.55, 0.55)
    c.setFont("Helvetica-Bold", 18)
    c.translate(aw / 2, ah / 2)
    c.rotate(38)
    for i in range(-4, 5):
        for j in range(-3, 4):
            c.drawCentredString(i * 160, j * 100, "MUESTRA - mibookeo.es")
    c.restoreState()


def bg_blanco(c, AW, AH):
    c.setFillColorRGB(1, 1, 1)
    c.rect(0, 0, AW * mm, AH * mm, fill=1, stroke=0)


def texto_pie(c, AW, texto, y_mm=MG / 2 + 1):
    if not texto:
        return
    c.setFillColorRGB(*CO)
    c.setFont("Helvetica-Oblique", 2.8 * mm)
    c.drawCentredString(AW * mm / 2, y_mm * mm, texto)


def hex_a_rgb01(hexcolor):
    try:
        h = (hexcolor or "").lstrip('#')
        r = int(h[0:2], 16) / 255
        g = int(h[2:4], 16) / 255
        b = int(h[4:6], 16) / 255
        return (r, g, b)
    except Exception:
        return (0.10, 0.10, 0.18)


def mapear_fuente(font_family, bold=False, italic=False):
    ff = (font_family or "").lower()

    # Fuentes personalizadas (subidas a backend/fonts) - se comprueban primero,
    # "orbitron black" tiene que ir antes que "orbitron" a secas
    fuentes_custom = [
        ("orbitron black", "OrbitronBlack"),
        ("orbitron", "OrbitronBold" if bold else "OrbitronRegular"),
        ("aclonica", "Aclonica"),
        ("gochi hand", "GochiHand"),
        ("grand hotel", "GrandHotel"),
        ("just another hand", "JustAnotherHand"),
        ("luckiest guy", "LuckiestGuy"),
        ("nerko one", "NerkoOne"),
        ("notable", "Notable"),
        ("permanent marker", "PermanentMarker"),
    ]
    for clave, nombre_fuente in fuentes_custom:
        if clave in ff:
            return nombre_fuente

    if "courier" in ff:
        base = "Courier"
    elif any(s in ff for s in ("georgia", "lora", "times", "serif")):
        base = "Times"
    else:
        base = "Helvetica"
    if base == "Times":
        if bold and italic:
            return "Times-BoldItalic"
        if bold:
            return "Times-Bold"
        if italic:
            return "Times-Italic"
        return "Times-Roman"
    suf = ("Bold" if bold else "") + ("Oblique" if italic else "")
    return f"{base}-{suf}" if suf else base


def _dibujar_onda_lado(c, x1, y1, x2, y2, grosor_pt, horizontal):
    amplitud = max(0.8 * mm, grosor_pt * 0.6)
    paso = amplitud * 3
    p = c.beginPath()
    p.moveTo(x1, y1)
    longitud = (x2 - x1) if horizontal else (y2 - y1)
    pasos = max(2, round(abs(longitud) / paso))
    for i in range(1, pasos + 1):
        t = i / pasos
        dir_ = 1 if i % 2 == 0 else -1
        if horizontal:
            x = x1 + (x2 - x1) * t
            xc = x1 + (x2 - x1) * (t - 0.5 / pasos)
            yc = y1 + dir_ * amplitud
            p.curveTo(xc, yc, xc, yc, x, y1)
        else:
            y = y1 + (y2 - y1) * t
            yc = y1 + (y2 - y1) * (t - 0.5 / pasos)
            xc = x1 + dir_ * amplitud
            p.curveTo(xc, yc, xc, yc, x1, y)
    c.drawPath(p, fill=0, stroke=1)


def dibujar_marco_pdf(c, AW, AH, marco, canvas_w):
    if not marco:
        return
    estilo = marco.get("estilo", "ninguno")
    if estilo == "ninguno":
        return

    aw_pt, ah_pt = AW * mm, AH * mm
    escala_pt = aw_pt / canvas_w if canvas_w else 1.0
    grosor_pt = marco.get("grosor", 6) * escala_pt
    color = hex_a_rgb01(marco.get("color", "#1a1a2e"))

    # Posición y tamaño reales del marco que el cliente dejó en el editor
    # (coordenadas de canvas, origen arriba-izquierda, igual que en textos/foto)
    left_px = marco.get("left", canvas_w * 0.06)
    top_px = marco.get("top", canvas_w * 0.06)
    width_px = marco.get("width", canvas_w * 0.88)
    height_px = marco.get("height", canvas_w * 0.88)

    x0 = left_px * escala_pt
    w = width_px * escala_pt
    h = height_px * escala_pt
    # El canvas crece hacia abajo, el PDF (ReportLab) crece hacia arriba: invertir Y
    y0 = ah_pt - (top_px * escala_pt) - h

    c.saveState()
    c.setStrokeColorRGB(*color)

    if estilo == "simple":
        c.setLineWidth(grosor_pt)
        c.rect(x0, y0, w, h, fill=0, stroke=1)

    elif estilo == "doble":
        g = max(0.5, grosor_pt / 2)
        sep = g * 2.5
        c.setLineWidth(g)
        c.rect(x0, y0, w, h, fill=0, stroke=1)
        c.rect(x0 + sep, y0 + sep, w - sep * 2, h - sep * 2, fill=0, stroke=1)

    elif estilo == "redondeadas":
        c.setLineWidth(grosor_pt)
        radio = max(2, grosor_pt * 2.5)
        c.roundRect(x0, y0, w, h, radio, fill=0, stroke=1)

    elif estilo == "puntos":
        c.setLineWidth(grosor_pt)
        c.setDash([1, grosor_pt * 1.8])
        c.rect(x0, y0, w, h, fill=0, stroke=1)
        c.setDash([])

    elif estilo == "ondas":
        c.setLineWidth(max(0.5, grosor_pt * 0.5))
        _dibujar_onda_lado(c, x0, y0, x0 + w, y0, grosor_pt, True)
        _dibujar_onda_lado(c, x0, y0 + h, x0 + w, y0 + h, grosor_pt, True)
        _dibujar_onda_lado(c, x0, y0, x0, y0 + h, grosor_pt, False)
        _dibujar_onda_lado(c, x0 + w, y0, x0 + w, y0 + h, grosor_pt, False)

    elif estilo == "polaroid":
        borde = max(3 * mm, w * 0.045)
        borde_abajo = borde * 2.6
        c.setFillColorRGB(1, 1, 1)
        c.rect(x0, y0 + h - borde, w, borde, fill=1, stroke=0)
        c.rect(x0, y0, borde, h, fill=1, stroke=0)
        c.rect(x0 + w - borde, y0, borde, h, fill=1, stroke=0)
        c.rect(x0, y0, w, borde_abajo, fill=1, stroke=0)

    c.restoreState()


def _dibujar_texto_editor(c, AW, AH, canvas_w, canvas_h, info, texto_fallback):
    if not info:
        return
    texto = info.get("texto") or texto_fallback or ""
    if not texto:
        return

    aw_pt, ah_pt = AW * mm, AH * mm
    left_frac = info.get("left", 0) / canvas_w
    top_frac = info.get("top", 0) / canvas_h
    width_frac = info.get("width", canvas_w * 0.8) / canvas_w
    fontsize_frac = info.get("fontSize", canvas_w * 0.06) / canvas_w

    x_pt = left_frac * aw_pt
    width_pt = width_frac * aw_pt
    fontsize_pt = fontsize_frac * aw_pt
    top_pt_desde_arriba = top_frac * ah_pt
    baseline_pt_desde_arriba = top_pt_desde_arriba + fontsize_pt * 0.82
    y_pt = ah_pt - baseline_pt_desde_arriba

    negrita = info.get("fontWeight") == "bold"
    cursiva = info.get("fontStyle") == "italic"
    subrayado = bool(info.get("underline"))
    fuente = mapear_fuente(info.get("fontFamily", ""), negrita, cursiva)
    color = hex_a_rgb01(info.get("fill", "#1a1a2e"))

    c.setFillColorRGB(*color)
    c.setFont(fuente, fontsize_pt)
    cx_pt = x_pt + width_pt / 2
    c.drawCentredString(cx_pt, y_pt, texto)

    if subrayado:
        ancho_pt = c.stringWidth(texto, fuente, fontsize_pt)
        y_linea = y_pt - fontsize_pt * 0.12
        c.setStrokeColorRGB(*color)
        c.setLineWidth(max(0.4, fontsize_pt * 0.04))
        c.line(cx_pt - ancho_pt / 2, y_linea, cx_pt + ancho_pt / 2, y_linea)


def dibujar_portada_editor(c, AW, AH, ruta, titulo, subtitulo, do_wm, editor):
    canvas_w = editor.get("canvas_w") or AW
    canvas_h = editor.get("canvas_h") or AH
    aw_pt, ah_pt = AW * mm, AH * mm

    color_fondo = hex_a_rgb01(editor.get("color_fondo", "#f5f0e6"))
    c.setFillColorRGB(*color_fondo)
    c.rect(0, 0, aw_pt, ah_pt, fill=1, stroke=0)

    foto_info = editor.get("foto")
    if ruta and os.path.exists(ruta) and foto_info:
        try:
            # Tamaño real (solo cabecera, sin descomprimir aún) - hace falta
            # para los cálculos de posición si el editor no mandó el tamaño
            with Image.open(ruta) as _tmp:
                iw_true, ih_true = _tmp.size

            img_original = Image.open(ruta)
            try:
                # Igual que en las páginas normales: pedir al decodificador
                # que ya descomprima en un tamaño acotado, en vez de
                # descomprimir fotos de móvil de 50-100+ megapíxeles enteras
                img_original.draft("RGB", (3500, 3500))
            except Exception:
                pass
            img_original = img_original.convert("RGB")
            img_original = ImageOps.exif_transpose(img_original)

            ancho_nat = foto_info.get("width") or iw_true
            alto_nat = foto_info.get("height") or ih_true
            scale_x = foto_info.get("scaleX") or 1
            scale_y = foto_info.get("scaleY") or 1

            disp_w_frac = (ancho_nat * scale_x) / canvas_w
            disp_h_frac = (alto_nat * scale_y) / canvas_h
            cx_frac = foto_info.get("left", canvas_w / 2) / canvas_w
            cy_frac = foto_info.get("top", canvas_h / 2) / canvas_h

            disp_w_pt = disp_w_frac * aw_pt
            disp_h_pt = disp_h_frac * ah_pt
            cx_pt = cx_frac * aw_pt
            cy_pt = ah_pt - (cy_frac * ah_pt)

            # Igual que en las páginas normales: reducir a 300ppp del tamaño
            # real que ocupa en la portada, no la resolución completa del móvil
            DPI_OBJETIVO = 300
            disp_w_mm = disp_w_pt / mm
            disp_h_mm = disp_h_pt / mm
            ancho_max_px = max(1, int((disp_w_mm / 25.4) * DPI_OBJETIVO))
            alto_max_px = max(1, int((disp_h_mm / 25.4) * DPI_OBJETIVO))
            if img_original.width > ancho_max_px or img_original.height > alto_max_px:
                img_original.thumbnail((ancho_max_px, alto_max_px), Image.LANCZOS)

            buf = io.BytesIO()
            img_original.save(buf, "JPEG", quality=92)
            buf.seek(0)
            rl_img = ImageReader(buf)

            angulo = foto_info.get("angle", 0) or 0

            c.saveState()
            p = c.beginPath()
            p.rect(0, 0, aw_pt, ah_pt)
            c.clipPath(p, stroke=0)
            c.translate(cx_pt, cy_pt)
            if angulo:
                c.rotate(-angulo)
            c.drawImage(rl_img, -disp_w_pt / 2, -disp_h_pt / 2,
                        width=disp_w_pt, height=disp_h_pt,
                        preserveAspectRatio=False, mask='auto')
            c.restoreState()
        except Exception as e:
            log(f"Error dibujando foto editada, uso modo automatico: {e}", "!")
            foto_zona(c, ruta, 0, 0, AW, AH, check_ppi=False)
    elif ruta:
        foto_zona(c, ruta, 0, 0, AW, AH, check_ppi=False)

    dibujar_marco_pdf(c, AW, AH, editor.get("marco"), canvas_w)

    _dibujar_texto_editor(c, AW, AH, canvas_w, canvas_h, editor.get("titulo"), titulo)
    _dibujar_texto_editor(c, AW, AH, canvas_w, canvas_h, editor.get("subtitulo"), subtitulo)

    if do_wm:
        wm(c, aw_pt, ah_pt)


def dibujar_portada(c, AW, AH, ruta, titulo, subtitulo, do_wm, editor=None):
    if editor:
        dibujar_portada_editor(c, AW, AH, ruta, titulo, subtitulo, do_wm, editor)
        return

    bg_blanco(c, AW, AH)
    if ruta:
        foto_zona(c, ruta, 0, MG + 18, AW, AH - MG - 18, check_ppi=False)
    c.setFillColorRGB(1, 1, 1)
    c.rect(0, 0, AW * mm, (MG + 18) * mm, fill=1, stroke=0)
    set_negro(c)
    c.setFont("Helvetica-Bold", 8 * mm)
    c.drawCentredString(AW * mm / 2, (MG + 10) * mm, titulo)
    if subtitulo:
        c.setFillColorRGB(0.5, 0.5, 0.5)
        c.setFont("Helvetica", 3 * mm)
        c.drawCentredString(AW * mm / 2, (MG + 4) * mm, subtitulo)
    if do_wm:
        wm(c, AW * mm, AH * mm)


def dibujar_lomo(c, AW, AH, titulo, color_fondo=None, do_wm=False, editor=None):
    aw, ah = AW * mm, AH * mm

    # Fondo: si la portada tiene editor, usa el mismo color que eligio el cliente
    if editor and editor.get("color_fondo"):
        try:
            color_fondo = hex_a_rgb01(editor.get("color_fondo"))
        except Exception:
            pass

    if color_fondo:
        c.setFillColorRGB(*color_fondo)
    else:
        c.setFillColorRGB(1, 1, 1)
    c.rect(0, 0, aw, ah, fill=1, stroke=0)

    c.saveState()
    c.translate(aw / 2, ah / 2)
    c.rotate(-90)

    # Fuente y color del texto: los mismos que eligio el cliente para el
    # titulo de la portada (editor.titulo.fontFamily / fill / bold / italic).
    # Si no hay editor (portada automatica), se mantiene el negro enriquecido de siempre.
    info_titulo = editor.get("titulo") if editor else None
    if info_titulo and info_titulo.get("fill"):
        negrita = info_titulo.get("fontWeight") == "bold"
        cursiva = info_titulo.get("fontStyle") == "italic"
        fuente = mapear_fuente(info_titulo.get("fontFamily", ""), negrita, cursiva)
        try:
            color_texto = hex_a_rgb01(info_titulo.get("fill", "#1a1a2e"))
            c.setFillColorRGB(*color_texto)
        except Exception:
            set_negro(c)
    else:
        set_negro(c)
        fuente = "Helvetica-Bold"

    c.setFont(fuente, 4 * mm)
    c.drawCentredString(0, 0, titulo)
    c.restoreState()
    if do_wm:
        wm(c, aw, ah)


def dibujar_contraportada(c, AW, AH, do_wm):
    bg_blanco(c, AW, AH)
    c.setFillColorRGB(0.65, 0.65, 0.65)
    c.setFont("Helvetica", 2.8 * mm)
    c.drawCentredString(AW * mm / 2, (MG + 2) * mm, "Bookeo - mibookeo.es")
    if do_wm:
        wm(c, AW * mm, AH * mm)


def dibujar_pagina_blanca(c, AW, AH):
    bg_blanco(c, AW, AH)


def layout_1(c, AW, AH, fotos_rutas, qr_idx, qr_url, pie, do_wm):
    bg_blanco(c, AW, AH)
    r = fotos_rutas[0]
    foto_zona(c, r, 0, 0, AW, AH)
    if qr_idx == 0:
        dibujar_qr_sobre_foto(c, 0, 0, AW, AH, qr_url, r)
    texto_pie(c, AW, pie)
    if do_wm:
        wm(c, AW * mm, AH * mm)


def layout_2H(c, AW, AH, fotos_rutas, qr_idx, qr_url, pie, do_wm):
    bg_blanco(c, AW, AH)
    pie_h = 8 if pie else 0
    fw = (AW - MG * 2 - GAP) / 2
    fh = AH - MG * 2 - pie_h
    y0 = MG + pie_h
    for i, r in enumerate(fotos_rutas[:2]):
        x = MG + i * (fw + GAP)
        foto_zona(c, r, x, y0, fw, fh)
        if qr_idx == i:
            dibujar_qr_sobre_foto(c, x, y0, fw, fh, qr_url, r)
    texto_pie(c, AW, pie)
    if do_wm:
        wm(c, AW * mm, AH * mm)


def layout_2V(c, AW, AH, fotos_rutas, qr_idx, qr_url, pie, do_wm):
    bg_blanco(c, AW, AH)
    pie_h = 8 if pie else 0
    fh = (AH - MG * 2 - GAP - pie_h) / 2
    fw = AW - MG * 2
    y0 = MG + pie_h
    for i, r in enumerate(fotos_rutas[:2]):
        y = y0 + i * (fh + GAP)
        foto_zona(c, r, MG, y, fw, fh)
        if qr_idx == i:
            dibujar_qr_sobre_foto(c, MG, y, fw, fh, qr_url, r)
    texto_pie(c, AW, pie)
    if do_wm:
        wm(c, AW * mm, AH * mm)


def layout_3(c, AW, AH, fotos_rutas, qr_idx, qr_url, pie, do_wm):
    bg_blanco(c, AW, AH)
    pie_h = 8 if pie else 0
    pw = AW * 0.60 - MG
    sh = (AH - MG * 2 - GAP - pie_h) / 2
    sw = AW - MG - pw - GAP - MG
    zh = AH - MG * 2 - pie_h
    y0 = MG + pie_h
    r0 = fotos_rutas[0]
    foto_zona(c, r0, MG, y0, pw, zh)
    if qr_idx == 0:
        dibujar_qr_sobre_foto(c, MG, y0, pw, zh, qr_url, r0)
    sx = MG + pw + GAP
    for i, r in enumerate(fotos_rutas[1:3]):
        y = y0 + i * (sh + GAP)
        foto_zona(c, r, sx, y, sw, sh)
        if qr_idx == i + 1:
            dibujar_qr_sobre_foto(c, sx, y, sw, sh, qr_url, r)
    texto_pie(c, AW, pie)
    if do_wm:
        wm(c, AW * mm, AH * mm)


def layout_4(c, AW, AH, fotos_rutas, qr_idx, qr_url, pie, do_wm):
    bg_blanco(c, AW, AH)
    pie_h = 8 if pie else 0
    cw = (AW - MG * 2 - GAP) / 2
    ch = (AH - MG * 2 - GAP - pie_h) / 2
    y0 = MG + pie_h
    pos = [(MG, y0 + ch + GAP), (MG + cw + GAP, y0 + ch + GAP), (MG, y0), (MG + cw + GAP, y0)]
    for i, r in enumerate(fotos_rutas[:4]):
        x, y = pos[i]
        foto_zona(c, r, x, y, cw, ch)
        if qr_idx == i:
            dibujar_qr_sobre_foto(c, x, y, cw, ch, qr_url, r)
    texto_pie(c, AW, pie)
    if do_wm:
        wm(c, AW * mm, AH * mm)


def layout_titulo_capitulo(c, AW, AH, titulo, subtitulo, fotos_rutas, variante, do_wm):
    bg_blanco(c, AW, AH)
    banda_h = AH * 0.25
    foto_h = AH - banda_h - MG
    cy = (AH - banda_h / 2) * mm
    set_negro(c)
    c.setFont("Helvetica-Bold", 9 * mm)
    c.drawCentredString(AW * mm / 2, cy + 3 * mm, titulo)
    if subtitulo:
        c.setFillColorRGB(0.5, 0.5, 0.5)
        c.setFont("Helvetica-Oblique", 3.2 * mm)
        c.drawCentredString(AW * mm / 2, cy - 4 * mm, subtitulo)
    n = len(fotos_rutas)
    if n == 0:
        pass
    elif n == 1:
        foto_zona(c, fotos_rutas[0], MG, MG, AW - MG * 2, foto_h)
    elif variante % 2 == 0 or n == 2:
        fw = (AW - MG * 2 - GAP) / 2
        for j, r in enumerate(fotos_rutas[:2]):
            foto_zona(c, r, MG + j * (fw + GAP), MG, fw, foto_h)
    else:
        foto_zona(c, fotos_rutas[0], MG, MG, AW - MG * 2, foto_h)
    if do_wm:
        wm(c, AW * mm, AH * mm)


def elegir_layout(fotos_grupo, layout_anterior="", variante=0):
    n = len(fotos_grupo)
    if n == 0:
        return None, []
    if n == 1:
        return "1", fotos_grupo
    if n == 2:
        if layout_anterior == "2H":
            return "2V", fotos_grupo
        elif layout_anterior == "2V":
            return "2H", fotos_grupo
        elif variante % 2 == 0:
            return "2H", fotos_grupo
        else:
            return "2V", fotos_grupo
    if n == 3:
        return "3", fotos_grupo
    if n >= 4:
        if layout_anterior == "4":
            if variante % 2 == 0:
                return "3", fotos_grupo[:3]
            else:
                return "2H", fotos_grupo[:2]
        return "4", fotos_grupo[:4]
    return "1", fotos_grupo[:1]


def paginas_para_grupo(fotos, qr_map, texto="", variante_inicio=0):
    paginas = []
    idx = 0
    variante = variante_inicio
    layout_ant = ""
    while idx < len(fotos):
        restantes = len(fotos) - idx
        if restantes >= 4 and layout_ant != "4":
            n_coger = 4
        elif restantes >= 4 and layout_ant == "4":
            n_coger = 3
        elif restantes == 3:
            n_coger = 3
        elif restantes == 2:
            n_coger = 2
        else:
            n_coger = 1
        grupo = fotos[idx:idx + n_coger]
        layout, fotos_layout = elegir_layout(grupo, layout_ant, variante)
        if layout is None:
            break
        qr_idx = -1
        qr_url = QR_URL_PRUEBA
        for fi, foto in enumerate(fotos_layout):
            nombre = foto["nombre"] if isinstance(foto, dict) else os.path.basename(foto)
            if nombre in qr_map:
                qr_idx = fi
                qr_url = qr_map[nombre]
                break
        paginas.append({
            "layout": layout,
            "fotos": fotos_layout,
            "qr_idx": qr_idx,
            "qr_url": qr_url,
            "texto": texto if idx == 0 else "",
        })
        avance = len(fotos_layout) if fotos_layout else 1
        idx += avance
        layout_ant = layout
        variante += 1
    return paginas


def agrupar_por_dia(fotos_capitulo):
    bloques = []
    bloque_actual = []
    dia_actual = None
    for foto in fotos_capitulo:
        dia_foto = foto["fecha"].strftime("%Y-%m-%d")
        if dia_actual is None:
            dia_actual = dia_foto
            bloque_actual = [foto]
        elif dia_foto == dia_actual:
            bloque_actual.append(foto)
        else:
            bloques.append(bloque_actual)
            bloque_actual = [foto]
            dia_actual = dia_foto
    if bloque_actual:
        bloques.append(bloque_actual)
    return bloques


def calcular_paginas_por_capitulo(capitulos_con_fotos, paginas_disponibles):
    total_fotos = sum(len(cap) for cap in capitulos_con_fotos)
    if total_fotos == 0:
        return [0] * len(capitulos_con_fotos)
    paginas_por_capitulo = []
    for cap in capitulos_con_fotos:
        proporcion = len(cap) / total_fotos
        paginas_asignadas = max(1, round(proporcion * paginas_disponibles))
        paginas_por_capitulo.append(paginas_asignadas)
    log(f"Reparto de paginas por capitulo: {paginas_por_capitulo} (objetivo total: {paginas_disponibles})", "i")
    return paginas_por_capitulo


def paginas_para_capitulo_caso_b(fotos_capitulo, paginas_asignadas, qr_map, texto_titulo="", variante_inicio=0):
    bloques = agrupar_por_dia(fotos_capitulo)
    total_fotos = len(fotos_capitulo)
    if total_fotos == 0 or paginas_asignadas <= 0:
        return []
    densidad_objetivo = total_fotos / paginas_asignadas
    paginas = []
    variante = variante_inicio
    layout_ant = ""
    for bloque in bloques:
        paginas_bloque = max(1, round(len(bloque) / densidad_objetivo))
        idx = 0
        while idx < len(bloque):
            log(f"  [B] bloque_size={len(bloque)} idx={idx}", "?")
            restantes_bloque = len(bloque) - idx 
            paginas_hechas_bloque = 0
            for p in paginas:
                if p.get("_bloque_id") == id(bloque):
                    paginas_hechas_bloque += 1
            paginas_restantes_bloque = max(1, paginas_bloque - paginas_hechas_bloque)
            n_coger = max(1, math.ceil(restantes_bloque / paginas_restantes_bloque))
            n_coger = min(n_coger, 4)
            grupo = bloque[idx:idx + n_coger]
            layout, fotos_layout = elegir_layout(grupo, layout_ant, variante)
            if layout is None:
                break
            qr_idx = -1
            qr_url = QR_URL_PRUEBA
            for fi, foto in enumerate(fotos_layout):
                nombre = foto["nombre"] if isinstance(foto, dict) else os.path.basename(foto)
                if nombre in qr_map:
                    qr_idx = fi
                    qr_url = qr_map[nombre]
                    break
            paginas.append({
                "layout": layout,
                "fotos": fotos_layout,
                "qr_idx": qr_idx,
                "qr_url": qr_url,
                "texto": texto_titulo if len(paginas) == 0 else "",
                "_bloque_id": id(bloque),
            })
            avance = len(fotos_layout) if fotos_layout else 1
            idx += avance
            layout_ant = layout
            variante += 1
    log(f"Capitulo repartido en {len(paginas)} paginas (objetivo: {paginas_asignadas}, bloques: {len(bloques)})", "i")
    return paginas


def paginas_con_tope_estricto(fotos, paginas_max, qr_map, variante_inicio=0):
    """Como paginas_para_capitulo_caso_b pero con GARANTIA dura de que nunca
    se generan mas de 'paginas_max' paginas. Se usa para el modo sin capitulos,
    donde no hay paginas de titulo que absorban el sobrante y una sola pagina
    de mas se nota directamente en el PDF final.
    """
    if paginas_max <= 0 or not fotos:
        return []

    MAX_FOTOS_POR_PAGINA = 4
    cupo_max = paginas_max * MAX_FOTOS_POR_PAGINA

    fotos_usar = fotos
    if len(fotos) > cupo_max:
        # No caben todas ni a 4 por pagina: muestreo uniforme conservando
        # el orden cronologico (y la primera/ultima foto) para no pasarnos.
        paso = len(fotos) / cupo_max
        indices = sorted(set(min(len(fotos) - 1, int(i * paso)) for i in range(cupo_max)))
        fotos_usar = [fotos[i] for i in indices]
        if fotos[-1] not in fotos_usar:
            fotos_usar[-1] = fotos[-1]
        log(f"Sin capitulos: {len(fotos)} fotos no caben en {paginas_max} paginas (max {cupo_max}) "
            f"- se seleccionan {len(fotos_usar)} representativas", "!")

    # Intento "bonito" respetando bloques por dia
    paginas = paginas_para_capitulo_caso_b(fotos_usar, paginas_max, qr_map, variante_inicio=variante_inicio)

    # Red de seguridad: si aun asi se paso (puede ocurrir con muchos dias
    # distintos y pocas fotos por dia), se reconstruye con reparto fijo
    # que SI garantiza el limite exacto.
    if len(paginas) > paginas_max:
        log(f"Sin capitulos: reparto por dia se paso ({len(paginas)}/{paginas_max}) "
            f"- recalculando con reparto fijo", "!")
        n = len(fotos_usar)
        base = n // paginas_max
        extra = n % paginas_max
        paginas = []
        idx = 0
        variante = variante_inicio
        layout_ant = ""
        for i in range(paginas_max):
            if idx >= n:
                break
            tam = base + (1 if i < extra else 0)
            tam = max(1, min(tam, MAX_FOTOS_POR_PAGINA))
            grupo = fotos_usar[idx:idx + tam]
            if not grupo:
                break
            layout, fotos_layout = elegir_layout(grupo, layout_ant, variante)
            if layout is None:
                idx += tam
                continue
            qr_idx = -1
            qr_url = QR_URL_PRUEBA
            for fi, foto in enumerate(fotos_layout):
                nombre = foto["nombre"] if isinstance(foto, dict) else os.path.basename(foto)
                if nombre in qr_map:
                    qr_idx = fi
                    qr_url = qr_map[nombre]
                    break
            paginas.append({
                "layout": layout, "fotos": fotos_layout,
                "qr_idx": qr_idx, "qr_url": qr_url, "texto": "",
            })
            idx += len(fotos_layout) if fotos_layout else tam
            layout_ant = layout
            variante += 1

    assert len(paginas) <= paginas_max, f"Tope de paginas violado: {len(paginas)} > {paginas_max}"
    log(f"Sin capitulos: {len(paginas)} paginas generadas (tope: {paginas_max})", "i")
    return paginas


def analizar_con_ia(fotos, dias, titulo_cliente="", sin_capitulos=False):
    log("Conectando con Claude API...", "i")
    cli = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    listado = f"INVENTARIO COMPLETO - {len(fotos)} fotos:\n"
    for f in fotos:
        es_frame = " [FOTOGRAMA VIDEO-QR]" if f.get("es_frame_video") else ""
        listado += f"  {f['fecha'].strftime('%d/%m/%Y')} - {f['nombre']}{es_frame}\n"

    MAX = 50
    muestra = fotos if len(fotos) <= MAX else [fotos[int(i * len(fotos) / MAX)] for i in range(MAX)]
    if fotos[-1] not in muestra:
        muestra[-1] = fotos[-1]

    contenido = [{"type": "text", "text": listado}]
    for f in muestra:
        try:
            img = Image.open(f["ruta"]).convert("RGB")
            img.thumbnail((100, 100), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=65)
            b64 = base64.standard_b64encode(buf.getvalue()).decode()
            contenido.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
            contenido.append({"type": "text", "text": f"- {f['nombre']} {f['fecha'].strftime('%d/%m/%Y')}"})
        except Exception:
            pass

    fecha_ini = min(f['fecha'] for f in fotos).strftime('%d/%m/%Y')
    fecha_fin = max(f['fecha'] for f in fotos).strftime('%d/%m/%Y')

    titulo_info = ""
    if titulo_cliente:
        titulo_info = f'\nEl cliente ya escribio este titulo para su album: "{titulo_cliente}"'

    if sin_capitulos:
        prompt = f"""Eres experto en libros de fotos para Bookeo. Tienes {len(fotos)} fotos del {fecha_ini} al {fecha_fin}.{titulo_info}

TU UNICA TAREA: proponer un titulo/subtitulo para el libro y 2 opciones de portada. El cliente ha pedido que el libro NO tenga capitulos, se organizara todo cronologicamente por fecha/hora sin agrupar.

RESPONDE SOLO JSON compacto:
{{"tipo":"otro","tipo_desc":"cronologico sin capitulos","titulo":"Catalina","subtitulo":"Febrero 2023 - Enero 2024","portada_opciones":[{{"foto":"foto1.jpg","titulo":"Catalina","subtitulo":"Febrero 2023 - Enero 2024"}},{{"foto":"foto5.jpg","titulo":"Catalina","subtitulo":"Nuestros mejores momentos"}}],"capitulos":[]}}

REGLAS:
- "capitulos" debe ser SIEMPRE un array vacio: []
- portada_opciones: EXACTAMENTE 2 propuestas distintas, cada una con una foto candidata diferente
- OBLIGATORIO si el cliente escribio un titulo: AMBAS propuestas deben usar como campo titulo EXACTAMENTE ese texto, sin cambiarlo. Solo puedes variar el subtitulo. Si no escribio titulo, invéntalo libremente
- El campo titulo general del JSON tambien debe ser EXACTAMENTE el titulo del cliente si lo escribio
- Los nombres de foto de portada_opciones deben estar EXACTAMENTE como en el inventario"""
    else:
        prompt = f"""Eres experto en libros de fotos para Bookeo. Tienes {len(fotos)} fotos del {fecha_ini} al {fecha_fin}.{titulo_info}

TU UNICA TAREA: agrupar las fotos en capitulos y proponer 2 opciones de portada. NO decides layouts, eso lo hace Python.

TIPOS DE LIBRO:
- bebe: agrupa por mes de vida. NUNCA mezcles fotos de meses distintos en un capitulo.
- viaje: agrupa por dia o destino
- boda: preparativos, ceremonia, convite, fiesta
- comunion: preparativos, iglesia, celebracion
- familiar: por estaciones
- dia_madre / dia_padre: cronologico con protagonista principal
- aniversario_persona: antiguo a reciente
- anual: invierno, semana santa, verano, vuelta cole, navidad
- otro: cronologico

RESPONDE SOLO JSON compacto:
{{"tipo":"bebe","tipo_desc":"primer ano de vida","titulo":"Catalina","subtitulo":"Febrero 2023 - Enero 2024","portada_opciones":[{{"foto":"foto1.jpg","titulo":"Catalina","subtitulo":"Febrero 2023 - Enero 2024"}},{{"foto":"foto5.jpg","titulo":"Catalina","subtitulo":"Su primer ano de vida"}}],"capitulos":[{{"titulo":"Mes 1","subtitulo":"Los primeros instantes","fotos":["foto1.jpg","foto2.jpg"]}}]}}

REGLAS:
- portada_opciones: EXACTAMENTE 2 propuestas distintas, cada una con una foto candidata diferente
- OBLIGATORIO si el cliente escribio un titulo: AMBAS propuestas deben usar como campo titulo EXACTAMENTE ese texto, sin cambiarlo. Solo puedes variar el subtitulo. Si no escribio titulo, invéntalo libremente
- El campo titulo general del JSON tambien debe ser EXACTAMENTE el titulo del cliente si lo escribio
- Todos los nombres de foto deben estar EXACTAMENTE como en el inventario
- Incluye TODAS las fotos del inventario en algun capitulo
- Los capitulos deben tener minimo 2 fotos, sin limite maximo
- Maximo 2 fotos por capitulo si solo tienes 2"""

    contenido.append({"type": "text", "text": prompt})

    log(f"Enviando {len(muestra)} miniaturas + inventario de {len(fotos)} fotos...", "i")

    for intento in range(3):
        try:
            resp = cli.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=8000,
                messages=[{"role": "user", "content": contenido}]
            )
            txt = resp.content[0].text.strip()
            if "```json" in txt:
                txt = txt.split("```json")[1].split("```")[0].strip()
            elif "```" in txt:
                txt = txt.split("```")[1].split("```")[0].strip()
            try:
                d = json.loads(txt)
            except Exception:
                for _ in range(txt.count('[') - txt.count(']')):
                    txt += ']'
                for _ in range(txt.count('{') - txt.count('}')):
                    txt += '}'
                d = json.loads(txt)

            log(f"Tipo: {d['tipo'].upper()} - {d['tipo_desc']}", "i")
            log(f"Titulo: {d['titulo']}", "i")
            n_caps = len(d.get('capitulos', []))
            n_fotos_ia = sum(len(cap.get('fotos', [])) for cap in d.get('capitulos', []))
            log(f"Capitulos: {n_caps} - Fotos asignadas: {n_fotos_ia}/{len(fotos)}", "i")
            return d
        except Exception as e:
            log(f"Intento {intento + 1} fallido: {e}", "!")
            if intento == 2:
                raise


def generar_propuestas_portada(fotos_rutas, videos_rutas, titulo_cliente="", formato="2128", orientacion="v", packs_extra=0, sin_capitulos=False):
    exts_foto = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff"}
    fotos = []
    nombres_vistos = set()
    for ruta in fotos_rutas:
        ext = Path(ruta).suffix.lower()
        if ext in exts_foto:
            nombre = os.path.basename(ruta)
            if nombre in nombres_vistos:
                log(f"Foto duplicada omitida: {nombre}", "!")
                continue
            nombres_vistos.add(nombre)
            fecha, fuente = leer_fecha(ruta)
            fotos.append({"ruta": ruta, "fecha": fecha, "nombre": nombre, "fuente_fecha": fuente})

    fotos.sort(key=lambda x: x["fecha"])

    N = len(fotos)
    if N < 2:
        raise ValueError("Se necesitan al menos 2 fotos para crear el libro")

    MIN_PAGINAS = 30
    P = MIN_PAGINAS + (packs_extra * 2)
    caso = "A" if N <= P else "B"

    log(f"Fotos unicas: {N} - Paginas objetivo: {P} (30 + {packs_extra} packs) - Caso: {caso}", "i")

    dias = {}
    for f in fotos:
        dia = f["fecha"].strftime("%Y-%m-%d")
        dias.setdefault(dia, []).append(f)

    diseño = analizar_con_ia(fotos, dias, titulo_cliente, sin_capitulos=sin_capitulos)

    return {
        "diseño": diseño,
        "fotos": fotos,
        "portada_opciones": diseño.get("portada_opciones", []),
        "formato": formato,
        "orientacion": orientacion,
        "paginas_objetivo": P,
        "caso_reparto": caso,
    }


def generar_pdf_completo(diseño, fotos, videos_rutas, qr_urls, portada_elegida,
                          nombre_cliente, carpeta_sal, carpeta_temp,
                          formato="2128", orientacion="v",
                          caso_reparto="A", paginas_objetivo=30,
                          fotos_r2=None, videos_r2=None):
    os.makedirs(carpeta_sal, exist_ok=True)
    os.makedirs(carpeta_temp, exist_ok=True)

    fotos_r2 = fotos_r2 or {}
    videos_r2 = videos_r2 or {}

    # Carpeta local del worker donde se descargan las fotos/vídeos de R2
    carpeta_fuente = os.path.join(carpeta_temp, "_fuente")
    os.makedirs(carpeta_fuente, exist_ok=True)

    # Reconvertir fechas de texto (ISO) a datetime, si vienen serializadas
    # desde Celery (JSON no soporta datetime nativo)
    fotos_reconvertidas = []
    for f in fotos:
        f_copia = dict(f)
        if isinstance(f_copia.get("fecha"), str):
            f_copia["fecha"] = datetime.datetime.fromisoformat(f_copia["fecha"])
        fotos_reconvertidas.append(f_copia)
    fotos = fotos_reconvertidas

    # Descargar cada foto de R2 al disco local del worker
    for f in fotos:
        nombre = f.get("nombre")
        if nombre and nombre in fotos_r2:
            destino = os.path.join(carpeta_fuente, nombre)
            if not os.path.exists(destino):
                try:
                    descargar_de_r2(fotos_r2[nombre], destino)
                except Exception as e:
                    log(f"No se pudo descargar {nombre} de R2: {e}", "!")
                    continue
            f["ruta"] = destino

    # Descargar cada vídeo de R2 al disco local del worker
    videos_rutas_local = []
    for ruta_original in videos_rutas:
        nombre_v = os.path.basename(ruta_original)
        if nombre_v in videos_r2:
            destino = os.path.join(carpeta_fuente, nombre_v)
            if not os.path.exists(destino):
                try:
                    descargar_de_r2(videos_r2[nombre_v], destino)
                except Exception as e:
                    log(f"No se pudo descargar vídeo {nombre_v} de R2: {e}", "!")
                    continue
            videos_rutas_local.append(destino)
        else:
            videos_rutas_local.append(ruta_original)
    videos_rutas = videos_rutas_local

    # Descargar la portada personalizada (si el cliente subió una) de R2
    if portada_elegida and portada_elegida.get("foto_personalizada_r2"):
        nombre_custom = os.path.basename(portada_elegida.get("foto_personalizada_ruta", "portada_custom.jpg"))
        destino_custom = os.path.join(carpeta_fuente, nombre_custom)
        try:
            descargar_de_r2(portada_elegida["foto_personalizada_r2"], destino_custom)
            portada_elegida["foto_personalizada_ruta"] = destino_custom
        except Exception as e:
            log(f"No se pudo descargar portada personalizada de R2: {e}", "!")
            portada_elegida["foto_personalizada_ruta"] = None

    AW, AH = obtener_medidas(formato, orientacion)

    exts_vid = {".mp4", ".mov", ".avi", ".m4v", ".mkv", ".wmv"}
    videos = []
    for ruta in videos_rutas:
        ext = Path(ruta).suffix.lower()
        if ext in exts_vid:
            fecha, fuente = leer_fecha(ruta)
            videos.append({"ruta": ruta, "fecha": fecha, "nombre": os.path.basename(ruta), "fuente_fecha": fuente})

    qr_map = {}
    if videos:
        for v in videos:
            nombre_v = v["nombre"]
            if nombre_v in qr_urls:
                mejor_foto = None
                mejor_diff = float("inf")
                for f in fotos:
                    diff = abs((f["fecha"] - v["fecha"]).total_seconds()) / 60
                    if diff < mejor_diff:
                        mejor_diff = diff
                        mejor_foto = f
                if mejor_foto and mejor_diff <= 120:
                    qr_map[mejor_foto["nombre"]] = qr_urls[nombre_v]
                else:
                    frame = extraer_fotograma(v["ruta"], carpeta_temp)
                    if frame:
                        nb_frame = os.path.basename(frame)
                        fotos.append({"ruta": frame, "fecha": v["fecha"], "nombre": nb_frame,
                                      "fuente_fecha": "video", "es_frame_video": True})
                        qr_map[nb_frame] = qr_urls[nombre_v]
                        fotos.sort(key=lambda x: x["fecha"])

    fotos_dict = {f["nombre"]: f for f in fotos}

    titulo_final = portada_elegida.get("titulo") or diseño.get("titulo", "Mi Album")
    subtitulo = portada_elegida.get("subtitulo", "")
    nb = nombre_cliente.lower().replace(" ", "_")
    r_final = os.path.join(carpeta_sal, f"bookeo_{nb}.pdf")

    generar_pdf(AW, AH, diseño, fotos_dict, qr_map, r_final, titulo_final, subtitulo,
                portada_elegida=portada_elegida, do_wm=False,
                caso_reparto=caso_reparto, paginas_objetivo=paginas_objetivo)

    import shutil
    if os.path.exists(carpeta_temp):
        shutil.rmtree(carpeta_temp, ignore_errors=True)

    log(f"PDF listo: {r_final}", "i")
    return r_final


def generar_pdf(AW, AH, diseño, fotos_dict, qr_map, ruta, titulo, subtitulo, portada_elegida=None, do_wm=False, caso_reparto="A", paginas_objetivo=30):
    aw, ah = AW * mm, AH * mm
    log(f"Generando PDF ({AW}x{AH}mm)...", "i")

    # En vez de un único PDF con TODO el libro abierto en memoria hasta el
    # final (que es lo que hacía que la memoria subiera sin parar según se
    # añadían fotos), se va guardando por trozos a disco y se juntan al
    # final. Así nunca hay más de un capítulo cargado en memoria a la vez.
    carpeta_segmentos = ruta + "_segmentos"
    os.makedirs(carpeta_segmentos, exist_ok=True)
    segmentos = []
    contador_segmento = [0]

    def abrir_segmento():
        contador_segmento[0] += 1
        ruta_seg = os.path.join(carpeta_segmentos, f"seg_{contador_segmento[0]:03d}.pdf")
        segmentos.append(ruta_seg)
        cnv = canvas.Canvas(ruta_seg, pagesize=(aw, ah))
        cnv.setTitle(titulo)
        cnv.setAuthor("Bookeo - mibookeo.es")
        return cnv

    def cerrar_segmento(cnv, etiqueta=""):
        cnv.save()
        log_memoria(f"segmento cerrado ({etiqueta})")
        gc.collect()

    fotos_usadas = set()
    capitulo_variante = 0

    portada_nombre = ""
    portada_ruta = ""
    if portada_elegida and portada_elegida.get("foto_personalizada_ruta"):
        portada_ruta = portada_elegida["foto_personalizada_ruta"]
        portada_nombre = os.path.basename(portada_ruta)
    elif portada_elegida and portada_elegida.get("foto"):
        portada_nombre = portada_elegida["foto"]
        portada_ruta = fotos_dict.get(portada_nombre, {}).get("ruta", "")
    elif not portada_elegida:
        portada_nombre = diseño.get("portada", "")
        portada_ruta = fotos_dict.get(portada_nombre, {}).get("ruta", "") if portada_nombre else ""
        if not portada_ruta and fotos_dict:
            portada_ruta = list(fotos_dict.values())[0]["ruta"]

    editor_portada = portada_elegida.get("editor") if portada_elegida else None

    c = abrir_segmento()
    log(f"Portada ruta: {portada_ruta}", "?")
    dibujar_portada(c, AW, AH, portada_ruta, titulo, subtitulo, do_wm, editor=editor_portada)
    log("Portada dibujada OK", "?")
    c.showPage()
    if portada_nombre:
        fotos_usadas.add(portada_nombre)

    dibujar_pagina_blanca(c, AW, AH)
    c.showPage()
    cerrar_segmento(c, "portada")

    paginas_por_capitulo = []
    if caso_reparto == "B":
        listas_fotos_capitulos = []
        for cap in diseño.get("capitulos", []):
            nombres_cap = cap.get("fotos", [])
            fotos_cap_tmp = [fotos_dict[n] for n in nombres_cap if n in fotos_dict]
            listas_fotos_capitulos.append(fotos_cap_tmp)
        paginas_por_capitulo = calcular_paginas_por_capitulo(listas_fotos_capitulos, paginas_objetivo)

    for idx_cap, cap in enumerate(diseño.get("capitulos", [])):
        log_memoria(f"inicio capitulo {idx_cap+1}: {cap.get('titulo','')}")
        tit_cap = cap.get("titulo", "")
        sub_cap = cap.get("subtitulo", "")
        nombres_cap = cap.get("fotos", [])

        fotos_cap = []
        for nombre in nombres_cap:
            if nombre in fotos_dict and nombre not in fotos_usadas:
                fotos_cap.append(fotos_dict[nombre])
                fotos_usadas.add(nombre)

        if not fotos_cap:
            continue

        c = abrir_segmento()

        fotos_titulo = fotos_cap[:2]
        rutas_titulo = [f["ruta"] for f in fotos_titulo if f.get("ruta")]
        if not rutas_titulo and fotos_cap:
            rutas_titulo = [fotos_cap[0]["ruta"]]
        layout_titulo_capitulo(c, AW, AH, tit_cap, sub_cap, rutas_titulo, capitulo_variante, do_wm)
        c.showPage()
        capitulo_variante += 1

        fotos_resto = fotos_cap[len(fotos_titulo):]
        if fotos_resto:
            if caso_reparto == "B":
                paginas_asignadas_cap = max(1, paginas_por_capitulo[idx_cap] - 1)
                paginas = paginas_para_capitulo_caso_b(
                    fotos_resto, paginas_asignadas_cap, qr_map, variante_inicio=capitulo_variante
                )
            else:
                paginas = paginas_para_grupo(fotos_resto, qr_map, variante_inicio=capitulo_variante)

            for pg in paginas:
                rutas = [f["ruta"] if isinstance(f, dict) else f for f in pg["fotos"]]
                kwargs = dict(fotos_rutas=rutas, qr_idx=pg["qr_idx"], qr_url=pg["qr_url"],
                              pie=pg["texto"], do_wm=do_wm)
                if pg["layout"] == "1":
                    layout_1(c, AW, AH, **kwargs)
                elif pg["layout"] == "2H":
                    layout_2H(c, AW, AH, **kwargs)
                elif pg["layout"] == "2V":
                    layout_2V(c, AW, AH, **kwargs)
                elif pg["layout"] == "3":
                    layout_3(c, AW, AH, **kwargs)
                elif pg["layout"] == "4":
                    layout_4(c, AW, AH, **kwargs)
                c.showPage()
                capitulo_variante += 1

        cerrar_segmento(c, f"capitulo {idx_cap+1}")

    c = abrir_segmento()

    fotos_sobrantes = [f for nombre, f in fotos_dict.items()
                       if nombre not in fotos_usadas and f.get("ruta") and os.path.exists(f.get("ruta", ""))]
    if fotos_sobrantes:
        fotos_sobrantes.sort(key=lambda x: x["fecha"])
        if caso_reparto == "B":
            # Caso B (mas fotos que paginas objetivo): hay que comprimir para
            # respetar el presupuesto de paginas. Sin capitulos no hay pagina
            # de titulo que reste presupuesto, asi que se usa el objetivo completo.
            # paginas_con_tope_estricto GARANTIZA que nunca se supera paginas_objetivo.
            paginas = paginas_con_tope_estricto(
                fotos_sobrantes, paginas_objetivo, qr_map, variante_inicio=capitulo_variante
            )
        else:
            paginas = paginas_para_grupo(fotos_sobrantes, qr_map, variante_inicio=capitulo_variante)
        for pg in paginas:
            rutas = [f["ruta"] if isinstance(f, dict) else f for f in pg["fotos"]
                     if (f.get("ruta") if isinstance(f, dict) else f) and
                     os.path.exists(f.get("ruta", "") if isinstance(f, dict) else f)]
            if not rutas:
                continue
            kwargs = dict(fotos_rutas=rutas, qr_idx=pg["qr_idx"], qr_url=pg["qr_url"],
                          pie=pg["texto"], do_wm=do_wm)
            if pg["layout"] == "1":
                layout_1(c, AW, AH, **kwargs)
            elif pg["layout"] == "2H":
                layout_2H(c, AW, AH, **kwargs)
            elif pg["layout"] == "2V":
                layout_2V(c, AW, AH, **kwargs)
            elif pg["layout"] == "3":
                layout_3(c, AW, AH, **kwargs)
            elif pg["layout"] == "4":
                layout_4(c, AW, AH, **kwargs)
            c.showPage()

    dibujar_lomo(c, AW, AH, titulo, do_wm=do_wm, editor=editor_portada)
    c.showPage()
    dibujar_contraportada(c, AW, AH, do_wm)
    c.showPage()

    cerrar_segmento(c, "final")

    # Unir todos los trozos en el PDF final
    log_memoria("antes de unir segmentos")
    writer = PdfWriter()
    for ruta_seg in segmentos:
        writer.append(ruta_seg)
    with open(ruta, "wb") as f_out:
        writer.write(f_out)
    writer.close()
    log_memoria("despues de unir segmentos")

    shutil.rmtree(carpeta_segmentos, ignore_errors=True)

    log(f"PDF completo guardado: {ruta}", "i")


def extraer_fotograma(ruta_video, carpeta_temp):
    if not OPENCV_OK:
        return None
    try:
        cap = cv2.VideoCapture(ruta_video)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total <= 0:
            cap.release()
            return None
        cas = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        mejor = None
        mejor_s = -1
        mejor_tiene_cara = False
        puntos = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]
        for pt in puntos:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * pt))
            ret, fr = cap.read()
            if not ret:
                continue
            g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            brillo = g.mean()
            nitidez = cv2.Laplacian(g, cv2.CV_64F).var()
            if brillo < 30 or brillo > 220:
                continue
            g_eq = cv2.equalizeHist(g)
            caras = cas.detectMultiScale(g_eq, 1.1, 4, minSize=(20, 20))
            tiene_cara = len(caras) > 0
            if tiene_cara and not mejor_tiene_cara:
                mejor = fr.copy()
                mejor_s = nitidez
                mejor_tiene_cara = True
            elif tiene_cara and mejor_tiene_cara:
                if nitidez > mejor_s:
                    mejor = fr.copy()
                    mejor_s = nitidez
            elif not tiene_cara and not mejor_tiene_cara:
                s = nitidez * (1 - abs(brillo - 120) / 200)
                if s > mejor_s:
                    mejor = fr.copy()
                    mejor_s = s
        cap.release()
        if mejor is None:
            return None
        os.makedirs(carpeta_temp, exist_ok=True)
        nb = os.path.splitext(os.path.basename(ruta_video))[0]
        ruta_jpg = os.path.join(carpeta_temp, f"frame_{nb}.jpg")
        if mejor_tiene_cara and OPENCV_OK:
            try:
                g = cv2.cvtColor(mejor, cv2.COLOR_BGR2GRAY)
                g_eq = cv2.equalizeHist(g)
                caras = cas.detectMultiScale(g_eq, 1.1, 4, minSize=(20, 20))
                if len(caras) > 0:
                    ih, iw = mejor.shape[:2]
                    x1 = min(c[0] for c in caras)
                    y1 = min(c[1] for c in caras)
                    x2 = max(c[0] + c[2] for c in caras)
                    y2 = max(c[1] + c[3] for c in caras)
                    cara_h = y2 - y1
                    cara_w = x2 - x1
                    mg_arr = int(cara_h * 1.3)
                    mg_lat = int(cara_w * 0.6)
                    mg_abj = int(cara_h * 0.4)
                    cx1 = max(0, x1 - mg_lat)
                    cy1 = max(0, y1 - mg_arr)
                    cx2 = min(iw, x2 + mg_lat)
                    cy2 = min(ih, y2 + mg_abj)
                    mejor = mejor[cy1:cy2, cx1:cx2]
            except Exception:
                pass
        img_pil = Image.fromarray(cv2.cvtColor(mejor, cv2.COLOR_BGR2RGB))
        img_pil.save(ruta_jpg, "JPEG", quality=92)
        return ruta_jpg
    except Exception as e:
        log(f"Error extrayendo fotograma: {e}", "!")
        return None


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            datos = json.load(f)
        resultado = generar_propuestas_portada(
            fotos_rutas=datos["fotos_rutas"],
            videos_rutas=datos.get("videos_rutas", []),
            formato=datos.get("formato", "2128"),
            orientacion=datos.get("orientacion", "v"),
        )
        portada_elegida = resultado["portada_opciones"][0] if resultado["portada_opciones"] else {}
        ruta = generar_pdf_completo(
            diseño=resultado["diseño"],
            fotos=resultado["fotos"],
            videos_rutas=datos.get("videos_rutas", []),
            qr_urls=datos.get("qr_urls", {}),
            portada_elegida=portada_elegida,
            nombre_cliente=datos["nombre_cliente"],
            carpeta_sal=datos["carpeta_sal"],
            carpeta_temp=datos.get("carpeta_temp", datos["carpeta_sal"] + "/temp"),
            formato=resultado["formato"],
            orientacion=resultado["orientacion"],
        )
        print(json.dumps({"pdf": ruta, "ok": True}))
    else:
        print("Uso: python3 crear_libro_railway.py datos.json")


if __name__ == "__main__":
    main()