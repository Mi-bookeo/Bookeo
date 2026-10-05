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

# Gelato cuenta en 'pageCount' las páginas interiores IMPRIMIBLES del libro
# (30 en el libro base; las dos guardas en blanco del PDF no cuentan). Si
# un día Gelato pidiera otro número (por ejemplo contando también las
# páginas de la cubierta), se ajusta SIN tocar código: variable
# GELATO_PAGECOUNT_EXTRA en Railway con el número que haya que sumar.
try:
    GELATO_PAGECOUNT_EXTRA = int(os.environ.get("GELATO_PAGECOUNT_EXTRA", "0") or 0)
except ValueError:
    GELATO_PAGECOUNT_EXTRA = 0


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


def crear_pedido_gelato(numero_pedido, cliente_id, libros, envio, forzar_draft=None):
    """
    Crea EL pedido de producción en Gelato para un pedido de Bookeo ya
    pagado - uno solo, aunque el pedido lleve varios libros distintos
    (cada libro va como un 'item' dentro del mismo pedido de Gelato, no
    como pedidos separados). Así encaja con cómo ya guardas los datos:
    un pedido de Bookeo = una fila en 'pedidos' = un único gelato_id ahí,
    y cada libro sigue siendo su propia fila en 'libros'.

    numero_pedido: el "BK-20260926-..." de siempre - se manda como
        orderReferenceId, así se puede buscar luego en el panel de Gelato
        por el mismo número que usa Bookeo.
    cliente_id: el id interno del cliente (uuid de Supabase) - va como
        customerReferenceId.
    libros: lista de dicts, uno por cada libro del pedido, cada uno con
        'formato' ('2020'/'2128'/'2828'), 'cantidad' y 'pdf_url' (el
        enlace de descarga del PDF final ya generado en R2). El
        productUid de cada uno sale de PRODUCT_UID_GELATO (el mismo
        diccionario que ya usa crear_libro_railway.py para las
        dimensiones de cubierta), así que no hay que repetirlo a mano.
    envio: dict con nombre, telefono, correo, direccion, direccion2
        (opcional), ciudad, codigo_postal, provincia, pais (opcional,
        'ES' por defecto) - son los mismos campos que ya recoge
        pago.html. Es UNA sola dirección para todo el pedido, como ya
        funciona hoy (no hay una dirección por libro).
    forzar_draft: True/False para saltarse GELATO_ORDENES_REALES en un
        caso concreto (por ejemplo, para hacer una prueba manual) - si no
        se indica, se usa la variable de entorno.

    Devuelve el JSON de respuesta de Gelato (incluye su propio "id" de
    pedido, el que hay que guardar en pedidos.gelato_id).

    OJO - no cobra nada por su cuenta: solo hace lo que se le pide. El
    pago real ya se cobró antes, en Stripe. Esto solo dispara la
    producción/envío del o de los libros.
    """
    if not GELATO_API_KEY:
        raise RuntimeError("GELATO_API_KEY no está configurada")
    if not libros:
        raise RuntimeError("crear_pedido_gelato() ha recibido una lista de libros vacía")

    items = []
    for i, libro in enumerate(libros):
        formato = libro.get("formato")
        uid = PRODUCT_UID_GELATO.get(formato)
        if not uid:
            raise RuntimeError(f"No hay productUid de Gelato configurado para el formato '{formato}' (libro {i + 1})")
        item = {
            "itemReferenceId": f"{numero_pedido}-libro{i + 1}",
            "productUid": uid,
            "quantity": libro.get("cantidad") or 1,
            "files": [{"type": "default", "url": libro["pdf_url"]}],
        }
        # Sin 'pageCount' Gelato no sabe cuántas páginas tiene el libro y
        # el precio del borrador no es fiable. Solo se manda si se conoce.
        try:
            paginas_gelato = int(libro.get("paginas_gelato") or 0)
        except (TypeError, ValueError):
            paginas_gelato = 0
        if paginas_gelato > 0:
            item["pageCount"] = paginas_gelato + GELATO_PAGECOUNT_EXTRA
        items.append(item)

    modo_draft = (not GELATO_ORDENES_REALES) if forzar_draft is None else forzar_draft
    nombre, apellidos = _nombre_y_apellidos(envio.get("nombre"))

    cuerpo = {
        "orderType": "draft" if modo_draft else "order",
        "orderReferenceId": numero_pedido,
        "customerReferenceId": cliente_id or numero_pedido,
        "currency": "EUR",
        "items": items,
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
            if 400 <= resp.status_code < 500:
                # Un 4xx significa que el pedido en sí está mal formado
                # (falta un campo, formato incorrecto...) - reintentar no
                # va a cambiar nada, así que se falla directamente, con el
                # motivo exacto que da Gelato en el cuerpo de la respuesta
                # (antes esto se perdía y solo se veía "400 Bad Request").
                raise RuntimeError(f"Gelato rechazó el pedido ({resp.status_code}): {resp.text}")
            resp.raise_for_status()
            return resp.json()
        except RuntimeError:
            raise  # el 4xx de arriba - no tiene sentido reintentar, se propaga tal cual
        except Exception as e:
            ultimo_error = e
            # SOLO se reintenta si la petición ni siquiera llegó a Gelato (no
            # se pudo conectar). Si llegó y no hubo respuesta a tiempo, o
            # Gelato contestó con un error 5xx, el pedido PUEDE haberse creado
            # ya - reintentar crearía un segundo pedido y se pagaría dos veces.
            no_llego = isinstance(e, requests.exceptions.ConnectTimeout) or any(
                t in str(e) for t in ("Failed to establish a new connection", "Name or service not known",
                                      "Temporary failure in name resolution", "Connection refused"))
            if not no_llego:
                raise RuntimeError(
                    f"Gelato no confirmó si creó el pedido ({e}). COMPRUEBA EN EL PANEL DE GELATO si el pedido "
                    f"{numero_pedido} existe ANTES de repetirlo a mano, para no crearlo dos veces."
                )
            print(f"[DEBUG] Intento {intento + 1}/3 fallido creando el pedido en Gelato ({numero_pedido}), no llegó a Gelato: {e}")
            time.sleep(1.5)
    raise RuntimeError(f"No se pudo conectar con Gelato tras 3 intentos: {ultimo_error}")


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
