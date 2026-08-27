"""
Bookeo · subir_drive.py
Gestiona la subida de vídeos al Google Drive del CLIENTE (no el tuyo):

Estructura de carpetas en el Drive del cliente:
  Mibookeo (NO BORRAR)/       ← una sola vez por cliente, se reutiliza siempre
    └── [Título del álbum]/   ← una subcarpeta nueva por cada pedido

Cada cliente autoriza su propia cuenta mediante el flujo OAuth
(ver google_auth.py). El refresh_token y la carpeta principal se
guardan en Supabase (tabla 'clientes'). La subcarpeta de cada pedido
NO se guarda en Supabase - se localiza buscando directamente en Google
Drive (ver obtener_o_crear_subcarpeta_pedido) por una propiedad
invisible que se le pone a la carpeta al crearla. Así la subida de
vídeos nunca depende de si ya existe o no una fila de 'pedidos' (esa
tabla tiene sus propias columnas obligatorias para el pedido/pago, que
no tienen nada que ver con dónde vive la carpeta en Drive) - antes de
mezclar esto con Supabase, subir vídeos funcionaba siempre sin fallos
de este tipo, así que se ha vuelto a ese planteamiento más simple. Si
el cliente sube un vídeo y nunca termina de hacer el libro, el vídeo se
queda en su Drive y es cosa suya borrarlo si quiere - no es un problema
que haya que resolver aquí.

REQUIERE:
  pip install google-api-python-client google-auth google-auth-oauthlib
"""

import os
import threading
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

from supabase_client import (
    obtener_cliente_drive,
    guardar_carpeta_principal_cliente,
)

GOOGLE_CLIENTES_CLIENT_ID     = os.environ.get("GOOGLE_CLIENTES_CLIENT_ID", "")
GOOGLE_CLIENTES_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENTES_CLIENT_SECRET", "")

NOMBRE_CARPETA_PRINCIPAL = "Mibookeo (NO BORRAR)"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# ═══════════════════════════════════════════════════════
#  BLOQUEO POR PEDIDO — evita carpetas duplicadas
# ═══════════════════════════════════════════════════════
# Si el cliente sube dos vídeos seguidos (elige el segundo antes de que
# termine de subirse el primero), FastAPI atiende las dos peticiones casi
# a la vez en hilos distintos. Sin este bloqueo, las dos comprueban en
# Supabase "¿ya existe la subcarpeta de este pedido?" AL MISMO TIEMPO, ven
# que no, y las dos acaban creando su propia carpeta nueva - de ahí las
# carpetas duplicadas con el mismo nombre de álbum. Con este candado, la
# segunda petición espera a que la primera termine de crear (y guardar en
# Supabase) la subcarpeta, y así la encuentra y la reutiliza en vez de
# crear otra.
_locks_pedido = {}
_locks_pedido_guard = threading.Lock()


def _lock_para_pedido(pedido_id):
    with _locks_pedido_guard:
        if pedido_id not in _locks_pedido:
            _locks_pedido[pedido_id] = threading.Lock()
        return _locks_pedido[pedido_id]


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

def obtener_o_crear_subcarpeta_pedido(service, carpeta_principal_id, pedido_id, nombre_album, cliente_id=None):
    """
    Busca si este pedido concreto ya tiene su subcarpeta creada BUSCANDO
    DIRECTAMENTE EN GOOGLE DRIVE, no en Supabase - al crear la carpeta se
    le pone el pedido_id como una "propiedad" invisible para el cliente
    (no aparece en el nombre ni en la vista de Drive), y la próxima vez se
    busca por esa propiedad exacta. Así nunca depende de si existe o no
    una fila en la tabla 'pedidos' (que tiene sus propias columnas
    obligatorias para el pedido/pago, nada que ver con esto).
    """
    query = (
        f"'{carpeta_principal_id}' in parents and "
        "mimeType = 'application/vnd.google-apps.folder' and "
        f"properties has {{ key='pedido_id' and value='{pedido_id}' }} and "
        "trashed = false"
    )
    try:
        resultado = service.files().list(q=query, fields="files(id)", pageSize=1).execute()
        encontrados = resultado.get("files", [])
    except HttpError as e:
        log(f"No se pudo buscar la subcarpeta del pedido, se creará una nueva: {e}", "⚠")
        encontrados = []

    if encontrados:
        subcarpeta_id = encontrados[0]["id"]
        log(f"Subcarpeta del pedido reutilizada: {subcarpeta_id}", "📁")
        return subcarpeta_id

    metadata = {
        "name": nombre_album or f"Pedido {pedido_id}",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [carpeta_principal_id],
        "properties": {"pedido_id": pedido_id},
    }
    subcarpeta = service.files().create(body=metadata, fields="id").execute()
    subcarpeta_id = subcarpeta["id"]
    hacer_publico(service, subcarpeta_id)

    log(f"Subcarpeta creada para pedido {pedido_id}: {subcarpeta_id}", "✅")
    return subcarpeta_id


# ═══════════════════════════════════════════════════════
#  SUBIR VÍDEO
# ═══════════════════════════════════════════════════════

def _es_error_de_cuota(e):
    """
    Google Drive devuelve un HttpError 403 cuando al CLIENTE (no a
    Bookeo) no le queda espacio en su Drive para el archivo que se está
    subiendo. Se detecta mirando el código de estado y el contenido del
    error (la razón incluye "storageQuotaExceeded"), para poder avisar
    con un mensaje claro en vez del JSON técnico crudo que devuelve Google.
    """
    try:
        status = getattr(getattr(e, "resp", None), "status", None)
        contenido = e.content.decode("utf-8", errors="ignore") if getattr(e, "content", None) else str(e)
    except Exception:
        status, contenido = None, str(e)
    contenido_lower = contenido.lower()
    return status == 403 and ("storagequotaexceeded" in contenido_lower or "storage quota" in contenido_lower)


def subir_video(service, ruta_local, nombre_archivo, subcarpeta_id):
    metadata = {"name": nombre_archivo, "parents": [subcarpeta_id]}
    media = MediaFileUpload(ruta_local, resumable=True)

    try:
        archivo = service.files().create(
            body=metadata, media_body=media, fields="id, webViewLink"
        ).execute()
    except HttpError as e:
        if _es_error_de_cuota(e):
            log(f"Subida rechazada por falta de espacio en el Drive del cliente: {e}", "⚠")
            # Mensaje pensado para llegar tal cual hasta el aviso que ve
            # el cliente en el editor - por eso va en español y sin jerga
            # técnica, a diferencia del resto de logs de este archivo.
            raise RuntimeError(
                "Tu Google Drive no tiene espacio suficiente para subir este vídeo. "
                "Libera espacio o amplía tu almacenamiento en drive.google.com y vuelve a intentarlo."
            ) from e
        raise

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
    # Todo lo que decide "¿ya existe la subcarpeta de este pedido?" y la
    # crea si no, va bajo el candado de este pedido_id concreto - así dos
    # vídeos subidos casi a la vez para el MISMO libro nunca pueden crear
    # dos carpetas distintas (ver _lock_para_pedido más arriba).
    with _lock_para_pedido(pedido_id):
        subcarpeta_id = obtener_o_crear_subcarpeta_pedido(
            service, carpeta_principal_id, pedido_id, nombre_album, cliente_id=cliente_id
        )
    url, file_id = subir_video(service, ruta_local, nombre_archivo, subcarpeta_id)
    return url, file_id