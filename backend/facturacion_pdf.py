"""
Bookeo · facturacion_pdf.py
Genera el PDF del ticket (para todas las ventas) y de la factura (solo
si el cliente la pide, con NIF/CIF y razón social).
"""

import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
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


def _cabecera(c, titulo, numero, fecha):
    c.setFont("Helvetica-Bold", 20)
    c.drawString(20 * mm, 275 * mm, "Bookeo")
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, 270 * mm, "www.mibookeo.es")

    c.setFont("Helvetica-Bold", 16)
    c.drawRightString(190 * mm, 275 * mm, titulo)
    c.setFont("Helvetica", 10)
    c.drawRightString(190 * mm, 269 * mm, f"Nº {numero}")
    c.drawRightString(190 * mm, 264 * mm, fecha.strftime("%d/%m/%Y"))

    c.line(20 * mm, 260 * mm, 190 * mm, 260 * mm)


def _tabla_lineas(c, y_inicio, libros, total):
    c.setFont("Helvetica-Bold", 10)
    c.drawString(20 * mm, y_inicio, "Concepto")
    c.drawRightString(190 * mm, y_inicio, "Importe")
    y = y_inicio - 6 * mm
    c.setFont("Helvetica", 10)
    for libro in libros:
        nombre = libro.get("titulo") or "Álbum de fotos"
        cantidad = libro.get("cantidad", 1)
        texto = f"{nombre} × {cantidad}" if cantidad and cantidad > 1 else nombre
        c.drawString(20 * mm, y, texto)
        y -= 6 * mm
    y -= 4 * mm
    c.line(20 * mm, y, 190 * mm, y)
    y -= 8 * mm
    return y


def generar_pdf_ticket(ruta_salida, numero_ticket, numero_pedido, fecha, correo_cliente, libros, total):
    """
    Ticket sencillo, sin desglose fiscal - se genera para TODAS las
    ventas, lo pida o no el cliente.
    """
    c = pdfcanvas.Canvas(ruta_salida, pagesize=A4)
    _cabecera(c, "TICKET", numero_ticket, fecha)

    c.setFont("Helvetica", 10)
    c.drawString(20 * mm, 250 * mm, f"Pedido: {numero_pedido}")
    c.drawString(20 * mm, 245 * mm, f"Cliente: {correo_cliente or '-'}")

    y = _tabla_lineas(c, 230 * mm, libros, total)

    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(190 * mm, y, f"Total: {total:.2f} €")

    c.setFont("Helvetica", 8)
    c.drawCentredString(105 * mm, 15 * mm, "Gracias por tu compra en Bookeo · www.mibookeo.es")
    c.save()


def generar_pdf_factura(ruta_salida, numero_factura, numero_pedido, fecha, datos_cliente, libros, total):
    """
    Factura con desglose de IVA y datos fiscales del cliente
    (NIF/CIF, razón social, dirección de facturación) - solo se genera
    si el cliente la ha pedido explícitamente en el pago.

    datos_cliente: {"nif":..., "razon_social":..., "direccion":...,
                     "ciudad":..., "codigo_postal":..., "provincia":...}
    """
    c = pdfcanvas.Canvas(ruta_salida, pagesize=A4)
    _cabecera(c, "FACTURA", numero_factura, fecha)

    # Datos fiscales de Bookeo (emisor)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 * mm, 250 * mm, "Emisor")
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, 245 * mm, DATOS_FISCALES_NEGOCIO["razon_social"])
    c.drawString(20 * mm, 240 * mm, f"NIF/CIF: {DATOS_FISCALES_NEGOCIO['nif']}")
    c.drawString(20 * mm, 235 * mm, DATOS_FISCALES_NEGOCIO["direccion"])
    c.drawString(20 * mm, 230 * mm, DATOS_FISCALES_NEGOCIO["codigo_postal_ciudad"])

    # Datos fiscales del cliente (receptor)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(110 * mm, 250 * mm, "Cliente")
    c.setFont("Helvetica", 9)
    c.drawString(110 * mm, 245 * mm, datos_cliente.get("razon_social") or "-")
    c.drawString(110 * mm, 240 * mm, f"NIF/CIF: {datos_cliente.get('nif') or '-'}")
    c.drawString(110 * mm, 235 * mm, datos_cliente.get("direccion") or "-")
    linea_cp_ciudad = f"{datos_cliente.get('codigo_postal') or ''} {datos_cliente.get('ciudad') or ''}".strip()
    c.drawString(110 * mm, 230 * mm, linea_cp_ciudad or "-")

    c.line(20 * mm, 222 * mm, 190 * mm, 222 * mm)
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, 216 * mm, f"Pedido: {numero_pedido}")

    y = _tabla_lineas(c, 205 * mm, libros, total)

    # Desglose de IVA - el 'total' que llega ya lo incluye (precio final
    # al público), así que se calcula la base hacia atrás.
    base_imponible = total / (1 + IVA_PORCENTAJE / 100)
    cuota_iva = total - base_imponible

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
