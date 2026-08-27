"""
╔══════════════════════════════════════════════════════╗
║           BOOKEO MVP · Creador de libros             ║
║         mibookeo.es · Versión 3.3                    ║
╚══════════════════════════════════════════════════════╝
"""

import os, io, sys, json, base64, datetime, re, math, uuid
import copy
import resource
import gc
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pypdf import PdfWriter
from pathlib import Path
from r2_storage import descargar_de_r2, subir_a_r2


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

# ═══ Gelato: cubierta (portada+lomo+contraportada como un único spread) ═══
import requests

try:
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPDF
    _SVGLIB_OK = True
except Exception as _e:
    _SVGLIB_OK = False
    print(f"[stickers] svglib no disponible, los stickers no se dibujarán en el PDF: {_e}")

GELATO_API_KEY = os.environ.get("GELATO_API_KEY", "")
GELATO_COVER_DIMENSIONS_URL = "https://product.gelatoapis.com/v3/products/{uid}/cover-dimensions"

# MODO DE PRUEBA: si está a "1", NO se llama a la API de Gelato en ningún
# momento - se calculan medidas aproximadas en local. Así se puede probar
# todo el flujo (creador -> editor -> PDF final -> pago) sin que Gelato
# reciba ni una sola petición mientras aún no hay pasarela de pago montada.
#
# ⚠️ IMPORTANTE: estas medidas son una ESTIMACIÓN, no las reales de Gelato
# (el ancho de lomo real depende del grosor exacto de SU papel, que no es
# público). Vale para probar que la estructura del PDF, el tamaño de
# página especial de la cubierta, etc. funcionan - pero un archivo
# generado en este modo NO se debe mandar a imprenta. Antes de activar
# pagos de verdad, hay que poner esta variable a "0" (o quitarla) en
# Railway para que siempre llame a la API real.
GELATO_MODO_PRUEBA = os.environ.get("GELATO_MODO_PRUEBA", "0") == "1"

# Medidas de recorte (trim, SIN sangrado) por formato - para el modo de
# prueba. FORMATOS_MM (más abajo) ya lleva +8mm de sangrado sumados para
# las páginas interiores; aquí se necesita el tamaño real del libro.
TRIM_MM_GELATO = {
    "2020": (200, 200),
    "2128": (210, 280),
    "2828": (280, 280),
}

# UID de producto Gelato (tapa dura, GEO simplified) por código de formato interno.
PRODUCT_UID_GELATO = {
    "2020": "photobooks-hardcover_pf_200x200-mm-8x8-inch_pt_170-gsm-65lb-coated-silk_cl_4-4_ccl_4-4_bt_glued-left_ct_matt-lamination_prt_1-0_cpt_130-gsm-65-lb-cover-coated-silk_ver",
    "2128": "photobooks-hardcover_pf_210x280-mm-8x11-inch_pt_170-gsm-65lb-coated-silk_cl_4-4_ccl_4-4_bt_glued-left_ct_matt-lamination_prt_1-0_cpt_130-gsm-65-lb-cover-coated-silk_ver",
    "2828": "photobooks-hardcover_pf_280x280-mm-11x11-inch_pt_170-gsm-65lb-coated-silk_cl_4-4_ccl_4-4_bt_glued-left_ct_matt-lamination_prt_1-0_cpt_130-gsm-65-lb-cover-coated-silk_ver",
}

_CACHE_DIMENSIONES_CUBIERTA = {}


def _estimar_dimensiones_cubierta_offline(formato, paginas_interiores):
    """
    Calcula unas medidas de cubierta APROXIMADAS sin llamar a Gelato, solo
    para el modo de prueba. Fórmulas típicas de fotolibro de tapa dura:
    ~15mm de giro alrededor del cartón, ~0.22mm de grosor por hoja de
    170gsm, ~8mm de bisagra a cada lado del lomo. NO son las medidas
    reales de Gelato - solo sirven para probar la estructura del PDF.
    """
    trim_w, trim_h = TRIM_MM_GELATO.get(formato, TRIM_MM_GELATO["2128"])
    giro = 15.0
    bisagra = 8.0
    hojas = max(1, paginas_interiores / 2)
    grosor_lomo = round(hojas * 0.22 + 4, 1)  # +4mm de cartón/guardas

    content_w, content_h = trim_w - 7, trim_h - 5  # margen de seguridad aprox.
    wrap_w = giro * 2 + content_w * 2 + bisagra * 2 + grosor_lomo
    wrap_h = giro * 2 + content_h

    top_content = giro + (wrap_h - giro * 2 - content_h) / 2
    left_back = giro
    left_joint_back = left_back + content_w
    left_spine = left_joint_back + bisagra
    left_joint_front = left_spine + grosor_lomo
    left_front = left_joint_front + bisagra

    return {
        "productUid": PRODUCT_UID_GELATO.get(formato, ""),
        "pagesCount": paginas_interiores,
        "measureUnit": "mm",
        "_modo_prueba": True,
        "wraparoundInsideSize": {"width": round(wrap_w, 1), "height": round(wrap_h, 1), "left": 0, "top": 0, "thickness": giro},
        "contentBackSize": {"width": content_w, "height": content_h, "left": left_back, "top": top_content},
        "jointBackSize": {"width": bisagra, "height": content_h, "left": left_joint_back, "top": top_content},
        "spineSize": {"width": grosor_lomo, "height": content_h, "left": left_spine, "top": top_content},
        "jointFrontSize": {"width": bisagra, "height": content_h, "left": left_joint_front, "top": top_content},
        "contentFrontSize": {"width": content_w, "height": content_h, "left": left_front, "top": top_content},
    }


def obtener_dimensiones_cubierta_gelato(formato, paginas_interiores):
    """
    Pide a la API de Gelato las medidas exactas de la cubierta (spread de
    contraportada+lomo+portada) para este formato y este número de páginas
    interiores. El ancho del lomo cambia con el número de páginas, así que
    esto NO se puede hardcodear - hay que preguntarle a Gelato en cada
    generación.

    Si GELATO_MODO_PRUEBA está activo, NO llama a Gelato - devuelve una
    estimación local (ver _estimar_dimensiones_cubierta_offline).

    Devuelve un dict con (todo en mm, origen arriba-izquierda):
      wraparoundInsideSize, wraparoundEdgeSize, contentBackSize,
      jointBackSize, spineSize, jointFrontSize, contentFrontSize

    Lanza RuntimeError si no se puede obtener (mejor fallar alto que generar
    una cubierta con medidas incorrectas que Gelato luego rechace o, peor,
    imprima mal).
    """
    if GELATO_MODO_PRUEBA:
        log("GELATO_MODO_PRUEBA activo: usando medidas de cubierta ESTIMADAS, no reales de Gelato", "!")
        return _estimar_dimensiones_cubierta_offline(formato, paginas_interiores)

    uid = PRODUCT_UID_GELATO.get(formato)
    if not uid:
        raise RuntimeError(f"No hay productUid de Gelato configurado para el formato '{formato}'")

    cache_key = (uid, paginas_interiores)
    if cache_key in _CACHE_DIMENSIONES_CUBIERTA:
        return _CACHE_DIMENSIONES_CUBIERTA[cache_key]

    if not GELATO_API_KEY:
        raise RuntimeError("GELATO_API_KEY no está configurada")

    url = GELATO_COVER_DIMENSIONS_URL.format(uid=uid)
    ultimo_error = None
    for intento in range(3):
        try:
            resp = requests.get(
                url,
                params={"pageCount": paginas_interiores},
                headers={"X-API-KEY": GELATO_API_KEY},
                timeout=15,
            )
            resp.raise_for_status()
            datos = resp.json()
            _CACHE_DIMENSIONES_CUBIERTA[cache_key] = datos
            return datos
        except Exception as e:
            ultimo_error = e
            log(f"Intento {intento + 1}/3 fallido pidiendo dimensiones de cubierta a Gelato: {e}", "!")
    raise RuntimeError(f"No se pudieron obtener las dimensiones de cubierta de Gelato tras 3 intentos: {ultimo_error}")


def _zona_a_puntos(zona, canvas_h_mm):
    """
    Convierte una zona de la respuesta de Gelato (left/top/width/height en
    mm, origen arriba-izquierda, Y hacia abajo) a (x, y, w, h) en puntos con
    el origen abajo-izquierda que usa ReportLab. 'canvas_h_mm' es la altura
    total del lienzo (wraparoundInsideSize.height) en mm.
    """
    x = zona["left"] * mm
    w = zona["width"] * mm
    h = zona["height"] * mm
    y = (canvas_h_mm - zona["top"] - zona["height"]) * mm
    return x, y, w, h

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
GAP = 6
MARGEN_FOTO_COMPLETA = 3  # mm - margen para el layout "1" (foto a página completa),
                          # más fino que el MG normal para que siga sintiéndose
                          # casi a sangre, pero sin tocar el borde de la página
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


def calcular_ventana_recorte_frac(ruta, w_mm, h_mm):
    """
    Calcula qué parte de la foto original hay que enseñar para rellenar un
    hueco de w_mm x h_mm (con detección de caras) - devuelve la ventana
    como FRACCIONES (0-1) del tamaño original, sin crear ninguna imagen
    nueva. Es la MISMA fuente de verdad que usa el PDF final
    (recortar_con_caras la llama para saber qué recortar) y que se le manda
    al editor (para que enseñe exactamente el mismo trozo de foto) - así
    los dos coinciden siempre, no son dos cálculos por separado.
    """
    try:
        img = Image.open(ruta)
        try:
            img.draft("RGB", (800, 800))
        except Exception:
            pass
        img = ImageOps.exif_transpose(img)
        iw, ih = img.size
        img.close()

        ratio_z = w_mm / h_mm
        ratio_i = iw / ih

        caras = detectar_caras(ruta)
        if caras and caras.get("iw"):
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
            x1_s = y1_s = x2_s = y2_s = cara_h = 0

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

        x0f, y0f, x1f, y1f = recorte
        return {"x0": x0f / iw, "y0": y0f / ih, "x1": x1f / iw, "y1": y1f / ih}
    except Exception as e:
        log(f"No se pudo calcular ventana de recorte para {os.path.basename(ruta)}: {e}", "!")
        return {"x0": 0, "y0": 0, "x1": 1, "y1": 1}


def recortar_con_caras(ruta, w_mm, h_mm, recorte_frac=None):
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

        # Si el cliente movió la foto a mano en el editor, se respeta esa
        # ventana tal cual (recorte_frac ya viene calculado). Si no, se
        # calcula aquí con la misma función que ve el editor - así los dos
        # coinciden siempre, sea cual sea el caso.
        ventana = recorte_frac or calcular_ventana_recorte_frac(ruta, w_mm, h_mm)
        recorte = (
            int(ventana["x0"] * iw), int(ventana["y0"] * ih),
            int(ventana["x1"] * iw), int(ventana["y1"] * ih),
        )
        img = img.crop(recorte)
        return img
    except Exception as e:
        log(f"Error recortando {os.path.basename(ruta)}: {e}", "!")
        try:
            img = Image.open(ruta).convert("RGB")
            return ImageOps.exif_transpose(img)
        except Exception:
            return None


def obtener_dimensiones_px(ruta):
    """Ancho/alto originales en píxeles de una foto - lectura barata (PIL
    no decodifica la imagen entera solo para leer el tamaño). Se usa para
    que el editor pueda calcular el PPI en vivo sin tener que servir la
    foto a resolución de impresión."""
    try:
        with Image.open(ruta) as img:
            return img.size
    except Exception:
        return (0, 0)


def ppi(ruta, w_mm, h_mm):
    try:
        img = Image.open(ruta)
        pw, ph = img.size
        return min(pw / (w_mm / 25.4), ph / (h_mm / 25.4))
    except Exception:
        return 300


def foto_zona(c, ruta, x, y, w, h, check_ppi=True, recorte_frac=None):
    if not ruta or not os.path.exists(ruta):
        log(f"Foto no encontrada: {ruta}", "!")
        return
    log_memoria(f"antes de {os.path.basename(ruta)}")
    img = recortar_con_caras(ruta, w, h, recorte_frac=recorte_frac)
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


def dibujar_qr_sobre_foto(c, x_foto, y_foto, w_foto, h_foto, url, ruta_foto=None, centro_custom_mm=None, tamano_custom_mm=None):
    """
    Dibuja el QR sobre una foto.

    Fondo SIEMPRE blanco puro y puntos SIEMPRE negro puro, con un marco fino
    - mejor lectura del escáner que adaptar el color a la foto de fondo.

    Si se pasa 'centro_custom_mm' (x_mm, y_mm) - la posición a la que el
    cliente arrastró el QR en el editor -, se dibuja ahí en vez de en la
    esquina por defecto. Si no, esquina superior derecha de la foto (de
    toda la vida). 'tamano_custom_mm' hace lo mismo con el tamaño (el
    cliente lo puede agrandar/achicar arrastrando una esquina) - si no se
    pasa, se usa el tamaño de siempre (QR_MM).
    """
    s = (tamano_custom_mm or QR_MM) * mm
    if centro_custom_mm:
        cx_mm, cy_mm = centro_custom_mm
        qx = cx_mm * mm - s / 2
        qy = cy_mm * mm - s / 2
    else:
        mg_qr = 2 * mm
        qx = (x_foto + w_foto) * mm - s - mg_qr
        qy = y_foto * mm + mg_qr

    color_pts = (0, 0, 0)      # negro puro
    color_fondo = (1, 1, 1)    # blanco puro

    pad = 1.5 * mm
    c.setFillColorRGB(*color_fondo)
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(0.3 * mm)
    c.roundRect(qx - pad, qy - pad, s + pad * 2, s + pad * 2, 1.5 * mm, fill=1, stroke=1)
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


def bg_blanco(c, AW, AH, color_hex=None):
    """Fondo de la página. Blanco por defecto; si el cliente eligió un
    color en el editor (pagina['fondo']), se usa ese en su lugar."""
    rgb = hex_a_rgb01(color_hex) if color_hex else (1, 1, 1)
    c.setFillColorRGB(*rgb)
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


def dibujar_marco_pdf(c, AW, AH, marco, canvas_w, canvas_h):
    if not marco:
        return
    estilo = marco.get("estilo", "ninguno")
    if estilo == "ninguno":
        return

    aw_pt, ah_pt = AW * mm, AH * mm
    # Escala INDEPENDIENTE por eje (ancho del canvas -> ancho de página,
    # alto del canvas -> alto de página) - antes se usaba un único factor
    # basado solo en el ancho para los dos ejes, que descuadraba la
    # posición vertical (marcos/formas movidos) en cualquier formato donde
    # el canvas de creador.html no tuviera exactamente la misma proporción
    # que la página real. Mismo mecanismo que ya usan las fotos
    # (dibujar_foto_editor_libre) y las formas de páginas interiores
    # (dibujar_formas_pdf), que por eso sí salían bien colocadas.
    escala_x = aw_pt / canvas_w if canvas_w else 1.0
    escala_y = ah_pt / canvas_h if canvas_h else 1.0
    grosor_pt = marco.get("grosor", 6) * escala_x
    color = hex_a_rgb01(marco.get("color", "#1a1a2e"))

    # Posición y tamaño reales del marco que el cliente dejó en el editor
    # (coordenadas de canvas, origen arriba-izquierda, igual que en textos/foto)
    left_px = marco.get("left", canvas_w * 0.06)
    top_px = marco.get("top", canvas_w * 0.06)
    width_px = marco.get("width", canvas_w * 0.88)
    height_px = marco.get("height", canvas_w * 0.88)
    angulo = marco.get("angle", 0) or 0

    x0 = left_px * escala_x
    w = width_px * escala_x
    h = height_px * escala_y
    # El canvas crece hacia abajo, el PDF (ReportLab) crece hacia arriba: invertir Y
    y0 = ah_pt - (top_px * escala_y) - h

    c.saveState()
    if angulo:
        # Los marcos del modo blanco se pueden rotar (control en una
        # esquina) - hay que rotar alrededor de su propio centro, no del
        # origen de la página.
        cx, cy = x0 + w / 2, y0 + h / 2
        c.translate(cx, cy)
        c.rotate(-angulo)
        c.translate(-cx, -cy)
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

    elif estilo == "cuadrado":
        # Formas rellenas de color (no solo el contorno), a diferencia de
        # los marcos de página completa que sí son solo línea.
        c.setFillColorRGB(*color)
        c.rect(x0, y0, w, h, fill=1, stroke=0)

    elif estilo == "circulo":
        c.setFillColorRGB(*color)
        c.ellipse(x0, y0, x0 + w, y0 + h, fill=1, stroke=0)

    elif estilo == "triangulo":
        c.setFillColorRGB(*color)
        path = c.beginPath()
        path.moveTo(x0 + w / 2, y0 + h)
        path.lineTo(x0, y0)
        path.lineTo(x0 + w, y0)
        path.close()
        c.drawPath(path, fill=1, stroke=0)

    c.restoreState()


def _partir_en_lineas(texto, fuente, fontsize_pt, width_pt, c):
    """Parte el texto en varias líneas por palabras, igual que hace
    fabric.Textbox en el navegador cuando el texto no cabe en el ancho del
    cuadro - sin esto, un título largo se vería bien partido en el editor
    pero en el PDF saldría todo en una sola línea, distinto de lo que vio
    el cliente."""
    palabras = texto.split(" ")
    lineas = []
    actual = ""
    for palabra in palabras:
        candidata = (actual + " " + palabra).strip()
        if not actual or c.stringWidth(candidata, fuente, fontsize_pt) <= width_pt:
            actual = candidata
        else:
            lineas.append(actual)
            actual = palabra
    if actual:
        lineas.append(actual)
    return lineas or [texto]


def _dibujar_texto_editor(c, AW, AH, canvas_w, canvas_h, info, texto_fallback):
    if not info:
        return
    texto = info.get("texto") or texto_fallback or ""
    if not texto:
        return

    # Si este texto en concreto guardó su propio tamaño de referencia de
    # canvas (a partir de ahora, así se guardan), se usa ese en vez del de
    # la página - evita que un cambio posterior en OTRA cosa de la misma
    # página (en otra sesión/pantalla) descuadre este texto sin tocarlo.
    canvas_w = info.get("canvas_w") or canvas_w
    canvas_h = info.get("canvas_h") or canvas_h

    aw_pt, ah_pt = AW * mm, AH * mm
    left_frac = info.get("left", 0) / canvas_w
    top_frac = info.get("top", 0) / canvas_h
    width_frac = info.get("width", canvas_w * 0.8) / canvas_w
    fontsize_frac = info.get("fontSize", canvas_w * 0.06) / canvas_w

    x_pt = left_frac * aw_pt
    width_pt = width_frac * aw_pt
    fontsize_pt = fontsize_frac * aw_pt
    y_ref_pt = top_frac * ah_pt

    negrita = info.get("fontWeight") == "bold"
    cursiva = info.get("fontStyle") == "italic"
    subrayado = bool(info.get("underline"))
    fuente = mapear_fuente(info.get("fontFamily", ""), negrita, cursiva)
    color = hex_a_rgb01(info.get("fill", "#1a1a2e"))

    c.setFillColorRGB(*color)
    c.setFont(fuente, fontsize_pt)

    # 'left'/'top' vienen del objeto Fabric.js tal cual, y su significado
    # depende del originX/originY con el que se creó esa caja de texto en
    # el navegador: la portada usa el origen por defecto ('left'/'top' =
    # esquina superior-izquierda), pero el pie de página y los títulos de
    # capítulo se crean con originX/originY 'center' (para que sea más
    # facil centrarlos al arrastrar). Sin este ajuste, un texto con origen
    # 'center' se dibujaria descuadrado, como si 'left'/'top' fuera su
    # esquina en vez de su centro.
    origin_x = info.get("originX", "left")
    origin_y = info.get("originY", "top")

    if origin_x == "center":
        cx_pt = x_pt
    elif origin_x == "right":
        cx_pt = x_pt - width_pt / 2
    else:
        cx_pt = x_pt + width_pt / 2

    # Red de seguridad: el editor manda left/width tal cual los dejó el
    # cliente en el navegador, pero nada le impide arrastrar la caja (o
    # dejarla con su ancho por defecto) hasta que sobrepase el borde
    # físico de la página impresa - el navegador no lo avisa porque su
    # canvas no recorta ahí, pero reportlab tampoco recorta solo, así que
    # el texto se imprimía literalmente cortado por la guillotina. Aquí
    # se fuerza a que la caja quede siempre dentro del margen de
    # seguridad (MG) de la página, recortando el ANCHO (nunca moviendo el
    # texto de sitio) para que las palabras que no quepan bajen de línea
    # en vez de salirse de la hoja.
    margen_seg_pt = MG * mm
    x_izq_min_pt = margen_seg_pt
    x_der_max_pt = aw_pt - margen_seg_pt
    x_izq_actual_pt = cx_pt - width_pt / 2
    x_der_actual_pt = cx_pt + width_pt / 2
    if x_izq_actual_pt < x_izq_min_pt:
        despl = x_izq_min_pt - x_izq_actual_pt
        cx_pt += despl
        x_izq_actual_pt += despl
        x_der_actual_pt += despl
    if x_der_actual_pt > x_der_max_pt:
        ancho_disponible_pt = max(x_der_max_pt - x_izq_actual_pt, 15 * mm)
        width_pt = min(width_pt, ancho_disponible_pt)

    lineas = _partir_en_lineas(texto, fuente, fontsize_pt, width_pt, c)
    interlineado_pt = fontsize_pt * 1.16  # aproximacion del interlineado de fabric.Textbox
    bloque_alto_pt = len(lineas) * interlineado_pt

    if origin_y == "center":
        top_pt_desde_arriba = y_ref_pt - bloque_alto_pt / 2
    elif origin_y == "bottom":
        top_pt_desde_arriba = y_ref_pt - bloque_alto_pt
    else:
        top_pt_desde_arriba = y_ref_pt

    alineacion = info.get("textAlign", "left")
    x_izq_pt = cx_pt - width_pt / 2
    x_der_pt = cx_pt + width_pt / 2

    for i, linea in enumerate(lineas):
        baseline_pt_desde_arriba = top_pt_desde_arriba + fontsize_pt * 0.82 + i * interlineado_pt
        y_pt = ah_pt - baseline_pt_desde_arriba

        if alineacion == "right":
            c.drawRightString(x_der_pt, y_pt, linea)
            ancho_pt = c.stringWidth(linea, fuente, fontsize_pt)
            x_sub_ini, x_sub_fin = x_der_pt - ancho_pt, x_der_pt
        elif alineacion == "center":
            c.drawCentredString(cx_pt, y_pt, linea)
            ancho_pt = c.stringWidth(linea, fuente, fontsize_pt)
            x_sub_ini, x_sub_fin = cx_pt - ancho_pt / 2, cx_pt + ancho_pt / 2
        else:  # "left", de toda la vida
            c.drawString(x_izq_pt, y_pt, linea)
            ancho_pt = c.stringWidth(linea, fuente, fontsize_pt)
            x_sub_ini, x_sub_fin = x_izq_pt, x_izq_pt + ancho_pt

        if subrayado:
            y_linea = y_pt - fontsize_pt * 0.12
            c.setStrokeColorRGB(*color)
            c.setLineWidth(max(0.4, fontsize_pt * 0.04))
            c.line(x_sub_ini, y_linea, x_sub_fin, y_linea)


def dibujar_foto_editor_libre(c, ruta, foto_info, canvas_w, canvas_h, aw_pt, ah_pt, clip_a_pagina=True):
    """
    Dibuja una foto en modo LIBRE: posición/escala/ángulo + recorte
    (cropX/cropY/width/height, igual que Fabric.js) tal como los dejó el
    cliente en el editor. Se usa tanto para la portada como para las fotos
    de las páginas interiores - es la MISMA función, para que las dos
    partes se comporten igual.

    OJO: antes esto NO aplicaba el recorte de verdad - solo usaba
    width/height para calcular el tamaño en la página, pero dibujaba la
    foto ENTERA estirada dentro de ese hueco. Ahora se recorta la imagen
    de verdad antes de dibujarla.
    """
    if not ruta or not os.path.exists(ruta) or not foto_info:
        return False
    try:
        # Igual que en el texto: si esta foto en concreto guardó su propio
        # tamaño de referencia de canvas, se usa ese en vez del de la
        # página - evita que una foto se desproporcione en el PDF porque
        # OTRA cosa de la misma página se editó después en otra sesión/
        # pantalla de tamaño distinto.
        canvas_w = foto_info.get("canvas_w") or canvas_w
        canvas_h = foto_info.get("canvas_h") or canvas_h
        with Image.open(ruta) as _tmp:
            # OJO: hay que aplicar exif_transpose también aquí. El navegador
            # (donde el cliente recortó en Fabric.js) rota la foto según el
            # EXIF antes de mostrarla, así que cropX/cropY/width/height que
            # guardó el cliente están en la escala de la imagen YA ROTADA.
            # Si aquí se lee el tamaño del archivo sin rotar, en cualquier
            # foto vertical de móvil (la inmensa mayoría) ancho y alto salen
            # intercambiados y el recorte final queda completamente
            # descuadrado - portada "ampliada"/sin recortar, fotos
            # interiores desplazadas, etc.
            _tmp_t = ImageOps.exif_transpose(_tmp)
            iw_true, ih_true = _tmp_t.size

        img_original = Image.open(ruta)
        try:
            img_original.draft("RGB", (3500, 3500))
        except Exception:
            pass
        img_original = img_original.convert("RGB")
        img_original = ImageOps.exif_transpose(img_original)
        iw_real, ih_real = img_original.size

        # Recorte real (cropX/cropY/width/height vienen en la escala de la
        # imagen ORIGINAL que vio el navegador - hay que reescalar si el
        # draft() de aquí devolvió una versión de otro tamaño)
        escala_x = iw_real / iw_true if iw_true else 1
        escala_y = ih_real / ih_true if ih_true else 1
        crop_x = foto_info.get("cropX") or 0
        crop_y = foto_info.get("cropY") or 0
        crop_w = foto_info.get("width") or iw_true
        crop_h = foto_info.get("height") or ih_true

        cx0 = max(0, int(crop_x * escala_x))
        cy0 = max(0, int(crop_y * escala_y))
        cx1 = min(iw_real, int((crop_x + crop_w) * escala_x))
        cy1 = min(ih_real, int((crop_y + crop_h) * escala_y))
        if cx1 > cx0 and cy1 > cy0:
            img_original = img_original.crop((cx0, cy0, cx1, cy1))

        scale_x = foto_info.get("scaleX") or 1
        scale_y = foto_info.get("scaleY") or 1
        disp_w_frac = (crop_w * scale_x) / canvas_w
        disp_h_frac = (crop_h * scale_y) / canvas_h
        cx_frac = foto_info.get("left", canvas_w / 2) / canvas_w
        cy_frac = foto_info.get("top", canvas_h / 2) / canvas_h

        disp_w_pt = disp_w_frac * aw_pt
        disp_h_pt = disp_h_frac * ah_pt
        cx_pt = cx_frac * aw_pt
        cy_pt = ah_pt - (cy_frac * ah_pt)

        # Reducir a 300ppp del tamaño real que ocupa en la página, no la
        # resolución completa del móvil
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
        if clip_a_pagina:
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
        return True
    except Exception as e:
        log(f"Error dibujando foto libre ({os.path.basename(ruta)}): {e}", "!")
        return False


_STICKERS_CACHE_DIR = "/tmp/bookeo_stickers_cache"
_stickers_svg_cache = {}  # codepoint -> Drawing ya parseado (una vez por proceso worker)


def _obtener_drawing_sticker(cp):
    if not _SVGLIB_OK or not cp:
        return None
    cp = str(cp).upper().strip()
    if cp in _stickers_svg_cache:
        return _stickers_svg_cache[cp]
    try:
        os.makedirs(_STICKERS_CACHE_DIR, exist_ok=True)
        ruta_local = os.path.join(_STICKERS_CACHE_DIR, f"{cp}.svg")
        if not os.path.exists(ruta_local):
            # OpenMoji: emojis de código abierto (CC BY-SA 4.0) - se usan
            # como imagen en vez de como carácter de texto precisamente
            # porque las fuentes decorativas del libro no tienen glifos de
            # emoji (se comprobó con las fuentes reales: ninguna los
            # tiene) - así el sticker se ve igual sin importar la fuente
            # que haya elegido el cliente para el resto del texto.
            # OJO: esta es la URL oficial documentada por OpenMoji para
            # CDN (README del proyecto) - la primera versión usaba
            # /npm/openmoji@15.0.0/... que no existe (404), por eso no se
            # veía nada, ni en el selector ni al colocar un sticker.
            url = f"https://cdn.jsdelivr.net/gh/hfg-gmuend/openmoji/color/svg/{cp}.svg"
            resp = requests.get(url, timeout=8)
            resp.raise_for_status()
            with open(ruta_local, "wb") as f:
                f.write(resp.content)
        drawing = svg2rlg(ruta_local)
        _stickers_svg_cache[cp] = drawing
        return drawing
    except Exception as e:
        print(f"[stickers] no se pudo descargar/leer el sticker '{cp}': {e}")
        _stickers_svg_cache[cp] = None
        return None


def dibujar_stickers_pdf(c, AW, AH, stickers, canvas_w, canvas_h):
    """
    Dibuja los stickers (imágenes OpenMoji en SVG) que el cliente haya
    colocado en la portada o en una página interior - se dibujan como
    vectores de verdad (no como texto), así que nunca dependen de si la
    fuente elegida tiene o no ese símbolo - a diferencia de escribir un
    emoji dentro de una caja de texto, que con las fuentes decorativas del
    libro simplemente desaparece sin avisar.
    """
    if not stickers or not _SVGLIB_OK or not canvas_w or not canvas_h:
        return
    aw_pt, ah_pt = AW * mm, AH * mm
    for st in stickers:
        cp = st.get("cp")
        drawing_original = _obtener_drawing_sticker(cp)
        if not drawing_original:
            continue
        try:
            drawing = copy.deepcopy(drawing_original)

            width_px = (st.get("width", 0) or 0) * (st.get("scaleX", 1) or 1)
            height_px = (st.get("height", 0) or 0) * (st.get("scaleY", 1) or 1)
            w_pt = (width_px / canvas_w) * aw_pt
            h_pt = (height_px / canvas_h) * ah_pt
            if w_pt <= 0 or h_pt <= 0 or not drawing.width or not drawing.height:
                continue

            sx = w_pt / drawing.width
            sy = h_pt / drawing.height
            drawing.width *= sx
            drawing.height *= sy
            drawing.scale(sx, sy)

            cx_frac = (st.get("left", canvas_w / 2) or 0) / canvas_w
            cy_frac = (st.get("top", canvas_h / 2) or 0) / canvas_h
            cx_pt = cx_frac * aw_pt
            cy_pt = ah_pt - (cy_frac * ah_pt)  # el canvas crece hacia abajo, el PDF hacia arriba
            angulo = st.get("angle", 0) or 0

            c.saveState()
            c.translate(cx_pt, cy_pt)
            if angulo:
                c.rotate(-angulo)
            renderPDF.draw(drawing, c, -w_pt / 2, -h_pt / 2)
            c.restoreState()
        except Exception as e:
            print(f"[stickers] error dibujando sticker '{cp}': {e}")


def dibujar_formas_pdf(c, AW, AH, formas, canvas_w, canvas_h):
    """
    Dibuja las formas sueltas (cuadrado/círculo/triángulo) que el cliente
    haya añadido a una página interior - mismo mecanismo de posición/
    tamaño/rotación que los marcos de la portada, pero como elementos
    independientes movibles que se pueden añadir varios en la misma página.
    """
    if not formas or not canvas_w or not canvas_h:
        return
    aw_pt, ah_pt = AW * mm, AH * mm
    for f in formas:
        tipo = f.get("tipo")
        width_px = (f.get("width", 0) or 0) * (f.get("scaleX", 1) or 1)
        height_px = (f.get("height", 0) or 0) * (f.get("scaleY", 1) or 1)
        w_pt = (width_px / canvas_w) * aw_pt
        h_pt = (height_px / canvas_h) * ah_pt
        if w_pt <= 0 or h_pt <= 0:
            continue
        grosor_pt = (f.get("grosor", 5) or 5) * (aw_pt / canvas_w)
        color = hex_a_rgb01(f.get("color", "#1a1a2e"))

        cx_frac = (f.get("left", canvas_w / 2) or 0) / canvas_w
        cy_frac = (f.get("top", canvas_h / 2) or 0) / canvas_h
        cx_pt = cx_frac * aw_pt
        cy_pt = ah_pt - (cy_frac * ah_pt)  # el canvas crece hacia abajo, el PDF hacia arriba
        angulo = f.get("angle", 0) or 0

        c.saveState()
        c.translate(cx_pt, cy_pt)
        if angulo:
            c.rotate(-angulo)
        c.setFillColorRGB(*color)
        if tipo == "cuadrado":
            c.rect(-w_pt / 2, -h_pt / 2, w_pt, h_pt, fill=1, stroke=0)
        elif tipo == "circulo":
            c.ellipse(-w_pt / 2, -h_pt / 2, w_pt / 2, h_pt / 2, fill=1, stroke=0)
        elif tipo == "triangulo":
            path = c.beginPath()
            path.moveTo(0, h_pt / 2)
            path.lineTo(-w_pt / 2, -h_pt / 2)
            path.lineTo(w_pt / 2, -h_pt / 2)
            path.close()
            c.drawPath(path, fill=1, stroke=0)
        c.restoreState()


def dibujar_portada_editor(c, AW, AH, ruta, titulo, subtitulo, do_wm, editor):
    canvas_w = editor.get("canvas_w") or AW
    canvas_h = editor.get("canvas_h") or AH
    aw_pt, ah_pt = AW * mm, AH * mm

    color_fondo = hex_a_rgb01(editor.get("color_fondo", "#f5f0e6"))
    c.setFillColorRGB(*color_fondo)
    c.rect(0, 0, aw_pt, ah_pt, fill=1, stroke=0)

    foto_info = editor.get("foto")
    fotos_blanco_lista = editor.get("fotos_blanco") or []
    if ruta and os.path.exists(ruta) and foto_info:
        ok = dibujar_foto_editor_libre(c, ruta, foto_info, canvas_w, canvas_h, aw_pt, ah_pt)
        if not ok:
            foto_zona(c, ruta, 0, 0, AW, AH, check_ppi=False)
    elif ruta and not fotos_blanco_lista:
        # OJO: esta ruta viene de pagina["fotos"][0]["ruta"] (la primera
        # foto de la página de portada) - solo tiene sentido usarla como
        # fondo a página completa cuando de verdad es el modo "una sola
        # foto" sin datos de posición (editor.get("foto") vacío). Si
        # estamos en modo blanco con varias fotos propias (fotos_blanco),
        # esa "primera foto" es solo una más de varias ya bien colocadas -
        # dibujarla aquí también la duplicaba como fondo gigante por
        # detrás de todo, sin venir a cuento.
        foto_zona(c, ruta, 0, 0, AW, AH, check_ppi=False)

    # Modo blanco: puede haber varias fotos independientes (cada una con
    # su propia posición/tamaño/recorte/ángulo, se pueden rotar) en vez de
    # una sola fija al recorte de siempre. La ruta de cada una ya viene
    # inyectada en la propia entrada (ver preparar_datos_libro).
    for foto_blanco in (editor.get("fotos_blanco") or []):
        ruta_fb = foto_blanco.get("ruta")
        if ruta_fb and os.path.exists(ruta_fb):
            dibujar_foto_editor_libre(c, ruta_fb, foto_blanco, canvas_w, canvas_h, aw_pt, ah_pt)

    # Modo blanco: puede haber varios marcos añadidos por el cliente
    # (editor.get("marcos"), una lista) en vez de uno solo fijo al recorte
    # de la foto (editor.get("marco")). Se dibujan todos, en el mismo
    # orden en que se añadieron.
    marcos_lista = editor.get("marcos")
    if marcos_lista:
        for m in marcos_lista:
            dibujar_marco_pdf(c, AW, AH, m, canvas_w, canvas_h)
    else:
        dibujar_marco_pdf(c, AW, AH, editor.get("marco"), canvas_w, canvas_h)

    dibujar_stickers_pdf(c, AW, AH, editor.get("stickers"), canvas_w, canvas_h)

    # Texto suelto adicional (el +T) - no es título ni subtítulo, solo
    # decoración extra que el cliente haya añadido a mano.
    for texto_extra in (editor.get("textos_extra") or []):
        _dibujar_texto_editor(c, AW, AH, canvas_w, canvas_h, texto_extra, texto_extra.get("texto", ""))

    banda = editor.get("banda")
    if banda:
        # Banda blanca de fondo para el título/subtítulo en el estilo
        # "foto a página completa" - mismo bloque que ve el cliente en el
        # editor, para que el PDF final coincida con la vista previa.
        banda_top_frac = banda.get("top", 0) / canvas_h
        banda_alto_frac = banda.get("height", 0) / canvas_h
        banda_y_pt = ah_pt - (banda_top_frac * ah_pt) - (banda_alto_frac * ah_pt)
        banda_alto_pt = banda_alto_frac * ah_pt
        try:
            color_banda = hex_a_rgb01(banda.get("color", "#ffffff"))
        except Exception:
            color_banda = (1, 1, 1)
        c.setFillColorRGB(*color_banda)
        c.rect(0, banda_y_pt, aw_pt, banda_alto_pt, fill=1, stroke=0)

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
    c.rotate(90)

    # Texto del lomo: normalmente el título del libro, pero si la portada es
    # "en blanco" y el cliente puso un texto de lomo aparte (con su propio
    # color, más simple que copiar un título con varios colores), se usa ese.
    texto_lomo = (editor.get("lomo_texto") if editor else None) or titulo
    color_lomo_manual = editor.get("lomo_color") if editor else None

    # Fuente y color del texto: por defecto, los mismos que eligio el
    # cliente para el titulo de la portada (editor.titulo.fontFamily /
    # fill / bold / italic). Si no hay editor (portada automatica), se
    # mantiene el negro enriquecido de siempre.
    info_titulo = editor.get("titulo") if editor else None
    negrita = info_titulo.get("fontWeight") == "bold" if info_titulo else False
    cursiva = info_titulo.get("fontStyle") == "italic" if info_titulo else False
    fuente = mapear_fuente(info_titulo.get("fontFamily", ""), negrita, cursiva) if info_titulo else "Helvetica-Bold"

    if color_lomo_manual:
        try:
            c.setFillColorRGB(*hex_a_rgb01(color_lomo_manual))
        except Exception:
            set_negro(c)
    elif info_titulo and info_titulo.get("fill"):
        try:
            c.setFillColorRGB(*hex_a_rgb01(info_titulo.get("fill", "#1a1a2e")))
        except Exception:
            set_negro(c)
    else:
        set_negro(c)

    # El lomo de un libro con pocas páginas es físicamente muy estrecho
    # (grosor_lomo depende del número de hojas - ver
    # obtener_dimensiones_cubierta_gelato) - un tamaño de letra fijo de
    # 4mm no cabe de ancho en un lomo de, por ejemplo, 6-7mm, así que el
    # texto se salía hacia las bisagras/portada-contraportada y quedaba
    # prácticamente invisible o cortado. Aquí el tamaño se adapta al
    # grosor real del lomo (con margen a cada lado), con un mínimo
    # legible - si ni así cabe, se seguirá viendo pequeño pero SIEMPRE
    # dentro del lomo, nunca fuera de él.
    margen_lomo_mm = 1.2
    fontsize_mm = min(4.0, max(2.2, aw / mm - margen_lomo_mm * 2))
    c.setFont(fuente, fontsize_mm * mm)
    c.drawCentredString(0, 0, texto_lomo)
    c.restoreState()
    if do_wm:
        wm(c, aw, ah)


def dibujar_contraportada(c, AW, AH, do_wm, editor=None):
    color_fondo_hex = editor.get("color_fondo") if editor else None
    bg_blanco(c, AW, AH, color_fondo_hex)
    c.setFillColorRGB(0.65, 0.65, 0.65)
    c.setFont("Helvetica", 2.8 * mm)
    c.drawCentredString(AW * mm / 2, (MG + 2) * mm, "Bookeo - mibookeo.es")
    if do_wm:
        wm(c, AW * mm, AH * mm)


def dibujar_cubierta_spread(c, dims, ruta_foto_portada, titulo, subtitulo, do_wm, editor=None):
    """
    Dibuja la cubierta ENTERA (contraportada + lomo + portada) como una
    única página-spread, con las medidas exactas que Gelato ha devuelto
    para este formato y número de páginas concreto (ver
    obtener_dimensiones_cubierta_gelato). Así es como Gelato exige la
    página 1 del PDF para fotolibros de tapa dura: un solo spread, no 3
    páginas sueltas.

    'dims' es la respuesta cruda de la API de cover-dimensions de Gelato
    (todo en mm, origen arriba-izquierda).
    """
    wrap = dims["wraparoundInsideSize"]
    canvas_h_mm = wrap["height"]
    aw_pt, ah_pt = wrap["width"] * mm, wrap["height"] * mm

    # 1) Fondo de TODO el lienzo (incluye la franja de giro alrededor del
    # cartón y las bisagras) con el color que el cliente eligió en el
    # editor - así no hay ningún borde de otro color al recortar/plegar,
    # y portada+lomo+contraportada quedan del mismo color como ya se
    # decidió (ver notas de producto).
    color_fondo = (1, 1, 1)
    if editor and editor.get("color_fondo"):
        try:
            color_fondo = hex_a_rgb01(editor.get("color_fondo"))
        except Exception:
            pass
    c.setFillColorRGB(*color_fondo)
    c.rect(0, 0, aw_pt, ah_pt, fill=1, stroke=0)

    # 2) Contraportada (detrás)
    x0, y0, w, h = _zona_a_puntos(dims["contentBackSize"], canvas_h_mm)
    c.saveState()
    c.translate(x0, y0)
    dibujar_contraportada(c, w / mm, h / mm, False, editor=editor)
    c.restoreState()

    # 3) Lomo (centro)
    x0, y0, w, h = _zona_a_puntos(dims["spineSize"], canvas_h_mm)
    c.saveState()
    c.translate(x0, y0)
    dibujar_lomo(c, w / mm, h / mm, titulo, do_wm=False, editor=editor)
    c.restoreState()

    # 4) Portada (delante)
    x0, y0, w, h = _zona_a_puntos(dims["contentFrontSize"], canvas_h_mm)
    c.saveState()
    c.translate(x0, y0)
    dibujar_portada(c, w / mm, h / mm, ruta_foto_portada, titulo, subtitulo, False, editor=editor)
    c.restoreState()

    if do_wm:
        wm(c, aw_pt, ah_pt)


def dibujar_pagina_blanca(c, AW, AH):
    bg_blanco(c, AW, AH)


def qr_pos_a_centro_mm(qr_pos, AW, AH):
    """
    Convierte la posición del QR que guarda el editor (fracción 0-1, origen
    arriba-izquierda, como en el navegador) a coordenadas en mm con el
    origen de ReportLab (abajo-izquierda, eje Y invertido). None si no hay
    posición personalizada (el cliente no lo movió).
    """
    if not qr_pos:
        return None
    x_mm = qr_pos.get("left", 0.5) * AW
    y_mm = AH - (qr_pos.get("top", 0.5) * AH)
    return (x_mm, y_mm)


def calcular_zonas_layout_mm(layout, num_fotos, AW, AH, pie_h=0):
    """
    Devuelve la lista de zonas (x,y,w,h) en mm para un layout - tiene que
    coincidir exactamente con lo que dibuja cada layout_X, y con
    calcularZonasLayoutEnDims() del editor (editor.html) en JavaScript.
    Se usa para saber qué tamaño de hueco le toca a cada foto y así poder
    calcular su ventana de recorte (calcular_ventana_recorte_frac).
    """
    if layout == "1" or num_fotos == 1:
        m = MARGEN_FOTO_COMPLETA
        return [(m, m, AW - m * 2, AH - m * 2)]
    if layout == "2H":
        fw = (AW - MG * 2 - GAP) / 2
        fh = AH - MG * 2 - pie_h
        y0 = MG + pie_h
        return [(MG + i * (fw + GAP), y0, fw, fh) for i in range(2)]
    if layout == "2V":
        fh = (AH - MG * 2 - GAP - pie_h) / 2
        fw = AW - MG * 2
        y0 = MG + pie_h
        return [(MG, y0 + i * (fh + GAP), fw, fh) for i in range(2)]
    if layout == "3":
        pw = AW * 0.60 - MG
        sh = (AH - MG * 2 - GAP - pie_h) / 2
        sw = AW - MG - pw - GAP - MG
        zh = AH - MG * 2 - pie_h
        y0 = MG + pie_h
        sx = MG + pw + GAP
        return [(MG, y0, pw, zh), (sx, y0, sw, sh), (sx, y0 + sh + GAP, sw, sh)]
    if layout == "4":
        cw = (AW - MG * 2 - GAP) / 2
        ch = (AH - MG * 2 - GAP - pie_h) / 2
        y0 = MG + pie_h
        return [
            (MG, y0 + ch + GAP, cw, ch), (MG + cw + GAP, y0 + ch + GAP, cw, ch),
            (MG, y0, cw, ch), (MG + cw + GAP, y0, cw, ch),
        ]
    fw = AW / max(1, num_fotos)
    return [(i * fw, 0, fw, AH) for i in range(num_fotos)]


def layout_1(c, AW, AH, fotos_rutas, qr_idx, qr_url, pie, do_wm, qr_pos=None, fondo=None, recortes=None):
    bg_blanco(c, AW, AH, fondo)
    r = fotos_rutas[0]
    m = MARGEN_FOTO_COMPLETA
    rf = (recortes or [None])[0]
    foto_zona(c, r, m, m, AW - m * 2, AH - m * 2, recorte_frac=rf)
    if qr_idx == 0:
        dibujar_qr_sobre_foto(c, m, m, AW - m * 2, AH - m * 2, qr_url, r, centro_custom_mm=qr_pos_a_centro_mm(qr_pos, AW, AH))
    texto_pie(c, AW, pie)
    if do_wm:
        wm(c, AW * mm, AH * mm)


def layout_2H(c, AW, AH, fotos_rutas, qr_idx, qr_url, pie, do_wm, qr_pos=None, fondo=None, recortes=None):
    bg_blanco(c, AW, AH, fondo)
    pie_h = 8 if pie else 0
    fw = (AW - MG * 2 - GAP) / 2
    fh = AH - MG * 2 - pie_h
    y0 = MG + pie_h
    recortes = recortes or [None, None]
    for i, r in enumerate(fotos_rutas[:2]):
        x = MG + i * (fw + GAP)
        foto_zona(c, r, x, y0, fw, fh, recorte_frac=recortes[i] if i < len(recortes) else None)
        if qr_idx == i:
            dibujar_qr_sobre_foto(c, x, y0, fw, fh, qr_url, r, centro_custom_mm=qr_pos_a_centro_mm(qr_pos, AW, AH))
    texto_pie(c, AW, pie)
    if do_wm:
        wm(c, AW * mm, AH * mm)


def layout_2V(c, AW, AH, fotos_rutas, qr_idx, qr_url, pie, do_wm, qr_pos=None, fondo=None, recortes=None):
    bg_blanco(c, AW, AH, fondo)
    pie_h = 8 if pie else 0
    fh = (AH - MG * 2 - GAP - pie_h) / 2
    fw = AW - MG * 2
    y0 = MG + pie_h
    recortes = recortes or [None, None]
    for i, r in enumerate(fotos_rutas[:2]):
        y = y0 + i * (fh + GAP)
        foto_zona(c, r, MG, y, fw, fh, recorte_frac=recortes[i] if i < len(recortes) else None)
        if qr_idx == i:
            dibujar_qr_sobre_foto(c, MG, y, fw, fh, qr_url, r, centro_custom_mm=qr_pos_a_centro_mm(qr_pos, AW, AH))
    texto_pie(c, AW, pie)
    if do_wm:
        wm(c, AW * mm, AH * mm)


def layout_3(c, AW, AH, fotos_rutas, qr_idx, qr_url, pie, do_wm, qr_pos=None, fondo=None, recortes=None):
    bg_blanco(c, AW, AH, fondo)
    pie_h = 8 if pie else 0
    pw = AW * 0.60 - MG
    sh = (AH - MG * 2 - GAP - pie_h) / 2
    sw = AW - MG - pw - GAP - MG
    zh = AH - MG * 2 - pie_h
    y0 = MG + pie_h
    recortes = recortes or [None, None, None]
    r0 = fotos_rutas[0]
    foto_zona(c, r0, MG, y0, pw, zh, recorte_frac=recortes[0] if len(recortes) > 0 else None)
    if qr_idx == 0:
        dibujar_qr_sobre_foto(c, MG, y0, pw, zh, qr_url, r0, centro_custom_mm=qr_pos_a_centro_mm(qr_pos, AW, AH))
    sx = MG + pw + GAP
    for i, r in enumerate(fotos_rutas[1:3]):
        y = y0 + i * (sh + GAP)
        foto_zona(c, r, sx, y, sw, sh, recorte_frac=recortes[i + 1] if i + 1 < len(recortes) else None)
        if qr_idx == i + 1:
            dibujar_qr_sobre_foto(c, sx, y, sw, sh, qr_url, r, centro_custom_mm=qr_pos_a_centro_mm(qr_pos, AW, AH))
    texto_pie(c, AW, pie)
    if do_wm:
        wm(c, AW * mm, AH * mm)


def layout_4(c, AW, AH, fotos_rutas, qr_idx, qr_url, pie, do_wm, qr_pos=None, fondo=None, recortes=None):
    bg_blanco(c, AW, AH, fondo)
    pie_h = 8 if pie else 0
    cw = (AW - MG * 2 - GAP) / 2
    ch = (AH - MG * 2 - GAP - pie_h) / 2
    y0 = MG + pie_h
    pos = [(MG, y0 + ch + GAP), (MG + cw + GAP, y0 + ch + GAP), (MG, y0), (MG + cw + GAP, y0)]
    recortes = recortes or [None, None, None, None]
    for i, r in enumerate(fotos_rutas[:4]):
        x, y = pos[i]
        foto_zona(c, r, x, y, cw, ch, recorte_frac=recortes[i] if i < len(recortes) else None)
        if qr_idx == i:
            dibujar_qr_sobre_foto(c, x, y, cw, ch, qr_url, r, centro_custom_mm=qr_pos_a_centro_mm(qr_pos, AW, AH))
    texto_pie(c, AW, pie)
    if do_wm:
        wm(c, AW * mm, AH * mm)


def _dibujar_texto_titulo_capitulo(c, AW, AH, canvas_w=None, canvas_h=None,
                                    titulo_info=None, subtitulo_info=None,
                                    titulo="", subtitulo=""):
    if titulo_info or subtitulo_info:
        cw = canvas_w or AW
        ch = canvas_h or AH
        if titulo_info:
            _dibujar_texto_editor(c, AW, AH, cw, ch, titulo_info, titulo)
        else:
            _dibujar_texto_titulo_capitulo_fijo(c, AW, AH, titulo, "")
        if subtitulo_info:
            _dibujar_texto_editor(c, AW, AH, cw, ch, subtitulo_info, subtitulo)
        return
    _dibujar_texto_titulo_capitulo_fijo(c, AW, AH, titulo, subtitulo)


def _dibujar_texto_titulo_capitulo_fijo(c, AW, AH, titulo, subtitulo):
    # OJO: en reportlab el eje Y crece hacia ARRIBA (y=0 es el borde
    # inferior de la página), al revés que en el canvas del editor
    # (donde y=0 es el borde SUPERIOR). El editor pinta las fotos arriba
    # (80%) y la banda de título abajo (20%) - aquí cy se calcula cerca
    # de 0 (abajo del todo en reportlab), para que coincida.
    banda_h = AH * 0.20
    cy = (banda_h / 2) * mm
    set_negro(c)
    c.setFont("Helvetica-Bold", 9 * mm)
    c.drawCentredString(AW * mm / 2, cy + 3 * mm, titulo)
    if subtitulo:
        c.setFillColorRGB(0.5, 0.5, 0.5)
        c.setFont("Helvetica-Oblique", 3.2 * mm)
        c.drawCentredString(AW * mm / 2, cy - 4 * mm, subtitulo)


def layout_titulo_capitulo(c, AW, AH, titulo, subtitulo, fotos_rutas, variante, do_wm):
    bg_blanco(c, AW, AH)
    banda_h = AH * 0.20
    foto_h = AH - banda_h - MG
    foto_y = banda_h  # las fotos empiezan justo encima de la banda (que ahora está abajo) y llegan hasta arriba
    _dibujar_texto_titulo_capitulo(c, AW, AH, titulo=titulo, subtitulo=subtitulo)
    n = len(fotos_rutas)
    if n == 0:
        pass
    elif n == 1:
        foto_zona(c, fotos_rutas[0], MG, foto_y, AW - MG * 2, foto_h)
    elif variante % 2 == 0 or n == 2:
        fw = (AW - MG * 2 - GAP) / 2
        for j, r in enumerate(fotos_rutas[:2]):
            foto_zona(c, r, MG + j * (fw + GAP), foto_y, fw, foto_h)
    else:
        foto_zona(c, fotos_rutas[0], MG, foto_y, AW - MG * 2, foto_h)
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

    # "Método del resto mayor": primero se da a cada capítulo la parte
    # entera que le toca, y las páginas que sobran (por el redondeo) se
    # reparten una a una a los capítulos con el resto más grande. Esto
    # GARANTIZA que la suma final sea exactamente paginas_disponibles (o
    # menos si hay menos capítulos que páginas) - antes cada capítulo
    # tenía un suelo de "al menos 1 página" con round(), y con muchos
    # capítulos pequeños (muchos días distintos, por ejemplo) esos
    # "al menos 1" sumados se pasaban del objetivo con facilidad.
    n = len(capitulos_con_fotos)
    proporciones = [len(cap) / total_fotos for cap in capitulos_con_fotos]
    exactos = [p * paginas_disponibles for p in proporciones]
    paginas_por_capitulo = [int(e) for e in exactos]  # parte entera de cada uno
    restantes = paginas_disponibles - sum(paginas_por_capitulo)

    # Reparte lo que sobra por redondeo hacia abajo, empezando por los
    # capítulos con la parte decimal más grande (los que "casi" llegaban
    # a la siguiente página entera).
    orden_por_resto = sorted(range(n), key=lambda i: exactos[i] - paginas_por_capitulo[i], reverse=True)
    for i in orden_por_resto[:max(0, restantes)]:
        paginas_por_capitulo[i] += 1

    # Todo capítulo con fotos se lleva al menos 1 página - si eso hace que
    # se pase del objetivo (puede pasar con muchísimos capítulos y pocas
    # páginas), se recorta quitando de los capítulos con más páginas
    # asignadas, nunca por debajo de 1.
    for i in range(n):
        if capitulos_con_fotos[i] and paginas_por_capitulo[i] < 1:
            paginas_por_capitulo[i] = 1
    exceso = sum(paginas_por_capitulo) - paginas_disponibles
    while exceso > 0:
        idx_mayor = max(range(n), key=lambda i: paginas_por_capitulo[i])
        if paginas_por_capitulo[idx_mayor] <= 1:
            break  # ya no se puede recortar más sin dejar un capítulo a 0
        paginas_por_capitulo[idx_mayor] -= 1
        exceso -= 1

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


def _completar_paginas_contenido_hasta_objetivo(paginas, paginas_objetivo, qr_map, AW, AH):
    """
    Red de seguridad SIMÉTRICA a paginas_con_tope_estricto (que solo
    garantiza no pasarse del máximo). El reparto por bloques de día
    (paginas_para_capitulo_caso_b) redondea hacia abajo en cada bloque -
    con varios capítulos pequeños ese redondeo puede acumularse y el
    libro termina con MENOS páginas de contenido que las contratadas
    (ej. 29 en vez del mínimo 30), aunque calcular_paginas_por_capitulo
    hubiera repartido bien el objetivo entre capítulos.

    Si falta alguna, se van partiendo en dos las páginas de CONTENIDO con
    más fotos (nunca portada/título de capítulo/blancas), hasta llegar al
    objetivo o quedarse sin páginas partibles (todas a 1 sola foto).
    """
    actuales = sum(1 for p in paginas if p.get("tipo") == "contenido")
    faltan = paginas_objetivo - actuales
    if faltan <= 0:
        return paginas

    log(f"Reparto de capítulos se quedó corto ({actuales}/{paginas_objetivo}) "
        f"- partiendo páginas con más fotos para llegar al mínimo", "!")

    intentos = 0
    while faltan > 0 and intentos < paginas_objetivo * 4:
        intentos += 1
        candidatos = [i for i, p in enumerate(paginas)
                      if p.get("tipo") == "contenido" and len(p.get("fotos", [])) >= 2]
        if not candidatos:
            break  # no queda ninguna página partible (todas a 1 sola foto)
        idx = max(candidatos, key=lambda i: len(paginas[i]["fotos"]))
        pagina = paginas[idx]
        fotos = pagina["fotos"]
        mitad = max(1, len(fotos) // 2)
        fotos_a, fotos_b = fotos[:mitad], fotos[mitad:]
        if not fotos_b:
            break

        def _grupo_a_pagina(grupo, con_texto):
            layout, fotos_layout = elegir_layout(grupo)
            qr_idx, qr_url = -1, ""
            for fi, foto in enumerate(fotos_layout):
                if foto.get("nombre") in qr_map:
                    qr_idx, qr_url = fi, qr_map[foto["nombre"]]
                    break
            pg = {"layout": layout, "fotos": fotos_layout, "qr_idx": qr_idx, "qr_url": qr_url,
                  "texto": pagina.get("texto", "") if con_texto else ""}
            nueva = _pagina_contenido_desde_calculo(pg, AW, AH)
            nueva["capitulo"] = pagina.get("capitulo")
            return nueva

        paginas[idx:idx + 1] = [_grupo_a_pagina(fotos_a, True), _grupo_a_pagina(fotos_b, False)]
        faltan -= 1

    return paginas


def analizar_con_ia(fotos, dias, titulo_cliente="", sin_capitulos=False):
    log("Conectando con Claude API...", "i")
    cli = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    listado = f"INVENTARIO COMPLETO - {len(fotos)} fotos:\n"
    for f in fotos:
        es_frame = " [FOTOGRAMA VIDEO-QR]" if f.get("es_frame_video") else ""
        es_dudosa = " [FECHA DUDOSA]" if f.get("fuente_fecha") in ("modificacion", "nombre") else ""
        listado += f"  {f['fecha'].strftime('%d/%m/%Y')} - {f['nombre']}{es_frame}{es_dudosa}\n"

    MAX = 50
    # Prioridad a las fotos de fecha DUDOSA (vienen del nombre de archivo o
    # de la fecha de modificación del propio archivo - típico de fotos
    # reenviadas o descargadas de WhatsApp, cuya fecha real de cuándo se
    # tomó no se puede fiar). Esas SÍ necesitan que la IA las vea de verdad
    # para saber a qué día/época pertenecen por el contenido (ropa de
    # abrigo -> invierno, bañador -> playa/verano, adornos -> navidad...).
    # Las de fecha FIABLE (exif, con hora exacta de la propia cámara) se
    # pueden agrupar bien solo con el texto del inventario (fecha y hora
    # seguidas = mismo día), sin gastar uno de los 50 huecos de imagen -
    # así esos huecos se aprovechan en las fotos que de verdad los necesitan,
    # en vez de perderse en fotos que ya se agrupaban bien sin mirarlas.
    fuentes_dudosas = ("modificacion", "nombre")
    fotos_dudosas = [f for f in fotos if f.get("fuente_fecha") in fuentes_dudosas]
    fotos_fiables = [f for f in fotos if f.get("fuente_fecha") not in fuentes_dudosas]

    if len(fotos) <= MAX:
        muestra = fotos
    elif len(fotos_dudosas) >= MAX:
        # Hay tantas dudosas que ni siquiera caben todas - muestreo
        # uniforme solo entre ellas, las fiables se quedan fuera del todo
        # (no las necesita ver, el texto del inventario ya les basta).
        muestra = [fotos_dudosas[int(i * len(fotos_dudosas) / MAX)] for i in range(MAX)]
    else:
        # Caben todas las dudosas + se rellenan los huecos que sobren con
        # una muestra uniforme de las fiables (para que la IA tenga
        # también algo de contexto visual general del libro).
        muestra = list(fotos_dudosas)
        huecos_libres = MAX - len(muestra)
        if huecos_libres > 0 and fotos_fiables:
            paso = max(1, len(fotos_fiables) // huecos_libres)
            muestra += [fotos_fiables[i] for i in range(0, len(fotos_fiables), paso)][:huecos_libres]
        muestra.sort(key=lambda f: f["fecha"])

    if fotos and fotos[-1] not in muestra:
        if muestra:
            muestra[-1] = fotos[-1]
        else:
            muestra = [fotos[-1]]

    # Generar cada miniatura (abrir + redimensionar + JPEG + base64) es
    # independiente por foto - antes se hacia una a una, con hasta 50
    # fotos eso ya sumaba varios segundos de espera para el cliente antes
    # de ver las propuestas de portada. Se paralelizan con threads
    # (ThreadPoolExecutor libera el GIL durante la parte pesada de PIL/
    # zlib) y se vuelven a poner en el orden original al final, porque el
    # orden por fecha en el mensaje le importa a Claude para el contexto.
    def _miniatura(f):
        try:
            img = Image.open(f["ruta"]).convert("RGB")
            img.thumbnail((100, 100), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=65)
            b64 = base64.standard_b64encode(buf.getvalue()).decode()
            return b64
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        miniaturas = list(pool.map(_miniatura, muestra))

    contenido = [{"type": "text", "text": listado}]
    for f, b64 in zip(muestra, miniaturas):
        if b64 is None:
            continue
        contenido.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
        contenido.append({"type": "text", "text": f"- {f['nombre']} {f['fecha'].strftime('%d/%m/%Y')}"})

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
- Las fotos con fecha y hora seguidas y SIN marca [FECHA DUDOSA] son del mismo dia o momento - agrupalas con confianza solo por esa fecha, aunque no veas su imagen (no todas las fotos del inventario tienen imagen, solo una muestra)
- Las fotos marcadas [FECHA DUDOSA] (vienen de WhatsApp o de la fecha del propio archivo, no de la camara) SI tienen imagen para que las mires: decide su capitulo por lo que ves (ropa de abrigo -> invierno, banador -> playa/verano, adornos -> navidad, etc.), no te fies de su fecha
- NUNCA metas en el mismo capitulo fotos de epocas claramente distintas solo porque quedaron sueltas - si una foto no encaja claramente en ningun capitulo por fecha ni por imagen, mira que epoca del año sugiere su imagen y usala para decidir, en vez de dejarla suelta o forzarla en un capitulo que no le corresponde
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


def preparar_fotos_ordenadas(fotos_rutas):
    """
    Deduplica y ordena por fecha (EXIF/nombre/mtime) las fotos subidas -
    la misma preparación que hace generar_propuestas_portada() antes de
    llamar a la IA, pero como función aparte para poder reutilizarla en el
    modo "crear desde cero" (que necesita las fotos ordenadas para la
    galería del editor, pero SIN llamar a Claude en absoluto).
    """
    exts_foto = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff"}
    rutas_unicas = []
    nombres_vistos = set()
    for ruta in fotos_rutas:
        ext = Path(ruta).suffix.lower()
        if ext in exts_foto:
            nombre = os.path.basename(ruta)
            if nombre in nombres_vistos:
                log(f"Foto duplicada omitida: {nombre}", "!")
                continue
            nombres_vistos.add(nombre)
            rutas_unicas.append(ruta)

    # Leer la fecha de cada foto es I/O por archivo - en paralelo con
    # threads (el GIL no frena la parte de I/O) tarda una fracción de lo
    # que tardaría una a una.
    fotos = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futuros = {pool.submit(leer_fecha, ruta): ruta for ruta in rutas_unicas}
        for fut in as_completed(futuros):
            ruta = futuros[fut]
            fecha, fuente = fut.result()
            fotos.append({"ruta": ruta, "fecha": fecha, "nombre": os.path.basename(ruta), "fuente_fecha": fuente})

    fotos.sort(key=lambda x: x["fecha"])
    return fotos


def generar_propuestas_portada(fotos_rutas, videos_rutas, titulo_cliente="", formato="2128", orientacion="v", packs_extra=0, sin_capitulos=False):
    fotos = preparar_fotos_ordenadas(fotos_rutas)

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


def calcular_estructura_libro_vacia(portada_elegida, paginas_objetivo=30, AW=200, AH=200):
    """
    Equivalente a calcular_estructura_libro() pero para el modo "crear mi
    álbum desde cero": sin IA, sin capítulos, sin reparto automático de
    fotos - solo la portada que eligió el cliente y páginas de contenido
    completamente vacías, listas para que el cliente las rellene él mismo
    a mano en el editor (con el mismo panel de "Fotos" que ya existe).
    """
    paginas = []

    portada_nombre = ""
    portada_ruta = ""
    if portada_elegida and portada_elegida.get("foto_personalizada_ruta"):
        portada_ruta = portada_elegida["foto_personalizada_ruta"]
        portada_nombre = os.path.basename(portada_ruta)

    fotos_portada = [{"nombre": portada_nombre, "ruta": portada_ruta}] if portada_ruta else []
    # Modo blanco: cada foto adicional también se incluye aquí (nombre +
    # ruta) para que se le resuelva una URL válida y la vista previa del
    # editor pueda mostrarla - no solo el PDF final.
    if portada_elegida and portada_elegida.get("fotos_blanco_rutas"):
        for nombre, ruta in portada_elegida["fotos_blanco_rutas"].items():
            if ruta:
                fotos_portada.append({"nombre": nombre, "ruta": ruta})

    paginas.append({
        "tipo": "portada", "layout": None,
        "fotos": fotos_portada,
        "texto": "", "qr_idx": -1, "qr_url": "", "capitulo": None,
        "editor": (portada_elegida.get("editor") if portada_elegida else None),
    })

    paginas.append({"tipo": "blanco", "layout": None, "fotos": [], "texto": "",
                     "qr_idx": -1, "qr_url": "", "capitulo": None})

    for _ in range(paginas_objetivo):
        paginas.append({"tipo": "contenido", "layout": None, "fotos": [], "texto": "",
                         "qr_idx": -1, "qr_url": "", "capitulo": None})

    paginas.append({"tipo": "blanco", "layout": None, "fotos": [], "texto": "",
                     "qr_idx": -1, "qr_url": "", "capitulo": None})
    paginas.append({"tipo": "contraportada", "layout": None, "fotos": [], "texto": "",
                     "qr_idx": -1, "qr_url": "", "capitulo": None})

    return paginas


def calcular_estructura_libro(diseño, fotos_dict, qr_map, portada_elegida, caso_reparto="A", paginas_objetivo=30, AW=200, AH=200):
    """
    Calcula la ESTRUCTURA del libro (que foto va en que pagina, que layout,
    capitulos, lomo...) SIN dibujar nada en PDF. Es la pieza clave para el
    editor: antes esto y el dibujado en PDF estaban mezclados en la misma
    funcion (generar_pdf), lo que hacia imposible mandar paginas al editor
    antes de tener el PDF entero, o dejar que el cliente las edite.

    Devuelve una lista de paginas, cada una un dict JSON-serializable:
      {
        "tipo": "portada" | "blanco" | "titulo_capitulo" | "contenido" | "contraportada",
        # "portada" es el spread ENTERO de cubierta en el PDF final
        # (contraportada + lomo + portada, con las medidas exactas de
        # Gelato) - pero como página de ESTRUCTURA sigue siendo 1 sola,
        # para que el editor la enseñe igual que antes.
        # "contraportada" es una página aparte solo para que el editor la
        # enseñe al final del libro; en el PDF final no se dibuja como
        # página propia (su contenido ya va dentro del spread de portada).
        # Ya no existe el tipo "lomo" como página suelta.
        "layout": "1" | "2H" | "2V" | "3" | "4" | None,
        "fotos": [{"nombre": str, "ruta": str}, ...],
        "texto": str,
        "qr_idx": int, "qr_url": str,
        "capitulo": {"titulo": str, "subtitulo": str, "variante": int} | None,
      }

    Esta misma lista se puede:
      - mandar por WebSocket al editor segun se van calculando
      - guardar y recibir de vuelta ya editada por el cliente
      - pasarse a dibujar_pdf_desde_estructura() para el PDF final
    """
    paginas = []
    fotos_usadas = set()
    capitulo_variante = 0

    # -- PORTADA --
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

    paginas.append({
        "tipo": "portada", "layout": None,
        "fotos": [{"nombre": portada_nombre, "ruta": portada_ruta}] if portada_ruta else [],
        "texto": "", "qr_idx": -1, "qr_url": "", "capitulo": None,
        # El editor (fondo/foto recortada/marco/banda/título/subtítulo) se
        # manda tal cual para que el editor.html pueda pintar el diseño
        # real de la portada en la vista previa, no solo la foto en bruto -
        # antes no viajaba en absoluto dentro de 'paginas' (vivía aparte,
        # solo en portada_elegida, que nunca llegaba al navegador).
        "editor": (portada_elegida.get("editor") if portada_elegida else None),
    })
    # OJO: la foto de portada NO se marca como "usada" a proposito.
    # La portada es una pieza aparte (la tapa del libro) - la misma foto
    # tiene que poder aparecer tambien dentro, como una pagina de contenido
    # mas. Si se marcara aqui como usada, esa foto desaparecia del interior
    # y, con N fotos subidas, el libro solo aprovechaba N-1 fotos de verdad.

    paginas.append({"tipo": "blanco", "layout": None, "fotos": [], "texto": "",
                     "qr_idx": -1, "qr_url": "", "capitulo": None})

    # -- CAPITULOS --
    paginas_por_capitulo = []
    if caso_reparto == "B":
        listas_fotos_capitulos = []
        for cap in diseño.get("capitulos", []):
            nombres_cap = cap.get("fotos", [])
            fotos_cap_tmp = [fotos_dict[n] for n in nombres_cap if n in fotos_dict]
            listas_fotos_capitulos.append(fotos_cap_tmp)
        paginas_por_capitulo = calcular_paginas_por_capitulo(listas_fotos_capitulos, paginas_objetivo)

    for idx_cap, cap in enumerate(diseño.get("capitulos", [])):
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

        fotos_titulo = fotos_cap[:2]
        rutas_titulo = [f for f in fotos_titulo if f.get("ruta")]
        if not rutas_titulo and fotos_cap:
            rutas_titulo = [fotos_cap[0]]

        paginas.append({
            "tipo": "titulo_capitulo", "layout": None,
            "fotos": [{"nombre": f["nombre"], "ruta": f["ruta"]} for f in rutas_titulo],
            "texto": "", "qr_idx": -1, "qr_url": "",
            "capitulo": {"titulo": tit_cap, "subtitulo": sub_cap, "variante": capitulo_variante},
        })
        capitulo_variante += 1

        fotos_resto = fotos_cap[len(fotos_titulo):]
        if fotos_resto:
            if caso_reparto == "B":
                paginas_asignadas_cap = max(1, paginas_por_capitulo[idx_cap] - 1)
                paginas_calc = paginas_con_tope_estricto(
                    fotos_resto, paginas_asignadas_cap, qr_map, variante_inicio=capitulo_variante
                )
            else:
                paginas_calc = paginas_para_grupo(fotos_resto, qr_map, variante_inicio=capitulo_variante)

            for pg in paginas_calc:
                paginas.append(_pagina_contenido_desde_calculo(pg, AW, AH))
                capitulo_variante += 1

    # -- FOTOS SOBRANTES --
    # Aqui caen tambien los fotogramas generados para vídeos sin foto
    # cercana en el tiempo (la IA no los conocia al decidir los capitulos,
    # asi que nunca estan en ningun capitulo - siempre acaban aqui).
    fotos_sobrantes = [f for nombre, f in fotos_dict.items()
                       if nombre not in fotos_usadas and f.get("ruta") and os.path.exists(f.get("ruta", ""))]
    if fotos_sobrantes:
        fotos_sobrantes.sort(key=lambda x: x["fecha"])
        if caso_reparto == "B":
            # OJO: el presupuesto para las sobrantes tiene que ser lo que
            # QUEDA del objetivo total, no el objetivo completo otra vez -
            # si los capitulos ya usaron, por ejemplo, 25 de 30 paginas, a
            # las sobrantes solo les quedan 5, no 30 de nuevo (si no, el
            # libro se podia ir muy por encima de las paginas contratadas).
            paginas_usadas_hasta_ahora = len(paginas) - 2  # -2: portada y guarda en blanco
            paginas_restantes = max(1, paginas_objetivo - paginas_usadas_hasta_ahora)
            paginas_calc = paginas_con_tope_estricto(
                fotos_sobrantes, paginas_restantes, qr_map, variante_inicio=capitulo_variante
            )
        else:
            paginas_calc = paginas_para_grupo(fotos_sobrantes, qr_map, variante_inicio=capitulo_variante)
        for pg in paginas_calc:
            pagina_dict = _pagina_contenido_desde_calculo(pg, AW, AH)
            if pagina_dict["fotos"]:
                paginas.append(pagina_dict)

    # -- RED DE SEGURIDAD: no quedarse corto del mínimo contratado --
    # Se aplica siempre (caso A y B) - el mínimo de páginas es un
    # compromiso comercial, no depende de si sobran o faltan fotos.
    paginas = _completar_paginas_contenido_hasta_objetivo(paginas, paginas_objetivo, qr_map, AW, AH)

    # -- GUARDA EN BLANCO FINAL --
    # Gelato exige que la última página del PDF (interior de la
    # contraportada) vaya en blanco y sin imprimir, igual que la guarda del
    # principio (pagina 2).
    paginas.append({"tipo": "blanco", "layout": None, "fotos": [], "texto": "",
                     "qr_idx": -1, "qr_url": "", "capitulo": None})

    # -- CONTRAPORTADA --
    # Se mantiene como página aparte SOLO para que el editor la enseñe
    # (portada, blanco, contenido, blanco, contraportada - así el cliente
    # ve todo el libro tal cual). El lomo NO se enseña como página suelta
    # (no tiene sentido como página cuadrada en el editor). Para el PDF
    # final de imprenta, esta página NO se dibuja aparte: su contenido se
    # fusiona con la portada y el lomo en un único spread de cubierta con
    # las medidas de Gelato (ver dibujar_cubierta_spread /
    # dibujar_pdf_desde_estructura, que se salta este tipo a propósito).
    paginas.append({"tipo": "contraportada", "layout": None, "fotos": [], "texto": "",
                     "qr_idx": -1, "qr_url": "", "capitulo": None})

    _evitar_fotograma_solo(paginas, AW, AH)

    return paginas


def _evitar_fotograma_solo(paginas, AW, AH):
    """
    Un fotograma de vídeo nunca debe ocupar media página ni una página
    entera - como mucho un cuarto de página, siempre compartiendo hueco
    con otras 3 fotos como mínimo. Cubre dos casos:
    1) El fotograma quedó solo en su propia página (layout "1", página
       entera).
    2) El fotograma quedó emparejado con una sola foto más (layout "2H"
       o "2V", media página cada una).
    En los dos casos se saca el fotograma de ahí y se fusiona con la
    página de contenido más cercana que tenga hueco libre (menos de 4
    fotos) - si en el caso 2 queda una foto normal sola en la página
    original, esa se queda a página completa (layout "1"), que es justo
    donde SÍ debe estar una foto de verdad.
    """
    LAYOUT_POR_N = {2: "2H", 3: "3", 4: "4"}
    i = 0
    while i < len(paginas):
        pg = paginas[i]
        fotos_pg = pg.get("fotos", [])
        es_pagina_entera = (pg.get("tipo") == "contenido" and pg.get("layout") == "1"
                             and len(fotos_pg) == 1 and fotos_pg[0].get("es_frame_video"))
        indice_frame_en_media = None
        if (pg.get("tipo") == "contenido" and pg.get("layout") in ("2H", "2V") and len(fotos_pg) == 2):
            for idx_f, f in enumerate(fotos_pg):
                if f.get("es_frame_video"):
                    indice_frame_en_media = idx_f
                    break

        if not es_pagina_entera and indice_frame_en_media is None:
            i += 1
            continue

        if es_pagina_entera:
            frame_foto = fotos_pg[0]
            frame_qr_idx = pg.get("qr_idx", -1)
            frame_qr_url = pg.get("qr_url", "")
        else:
            frame_foto = fotos_pg[indice_frame_en_media]
            frame_qr_idx = pg.get("qr_idx", -1) if pg.get("qr_idx") == indice_frame_en_media else -1
            frame_qr_url = pg.get("qr_url", "") if frame_qr_idx != -1 else ""

        destino = None
        for j in (i - 1, i + 1):
            if 0 <= j < len(paginas):
                candidata = paginas[j]
                if (candidata is not pg and candidata.get("tipo") == "contenido"
                        and len(candidata.get("fotos", [])) < 4):
                    destino = candidata
                    break

        if destino is None:
            i += 1
            continue

        if frame_qr_idx != -1 and frame_qr_url and destino.get("qr_idx", -1) == -1:
            destino["qr_idx"] = len(destino["fotos"])
            destino["qr_url"] = frame_qr_url
        destino["fotos"].append(frame_foto)
        nuevo_n = len(destino["fotos"])
        destino["layout"] = LAYOUT_POR_N.get(nuevo_n, "4")

        # El hueco cambió de tamaño (antes página entera o media, ahora un
        # cuarto o menos) - hay que recalcular qué ventana de la foto
        # enseñar para el hueco nuevo, si no el recorte quedaría pensado
        # para la proporción equivocada.
        try:
            pie_h = 8 if destino.get("texto") else 0
            zonas_nuevas = calcular_zonas_layout_mm(destino["layout"], nuevo_n, AW, AH, pie_h=pie_h)
            idx_frame = nuevo_n - 1
            if idx_frame < len(zonas_nuevas):
                _, _, w_mm, h_mm = zonas_nuevas[idx_frame]
                frame_foto["recorte_frac"] = calcular_ventana_recorte_frac(frame_foto["ruta"], w_mm, h_mm)
        except Exception as e:
            log(f"No se pudo recalcular el recorte del fotograma fusionado: {e}", "!")

        if es_pagina_entera:
            paginas.pop(i)
            continue  # no incrementamos i - la lista se acaba de acortar
        else:
            # Queda 1 foto normal sola en la página original - pasa a
            # página completa (layout "1"), que es justo el sitio
            # correcto para una foto de verdad sola.
            foto_restante = fotos_pg[1 - indice_frame_en_media]
            pg["fotos"] = [foto_restante]
            pg["layout"] = "1"
            if pg.get("qr_idx", -1) == (1 - indice_frame_en_media):
                pg["qr_idx"] = 0
            elif pg.get("qr_idx", -1) != -1:
                pg["qr_idx"] = -1
                pg["qr_url"] = ""
            try:
                pie_h = 8 if pg.get("texto") else 0
                zonas_una = calcular_zonas_layout_mm("1", 1, AW, AH, pie_h=pie_h)
                _, _, w_mm, h_mm = zonas_una[0]
                foto_restante["recorte_frac"] = calcular_ventana_recorte_frac(foto_restante["ruta"], w_mm, h_mm)
            except Exception as e:
                log(f"No se pudo recalcular el recorte de la foto que quedó sola: {e}", "!")

        i += 1


def _pagina_contenido_desde_calculo(pg, AW, AH):
    """Convierte el dict interno que devuelven paginas_para_grupo /
    paginas_para_capitulo_caso_b / paginas_con_tope_estricto al formato
    JSON-serializable comun de la estructura del libro. Calcula tambien la
    ventana de recorte (con caras) de cada foto para su hueco - esto es lo
    que hace que el editor y el PDF final enseñen siempre el mismo trozo de
    cada foto, en vez de calcularlo cada uno por su lado."""
    fotos_out = []
    for f in pg["fotos"]:
        if isinstance(f, dict):
            ruta = f.get("ruta", "")
            nombre = f.get("nombre") or (os.path.basename(ruta) if ruta else "")
        else:
            ruta = f
            nombre = os.path.basename(ruta) if ruta else ""
        if ruta and os.path.exists(ruta):
            ancho_px, alto_px = obtener_dimensiones_px(ruta)
            es_frame = isinstance(f, dict) and f.get("es_frame_video", False)
            fotos_out.append({"nombre": nombre, "ruta": ruta, "ancho_px": ancho_px, "alto_px": alto_px,
                               "es_frame_video": es_frame})

    layout = pg["layout"]
    pie_h = 8 if pg.get("texto") else 0
    try:
        zonas = calcular_zonas_layout_mm(layout, len(fotos_out), AW, AH, pie_h=pie_h)
    except Exception:
        zonas = []
    for i, foto in enumerate(fotos_out):
        if i < len(zonas):
            _, _, w_mm, h_mm = zonas[i]
            foto["recorte_frac"] = calcular_ventana_recorte_frac(foto["ruta"], w_mm, h_mm)

    return {
        "tipo": "contenido",
        "layout": layout,
        "fotos": fotos_out,
        "texto": pg.get("texto", ""),
        "qr_idx": pg.get("qr_idx", -1),
        "qr_url": pg.get("qr_url", ""),
        "capitulo": None,
    }


def dibujar_pdf_desde_estructura(paginas, AW, AH, ruta, titulo, subtitulo, portada_elegida=None, do_wm=False, formato="2128"):
    """
    Dibuja el PDF final a partir de una ESTRUCTURA ya calculada (y,
    normalmente, ya editada por el cliente en el editor). Esta funcion NO
    decide nada de contenido - eso ya viene resuelto en la lista 'paginas'.
    Solo dibuja, pagina a pagina, igual que hacia antes generar_pdf() pero
    leyendo de datos en vez de recalcular.

    La página "portada" es especial: es el spread ENTERO de cubierta
    (contraportada+lomo+portada) y Gelato exige que tenga un tamaño de
    página distinto al resto (con sangrado, giro y ancho de lomo exactos
    para este formato y número de páginas). Por eso va en su propio
    segmento de PDF, con su propio pagesize, separado de las páginas
    interiores.
    """
    aw, ah = AW * mm, AH * mm
    log(f"Dibujando PDF desde estructura ({len(paginas)} paginas)...", "i")

    carpeta_segmentos = ruta + "_segmentos"
    os.makedirs(carpeta_segmentos, exist_ok=True)
    segmentos = []
    contador_segmento = [0]

    def abrir_segmento(pagesize=None):
        contador_segmento[0] += 1
        ruta_seg = os.path.join(carpeta_segmentos, f"seg_{contador_segmento[0]:03d}.pdf")
        segmentos.append(ruta_seg)
        cnv = canvas.Canvas(ruta_seg, pagesize=(pagesize or (aw, ah)))
        cnv.setTitle(titulo)
        cnv.setAuthor("Bookeo - mibookeo.es")
        return cnv

    def cerrar_segmento(cnv, etiqueta=""):
        cnv.save()
        log_memoria(f"segmento cerrado ({etiqueta})")
        gc.collect()

    editor_portada = portada_elegida.get("editor") if portada_elegida else None

    # Nº de páginas interiores reales (todo menos la cubierta y las 2
    # guardas en blanco) - Gelato lo necesita para calcular el ancho del
    # lomo, así que hay que contarlo ANTES de dibujar nada.
    paginas_interiores = sum(1 for p in paginas if p.get("tipo") not in ("portada", "blanco", "contraportada"))

    MAX_PAGINAS_POR_SEGMENTO = 8  # mismo criterio de memoria que antes (por capitulo)
    c = None
    paginas_en_segmento = 0

    for pagina in paginas:
        tipo = pagina.get("tipo")

        if tipo == "portada":
            # Segmento propio, con el pagesize especial del spread de
            # cubierta - no se mezcla con las páginas interiores (aw, ah).
            if c is not None:
                cerrar_segmento(c, "antes de la cubierta")
                c = None

            dims = obtener_dimensiones_cubierta_gelato(formato, paginas_interiores)
            wrap = dims["wraparoundInsideSize"]
            pagesize_cubierta = (wrap["width"] * mm, wrap["height"] * mm)

            c_cubierta = abrir_segmento(pagesize=pagesize_cubierta)
            ruta_foto = pagina["fotos"][0]["ruta"] if pagina.get("fotos") else ""
            dibujar_cubierta_spread(c_cubierta, dims, ruta_foto, titulo, subtitulo, do_wm, editor=editor_portada)
            c_cubierta.showPage()
            cerrar_segmento(c_cubierta, "cubierta")
            continue

        if tipo == "contraportada":
            # Solo existe para que el editor la enseñe como página aparte -
            # su contenido ya se dibujó dentro del spread de la portada
            # (ver arriba). No se dibuja como página de imprenta.
            continue

        if c is None:
            c = abrir_segmento()
            paginas_en_segmento = 0

        if tipo == "blanco":
            dibujar_pagina_blanca(c, AW, AH)

        elif tipo == "titulo_capitulo":
            cap = pagina.get("capitulo") or {}
            fotos_cap = [f for f in pagina.get("fotos", []) if f.get("ruta")]

            # Igual que en "contenido": si el cliente tocó alguna de las
            # fotos de esta página (moverla/recortarla en el editor), se
            # respeta tal cual en vez del recorte automático de siempre -
            # así el PDF final coincide con lo que el editor le enseñó,
            # incluyendo el NÚMERO de fotos (antes el editor solo pintaba
            # 1 aunque hubiera 2, y el PDF sacaba las 2 - desincronizados).
            modo_libre = any(f.get("editor_foto") for f in fotos_cap) or bool(pagina.get("stickers"))

            if modo_libre:
                canvas_w = pagina.get("canvas_w") or AW
                canvas_h = pagina.get("canvas_h") or AH
                aw_pt, ah_pt = AW * mm, AH * mm
                bg_blanco(c, AW, AH)

                for f in fotos_cap:
                    info = f.get("editor_foto")
                    if info:
                        ok = dibujar_foto_editor_libre(c, f["ruta"], info, canvas_w, canvas_h, aw_pt, ah_pt)
                        if not ok:
                            foto_zona(c, f["ruta"], MG, MG, AW - MG * 2, AH * 0.70 - MG)
                    else:
                        # Esta foto en concreto no la toco el cliente pero
                        # su hermana si (por eso modo_libre=True) - se
                        # coloca con el recorte automatico de siempre para
                        # no dejarla en blanco.
                        foto_zona(c, f["ruta"], MG, MG, AW - MG * 2, AH * 0.70 - MG)

                dibujar_stickers_pdf(c, AW, AH, pagina.get("stickers"), canvas_w, canvas_h)
                dibujar_formas_pdf(c, AW, AH, pagina.get("formas"), canvas_w, canvas_h)

                _dibujar_texto_titulo_capitulo(
                    c, AW, AH, canvas_w, canvas_h,
                    cap.get("titulo_info"), cap.get("subtitulo_info"),
                    cap.get("titulo", ""), cap.get("subtitulo", ""),
                )
                if do_wm:
                    wm(c, AW * mm, AH * mm)
            else:
                rutas_titulo = [f["ruta"] for f in fotos_cap]
                layout_titulo_capitulo(c, AW, AH, cap.get("titulo", ""), cap.get("subtitulo", ""),
                                        rutas_titulo, cap.get("variante", 0), do_wm)

        elif tipo == "contenido":
            fotos_descartadas = [f.get("nombre", "?") for f in pagina.get("fotos", []) if not (f.get("ruta") and os.path.exists(f["ruta"]))]
            if fotos_descartadas:
                log(f"Foto(s) descartada(s) en página de contenido (no se encontró el archivo): {fotos_descartadas}", "!")
            fotos_validas = [f for f in pagina.get("fotos", []) if f.get("ruta") and os.path.exists(f["ruta"])]
            if not fotos_validas:
                c.showPage()
                continue

            # Si alguna foto de la página tiene datos de edición libre
            # (posición/escala/recorte tipo Fabric.js, igual que la
            # portada), se dibuja TODA la página en modo libre - cada foto
            # donde y como el cliente la dejó, en vez del layout preestablecido.
            # También entra en modo libre si el cliente personalizó el
            # texto (posición/fuente/color) aunque no haya tocado ninguna
            # foto - si no, ese estilo se guardaba pero nunca se dibujaba.
            texto_info = pagina.get("texto_info")
            stickers_pagina = pagina.get("stickers")
            modo_libre = any(f.get("editor_foto") for f in fotos_validas) or bool(texto_info) or bool(stickers_pagina)

            if modo_libre:
                canvas_w = pagina.get("canvas_w") or AW
                canvas_h = pagina.get("canvas_h") or AH
                aw_pt, ah_pt = AW * mm, AH * mm
                bg_blanco(c, AW, AH, pagina.get("fondo"))

                for f in fotos_validas:
                    info = f.get("editor_foto")
                    if info:
                        ok = dibujar_foto_editor_libre(c, f["ruta"], info, canvas_w, canvas_h, aw_pt, ah_pt)
                        if not ok:
                            m = MARGEN_FOTO_COMPLETA
                            foto_zona(c, f["ruta"], m, m, AW - m * 2, AH - m * 2)
                    else:
                        # Esta foto no se toco pero otra cosa de la pagina
                        # si (otra foto, o el texto) - se coloca con el
                        # recorte automatico para no dejarla en blanco.
                        m = MARGEN_FOTO_COMPLETA
                        foto_zona(c, f["ruta"], m, m, AW - m * 2, AH - m * 2)

                dibujar_stickers_pdf(c, AW, AH, stickers_pagina, canvas_w, canvas_h)
                dibujar_formas_pdf(c, AW, AH, pagina.get("formas"), canvas_w, canvas_h)

                if pagina.get("qr_url"):
                    qr_pos = pagina.get("qr_pos")
                    if qr_pos:
                        centro_mm = qr_pos_a_centro_mm(qr_pos, AW, AH)
                    else:
                        s = QR_MM + MARGEN_FOTO_COMPLETA
                        centro_mm = (AW - s / 2, AH - s / 2)  # esquina superior derecha por defecto
                    # qr_size llega como fracción del ancho del canvas del
                    # editor (0.15 = 15% del ancho de página, por ejemplo)
                    # - se convierte a mm reales de la página para que el
                    # tamaño que eligió el cliente se respete en el PDF.
                    qr_size_frac = pagina.get("qr_size")
                    tamano_mm = qr_size_frac * AW if qr_size_frac else None
                    dibujar_qr_sobre_foto(c, 0, 0, AW, AH, pagina.get("qr_url"), None,
                                           centro_custom_mm=centro_mm, tamano_custom_mm=tamano_mm)

                if texto_info:
                    _dibujar_texto_editor(c, AW, AH, canvas_w, canvas_h, texto_info, pagina.get("texto", ""))
                else:
                    texto_pie(c, AW, pagina.get("texto", ""))
                if do_wm:
                    wm(c, AW * mm, AH * mm)

            else:
                # Camino de siempre: layout preestablecido que propuso la
                # IA, tal cual, sin que el cliente lo haya tocado.
                rutas = [f["ruta"] for f in fotos_validas]
                recortes = [f.get("recorte_frac") for f in fotos_validas]
                layout = pagina.get("layout")
                kwargs = dict(fotos_rutas=rutas, qr_idx=pagina.get("qr_idx", -1),
                              qr_url=pagina.get("qr_url", ""), pie=pagina.get("texto", ""), do_wm=do_wm,
                              qr_pos=pagina.get("qr_pos"), fondo=pagina.get("fondo"), recortes=recortes)
                if layout == "1":
                    layout_1(c, AW, AH, **kwargs)
                elif layout == "2H":
                    layout_2H(c, AW, AH, **kwargs)
                elif layout == "2V":
                    layout_2V(c, AW, AH, **kwargs)
                elif layout == "3":
                    layout_3(c, AW, AH, **kwargs)
                elif layout == "4":
                    layout_4(c, AW, AH, **kwargs)

        else:
            continue

        c.showPage()
        paginas_en_segmento += 1

        if paginas_en_segmento >= MAX_PAGINAS_POR_SEGMENTO:
            cerrar_segmento(c, f"segmento {contador_segmento[0]}")
            c = None

    if c is not None:
        cerrar_segmento(c, "final")

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
    return ruta


def asignar_frames_video_a_capitulos(diseño, fotos_dict):
    """
    Asigna cada fotograma de vídeo (es_frame_video=True) a su capítulo.

    Primero calcula, por fecha, cuáles son los 1-3 capítulos candidatos más
    cercanos en el tiempo. Si solo hay un candidato claro (o ninguno tiene
    fecha de referencia), se usa ese por fecha sin más - no hace falta
    gastar una llamada a la IA para algo obvio.

    EXCEPCIÓN IMPORTANTE: si la fecha del vídeo no es de fiar
    (fuente_fecha == "modificacion", es decir, no venía ni de EXIF ni del
    nombre del archivo) - el caso típico de un vídeo que pasó por
    "Reducir / Unir vídeo": ese proceso descarga un archivo NUEVO, que
    pierde la fecha real de grabación y se queda con la fecha de cuando se
    volvió a subir (normalmente HOY, la más reciente de todo el álbum).
    Comparar por fecha en ese caso es peor que inútil: casi siempre cae en
    el ÚLTIMO capítulo, sea o no el que le corresponde de verdad. Por eso
    aquí la fecha se ignora del todo y se fuerza la comparación por imagen
    contra TODOS los capítulos, sin excepción.

    Si hay AMBIGÜEDAD REAL entre varios capítulos con fechas parecidas
    (el caso típico: una boda, donde preparativos/ceremonia/convite pueden
    ser el mismo día pero visualmente muy distintos), se le manda el
    fotograma del vídeo + una miniatura de cada capítulo candidato a Claude,
    para que decida por imagen igual que hace con el resto del libro
    (reconoce lugares, ropa, etc.) - no solo por fecha.

    Sin esto, un vídeo sin foto cercana en el tiempo acabaría siempre en el
    grupo de "sobrantes" al final del libro, desconectado de su momento
    real. Con esto, la página de su capítulo correcto simplemente ajusta
    (3 fotos -> 4) en vez de crear una página aparte al final.

    No hace nada en modo "sin capítulos" (diseño["capitulos"] vacío).
    """
    capitulos = diseño.get("capitulos", [])
    if not capitulos:
        return

    fechas_capitulo = []
    for cap in capitulos:
        fechas = [fotos_dict[n]["fecha"] for n in cap.get("fotos", []) if n in fotos_dict]
        if fechas:
            fecha_media = min(fechas) + (max(fechas) - min(fechas)) / 2
        else:
            fecha_media = None
        fechas_capitulo.append(fecha_media)

    hay_fechas_capitulo = any(f is not None for f in fechas_capitulo)

    # Ventana de ambiguedad: si dos o mas capitulos quedan a menos de 12h de
    # diferencia entre si respecto al vídeo, no está claro por fecha sola
    VENTANA_AMBIGUEDAD_SEGUNDOS = 12 * 3600

    for nombre, foto in fotos_dict.items():
        if not foto.get("es_frame_video"):
            continue
        if any(nombre in cap.get("fotos", []) for cap in capitulos):
            continue  # por si acaso ya estuviera (no deberia pasar)

        fecha_no_fiable = foto.get("fuente_fecha") == "modificacion"

        if fecha_no_fiable:
            # La fecha no sirve de nada aqui - se compara por imagen contra
            # TODOS los capitulos con foto de referencia, no solo los
            # "cercanos en fecha" (esa cercania seria falsa de todas formas).
            candidatos = [idx for idx, cap in enumerate(capitulos) if cap.get("fotos")]
            idx_elegido = None
            if len(candidatos) > 1:
                idx_elegido = asignar_frame_por_ia(foto, capitulos, candidatos, fotos_dict)
            if idx_elegido is None and candidatos:
                # Si la IA no responde por lo que sea, mejor meterlo en
                # cualquier capitulo con contenido que dejarlo caer a
                # "sobrantes" al final, que sería igual de arbitrario.
                idx_elegido = candidatos[0]
            if idx_elegido is not None:
                capitulos[idx_elegido].setdefault("fotos", []).append(nombre)
                log(f"Fotograma de vídeo '{nombre}' (fecha no fiable, viene de un vídeo "
                    f"re-procesado) asignado POR IMAGEN al capítulo "
                    f"'{capitulos[idx_elegido].get('titulo', '')}'", "i")
            continue

        if not hay_fechas_capitulo:
            continue  # ningun capitulo tiene fecha de referencia, no hay con que comparar

        # Distancia (en segundos) del vídeo a cada capítulo con fecha
        distancias = []
        for idx, fecha_media in enumerate(fechas_capitulo):
            if fecha_media is None:
                continue
            diff = abs((foto["fecha"] - fecha_media).total_seconds())
            distancias.append((diff, idx))
        distancias.sort(key=lambda x: x[0])

        if not distancias:
            continue  # cae en "sobrantes" como red de seguridad

        mejor_idx = distancias[0][1]

        # ¿Hay ambigüedad real? (otro capítulo casi igual de cerca)
        candidatos_ambiguos = [idx for diff, idx in distancias if diff <= distancias[0][0] + VENTANA_AMBIGUEDAD_SEGUNDOS]
        if len(candidatos_ambiguos) > 1:
            idx_ia = asignar_frame_por_ia(foto, capitulos, candidatos_ambiguos, fotos_dict)
            if idx_ia is not None:
                mejor_idx = idx_ia

        capitulos[mejor_idx].setdefault("fotos", []).append(nombre)
        log(f"Fotograma de vídeo '{nombre}' asignado al capítulo "
            f"'{capitulos[mejor_idx].get('titulo', '')}'", "i")


def asignar_frame_por_ia(foto_frame, capitulos, indices_candidatos, fotos_dict):
    """
    Cuando la fecha no basta para decidir el capítulo de un fotograma de
    vídeo (varios capítulos casi igual de cerca en el tiempo), se le manda
    a Claude el fotograma + una foto representativa de cada capítulo
    candidato, y que decida cuál encaja mejor por imagen (lugar, ropa,
    ambiente) - igual que ya hace la IA para el resto del libro.

    Devuelve el índice del capítulo elegido, o None si algo falla (en cuyo
    caso el llamador se queda con el más cercano por fecha).
    """
    try:
        cli = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

        contenido = [{"type": "text", "text":
            "Este es un fotograma de un vídeo de un álbum de fotos. Tengo varios "
            "capítulos candidatos por fecha, pero la fecha no es suficiente para "
            "decidir. Mira la imagen del fotograma y las miniaturas de cada "
            "capítulo (lugar, ropa, ambiente) y dime a qué capítulo pertenece de "
            "verdad.\n\nFOTOGRAMA DEL VÍDEO:"}]

        def añadir_miniatura(ruta):
            img = Image.open(ruta).convert("RGB")
            img.thumbnail((150, 150), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=70)
            b64 = base64.standard_b64encode(buf.getvalue()).decode()
            contenido.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})

        añadir_miniatura(foto_frame["ruta"])

        opciones_validas = []
        for idx in indices_candidatos:
            cap = capitulos[idx]
            nombres_fotos_cap = cap.get("fotos", [])
            if not nombres_fotos_cap:
                continue
            foto_rep = fotos_dict.get(nombres_fotos_cap[0])
            if not foto_rep or not foto_rep.get("ruta") or not os.path.exists(foto_rep["ruta"]):
                continue
            contenido.append({"type": "text", "text": f"\nCAPÍTULO {idx} - \"{cap.get('titulo', '')}\":"})
            añadir_miniatura(foto_rep["ruta"])
            opciones_validas.append(idx)

        if len(opciones_validas) < 2:
            return None  # no hay suficientes candidatos con foto de verdad para comparar

        contenido.append({"type": "text", "text":
            f"\n¿A cuál de estos capítulos pertenece el fotograma? "
            f"Responde SOLO con el número del capítulo (uno de: {opciones_validas}), nada más."})

        resp = cli.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=10,
            messages=[{"role": "user", "content": contenido}],
        )
        texto = resp.content[0].text.strip()
        m = re.search(r"\d+", texto)
        if not m:
            return None
        idx_elegido = int(m.group())
        return idx_elegido if idx_elegido in opciones_validas else None

    except Exception as e:
        log(f"No se pudo asignar fotograma por IA, se usa fecha: {e}", "!")
        return None

        if mejor_idx is not None:
            capitulos[mejor_idx].setdefault("fotos", []).append(nombre)
            log(f"Fotograma de vídeo '{nombre}' asignado al capítulo "
                f"'{capitulos[mejor_idx].get('titulo', '')}'", "i")
        # si ningun capitulo tiene fecha de referencia (raro), se queda sin
        # asignar y cae en "sobrantes" como red de seguridad


def preparar_datos_libro(diseño, fotos, videos_rutas, qr_urls, portada_elegida,
                          carpeta_temp, formato="2128", orientacion="v",
                          fotos_r2=None, videos_r2=None, pedido_id=None,
                          desde_cero=False):
    """
    Paso comun antes de calcular la estructura del libro (tanto si es para
    el editor como para el PDF final directo): descarga fotos/videos de R2,
    empareja los QR con la foto mas cercana en el tiempo, extrae fotograma
    si hace falta portada de video. Devuelve todo lo necesario ya listo.
    """
    fotos_r2 = fotos_r2 or {}
    videos_r2 = videos_r2 or {}

    carpeta_fuente = os.path.join(carpeta_temp, "_fuente")
    os.makedirs(carpeta_fuente, exist_ok=True)

    fotos_reconvertidas = []
    for f in fotos:
        f_copia = dict(f)
        if isinstance(f_copia.get("fecha"), str):
            f_copia["fecha"] = datetime.datetime.fromisoformat(f_copia["fecha"])
        fotos_reconvertidas.append(f_copia)
    fotos = fotos_reconvertidas

    if desde_cero:
        # "Crear desde cero": en este paso no se coloca ninguna foto de
        # forma automatica (las paginas salen completamente vacias), asi
        # que ningun archivo local hace falta todavia aqui - la galeria
        # de fotos del editor usa URLs de R2 directas, no necesita que se
        # haya descargado nada a este disco. Descargar las 80 fotos, una
        # a una, solo para no usar ninguna era el cuello de botella real
        # de "desde cero": con esto, este paso pasa de fácil un minuto a
        # ser practicamente instantaneo.
        pass
    else:
        def _descargar_una_foto(f):
            nombre = f.get("nombre")
            if not (nombre and nombre in fotos_r2):
                return
            destino = os.path.join(carpeta_fuente, nombre)
            if not os.path.exists(destino):
                try:
                    descargar_de_r2(fotos_r2[nombre], destino)
                except Exception as e:
                    log(f"No se pudo descargar {nombre} de R2: {e}", "!")
                    return
            f["ruta"] = destino

        # 16 descargas a la vez en vez de una detrás de otra - aquí sí
        # hacen falta los archivos de verdad (deteccion de caras, calidad
        # de impresion...), pero no hay motivo para esperarlas en fila.
        with ThreadPoolExecutor(max_workers=16) as executor:
            list(executor.map(_descargar_una_foto, fotos))

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

    if portada_elegida and portada_elegida.get("foto_personalizada_r2"):
        nombre_custom = os.path.basename(portada_elegida.get("foto_personalizada_ruta", "portada_custom.jpg"))
        destino_custom = os.path.join(carpeta_fuente, nombre_custom)
        try:
            descargar_de_r2(portada_elegida["foto_personalizada_r2"], destino_custom)
            portada_elegida["foto_personalizada_ruta"] = destino_custom
        except Exception as e:
            log(f"No se pudo descargar portada personalizada de R2: {e}", "!")
            portada_elegida["foto_personalizada_ruta"] = None

    # Modo blanco: puede haber varias fotos en portada_elegida["editor"]
    # ["fotos_blanco"] (posición/tamaño) emparejadas por "nombre" con
    # portada_elegida["fotos_blanco_archivos"] (rutas/R2) - cada worker
    # necesita su propia copia local, igual que con la personalizada única.
    if portada_elegida and portada_elegida.get("fotos_blanco_archivos"):
        rutas_refrescadas = {}
        for nombre, info in portada_elegida["fotos_blanco_archivos"].items():
            clave_r2 = info.get("r2")
            if not clave_r2:
                continue
            destino = os.path.join(carpeta_fuente, nombre)
            try:
                descargar_de_r2(clave_r2, destino)
                rutas_refrescadas[nombre] = destino
            except Exception as e:
                log(f"No se pudo descargar foto de portada en blanco '{nombre}' de R2: {e}", "!")
        portada_elegida["fotos_blanco_rutas"] = rutas_refrescadas
        # Se inyecta la ruta ya refrescada directamente en cada entrada de
        # editor["fotos_blanco"] (por "nombre") para que dibujar_portada_editor()
        # no necesite un parámetro aparte - todo lo que hace falta para
        # dibujar cada foto ya viaja junto en el propio "editor".
        if portada_elegida.get("editor") and portada_elegida["editor"].get("fotos_blanco"):
            for entrada in portada_elegida["editor"]["fotos_blanco"]:
                entrada["ruta"] = rutas_refrescadas.get(entrada.get("nombre"))

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
                        # OJO: se hereda la fiabilidad real de la fecha del
                        # vídeo (exif / nombre / modificacion), NO un valor
                        # fijo. Un vídeo que pasó por "Reducir / Unir vídeo"
                        # pierde su fecha real de grabación y se re-descarga
                        # con la fecha de "hoy" (fuente_fecha="modificacion")
                        # - eso hay que saberlo para no fiarse de esa fecha
                        # al asignar el capítulo (ver asignar_frames_video_a_capitulos).
                        fotos.append({"ruta": frame, "fecha": v["fecha"], "nombre": nb_frame,
                                      "fuente_fecha": v.get("fuente_fecha", "modificacion"),
                                      "es_frame_video": True})
                        qr_map[nb_frame] = qr_urls[nombre_v]
                        fotos.sort(key=lambda x: x["fecha"])

                        # El fotograma se crea en el disco del worker, pero
                        # el editor (que corre en el navegador del cliente)
                        # necesita una URL de verdad para poder enseñarlo -
                        # sin subirlo a R2, el editor no tiene nada que
                        # mostrar ahí (por eso no se veía).
                        if pedido_id and fotos_r2 is not None:
                            try:
                                clave_r2 = subir_a_r2(frame, pedido_id, nb_frame)
                                fotos_r2[nb_frame] = clave_r2
                            except Exception as e:
                                log(f"No se pudo subir el fotograma '{nb_frame}' a R2: {e}", "!")

    fotos_dict = {f["nombre"]: f for f in fotos}

    # Los fotogramas de vídeo (es_frame_video) se acaban de crear AHORA,
    # despues de que la IA decidiera los capitulos - por eso no estan en
    # ningun diseño["capitulos"][i]["fotos"]. Sin este paso, calcular_estructura_libro()
    # los mandaria todos al grupo de "sobrantes" al final del libro, fuera
    # de su capitulo real (ej: un vídeo de la playa apareceria al final en
    # vez de ir junto con las demas fotos de playa). Se asignan aqui al
    # capitulo cuya fecha les corresponde, para que ajusten esa pagina
    # (3 fotos -> 4, por ejemplo) en vez de crear una pagina aparte al final.
    asignar_frames_video_a_capitulos(diseño, fotos_dict)

    titulo_final = portada_elegida.get("titulo") or diseño.get("titulo", "Mi Album")
    subtitulo = portada_elegida.get("subtitulo", "")

    return {
        "AW": AW, "AH": AH,
        "fotos_dict": fotos_dict,
        "qr_map": qr_map,
        "titulo_final": titulo_final,
        "subtitulo": subtitulo,
        "portada_elegida": portada_elegida,
    }


def calcular_estructura_libro_completo(diseño, fotos, videos_rutas, qr_urls, portada_elegida,
                                        carpeta_temp, formato="2128", orientacion="v",
                                        caso_reparto="A", paginas_objetivo=30,
                                        fotos_r2=None, videos_r2=None, pedido_id=None,
                                        desde_cero=False):
    """
    Punto de entrada de la FASE DE CALCULO (rapida, sin dibujar PDF).
    Esto es lo que llamara la nueva tarea de Celery para ir publicando cada
    pagina por WebSocket segun se van calculando, antes de que el cliente
    edite nada. Devuelve un dict con la estructura completa + metadatos
    necesarios para luego dibujar el PDF final.
    """
    os.makedirs(carpeta_temp, exist_ok=True)
    datos = preparar_datos_libro(diseño, fotos, videos_rutas, qr_urls, portada_elegida,
                                  carpeta_temp, formato, orientacion, fotos_r2, videos_r2,
                                  pedido_id=pedido_id, desde_cero=desde_cero)

    if desde_cero:
        # "Crear desde cero": sin IA, sin capítulos - solo la portada y
        # páginas vacías para que el cliente las rellene a mano.
        paginas = calcular_estructura_libro_vacia(datos["portada_elegida"], paginas_objetivo,
                                                    AW=datos["AW"], AH=datos["AH"])
    else:
        paginas = calcular_estructura_libro(diseño, datos["fotos_dict"], datos["qr_map"],
                                             datos["portada_elegida"], caso_reparto, paginas_objetivo,
                                             AW=datos["AW"], AH=datos["AH"])

    return {
        "paginas": paginas,
        "AW": datos["AW"], "AH": datos["AH"],
        "titulo": datos["titulo_final"], "subtitulo": datos["subtitulo"],
        "portada_elegida": datos["portada_elegida"],
    }


def generar_pdf_completo(diseño, fotos, videos_rutas, qr_urls, portada_elegida,
                          nombre_cliente, carpeta_sal, carpeta_temp,
                          formato="2128", orientacion="v",
                          caso_reparto="A", paginas_objetivo=30,
                          fotos_r2=None, videos_r2=None,
                          estructura_editada=None, desde_cero=False):
    """
    Genera el PDF final completo.

    Si se pasa 'estructura_editada' (la lista de paginas que devuelve el
    editor tras los cambios del cliente: fotos movidas, texto añadido...),
    se usa tal cual en vez de recalcular desde cero - asi el PDF final
    respeta exactamente lo que el cliente dejo en el editor.

    Si no se pasa (flujo actual, sin editor todavia), calcula la estructura
    igual que siempre y dibuja directo - se comporta exactamente como antes.
    """
    os.makedirs(carpeta_sal, exist_ok=True)
    os.makedirs(carpeta_temp, exist_ok=True)

    datos = preparar_datos_libro(diseño, fotos, videos_rutas, qr_urls, portada_elegida,
                                  carpeta_temp, formato, orientacion, fotos_r2, videos_r2)

    if estructura_editada is not None:
        paginas = estructura_editada
        # Las rutas locales dentro de la estructura editada vienen de OTRO
        # proceso (el worker que calculo la estructura la primera vez, para
        # el editor) y casi seguro no existen en el disco de este worker.
        # Se refrescan aqui usando el nombre de archivo contra las fotos
        # recien descargadas en este mismo proceso - asi da igual en que
        # worker de Railway se ejecute cada fase.
        carpeta_fuente_refresco = os.path.join(carpeta_temp, "_fuente")
        fotos_r2_map = fotos_r2 or {}
        for pagina in paginas:
            for foto in pagina.get("fotos", []):
                nombre = foto.get("nombre")
                if not nombre:
                    continue
                if nombre in datos["fotos_dict"] and os.path.exists(datos["fotos_dict"][nombre].get("ruta", "")):
                    foto["ruta"] = datos["fotos_dict"][nombre]["ruta"]
                elif nombre in fotos_r2_map:
                    # Último recurso: el nombre no estaba en fotos_dict (o
                    # su ruta no llegó a existir por lo que sea), pero SÍ
                    # tenemos su clave R2 directamente - se descarga aquí
                    # mismo en vez de perder la foto en silencio, que es lo
                    # que pasaba antes (una foto detrás de un QR, o
                    # cualquier otra añadida a mitad de edición, podía
                    # desaparecer del PDF final sin ningún aviso).
                    destino = os.path.join(carpeta_fuente_refresco, nombre)
                    if not os.path.exists(destino):
                        try:
                            descargar_de_r2(fotos_r2_map[nombre], destino)
                        except Exception as e:
                            log(f"No se pudo recuperar la foto '{nombre}' de R2 en el refresco final: {e}", "!")
                            continue
                    foto["ruta"] = destino
    else:
        if desde_cero:
            paginas = calcular_estructura_libro_vacia(datos["portada_elegida"], paginas_objetivo,
                                                        AW=datos["AW"], AH=datos["AH"])
        else:
            paginas = calcular_estructura_libro(diseño, datos["fotos_dict"], datos["qr_map"],
                                                 datos["portada_elegida"], caso_reparto, paginas_objetivo,
                                                 AW=datos["AW"], AH=datos["AH"])

    nb = nombre_cliente.lower().replace(" ", "_")
    # Un identificador único por cada generación - antes el nombre era
    # siempre "bookeo_{nombre}.pdf" para el mismo pedido, así que al mover
    # páginas (o cualquier otro cambio) y volver a generar el PDF, una
    # copia en caché de la generación ANTERIOR (del móvil, del navegador,
    # o incluso de R2) podía acabar sirviéndose en vez del PDF nuevo de
    # verdad - con el mismo nombre no había forma de distinguir "esta es
    # la versión de antes" de "esta es la de ahora".
    sufijo_unico = uuid.uuid4().hex[:8]
    r_final = os.path.join(carpeta_sal, f"bookeo_{nb}_{sufijo_unico}.pdf")

    dibujar_pdf_desde_estructura(paginas, datos["AW"], datos["AH"], r_final,
                                  datos["titulo_final"], datos["subtitulo"],
                                  portada_elegida=datos["portada_elegida"], do_wm=False,
                                  formato=formato)

    if os.path.exists(carpeta_temp):
        shutil.rmtree(carpeta_temp, ignore_errors=True)

    log(f"PDF listo: {r_final}", "i")
    return r_final


def generar_pdf(AW, AH, diseño, fotos_dict, qr_map, ruta, titulo, subtitulo, portada_elegida=None, do_wm=False, caso_reparto="A", paginas_objetivo=30, formato="2128"):
    """Wrapper de compatibilidad: calcula la estructura y la dibuja en un solo
    paso, igual que antes. Se mantiene por si algo externo la llama
    directamente; el codigo nuevo deberia usar calcular_estructura_libro() +
    dibujar_pdf_desde_estructura() por separado (necesario para el editor)."""
    paginas = calcular_estructura_libro(diseño, fotos_dict, qr_map, portada_elegida, caso_reparto, paginas_objetivo, AW=AW, AH=AH)
    dibujar_pdf_desde_estructura(paginas, AW, AH, ruta, titulo, subtitulo, portada_elegida=portada_elegida, do_wm=do_wm, formato=formato)
    return paginas


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