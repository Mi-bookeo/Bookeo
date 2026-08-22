"""
Bookeo · supabase_client.py
Funciones para leer/guardar datos de clientes y pedidos relacionados
con la conexión de Google Drive.
"""

import os
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ═══════════════════════════════════════════════════════
#  CLIENTE — buscar o crear por email
# ═══════════════════════════════════════════════════════

def obtener_o_crear_cliente(email):
    """
    Busca un cliente por su email. Si ya existe, devuelve su id.
    Si no existe, lo crea y devuelve el id recién generado.
    """
    resp = supabase.table("clientes").select("id").eq("email", email).execute()

    if resp.data:
        return resp.data[0]["id"]

    nuevo = supabase.table("clientes").insert({"email": email}).execute()
    return nuevo.data[0]["id"]


def obtener_cliente_drive(cliente_id):
    """Devuelve refresh_token y carpeta_drive_id principal del cliente, si existen."""
    resp = supabase.table("clientes").select(
        "google_refresh_token, carpeta_drive_id, google_drive_email"
    ).eq("id", cliente_id).single().execute()
    return resp.data if resp.data else None


def guardar_refresh_token_cliente(cliente_id, refresh_token, email=None):
    """Guarda el refresh_token tras el login OAuth del cliente."""
    datos = {"google_refresh_token": refresh_token}
    if email:
        datos["google_drive_email"] = email
    supabase.table("clientes").update(datos).eq("id", cliente_id).execute()


def guardar_carpeta_principal_cliente(cliente_id, carpeta_drive_id):
    """Guarda el ID de la carpeta principal 'Mibookeo (NO BORRAR)' del cliente."""
    supabase.table("clientes").update(
        {"carpeta_drive_id": carpeta_drive_id}
    ).eq("id", cliente_id).execute()


# ═══════════════════════════════════════════════════════
#  PEDIDO — subcarpeta específica de ese álbum
# ═══════════════════════════════════════════════════════

def obtener_subcarpeta_pedido(pedido_id):
    """Devuelve el ID de la subcarpeta de este pedido, si ya existe."""
    resp = supabase.table("pedidos").select(
        "subcarpeta_drive_id"
    ).eq("id", pedido_id).single().execute()
    return resp.data.get("subcarpeta_drive_id") if resp.data else None


def guardar_subcarpeta_pedido(pedido_id, subcarpeta_drive_id):
    """Guarda el ID de la subcarpeta creada para este pedido concreto."""
    supabase.table("pedidos").update(
        {"subcarpeta_drive_id": subcarpeta_drive_id}
    ).eq("id", pedido_id).execute()


# ═══════════════════════════════════════════════════════
#  CLIENTE — datos de contacto (pantalla de pago)
# ═══════════════════════════════════════════════════════
# OJO: la dirección de envío/facturación NO se guarda en Supabase - solo
# se usa para mandarla a Gelato en el momento de fabricar el pedido. Aquí
# solo se guarda lo mínimo del cliente: nombre, correo y teléfono.

def guardar_datos_contacto_cliente(email, nombre=None, telefono=None):
    """
    Busca (o crea) el cliente por email y actualiza su nombre/teléfono si
    se han pasado. Se llama desde pago.html al rellenar el formulario.
    """
    cliente_id = obtener_o_crear_cliente(email)
    datos = {}
    if nombre:
        datos["nombre"] = nombre
    if telefono:
        datos["telefono"] = telefono
    if datos:
        supabase.table("clientes").update(datos).eq("id", cliente_id).execute()
    return cliente_id


def guardar_marketing_cliente(email, marketing):
    """Guarda si el cliente acepta recibir ofertas/novedades por correo."""
    cliente_id = obtener_o_crear_cliente(email)
    supabase.table("clientes").update({"marketing": bool(marketing)}).eq("id", cliente_id).execute()


# ═══════════════════════════════════════════════════════
#  LIBRO — una fila por cada álbum creado (tabla 'libros')
# ═══════════════════════════════════════════════════════
# El "pedido_id" que se usa en todo el resto del código (uno distinto por
# cada álbum que el cliente crea) es en realidad el identificador de un
# LIBRO, no de un PEDIDO - un mismo pedido puede agrupar varios libros
# (con "Crear otro libro"). La fila de 'pedidos' (con cliente, precio
# total, Stripe, Gelato...) se crea más adelante, cuando el cliente paga
# de verdad y se juntan todos los libros de ese pedido - eso todavía no
# está conectado.

def guardar_libro(pedido_id, datos):
    """
    Guarda (o actualiza) la fila de un libro en la tabla 'libros', para
    poder encontrarlo si un cliente escribe pidiendo volver a su diseño.
    'datos' puede traer: titulo, tipo_libro ('IA' o 'cero'), estado,
    unidad_url, editor_url.

    'pedido_id' es el identificador que ya usa el resto del backend (uno
    por cada álbum creado) - es el que el cliente tiene de verdad (le
    aparece en la URL del editor), así que es lo que hay que buscar si
    escribe pidiendo ayuda. La columna 'id' propia de la fila la genera
    Supabase sola, no se toca aquí.

    OJO: se asume que la columna 'pedido_id' tiene una restricción de
    valor único en la tabla, para que el upsert actualice la misma fila
    en vez de crear una duplicada cada vez que se llama a esto para el
    mismo libro - si no la tiene, avisa y la añadimos.
    """
    fila = dict(datos)
    fila["pedido_id"] = pedido_id
    supabase.table("libros").upsert(fila, on_conflict="pedido_id").execute()