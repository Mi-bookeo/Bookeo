"""
Bookeo · facturacion_pdf.py
Genera el PDF del ticket (para todas las ventas) y de la factura (solo
si el cliente la pide, con NIF/CIF y razón social).
"""

import datetime
import os
from decimal import Decimal, ROUND_HALF_UP
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdfcanvas

# ══════════════════════════════════════════════════════════════
# DESARROLLADOR (Abel): pon aquí los datos fiscales reales del negocio -
# sin esto, las facturas saldrían con datos de ejemplo, no válidas de
# verdad para Hacienda.
# ══════════════════════════════════════════════════════════════
DATOS_FISCALES_NEGOCIO = {
    "razon_social": "PON AQUÍ TU NOMBRE O RAZÓN SOCIAL",
    "nif": "PON AQUÍ TU NIF/CIF",
    "direccion": "PON AQUÍ TU DIRECCIÓN FISCAL",
    "codigo_postal_ciudad": "PON AQUÍ CP Y CIUDAD",
}

IVA_PORCENTAJE = 21  # IVA general en España - el 'total' que llega ya lo incluye

# Mismo texto que ya usa creador.html para cada formato - para que el
# ticket/factura diga "20 × 20 cm" en vez del código interno "2020".
FORMATO_NOMBRES = {"2020": "20 × 20 cm", "2128": "21 × 28 cm", "2828": "28 × 28 cm"}


# Logo de Bookeo: archivo logo_bookeo.png en la MISMA carpeta que este archivo
# (la del backend). Si no está, la cabecera sale con el texto "Bookeo" de siempre.
RUTA_LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo_bookeo.png")


def _cabecera(c, titulo, numero, fecha):
    logo_dibujado = False
    if os.path.exists(RUTA_LOGO):
        try:
            logo = ImageReader(RUTA_LOGO)
            w_px, h_px = logo.getSize()
            ancho = 36 * mm
            alto = ancho * h_px / w_px
            if alto > 20 * mm:  # por si algún día el logo es más alto que ancho
                alto = 20 * mm
                ancho = alto * w_px / h_px
            c.drawImage(logo, 20 * mm, 285 * mm - alto, width=ancho, height=alto, mask="auto")
            logo_dibujado = True
        except Exception:
            logo_dibujado = False
    if not logo_dibujado:
        c.setFont("Helvetica-Bold", 20)
        c.drawString(20 * mm, 275 * mm, "Bookeo")
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, 264 * mm, "www.mibookeo.es")

    c.setFont("Helvetica-Bold", 16)
    c.drawRightString(190 * mm, 280 * mm, titulo)
    c.setFont("Helvetica", 10)
    c.drawRightString(190 * mm, 274 * mm, f"Nº {numero}")
    c.drawRightString(190 * mm, 269 * mm, fecha.strftime("%d/%m/%Y"))

    c.line(20 * mm, 255 * mm, 190 * mm, 255 * mm)


def _redondear(x):
    """Redondeo de dinero a céntimos, mitades hacia arriba (como se hace a mano)."""
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _desglose_sin_iva(libros, envio_importe, descuento, total):
    """
    Importes SIN IVA de la factura. Los precios que llegan llevan el IVA
    incluido; aquí se pasan a base imponible.
      - Cada libro: importe de la línea sin IVA, y su precio unitario sin IVA
        (importe / cantidad). El unitario sale con 2 decimales y, solo cuando
        con 2 no cuadra "unitario × cantidad = importe", con 4 (es normal en
        una factura).
      - La base imponible es la que sale del total pagado (total / 1,21), así
        el IVA es exactamente el 21 % de la base y base + IVA = total pagado.
        Si al redondear cada línea las líneas no suman justo esa base, la
        diferencia (unos céntimos) se absorbe en la línea de descuento, o si no
        hay en la de envío, o si no en el primer libro.
    """
    factor = Decimal(1) + Decimal(IVA_PORCENTAJE) / Decimal(100)
    base = lambda x: _redondear(Decimal(str(x)) / factor)
    b_libros = [base(l["precio"]) if l.get("precio") is not None else None for l in libros]
    b_envio = base(envio_importe) if envio_importe is not None else None
    b_desc = base(descuento["importe"]) if descuento and descuento.get("importe") else None
    objetivo = base(total)
    suma = round(sum(x for x in b_libros if x is not None) + (b_envio or 0) - (b_desc or 0), 2)
    resto = round(objetivo - suma, 2)
    if resto and abs(resto) <= 0.03:
        if b_desc is not None:
            b_desc = round(b_desc - resto, 2)
        elif b_envio:
            b_envio = round(b_envio + resto, 2)
        else:
            for k, x in enumerate(b_libros):
                if x is not None:
                    b_libros[k] = round(x + resto, 2)
                    break
        suma = objetivo
    unitarios, decimales = [], []
    for libro, linea in zip(libros, b_libros):
        if linea is None:
            unitarios.append(None)
            decimales.append(2)
            continue
        cantidad = libro.get("cantidad") or 1
        u2 = _redondear(Decimal(str(linea)) / Decimal(cantidad))
        if round(u2 * cantidad, 2) == linea:
            unitarios.append(u2)
            decimales.append(2)
        else:
            unitarios.append(float((Decimal(str(linea)) / Decimal(cantidad)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)))
            decimales.append(4)
    return {"unitarios": unitarios, "decimales": decimales, "libros": b_libros,
            "envio": b_envio, "descuento": b_desc, "base_total": suma}


def _ajustar_texto(c, texto, fuente, tam, ancho_max):
    """Recorta el texto con '…' si no cabe en 'ancho_max' (puntos)."""
    if c.stringWidth(texto, fuente, tam) <= ancho_max:
        return texto
    while texto and c.stringWidth(texto + "…", fuente, tam) > ancho_max:
        texto = texto[:-1]
    return texto + "…"


def _tabla_lineas(c, y_inicio, libros, descuento=None, envio_importe=None, desglose=None):
    """
    libros: [{"titulo", "cantidad", "formato", "precio"}] - "formato" y
    "precio" son opcionales (si no vienen, esa línea simplemente no
    muestra formato ni importe por separado, por si algún día llega un
    libro sin esos datos). "precio" es el de la línea entera (cantidad
    incluida).
    descuento: {"codigo": "GRACIAS10-...", "importe": 3.50} o None.
    envio_importe: coste de envío a mostrar como línea aparte, o None si
    no aplica (envío incluido en el precio del libro, sin línea propia).
    desglose: resultado de _desglose_sin_iva - si se pasa (factura), cada libro
    sale con su cantidad, su precio unitario y su importe, todo SIN IVA; si no
    (ticket), con el IVA incluido y sin precio unitario.
    """
    factura = desglose is not None
    c.setFont("Helvetica-Bold", 9 if factura else 10)
    c.drawString(20 * mm, y_inicio, "Concepto")
    if factura:
        c.drawString(95 * mm, y_inicio, "Formato")
        c.drawRightString(127 * mm, y_inicio, "Cant.")
        c.drawRightString(160 * mm, y_inicio, "Precio unit. s/IVA")
        c.drawRightString(190 * mm, y_inicio, "Importe s/IVA")
    else:
        c.drawString(140 * mm, y_inicio, "Formato")
        c.drawRightString(190 * mm, y_inicio, "Importe")
    y = y_inicio - 6 * mm
    c.setFont("Helvetica", 10)
    for k, libro in enumerate(libros):
        nombre = libro.get("titulo") or "Álbum de fotos"
        cantidad = libro.get("cantidad", 1)
        formato_txt = FORMATO_NOMBRES.get(libro.get("formato"), "")
        if factura:
            c.drawString(20 * mm, y, _ajustar_texto(c, nombre, "Helvetica", 10, 72 * mm))
            if formato_txt:
                c.drawString(95 * mm, y, formato_txt)
            c.drawRightString(127 * mm, y, str(cantidad or 1))
            if libro.get("precio") is not None:
                c.drawRightString(160 * mm, y, f"{desglose['unitarios'][k]:.{desglose['decimales'][k]}f} €")
                c.drawRightString(190 * mm, y, f"{desglose['libros'][k]:.2f} €")
        else:
            texto = f"{nombre} × {cantidad}" if cantidad and cantidad > 1 else nombre
            c.drawString(20 * mm, y, texto)
            if formato_txt:
                c.drawString(140 * mm, y, formato_txt)
            if libro.get("precio") is not None:
                c.drawRightString(190 * mm, y, f"{libro['precio']:.2f} €")
        y -= 6 * mm

    if envio_importe is not None:
        c.drawString(20 * mm, y, "Envío")
        importe_envio = desglose["envio"] if factura else envio_importe
        c.drawRightString(190 * mm, y, "Gratis" if envio_importe == 0 else f"{importe_envio:.2f} €")
        y -= 6 * mm

    if descuento and descuento.get("importe"):
        codigo = descuento.get("codigo")
        etiqueta = f"Descuento ({codigo})" if codigo else "Descuento"
        c.drawString(20 * mm, y, etiqueta)
        importe_desc = desglose["descuento"] if factura else descuento["importe"]
        c.drawRightString(190 * mm, y, f"-{importe_desc:.2f} €")
        y -= 6 * mm

    y -= 4 * mm
    c.line(20 * mm, y, 190 * mm, y)
    y -= 8 * mm
    return y


def generar_pdf_ticket(ruta_salida, numero_ticket, numero_pedido, fecha, correo_cliente, libros, total,
                        descuento=None, envio_importe=None, nombre_cliente=None, direccion_cliente=None):
    """
    Ticket sencillo, sin desglose fiscal - se genera para TODAS las
    ventas, lo pida o no el cliente.

    descuento: {"codigo": str, "importe": float} o None.
    envio_importe: float o None (ver _tabla_lineas).
    """
    c = pdfcanvas.Canvas(ruta_salida, pagesize=A4)
    _cabecera(c, "TICKET", numero_ticket, fecha)

    # NIF/CIF obligatorio incluso en un ticket (factura simplificada) -
    # sin esto no sería válido de verdad ante Hacienda, aunque no lleve
    # el resto del desglose fiscal completo de una factura normal.
    c.setFont("Helvetica", 8)
    c.drawString(20 * mm, 259.5 * mm, f"{DATOS_FISCALES_NEGOCIO['razon_social']} · NIF/CIF: {DATOS_FISCALES_NEGOCIO['nif']}")

    # Pedido en grande y, debajo, los datos del cliente tal cual (sin etiquetas).
    c.setFont("Helvetica-Bold", 15)
    c.drawString(20 * mm, 244 * mm, f"Pedido: {numero_pedido}")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(20 * mm, 236 * mm, "Cliente:")
    c.setFont("Helvetica", 10)
    y_cliente = 231 * mm
    for linea in (nombre_cliente, direccion_cliente, correo_cliente):
        if linea:
            c.drawString(20 * mm, y_cliente, str(linea))
            y_cliente -= 5 * mm

    y = _tabla_lineas(c, 205 * mm, libros, descuento=descuento, envio_importe=envio_importe)

    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(190 * mm, y, f"Total: {total:.2f} €")
    c.setFont("Helvetica", 8)
    c.drawRightString(190 * mm, y - 5 * mm, "IVA incluido")

    c.setFont("Helvetica", 8)
    c.drawCentredString(105 * mm, 15 * mm, "Gracias por tu compra en Bookeo · www.mibookeo.es")
    c.save()


def generar_pdf_factura(ruta_salida, numero_factura, numero_pedido, fecha, datos_cliente, libros, total,
                         descuento=None, envio_importe=None):
    """
    Factura con desglose de IVA y datos fiscales del cliente
    (NIF/CIF, razón social, dirección de facturación) - solo se genera
    si el cliente la ha pedido explícitamente en el pago.

    datos_cliente: {"nif":..., "razon_social":..., "direccion":...,
                     "ciudad":..., "codigo_postal":..., "provincia":...}
    descuento: {"codigo": str, "importe": float} o None.
    envio_importe: float o None (ver _tabla_lineas).
    """
    c = pdfcanvas.Canvas(ruta_salida, pagesize=A4)
    _cabecera(c, "FACTURA", numero_factura, fecha)

    # Datos fiscales de Bookeo (emisor)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 * mm, 247 * mm, "Emisor")
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, 242 * mm, DATOS_FISCALES_NEGOCIO["razon_social"])
    c.drawString(20 * mm, 237 * mm, f"NIF/CIF: {DATOS_FISCALES_NEGOCIO['nif']}")
    c.drawString(20 * mm, 232 * mm, DATOS_FISCALES_NEGOCIO["direccion"])
    c.drawString(20 * mm, 227 * mm, DATOS_FISCALES_NEGOCIO["codigo_postal_ciudad"])

    # Datos fiscales del cliente (receptor), tal cual (el NIF/CIF lleva su
    # etiqueta para que no se confunda con otro número).
    c.setFont("Helvetica-Bold", 9)
    c.drawString(110 * mm, 247 * mm, "Cliente")
    c.setFont("Helvetica", 9)
    c.drawString(110 * mm, 242 * mm, datos_cliente.get("razon_social") or "-")
    c.drawString(110 * mm, 237 * mm, f"NIF/CIF: {datos_cliente.get('nif') or '-'}")
    c.drawString(110 * mm, 232 * mm, datos_cliente.get("direccion") or "-")
    linea_cp_ciudad = f"{datos_cliente.get('codigo_postal') or ''} {datos_cliente.get('ciudad') or ''}".strip()
    if linea_cp_ciudad and datos_cliente.get("provincia"):
        linea_cp_ciudad += f" ({datos_cliente.get('provincia')})"
    c.drawString(110 * mm, 227 * mm, linea_cp_ciudad or "-")

    c.line(20 * mm, 221 * mm, 190 * mm, 221 * mm)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(20 * mm, 213 * mm, f"Pedido: {numero_pedido}")

    # Cada línea sin IVA + desglose final. El 'total' que llega ya lleva el IVA
    # (precio final al público), así que la base se calcula hacia atrás.
    desglose = _desglose_sin_iva(libros, envio_importe, descuento, total)
    y = _tabla_lineas(c, 203 * mm, libros, descuento=descuento, envio_importe=envio_importe, desglose=desglose)

    base_imponible = desglose["base_total"]
    cuota_iva = round(total - base_imponible, 2)

    c.setFont("Helvetica", 10)
    c.drawRightString(140 * mm, y, "Base imponible:")
    c.drawRightString(190 * mm, y, f"{base_imponible:.2f} €")
    y -= 6 * mm
    c.drawRightString(140 * mm, y, f"IVA ({IVA_PORCENTAJE}%):")
    c.drawRightString(190 * mm, y, f"{cuota_iva:.2f} €")
    y -= 8 * mm
    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(140 * mm, y, "Total:")
    c.drawRightString(190 * mm, y, f"{total:.2f} €")

    c.setFont("Helvetica", 8)
    c.drawCentredString(105 * mm, 15 * mm, "Bookeo · www.mibookeo.es")
    c.save()
