"""
Bookeo · subir_drive.py
Gestiona la subida de vídeos al Google Drive del CLIENTE (no el tuyo):
crea (o reutiliza) la carpeta "Mibookeo (NO BORRAR)" dentro de SU Drive,
sube el vídeo, la hace pública y devuelve la URL para el QR.

Cada cliente autoriza su propia cuenta mediante el flujo OAuth
(ver google_auth.py). El refresh_token de cada cliente se guarda en
Supabase y se pasa aquí como parámetro — nunca se usa un token fijo.

Se llama tanto desde el flujo inicial de subida (endpoint /crear-pedido)
como desde el visor Fabric.js cuando el cliente añade un vídeo nuevo
(endpoint /viewer/add-video, pendiente de crear). Misma función.

REQUIERE:
  pip install google-api-python-client google-auth google-auth-oauthlib
"""

import os
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# ═══════════════════════════════════════════════════════
#  CONFIGURACIÓN — credenciales de la app (variables de Railway)
#  Estas identifican a TU APLICACIÓN Bookeo, no a un usuario.
#  El refresh_token de cada cliente se recibe como parámetro.
# ═══════════════════════════════════════════════════════

GOOGLE_CLIENTES_CLIENT_ID     = os.environ.get("GOOGLE_CLIENTES_CLIENT_ID", "")
GOOGLE_CLIENTES_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENTES_CLIENT_SECRET", "")

NOMBRE_CARPETA = "Mibookeo (NO BORRAR)"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def log(msg, e="→"):
    print(f"[subir_drive] {e} {msg}")


# ═══════════════════════════════════════════════════════
#  AUTENTICACIÓN — usando el refresh_token del CLIENTE
# ═══════════════════════════════════════════════════════

def obtener_servicio_drive(refresh_token_cliente):
    """
    Crea el cliente autenticado de Google Drive usando el refresh_token
    del CLIENTE (obtenido tras su login OAuth), para operar sobre SU
    Drive, no el tuyo.
    """
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
#  CARPETA DEL CLIENTE — crear o reutilizar
# ═══════════════════════════════════════════════════════

def obtener_o_crear_carpeta(service, pedido_id, carpeta_id_existente=None):
    """
    Devuelve el ID de la carpeta dentro del Drive del cliente.
    Si ya existe (guardado en Supabase, tabla 'pedidos'), la reutiliza.
    Si no existe, la crea y hay que guardar el ID devuelto en Supabase
    para que las siguientes subidas (desde el visor) usen la misma.
    """
    if carpeta_id_existente:
        try:
            service.files().get(fileId=carpeta_id_existente, fields="id").execute()
            log(f"Carpeta existente reutilizada: {carpeta_id_existente}", "📁")
            return carpeta_id_existente
        except HttpError:
            log("Carpeta guardada no encontrada, se creará una nueva", "⚠")

    metadata = {
        "name": NOMBRE_CARPETA,
        "mimeType": "application/vnd.google-apps.folder",
    }
    carpeta = service.files().create(body=metadata, fields="id").execute()
    carpeta_id = carpeta["id"]

    hacer_publico(service, carpeta_id)

    log(f"Carpeta creada y pública en Drive del cliente: {carpeta_id} (pedido {pedido_id})", "✅")
    return carpeta_id


# ═══════════════════════════════════════════════════════
#  PERMISOS — "cualquiera con el enlace puede ver"
# ═══════════════════════════════════════════════════════

def hacer_publico(service, file_id):
    """
    Aplica el permiso público a una carpeta o archivo.
    type='anyone', role='reader' → cualquiera con el enlace puede verlo,
    sin necesidad de iniciar sesión en Google.
    """
    permiso = {"type": "anyone", "role": "reader"}
    service.permissions().create(fileId=file_id, body=permiso).execute()
    log(f"Permiso público aplicado a {file_id}", "🔓")


# ═══════════════════════════════════════════════════════
#  SUBIR VÍDEO
# ═══════════════════════════════════════════════════════

def subir_video(service, ruta_local, nombre_archivo, carpeta_id):
    """
    Sube el vídeo a la carpeta dentro del Drive del cliente
    y devuelve la URL pública.
    """
    metadata = {"name": nombre_archivo, "parents": [carpeta_id]}
    media = MediaFileUpload(ruta_local, resumable=True)

    archivo = service.files().create(
        body=metadata, media_body=media, fields="id, webViewLink"
    ).execute()

    file_id = archivo["id"]
    # El archivo hereda el permiso público de la carpeta, pero por seguridad
    # lo confirmamos también a nivel de archivo individual.
    hacer_publico(service, file_id)

    url = archivo.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
    log(f"Vídeo subido al Drive del cliente: {nombre_archivo} → {url}", "🎬")
    return url, file_id


# ═══════════════════════════════════════════════════════
#  FUNCIÓN PRINCIPAL — llamada por el backend FastAPI
#  Úsala tanto en /crear-pedido (subida inicial) como en
#  /viewer/add-video (cuando el cliente añade un vídeo en Fabric.js)
# ═══════════════════════════════════════════════════════

def procesar_video(ruta_local, nombre_archivo, pedido_id, refresh_token_cliente, carpeta_id_existente=None):
    """
    Punto de entrada único para subir un vídeo al Drive DEL CLIENTE
    y generar su URL de QR.

    ruta_local            → ruta temporal del vídeo ya guardado en disco
    nombre_archivo        → nombre del archivo (para Drive)
    pedido_id             → ID del pedido/cliente (para logs y trazabilidad)
    refresh_token_cliente → token OAuth del cliente, obtenido tras su login
                            (recuperado de Supabase, tabla 'pedidos')
    carpeta_id_existente  → ID de carpeta ya creada para este pedido
                            (recupéralo de Supabase si el cliente ya subió
                            vídeos antes; si es None, se crea una nueva)

    Devuelve: (url_publica, file_id, carpeta_id)
    IMPORTANTE: guarda carpeta_id en Supabase (tabla 'pedidos') la primera
    vez, para que futuras llamadas reutilicen la misma carpeta.
    """
    service = obtener_servicio_drive(refresh_token_cliente)
    carpeta_id = obtener_o_crear_carpeta(service, pedido_id, carpeta_id_existente)
    url, file_id = subir_video(service, ruta_local, nombre_archivo, carpeta_id)
    return url, file_id, carpeta_id
