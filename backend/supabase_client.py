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
    # OJO: antes usaba .single(), que lanza un error (PGRST116) si no
    # encuentra NINGUNA fila con ese cliente_id - por ejemplo si el
    # navegador del cliente tenía guardado en localStorage un cliente_id
    # de una sesión antigua que ya no existe en la base de datos. Eso
    # tumbaba la petición entera con un 500 en vez de simplemente seguir
    # sin datos de Drive. Ahora se comprueba la lista de resultados sin
    # .single(), igual que el resto de funciones de este archivo.
    resp = supabase.table("clientes").select(
        "google_refresh_token, carpeta_drive_id, google_drive_email"
    ).eq("id", cliente_id).execute()
    return resp.data[0] if resp.data else None


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

# ═══════════════════════════════════════════════════════
#  PEDIDO — una fila por cada LIBRO (tabla 'pedidos')
# ═══════════════════════════════════════════════════════
# OJO: el "pedido_id" que usa el resto del código (uno por cada álbum que
# el cliente crea) ES el mismo id de la fila en 'pedidos' (columna 'id') -
# no hay traducción entre uno y otro. Si el cliente compra 2 libros en el
# mismo pago, son 2 filas en 'pedidos' (no una) que luego se agrupan por
# tener el mismo stripe_id/numero_pedido - eso se asigna más adelante, al
# confirmar el pago de verdad (solo ahí se conoce el carrito completo).
#
# La fila se crea aquí, en el momento en que el PDF termina de generarse
# -no cuando paga- para que un libro que se queda a medias (el cliente lo
# ve pero no paga) quede igualmente registrado, con fecha_pedido puesta y
# fecha_pago en NULO. Un futuro correo de recordatorio solo tiene que
# buscar los que tengan fecha_pago NULO y fecha_pedido de hace X días.

def crear_o_actualizar_pedido_inicial(pedido_id, datos):
    """
    Crea (o actualiza si ya existe) la fila de 'pedidos' para este libro.
    'datos' puede traer: cliente_id, formato, orientacion, paginas.
    fecha_pedido se pone sola (default now() en la tabla); fecha_pago se
    queda en NULO hasta que se pague de verdad - eso todavía no está
    conectado, se hace mañana junto con Stripe.
    """
    fila = {k: v for k, v in datos.items() if v is not None}
    fila["id"] = pedido_id
    supabase.table("pedidos").upsert(fila, on_conflict="id").execute()


def obtener_subcarpeta_pedido(pedido_id):
    """Devuelve el ID de la subcarpeta de Drive de este pedido, si ya existe."""
    resp = supabase.table("pedidos").select(
        "subcarpeta_drive_id"
    ).eq("id", pedido_id).execute()
    if resp.data and resp.data[0].get("subcarpeta_drive_id"):
        return resp.data[0]["subcarpeta_drive_id"]
    return None


def guardar_subcarpeta_pedido(pedido_id, subcarpeta_drive_id, cliente_id=None):
    """
    Guarda el ID de la subcarpeta creada para este pedido concreto. Usa
    upsert (no update) porque puede llamarse antes de que exista la fila
    del pedido (por ejemplo si el vídeo se sube durante la edición, antes
    de generar el PDF final) - en ese caso el upsert tiene que INSERTAR
    una fila nueva, y 'cliente_id' es obligatorio (NOT NULL) en la tabla,
    así que hay que pasarlo aquí si se conoce (si la fila ya existe,
    Postgres simplemente ignora este valor y no toca el que ya hubiera).
    """
    fila = {"id": pedido_id, "subcarpeta_drive_id": subcarpeta_drive_id}
    if cliente_id:
        fila["cliente_id"] = cliente_id
    supabase.table("pedidos").upsert(fila, on_conflict="id").execute()


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
# 'libros' guarda el DISEÑO del álbum (para poder recuperarlo si el
# cliente escribe pidiendo ayuda); 'pedidos' guarda el estado comercial
# (pagado o no, Stripe, Gelato, factura...) de ese mismo id. No se repite
# el email del cliente aquí - se llega a él por pedidos.cliente_id -> 
# clientes.email cuando haga falta.

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