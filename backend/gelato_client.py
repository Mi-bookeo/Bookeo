# -*- coding: utf-8 -*-
"""
Cliente de la API de Gelato (imprenta bajo demanda) - crear el pedido de
producción real y consultar su estado más adelante.

De momento este archivo va SUELTO, sin engancharse todavía a
main.py/el webhook de Stripe - es el mismo paso que ya se hizo con
resend_client.py o r2_storage.py: primero el cliente de la API, probado
por su cuenta, y luego (en otro momento) se conecta al flujo real del
pedido.

Reutiliza las mismas piezas que ya existen en crear_libro_railway.py
para las dimensiones de cubierta: GELATO_API_KEY (misma variable de
Railway) y PRODUCT_UID_GELATO (mismo UID de producto por formato) - para
no tener el UID de cada formato escrito por partida doble en dos sitios.
"""
import os
import time
import requests

from crear_libro_railway import PRODUCT_UID_GELATO

GELATO_API_KEY = os.environ.get("GELATO_API_KEY", "")
GELATO_ORDERS_URL = "https://order.gelatoapis.com/v4/orders"

# Por defecto SIEMPRE en modo "draft" (Gelato lo guarda pero nunca lo
# manda a producción, hasta que se confirme a mano o por API) - hay que
# poner esta variable a "1" en Railway explícitamente para que un pedido
# real llegue a imprimirse de verdad. Así, mientras se prueba el resto
# del sistema, es imposible mandar un libro a imprenta sin querer.
GELATO_ORDENES_REALES = os.environ.get("GELATO_ORDENES_REALES", "0") == "1"


def _nombre_y_apellidos(nombre_completo):
    """
    Gelato pide firstName/lastName por separado; el formulario de Bookeo
    solo pide un campo de nombre. Se parte por el primer espacio - si no
    hay apellido, se manda un espacio en blanco (Gelato exige que
    lastName no esté vacío).
    """
    partes = (nombre_completo or "").strip().split(" ", 1)
    nombre = partes[0] if partes and partes[0] else "Cliente"
    apellidos = partes[1] if len(partes) > 1 else " "
    return nombre, apellidos


def crear_pedido_gelato(numero_pedido, cliente_id, formato, cantidad, pdf_url, envio, forzar_draft=None):
    """
    Crea el pedido de producción en Gelato para un libro de Bookeo ya
    pagado.

    numero_pedido: el "BK-20260926-..." de siempre - se manda como
        orderReferenceId, así se puede buscar luego en el panel de Gelato
        por el mismo número que usa Bookeo.
    cliente_id: el id interno del cliente (uuid de Supabase) - va como
        customerReferenceId.
    formato: '2020'/'2128'/'2828' - se traduce al productUid real de
        Gelato a través de PRODUCT_UID_GELATO (el mismo diccionario que
        ya usa crear_libro_railway.py para las dimensiones de cubierta).
    cantidad: nº de ejemplares de ESTE libro en el pedido.
    pdf_url: enlace de descarga del PDF final (el mismo que ya se genera
        en R2 con 7 días de validez) - Gelato lo descarga de ahí, no hay
        que subirle el archivo a mano.
    envio: dict con nombre, telefono, correo, direccion, direccion2
        (opcional), ciudad, codigo_postal, provincia, pais (opcional,
        'ES' por defecto) - son los mismos campos que ya recoge
        pago.html.
    forzar_draft: True/False para saltarse GELATO_ORDENES_REALES en un
        caso concreto (por ejemplo, para hacer una prueba manual) - si no
        se indica, se usa la variable de entorno.

    Devuelve el JSON de respuesta de Gelato (incluye su propio "id" de
    pedido, que conviene guardar para poder consultarlo después).

    OJO - no cobra nada por su cuenta: solo hace lo que se le pide. El
    pago real ya se cobró antes, en Stripe. Esto solo dispara la
    producción/envío del libro.
    """
    if not GELATO_API_KEY:
        raise RuntimeError("GELATO_API_KEY no está configurada")

    uid = PRODUCT_UID_GELATO.get(formato)
    if not uid:
        raise RuntimeError(f"No hay productUid de Gelato configurado para el formato '{formato}'")

    modo_draft = (not GELATO_ORDENES_REALES) if forzar_draft is None else forzar_draft
    nombre, apellidos = _nombre_y_apellidos(envio.get("nombre"))

    cuerpo = {
        "orderType": "draft" if modo_draft else "order",
        "orderReferenceId": numero_pedido,
        "customerReferenceId": cliente_id or numero_pedido,
        "currency": "EUR",
        "items": [
            {
                "itemReferenceId": f"{numero_pedido}-libro",
                "productUid": uid,
                "quantity": cantidad or 1,
                "files": [{"type": "default", "url": pdf_url}],
            }
        ],
        "shippingAddress": {
            "firstName": nombre,
            "lastName": apellidos,
            "companyName": "",
            "addressLine1": envio.get("direccion") or "",
            "addressLine2": envio.get("direccion2") or "",
            "city": envio.get("ciudad") or "",
            "postCode": envio.get("codigo_postal") or "",
            "state": envio.get("provincia") or "",
            "country": envio.get("pais") or "ES",
            "email": envio.get("correo") or "",
            "phone": envio.get("telefono") or "",
        },
        # Sin 'shipmentMethodUid' a propósito - así Gelato elige solo el
        # método de envío que tenga disponible para España, en vez de
        # forzar uno concreto que quizá no aplique a todos los pedidos.
    }

    ultimo_error = None
    for intento in range(3):
        try:
            resp = requests.post(
                GELATO_ORDERS_URL,
                json=cuerpo,
                headers={"X-API-KEY": GELATO_API_KEY, "Content-Type": "application/json"},
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            ultimo_error = e
            print(f"[DEBUG] Intento {intento + 1}/3 fallido creando el pedido en Gelato ({numero_pedido}): {e}")
            time.sleep(1.5)
    raise RuntimeError(f"No se pudo crear el pedido en Gelato tras 3 intentos: {ultimo_error}")


def consultar_pedido_gelato(gelato_order_id):
    """
    Consulta el estado actual de un pedido ya creado en Gelato (el "id"
    que devolvió crear_pedido_gelato, NO el numero_pedido de Bookeo).
    Útil para comprobar a mano si un pedido de prueba (draft) se ha
    creado bien, o el estado de uno real (en producción, enviado...).
    """
    if not GELATO_API_KEY:
        raise RuntimeError("GELATO_API_KEY no está configurada")

    resp = requests.get(
        f"{GELATO_ORDERS_URL}/{gelato_order_id}",
        headers={"X-API-KEY": GELATO_API_KEY},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()
