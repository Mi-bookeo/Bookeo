"""
╔══════════════════════════════════════════════════════╗
║           BOOKEO MVP · Creador de libros             ║
║         mibookeo.es · Versión 3.2                    ║
╚══════════════════════════════════════════════════════╝

NUEVA LÓGICA: Python decide los layouts, la IA solo agrupa.
Sin huecos posibles. Sin QR solos. Resultados perfectos.

Tamaño de página (AW/AH) se pasa como parámetro explícito en
cada llamada — NO es variable global — para soportar varios
pedidos procesándose a la vez sin pisarse entre sí.

REQUIERE:
  pip3 install anthropic pillow reportlab opencv-python-headless "qrcode[pil]"
"""

import os, io, sys, json, base64, smtplib, datetime, re, math
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

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
        print("⚠ OpenCV: clasificador de caras no encontrado")
except ImportError:
    OPENCV_OK = False
    print("⚠ OpenCV no disponible — recorte centrado sin detección de caras")

# ═══════════════════════════════════════════════════════
#  CONFIGURACIÓN — variables de entorno de Railway
# ═══════════════════════════════════════════════════════

CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ═══════════════════════════════════════════════════════
#  MEDIDAS FIJAS (no dependen del formato)
# ═══════════════════════════════════════════════════════

MG = 10     # margen interior mm
GAP = 3     # separación entre fotos mm
QR_MM = 22  # tamaño del QR en mm

QR_URL_PRUEBA = ""  # valor de respaldo, no se usa en producción real
MIN_PPI_OK  = 180
MIN_PPI_BEST = 250

CV  = (125/255, 184/255, 152/255)
CO  = (26/255,  26/255,  46/255)
CCL = (0.96, 0.94, 0.90)

# ═══════════════════════════════════════════════════════
#  FORMATO — tamaño de página según lo elegido por el cliente
#  Se pasa como parámetro explícito (AW, AH), NUNCA como global,
#  para soportar varios pedidos en paralelo sin pisarse.
# ═══════════════════════════════════════════════════════

FORMATOS_MM = {
    "2020": (208, 208),   # 20x20cm + 4mm sangrado por lado
    "2828": (288, 288),   # 28x28cm + 4mm sangrado por lado
    "2128": (218, 288),   # 21x28cm + 4mm sangrado por lado (vertical)
}

def obtener_medidas(formato="2128", orientacion="v"):
    """
    Devuelve (AW, AH) en mm según el formato y orientación elegidos.
    Si el formato no es reconocido, usa 21x28 vertical por defecto.
    """
    ancho, alto = FORMATOS_MM.get(formato, FORMATOS_MM["2128"])
    if formato == "2128" and orientacion == "h":
        ancho, alto = alto, ancho
    return ancho, alto

# ═══════════════════════════════════════════════════════
#  LOG
# ═══════════════════════════════════════════════════════

def log(msg, e="→"):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {e} {msg}")

def set_negro(c):
    """Negro enriquecido CMYK para textos — mejor en impresión Gelato."""
    c.setFillColorCMYK(0.60, 0.60, 0.60, 1.0)

# ═══════════════════════════════════════════════════════
#  FECHAS
# ═══════════════════════════════════════════════════════

def leer_fecha(ruta):
    nombre = os.path.basename(ruta)
    try:
        img = Image.open(ruta)
        exif = img._getexif()
        if exif:
            for tid, val in exif.items():
                if ExifTags.TAGS.get(tid) in ["DateTimeOriginal","DateTime","DateTimeDigitized"] and isinstance(val,str):
                    try:
                        f = datetime.datetime.strptime(val.strip(), "%Y:%m:%d %H:%M:%S")
                        if f.year > 2000: return f, "exif"
                    except: pass
    except: pass
    for pat in [r'(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})',
                r'IMG-(\d{4})(\d{2})(\d{2})-WA\d+',
                r'(\d{4})(\d{2})(\d{2})']:
        m = re.search(pat, nombre)
        if m:
            try:
                g = m.groups()
                a,me,d = int(g[0]),int(g[1]),int(g[2])
                h = int(g[3]) if len(g)>3 else 12
                mi = int(g[4]) if len(g)>4 else 0
                if 2000<=a<=2030 and 1<=me<=12 and 1<=d<=31:
                    return datetime.datetime(a,me,d,h,mi), "nombre"
            except: pass
    return datetime.datetime.fromtimestamp(os.path.getmtime(ruta)), "modificacion"

# ═══════════════════════════════════════════════════════
#  OPENCV — RECORTE INTELIGENTE CON CARAS
# ═══════════════════════════════════════════════════════

def detectar_caras(ruta):
    """Detecta caras en una imagen y devuelve su bounding box total."""
    if not OPENCV_OK: return None
    try:
        img_pil = Image.open(ruta).convert("RGB")
        img_pil = ImageOps.exif_transpose(img_pil)
        cv_img = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
        gris = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
        gris_eq = cv2.equalizeHist(gris)
        cas = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

        det = cas.detectMultiScale(gris_eq, scaleFactor=1.05, minNeighbors=4, minSize=(20,20))
        if len(det) == 0:
            det = cas.detectMultiScale(gris_eq, scaleFactor=1.1, minNeighbors=3, minSize=(15,15))
        if len(det) == 0:
            return None

        caras = det.tolist()
        x1 = min(c[0] for c in caras)
        y1 = min(c[1] for c in caras)
        x2 = max(c[0]+c[2] for c in caras)
        y2 = max(c[1]+c[3] for c in caras)
        iw, ih = img_pil.size
        return {"x1":x1,"y1":y1,"x2":x2,"y2":y2,"iw":iw,"ih":ih,"n":len(caras)}
    except:
        return None

def recortar_con_caras(ruta, w_mm, h_mm):
    """Recorte inteligente con doble verificación de caras."""
    try:
        img = Image.open(ruta).convert("RGB")
        img = ImageOps.exif_transpose(img)
        iw, ih = img.size
        ratio_z = w_mm / h_mm
        ratio_i = iw / ih

        caras = detectar_caras(ruta)

        if caras:
            cx_cara = (caras["x1"] + caras["x2"]) / 2
            cy_cara = (caras["y1"] + caras["y2"]) / 2
            cara_h  = caras["y2"] - caras["y1"]
            cara_w  = caras["x2"] - caras["x1"]

            margen_arriba = int(cara_h * 1.5)
            y_minimo_visible = max(0, caras["y1"] - margen_arriba)
            y_maximo_visible = min(ih, caras["y2"] + int(cara_h * 0.4))

            cx = cx_cara
        else:
            cx = iw / 2
            y_minimo_visible = 0
            y_maximo_visible = ih

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

                if y0 > caras["y1"] - int(cara_h * 0.3):
                    y0 = max(0, caras["y1"] - int(cara_h * 1.5))
                    y0 = max(0, min(y0, ih - nh))

                if y0 + nh < caras["y2"]:
                    y0 = max(0, int(cy_cara - nh * 0.45))
                    y0 = max(0, min(y0, ih - nh))
            else:
                y0 = int(ih / 2 - nh / 2)
                y0 = max(0, min(y0, ih - nh))

            recorte = (0, y0, iw, y0 + nh)

        img = img.crop(recorte)

        if caras:
            log(f"  · {caras['n']} cara(s) · recorte OK {os.path.basename(ruta)}", "")

        return img

    except Exception as e:
        log(f"  · Error recortando {os.path.basename(ruta)}: {e}", "")
        try:
            img = Image.open(ruta).convert("RGB")
            return ImageOps.exif_transpose(img)
        except:
            return None

def ppi(ruta, w_mm, h_mm):
    try:
        img = Image.open(ruta)
        pw, ph = img.size
        return min(pw/(w_mm/25.4), ph/(h_mm/25.4))
    except: return 300

# ═══════════════════════════════════════════════════════
#  DIBUJAR FOTO EN ZONA
# ═══════════════════════════════════════════════════════

def foto_zona(c, ruta, x, y, w, h, check_ppi=True):
    """Dibuja foto en zona (en mm). Recorta centrando caras."""
    if not ruta or not os.path.exists(ruta):
        log(f"  · Foto no encontrada: {ruta}", "⚠")
        return
    img = recortar_con_caras(ruta, w, h)
    if img is None: return

    if check_ppi:
        p = ppi(ruta, w, h)
        if p < MIN_PPI_OK:
            log(f"  🔴 {os.path.basename(ruta)} {int(p)} PPI baja calidad", "")
        elif p < MIN_PPI_BEST:
            log(f"  🟡 {os.path.basename(ruta)} {int(p)} PPI aceptable", "")

    iw, ih = img.size
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    buf.seek(0)
    rl = ImageReader(buf)

    xm, ym, wm, hm = x*mm, y*mm, w*mm, h*mm
    c.saveState()
    p2 = c.beginPath(); p2.rect(xm, ym, wm, hm)
    c.clipPath(p2, stroke=0)
    ri = iw/ih; rz = wm/hm
    if ri > rz:
        hd=hm; wd=hm*ri; xd=xm+(wm-wd)/2; yd=ym
    else:
        wd=wm; hd=wm/ri; xd=xm; yd=ym+(hm-hd)/2
    c.drawImage(rl, xd, yd, wd, hd)
    c.restoreState()

# ═══════════════════════════════════════════════════════
#  QR — EN ESQUINA SOBRE LA FOTO
# ═══════════════════════════════════════════════════════

def dibujar_qr_sobre_foto(c, x_foto, y_foto, w_foto, h_foto, url, ruta_foto=None):
    """QR en esquina inferior derecha SOBRE la foto."""
    s = QR_MM * mm
    mg_qr = 2 * mm
    qx = (x_foto + w_foto)*mm - s - mg_qr
    qy = y_foto*mm + mg_qr

    color_pts = CO
    color_fondo = (0.96, 0.94, 0.90)
    if ruta_foto and os.path.exists(ruta_foto):
        try:
            img = Image.open(ruta_foto).convert("RGB")
            iw, ih = img.size
            zona = img.crop((int(iw*0.75), 0, iw, int(ih*0.25)))
            lum = sum(ImageStat.Stat(zona).mean[:3])/3
            if lum < 100:
                color_pts = (1.0, 1.0, 1.0)
                color_fondo = (0.06, 0.06, 0.12)
        except: pass

    pad = 1.5*mm
    c.setFillColorRGB(*color_fondo)
    c.roundRect(qx-pad, qy-pad, s+pad*2, s+pad*2, 1.5*mm, fill=1, stroke=0)

    if QRCODE_OK:
        try:
            def hex_col(t): return "#{:02x}{:02x}{:02x}".format(int(t[0]*255),int(t[1]*255),int(t[2]*255))
            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=1)
            qr.add_data(url); qr.make(fit=True)
            qri = qr.make_image(fill_color=hex_col(color_pts), back_color=hex_col(color_fondo))
            qri = qri.resize((200,200), Image.LANCZOS)
            buf = io.BytesIO(); qri.save(buf, "PNG"); buf.seek(0)
            c.drawImage(ImageReader(buf), qx, qy, s, s, mask='auto')
        except: pass
    else:
        c.setFillColorRGB(*color_pts)
        cell = s/8
        pat = [[1,1,1,1,1,1,1,0],[1,0,0,0,0,0,1,0],[1,0,1,1,1,0,1,0],
               [1,0,0,0,0,0,1,0],[1,1,1,1,1,1,1,0],[0,0,0,0,0,0,0,1],[1,0,1,0,1,1,0,1],[0,1,0,1,0,0,1,0]]
        for r in range(8):
            for col in range(8):
                if pat[r][col]:
                    c.rect(qx+col*cell, qy+(7-r)*cell, cell-0.2*mm, cell-0.2*mm, fill=1, stroke=0)

# ═══════════════════════════════════════════════════════
#  WATERMARK
# ═══════════════════════════════════════════════════════

def wm(c, aw, ah):
    c.saveState()
    c.setFillColorRGB(0.55,0.55,0.55)
    c.setFont("Helvetica-Bold", 18)
    c.translate(aw/2, ah/2); c.rotate(38)
    for i in range(-4,5):
        for j in range(-3,4):
            c.drawCentredString(i*160, j*100, "MUESTRA · mibookeo.es")
    c.restoreState()

# ═══════════════════════════════════════════════════════
#  LAYOUTS — ahora reciben AW, AH como parámetros
# ═══════════════════════════════════════════════════════

def bg_blanco(c, AW, AH):
    c.setFillColorRGB(1,1,1)
    c.rect(0, 0, AW*mm, AH*mm, fill=1, stroke=0)

def texto_pie(c, AW, texto, y_mm=MG/2+1):
    if not texto: return
    c.setFillColorRGB(*CO)
    c.setFont("Helvetica-Oblique", 2.8*mm)
    c.drawCentredString(AW*mm/2, y_mm*mm, texto)

def dibujar_portada(c, AW, AH, ruta, titulo, subtitulo, do_wm):
    bg_blanco(c, AW, AH)
    if ruta:
        foto_zona(c, ruta, 0, MG+18, AW, AH-MG-18, check_ppi=False)
    c.setFillColorRGB(1,1,1)
    c.rect(0, 0, AW*mm, (MG+18)*mm, fill=1, stroke=0)
    set_negro(c)
    c.setFont("Helvetica-Bold", 8*mm)
    c.drawCentredString(AW*mm/2, (MG+10)*mm, titulo)
    if subtitulo:
        c.setFillColorRGB(0.5,0.5,0.5)
        c.setFont("Helvetica", 3*mm)
        c.drawCentredString(AW*mm/2, (MG+4)*mm, subtitulo)
    if do_wm: wm(c, AW*mm, AH*mm)

def dibujar_lomo(c, AW, AH, titulo, color_fondo=None, do_wm=False):
    """Lomo del libro — título vertical descendente (estándar España)."""
    aw, ah = AW*mm, AH*mm
    if color_fondo:
        c.setFillColorRGB(*color_fondo)
    else:
        c.setFillColorRGB(1,1,1)
    c.rect(0, 0, aw, ah, fill=1, stroke=0)

    c.saveState()
    c.translate(aw/2, ah/2)
    c.rotate(-90)
    set_negro(c)
    c.setFont("Helvetica-Bold", 4*mm)
    c.drawCentredString(0, 0, titulo)
    c.restoreState()

    if do_wm: wm(c, aw, ah)

def dibujar_contraportada(c, AW, AH, do_wm):
    bg_blanco(c, AW, AH)
    c.setFillColorRGB(0.65,0.65,0.65)
    c.setFont("Helvetica", 2.8*mm)
    c.drawCentredString(AW*mm/2, (MG+2)*mm, "Bookeo · mibookeo.es")
    if do_wm: wm(c, AW*mm, AH*mm)

def dibujar_pagina_blanca(c, AW, AH):
    bg_blanco(c, AW, AH)

def layout_1(c, AW, AH, fotos_rutas, qr_idx, qr_url, pie, do_wm):
    bg_blanco(c, AW, AH)
    r = fotos_rutas[0]
    foto_zona(c, r, 0, 0, AW, AH)
    if qr_idx == 0:
        dibujar_qr_sobre_foto(c, 0, 0, AW, AH, qr_url, r)
    texto_pie(c, AW, pie)
    if do_wm: wm(c, AW*mm, AH*mm)

def layout_2H(c, AW, AH, fotos_rutas, qr_idx, qr_url, pie, do_wm):
    bg_blanco(c, AW, AH)
    pie_h = 8 if pie else 0
    fw = (AW - MG*2 - GAP) / 2
    fh = AH - MG*2 - pie_h
    y0 = MG + pie_h
    for i, r in enumerate(fotos_rutas[:2]):
        x = MG + i*(fw+GAP)
        foto_zona(c, r, x, y0, fw, fh)
        if qr_idx == i:
            dibujar_qr_sobre_foto(c, x, y0, fw, fh, qr_url, r)
    texto_pie(c, AW, pie)
    if do_wm: wm(c, AW*mm, AH*mm)

def layout_2V(c, AW, AH, fotos_rutas, qr_idx, qr_url, pie, do_wm):
    bg_blanco(c, AW, AH)
    pie_h = 8 if pie else 0
    fh = (AH - MG*2 - GAP - pie_h) / 2
    fw = AW - MG*2
    y0 = MG + pie_h
    for i, r in enumerate(fotos_rutas[:2]):
        y = y0 + i*(fh+GAP)
        foto_zona(c, r, MG, y, fw, fh)
        if qr_idx == i:
            dibujar_qr_sobre_foto(c, MG, y, fw, fh, qr_url, r)
    texto_pie(c, AW, pie)
    if do_wm: wm(c, AW*mm, AH*mm)

def layout_3(c, AW, AH, fotos_rutas, qr_idx, qr_url, pie, do_wm):
    bg_blanco(c, AW, AH)
    pie_h = 8 if pie else 0
    pw = AW * 0.60 - MG
    sh = (AH - MG*2 - GAP - pie_h) / 2
    sw = AW - MG - pw - GAP - MG
    zh = AH - MG*2 - pie_h
    y0 = MG + pie_h

    r0 = fotos_rutas[0]
    foto_zona(c, r0, MG, y0, pw, zh)
    if qr_idx == 0: dibujar_qr_sobre_foto(c, MG, y0, pw, zh, qr_url, r0)

    sx = MG + pw + GAP
    for i, r in enumerate(fotos_rutas[1:3]):
        y = y0 + i*(sh+GAP)
        foto_zona(c, r, sx, y, sw, sh)
        if qr_idx == i+1: dibujar_qr_sobre_foto(c, sx, y, sw, sh, qr_url, r)

    texto_pie(c, AW, pie)
    if do_wm: wm(c, AW*mm, AH*mm)

def layout_4(c, AW, AH, fotos_rutas, qr_idx, qr_url, pie, do_wm):
    bg_blanco(c, AW, AH)
    pie_h = 8 if pie else 0
    cw = (AW - MG*2 - GAP) / 2
    ch = (AH - MG*2 - GAP - pie_h) / 2
    y0 = MG + pie_h
    pos = [(MG, y0+ch+GAP), (MG+cw+GAP, y0+ch+GAP), (MG, y0), (MG+cw+GAP, y0)]
    for i, r in enumerate(fotos_rutas[:4]):
        x, y = pos[i]
        foto_zona(c, r, x, y, cw, ch)
        if qr_idx == i: dibujar_qr_sobre_foto(c, x, y, cw, ch, qr_url, r)
    texto_pie(c, AW, pie)
    if do_wm: wm(c, AW*mm, AH*mm)

def layout_titulo_capitulo(c, AW, AH, titulo, subtitulo, fotos_rutas, variante, do_wm):
    bg_blanco(c, AW, AH)
    banda_h = AH * 0.25
    foto_h = AH - banda_h - MG

    cy = (AH - banda_h/2) * mm
    set_negro(c)
    c.setFont("Helvetica-Bold", 9*mm)
    c.drawCentredString(AW*mm/2, cy+3*mm, titulo)
    if subtitulo:
        c.setFillColorRGB(0.5,0.5,0.5)
        c.setFont("Helvetica-Oblique", 3.2*mm)
        c.drawCentredString(AW*mm/2, cy-4*mm, subtitulo)

    n = len(fotos_rutas)
    if n == 0: pass
    elif n == 1:
        foto_zona(c, fotos_rutas[0], MG, MG, AW-MG*2, foto_h)
    elif variante % 2 == 0 or n == 2:
        fw = (AW-MG*2-GAP)/2
        for j, r in enumerate(fotos_rutas[:2]):
            foto_zona(c, r, MG+j*(fw+GAP), MG, fw, foto_h)
    else:
        foto_zona(c, fotos_rutas[0], MG, MG, AW-MG*2, foto_h)

    if do_wm: wm(c, AW*mm, AH*mm)

# ═══════════════════════════════════════════════════════
#  MOTOR DE LAYOUT
# ═══════════════════════════════════════════════════════

def elegir_layout(fotos_grupo, layout_anterior="", variante=0):
    n = len(fotos_grupo)
    if n == 0: return None, []
    if n == 1: return "1", fotos_grupo

    if n == 2:
        if layout_anterior == "2H": return "2V", fotos_grupo
        elif layout_anterior == "2V": return "2H", fotos_grupo
        elif variante % 2 == 0: return "2H", fotos_grupo
        else: return "2V", fotos_grupo

    if n == 3: return "3", fotos_grupo

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

        grupo = fotos[idx:idx+n_coger]
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
            "texto": texto if idx == 0 else ""
        })

        idx += len(fotos_layout)
        layout_ant = layout
        variante += 1

    return paginas

# ═══════════════════════════════════════════════════════
#  IA — AGRUPA + PROPONE 2 PORTADAS
# ═══════════════════════════════════════════════════════

def analizar_con_ia(fotos, dias, titulo_cliente=""):
    log("Conectando con Claude API...", "🤖")
    cli = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    listado = f"INVENTARIO COMPLETO — {len(fotos)} fotos:\n"
    for f in fotos:
        es_frame = " [FOTOGRAMA VIDEO-QR]" if f.get("es_frame_video") else ""
        listado += f"  {f['fecha'].strftime('%d/%m/%Y')} · {f['nombre']}{es_frame}\n"

    MAX = 50
    muestra = fotos if len(fotos) <= MAX else [fotos[int(i*len(fotos)/MAX)] for i in range(MAX)]
    if fotos[-1] not in muestra: muestra[-1] = fotos[-1]

    contenido = [{"type":"text","text":listado}]
    for f in muestra:
        try:
            img = Image.open(f["ruta"]).convert("RGB")
            img.thumbnail((100,100), Image.LANCZOS)
            buf = io.BytesIO(); img.save(buf,"JPEG",quality=65)
            b64 = base64.standard_b64encode(buf.getvalue()).decode()
            contenido.append({"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":b64}})
            contenido.append({"type":"text","text":f"· {f['nombre']} {f['fecha'].strftime('%d/%m/%Y')}"})
        except: pass

    fecha_ini = min(f['fecha'] for f in fotos).strftime('%d/%m/%Y')
    fecha_fin = max(f['fecha'] for f in fotos).strftime('%d/%m/%Y')

    titulo_info = f'\nEl cliente ya escribió este título para su álbum: "{titulo_cliente}"' if titulo_cliente else ""
    
    prompt = f"""Eres experto en libros de fotos para Bookeo. Tienes {len(fotos)} fotos del {fecha_ini} al {fecha_fin}.

TU ÚNICA TAREA: agrupar las fotos en capítulos y proponer 2 opciones de portada. NO decides layouts — eso lo hace Python.

TIPOS DE LIBRO:
- bebe: agrupa por mes de vida. NUNCA mezcles fotos de meses distintos en un capítulo.
- viaje: agrupa por día o destino
- boda: preparativos → ceremonia → convite → fiesta
- comunion: preparativos → iglesia → celebración
- familiar: por estaciones (invierno/primavera/verano/otoño/navidad)
- dia_madre / dia_padre: cronológico con protagonista principal
- aniversario_persona: antiguo→reciente
- anual: invierno→semana santa→verano→vuelta cole→navidad
- otro: cronológico

RESPONDE SOLO JSON compacto:
{{"tipo":"bebe","tipo_desc":"primer año de vida","titulo":"Catalina · El primer año","subtitulo":"Febrero 2023 - Enero 2024","portada_opciones":[{{"foto":"foto1.jpg","titulo":"Catalina · El primer año","subtitulo":"Febrero 2023 - Enero 2024"}},{{"foto":"foto5.jpg","titulo":"Catalina","subtitulo":"Su primer año de vida"}}],"capitulos":[{{"titulo":"Mes 1 · Febrero 2023","subtitulo":"Los primeros instantes","fotos":["foto1.jpg","foto2.jpg"]}}]}}

REGLAS:
- portada_opciones: EXACTAMENTE 2 propuestas distintas, cada una con una foto candidata diferente (las 2 fotos más impactantes visualmente)
- Si el cliente escribió un título, ÚSALO como base: las 2 propuestas deben ser variaciones creativas de ESE título (no títulos completamente distintos e inventados). Por ejemplo si el cliente puso "Verano 2024", las propuestas podrían ser "Verano 2024" con un subtítulo evocador, y "Nuestro verano · 2024" con otro subtítulo distinto. Si el cliente no escribió título, entonces sí puedes proponer libremente
- Todos los nombres de foto deben estar EXACTAMENTE como en el inventario
- Incluye TODAS las fotos del inventario en algún capítulo
- Los capítulos deben tener un mínimo de 2 fotos, sin límite máximo — si un capítulo temático (como "Verano" o un mes completo) tiene muchas fotos, mantenlas todas juntas en ese mismo capítulo, no lo dividas artificialmente
- Máximo 2 fotos por capítulo si solo tienes 2 — no fuerces capítulos vacíos"""

    contenido.append({"type":"text","text":prompt})

    log(f"Enviando {len(muestra)} miniaturas + inventario de {len(fotos)} fotos...", "📷")

    for intento in range(3):
        try:
            resp = cli.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=8000,
                messages=[{"role":"user","content":contenido}]
            )
            txt = resp.content[0].text.strip()
            if "```json" in txt: txt = txt.split("```json")[1].split("```")[0].strip()
            elif "```" in txt: txt = txt.split("```")[1].split("```")[0].strip()

            try:
                d = json.loads(txt)
            except:
                for _ in range(txt.count('[')-txt.count(']')): txt += ']'
                for _ in range(txt.count('{')-txt.count('}')): txt += '}'
                d = json.loads(txt)

            log(f"Tipo: {d['tipo'].upper()} · {d['tipo_desc']}", "🎨")
            log(f"Título: {d['titulo']}", "📖")
            n_caps = len(d.get('capitulos',[]))
            n_fotos_ia = sum(len(cap.get('fotos',[])) for cap in d.get('capitulos',[]))
            log(f"Capítulos: {n_caps} · Fotos asignadas: {n_fotos_ia}/{len(fotos)}", "📄")
            return d

        except Exception as e:
            log(f"Intento {intento+1} fallido: {e}", "⚠")
            if intento == 2: raise

# ═══════════════════════════════════════════════════════
#  FASE A — PROPUESTAS DE PORTADA (rápido, sin generar PDF)
# ═══════════════════════════════════════════════════════

def generar_propuestas_portada(fotos_rutas, videos_rutas, titulo_cliente="", formato="2128", orientacion="v"):
    """
    Llamada por main.py cuando el cliente termina de subir fotos/vídeos.
    Analiza con IA y devuelve el diseño completo (capítulos ya decididos)
    más 2 propuestas de portada, SIN generar el PDF todavía.

    Devuelve: dict con 'diseño', 'fotos', 'portada_opciones', 'formato', 'orientacion'
    (formato/orientacion se devuelven para reutilizarlos en generar_pdf_completo)
    """
    exts_foto = {".jpg",".jpeg",".png",".heic",".heif",".tiff"}
    fotos = []
    for ruta in fotos_rutas:
        ext = Path(ruta).suffix.lower()
        if ext in exts_foto:
            fecha, fuente = leer_fecha(ruta)
            fotos.append({"ruta":ruta,"fecha":fecha,"nombre":os.path.basename(ruta),"fuente_fecha":fuente})

    fotos.sort(key=lambda x: x["fecha"])
    if len(fotos) < 2:
        raise ValueError("Se necesitan al menos 2 fotos para crear el libro")

    dias = {}
    for f in fotos:
        dia = f["fecha"].strftime("%Y-%m-%d")
        dias.setdefault(dia, []).append(f)

    diseño = analizar_con_ia(fotos, dias, titulo_cliente)

    return {
        "diseño": diseño,
        "fotos": fotos,
        "portada_opciones": diseño.get("portada_opciones", []),
        "formato": formato,
        "orientacion": orientacion,
    }

# ═══════════════════════════════════════════════════════
#  FASE B — GENERAR PDF COMPLETO (con portada ya decidida)
# ═══════════════════════════════════════════════════════

def generar_pdf_completo(diseño, fotos, videos_rutas, qr_urls, portada_elegida,
                          nombre_cliente, carpeta_sal, carpeta_temp,
                          formato="2128", orientacion="v"):
    """
    Llamada por main.py cuando el cliente confirma su portada.

    formato/orientacion → deben ser los MISMOS que se usaron en
    generar_propuestas_portada() para este mismo pedido.
    portada_elegida → dict {"foto": nombre_o_None, "titulo":..., "subtitulo":...}
                       Si "foto" es None → portada en blanco (sin imagen)
    Devuelve: ruta del PDF generado
    """
    os.makedirs(carpeta_sal, exist_ok=True)
    os.makedirs(carpeta_temp, exist_ok=True)

    AW, AH = obtener_medidas(formato, orientacion)

    exts_vid = {".mp4",".mov",".avi",".m4v",".mkv",".wmv"}
    videos = []
    for ruta in videos_rutas:
        ext = Path(ruta).suffix.lower()
        if ext in exts_vid:
            fecha, fuente = leer_fecha(ruta)
            videos.append({"ruta":ruta,"fecha":fecha,"nombre":os.path.basename(ruta),"fuente_fecha":fuente})

    qr_map = {}
    if videos:
        for v in videos:
            nombre_v = v["nombre"]
            if nombre_v in qr_urls:
                mejor_foto = None
                mejor_diff = float("inf")
                for f in fotos:
                    diff = abs((f["fecha"]-v["fecha"]).total_seconds())/60
                    if diff < mejor_diff:
                        mejor_diff = diff; mejor_foto = f
                if mejor_foto and mejor_diff <= 120:
                    qr_map[mejor_foto["nombre"]] = qr_urls[nombre_v]
                    log(f"  · QR '{nombre_v}' → junto a '{mejor_foto['nombre']}'", "🔗")
                else:
                    frame = extraer_fotograma(v["ruta"], carpeta_temp)
                    if frame:
                        nb_frame = os.path.basename(frame)
                        fotos.append({"ruta":frame,"fecha":v["fecha"],"nombre":nb_frame,
                                      "fuente_fecha":"video","es_frame_video":True})
                        qr_map[nb_frame] = qr_urls[nombre_v]
                        fotos.sort(key=lambda x: x["fecha"])
                        log(f"  · Fotograma '{nb_frame}' con QR real", "🎬")

    fotos_dict = {f["nombre"]: f for f in fotos}

    titulo_final = portada_elegida.get("titulo") or diseño.get("titulo", "Mi Álbum")
    subtitulo = portada_elegida.get("subtitulo", "")
    nb = nombre_cliente.lower().replace(" ", "_")
    r_final = os.path.join(carpeta_sal, f"bookeo_{nb}.pdf")

    generar_pdf(AW, AH, diseño, fotos_dict, qr_map, r_final, titulo_final, subtitulo,
                portada_elegida=portada_elegida, do_wm=False)

    import shutil
    if os.path.exists(carpeta_temp):
        shutil.rmtree(carpeta_temp, ignore_errors=True)

    log(f"PDF listo: {r_final}", "✅")
    return r_final

# ═══════════════════════════════════════════════════════
#  GENERADOR DE PDF
# ═══════════════════════════════════════════════════════

def generar_pdf(AW, AH, diseño, fotos_dict, qr_map, ruta, titulo, subtitulo, portada_elegida=None, do_wm=False):
    """
    AW, AH: tamaño de página en mm (calculado según formato/orientación)
    diseño: resultado de la IA con capitulos
    fotos_dict: {nombre: foto_dict}
    qr_map: {nombre_foto: url_video}
    portada_elegida: dict con la portada confirmada por el cliente (o None → usa la de la IA)
    """
    aw, ah = AW*mm, AH*mm
    tipo_txt = "con marca de agua" if do_wm else "limpio"
    log(f"Generando PDF {tipo_txt} ({AW}x{AH}mm)...", "📄")

    c = canvas.Canvas(ruta, pagesize=(aw, ah))
    c.setTitle(titulo); c.setAuthor("Bookeo · mibookeo.es")

    fotos_usadas = set()
    capitulo_variante = 0

    # ── PORTADA (ya decidida por el cliente) ──
    portada_nombre = ""
    portada_ruta = ""
    if portada_elegida and portada_elegida.get("foto"):
        portada_nombre = portada_elegida["foto"]
        portada_ruta = fotos_dict.get(portada_nombre, {}).get("ruta", "")
    elif not portada_elegida:
        portada_nombre = diseño.get("portada", "")
        portada_ruta = fotos_dict.get(portada_nombre, {}).get("ruta", "") if portada_nombre else ""
        if not portada_ruta and fotos_dict:
            portada_ruta = list(fotos_dict.values())[0]["ruta"]

    dibujar_portada(c, AW, AH, portada_ruta, titulo, subtitulo, do_wm)
    c.showPage()
    if portada_nombre:
        fotos_usadas.add(portada_nombre)

    # ── PÁGINA EN BLANCO detrás de portada ──
    dibujar_pagina_blanca(c, AW, AH); c.showPage()

    # ── CAPÍTULOS ──
    for cap in diseño.get("capitulos", []):
        tit_cap = cap.get("titulo","")
        sub_cap = cap.get("subtitulo","")
        nombres_cap = cap.get("fotos", [])

        fotos_cap = []
        for nombre in nombres_cap:
            if nombre in fotos_dict and nombre not in fotos_usadas:
                fotos_cap.append(fotos_dict[nombre])
                fotos_usadas.add(nombre)

        if not fotos_cap:
            continue

        fotos_titulo = fotos_cap[:2]
        rutas_titulo = [f["ruta"] for f in fotos_titulo if f.get("ruta")]
        if not rutas_titulo and fotos_cap:
            rutas_titulo = [fotos_cap[0]["ruta"]]
        layout_titulo_capitulo(c, AW, AH, tit_cap, sub_cap, rutas_titulo, capitulo_variante, do_wm)
        c.showPage()
        capitulo_variante += 1

        fotos_resto = fotos_cap[len(fotos_titulo):]
        if fotos_resto:
            paginas = paginas_para_grupo(fotos_resto, qr_map, variante_inicio=capitulo_variante)
            for pg in paginas:
                rutas = [f["ruta"] if isinstance(f,dict) else f for f in pg["fotos"]]
                kwargs = dict(fotos_rutas=rutas, qr_idx=pg["qr_idx"], qr_url=pg["qr_url"],
                              pie=pg["texto"], do_wm=do_wm)
                if pg["layout"] == "1": layout_1(c, AW, AH, **kwargs)
                elif pg["layout"] == "2H": layout_2H(c, AW, AH, **kwargs)
                elif pg["layout"] == "2V": layout_2V(c, AW, AH, **kwargs)
                elif pg["layout"] == "3": layout_3(c, AW, AH, **kwargs)
                elif pg["layout"] == "4": layout_4(c, AW, AH, **kwargs)
                c.showPage()
                capitulo_variante += 1

    # ── FOTOS NO USADAS (red de seguridad) ──
    fotos_sobrantes = [f for nombre, f in fotos_dict.items()
                       if nombre not in fotos_usadas and f.get("ruta") and os.path.exists(f.get("ruta",""))]
    if fotos_sobrantes:
        log(f"  Añadiendo {len(fotos_sobrantes)} fotos sobrantes en orden cronológico...", "📸")
        fotos_sobrantes.sort(key=lambda x: x["fecha"])
        paginas = paginas_para_grupo(fotos_sobrantes, qr_map, variante_inicio=capitulo_variante)
        for pg in paginas:
            rutas = [f["ruta"] if isinstance(f,dict) else f for f in pg["fotos"]
                     if (f.get("ruta") if isinstance(f,dict) else f) and
                        os.path.exists(f.get("ruta","") if isinstance(f,dict) else f)]
            if not rutas:
                continue
            kwargs = dict(fotos_rutas=rutas, qr_idx=pg["qr_idx"], qr_url=pg["qr_url"],
                          pie=pg["texto"], do_wm=do_wm)
            if pg["layout"] == "1": layout_1(c, AW, AH, **kwargs)
            elif pg["layout"] == "2H": layout_2H(c, AW, AH, **kwargs)
            elif pg["layout"] == "2V": layout_2V(c, AW, AH, **kwargs)
            elif pg["layout"] == "3": layout_3(c, AW, AH, **kwargs)
            elif pg["layout"] == "4": layout_4(c, AW, AH, **kwargs)
            c.showPage()

    # ── LOMO ──
    dibujar_lomo(c, AW, AH, titulo, do_wm=do_wm)
    c.showPage()

    # ── CONTRAPORTADA ──
    dibujar_contraportada(c, AW, AH, do_wm)
    c.showPage()

    c.save()
    log(f"PDF completo guardado: {ruta}", "✅")
    log(f"  · Páginas: portada + lomo + {len(diseño.get('capitulos',[]))} capítulos + contraportada", "📄")

# ═══════════════════════════════════════════════════════
#  VÍDEOS
# ═══════════════════════════════════════════════════════

def extraer_fotograma(ruta_video, carpeta_temp):
    """Extrae el mejor fotograma del vídeo (prioriza caras bien encuadradas)."""
    if not OPENCV_OK: return None
    try:
        cap = cv2.VideoCapture(ruta_video)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total <= 0: cap.release(); return None

        cas = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

        mejor = None
        mejor_s = -1
        mejor_tiene_cara = False

        puntos = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]
        for pt in puntos:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total*pt))
            ret, fr = cap.read()
            if not ret: continue

            g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            brillo = g.mean()
            nitidez = cv2.Laplacian(g, cv2.CV_64F).var()

            if brillo < 30 or brillo > 220: continue

            g_eq = cv2.equalizeHist(g)
            caras = cas.detectMultiScale(g_eq, 1.1, 4, minSize=(20,20))
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
                s = nitidez * (1 - abs(brillo-120)/200)
                if s > mejor_s:
                    mejor = fr.copy()
                    mejor_s = s

        cap.release()

        if mejor is None: return None

        os.makedirs(carpeta_temp, exist_ok=True)
        nb = os.path.splitext(os.path.basename(ruta_video))[0]
        ruta_jpg = os.path.join(carpeta_temp, f"frame_{nb}.jpg")

        if mejor_tiene_cara and OPENCV_OK:
            try:
                g = cv2.cvtColor(mejor, cv2.COLOR_BGR2GRAY)
                g_eq = cv2.equalizeHist(g)
                caras = cas.detectMultiScale(g_eq, 1.1, 4, minSize=(20,20))
                if len(caras) > 0:
                    ih, iw = mejor.shape[:2]
                    x1 = min(c[0] for c in caras)
                    y1 = min(c[1] for c in caras)
                    x2 = max(c[0]+c[2] for c in caras)
                    y2 = max(c[1]+c[3] for c in caras)
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
            except: pass

        img_pil = Image.fromarray(cv2.cvtColor(mejor, cv2.COLOR_BGR2RGB))
        img_pil.save(ruta_jpg, "JPEG", quality=92)
        cara_txt = "con cara encuadrada" if mejor_tiene_cara else "sin cara · mejor frame"
        log(f"  · Fotograma extraído · {cara_txt}", "")
        return ruta_jpg

    except Exception as e:
        log(f"  · Error extrayendo fotograma: {e}", "")
        return None

# ═══════════════════════════════════════════════════════
#  MAIN — uso local opcional (no usado por Railway)
# ═══════════════════════════════════════════════════════

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
        print("O importar: from crear_libro_railway import generar_propuestas_portada, generar_pdf_completo")

if __name__ == "__main__":
    main()