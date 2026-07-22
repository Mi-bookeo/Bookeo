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
#  CLIENTE — carpeta principal de Drive + refresh_token
# ═══════════════════════════════════════════════════════

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