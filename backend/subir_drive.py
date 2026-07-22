"""
Bookeo · subir_drive.py
Gestiona la subida de vídeos al Google Drive del CLIENTE (no el tuyo):

Estructura de carpetas en el Drive del cliente:
  Mibookeo (NO BORRAR)/       ← una sola vez por cliente, se reutiliza siempre
    └── [Título del álbum]/   ← una subcarpeta nueva por cada pedido

Cada cliente autoriza su propia cuenta mediante el flujo OAuth
(ver google_auth.py). El refresh_token y la carpeta principal se
guardan en Supabase (tabla 'clientes'); la subcarpeta de cada pedido
se guarda en Supabase (tabla 'pedidos').

REQUIERE:
  pip install google-api-python-client google-auth google-auth-oauthlib
"""

import os
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

from supabase_client import (
    obtener_cliente_drive,
    guardar_carpeta_principal_cliente,
    obtener_subcarpeta_pedido,
    guardar_subcarpeta_pedido,
)

GOOGLE_CLIENTES_CLIENT_ID     = os.environ.get("GOOGLE_CLIENTES_CLIENT_ID", "")
GOOGLE_CLIENTES_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENTES_CLIENT_SECRET", "")

NOMBRE_CARPETA_PRINCIPAL = "Mibookeo (NO BORRAR)"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def log(msg, e="→"):
    print(f"[subir_drive] {e} {msg}")


# ═══════════════════════════════════════════════════════
#  AUTENTICACIÓN — usando el refresh_token del CLIENTE
# ═══════════════════════════════════════════════════════

def obtener_servicio_drive(refresh_token_cliente):
    creds = Credentials(
        token=None,
        refresh_token=refresh_token_cliente,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENTES_CLIENT_ID,
        client_secret=GOOGLE_CLIENTES_CLIENT_SECRET,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


# ═══════════════════════════════════════════════════════
#  PERMISOS — "cualquiera con el enlace puede ver"
# ═══════════════════════════════════════════════════════

def hacer_publico(service, file_id):
    permiso = {"type": "anyone", "role": "reader"}
    service.permissions().create(fileId=file_id, body=permiso).execute()
    log(f"Permiso público aplicado a {file_id}", "🔓")


# ═══════════════════════════════════════════════════════
#  CARPETA PRINCIPAL — una sola vez por cliente, se reutiliza
# ═══════════════════════════════════════════════════════

def obtener_o_crear_carpeta_principal(service, cliente_id):
    """
    Busca en Supabase si este cliente ya tiene la carpeta principal
    'Mibookeo (NO BORRAR)'. Si existe, la reutiliza. Si no, la crea
    una única vez y guarda su ID en la tabla 'clientes'.
    """
    datos_cliente = obtener_cliente_drive(cliente_id)
    carpeta_id = datos_cliente.get("carpeta_drive_id") if datos_cliente else None

    if carpeta_id:
        try:
            service.files().get(fileId=carpeta_id, fields="id").execute()
            log(f"Carpeta principal reutilizada: {carpeta_id}", "📁")
            return carpeta_id
        except HttpError:
            log("Carpeta principal guardada no encontrada, se creará una nueva", "⚠")

    metadata = {
        "name": NOMBRE_CARPETA_PRINCIPAL,
        "mimeType": "application/vnd.google-apps.folder",
    }
    carpeta = service.files().create(body=metadata, fields="id").execute()
    carpeta_id = carpeta["id"]
    hacer_publico(service, carpeta_id)
    guardar_carpeta_principal_cliente(cliente_id, carpeta_id)

    log(f"Carpeta principal creada para cliente {cliente_id}: {carpeta_id}", "✅")
    return carpeta_id


# ═══════════════════════════════════════════════════════
#  SUBCARPETA DEL PEDIDO — una nueva por cada álbum
# ═══════════════════════════════════════════════════════

def obtener_o_crear_subcarpeta_pedido(service, carpeta_principal_id, pedido_id, nombre_album):
    """
    Busca si este pedido concreto ya tiene su subcarpeta creada.
    Si no, la crea dentro de la carpeta principal, usando el título
    del álbum como nombre.
    """
    subcarpeta_id = obtener_subcarpeta_pedido(pedido_id)

    if subcarpeta_id:
        try:
            service.files().get(fileId=subcarpeta_id, fields="id").execute()
            log(f"Subcarpeta del pedido reutilizada: {subcarpeta_id}", "📁")
            return subcarpeta_id
        except HttpError:
            log("Subcarpeta guardada no encontrada, se creará una nueva", "⚠")

    metadata = {
        "name": nombre_album or f"Pedido {pedido_id}",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [carpeta_principal_id],
    }
    subcarpeta = service.files().create(body=metadata, fields="id").execute()
    subcarpeta_id = subcarpeta["id"]
    hacer_publico(service, subcarpeta_id)
    guardar_subcarpeta_pedido(pedido_id, subcarpeta_id)

    log(f"Subcarpeta creada para pedido {pedido_id}: {subcarpeta_id}", "✅")
    return subcarpeta_id


# ═══════════════════════════════════════════════════════
#  SUBIR VÍDEO
# ═══════════════════════════════════════════════════════

def subir_video(service, ruta_local, nombre_archivo, subcarpeta_id):
    metadata = {"name": nombre_archivo, "parents": [subcarpeta_id]}
    media = MediaFileUpload(ruta_local, resumable=True)

    archivo = service.files().create(
        body=metadata, media_body=media, fields="id, webViewLink"
    ).execute()

    file_id = archivo["id"]
    hacer_publico(service, file_id)

    url = archivo.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
    log(f"Vídeo subido: {nombre_archivo} → {url}", "🎬")
    return url, file_id


# ═══════════════════════════════════════════════════════
#  FUNCIÓN PRINCIPAL — llamada por el backend FastAPI
# ═══════════════════════════════════════════════════════

def procesar_video(ruta_local, nombre_archivo, cliente_id, pedido_id,
                    refresh_token_cliente, nombre_album=None):
    """
    Punto de entrada único para subir un vídeo.

    ruta_local            → ruta temporal del vídeo ya guardado en disco
    nombre_archivo        → nombre del archivo (para Drive)
    cliente_id            → ID del cliente en Supabase (para la carpeta principal)
    pedido_id             → ID del pedido en Supabase (para la subcarpeta)
    refresh_token_cliente → token OAuth del cliente
    nombre_album          → título del álbum, usado como nombre de la subcarpeta

    Devuelve: (url_publica, file_id)
    """
    service = obtener_servicio_drive(refresh_token_cliente)
    carpeta_principal_id = obtener_o_crear_carpeta_principal(service, cliente_id)
    subcarpeta_id = obtener_o_crear_subcarpeta_pedido(
        service, carpeta_principal_id, pedido_id, nombre_album
    )
    url, file_id = subir_video(service, ruta_local, nombre_archivo, subcarpeta_id)
    return url, file_id
