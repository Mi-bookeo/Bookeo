"""
Bookeo · facturacion_drive.py
Sube el PDF del ticket o la factura de cada venta a la carpeta del mes
que corresponda, en TU Drive de negocio (no el de los clientes).

Estructura ya existente en tu Drive (creada a mano por ti, de antes):
  Tickets/
    2026/
      9.SEPTIEMBRE/
      10.OCTUBRE/
      ...
  Facturas/
    2026/
      9.SEPTIEMBRE/
      ...

Usa la autorización ÚNICA de la cuenta de negocio (ver
GOOGLE_NEGOCIO_REFRESH_TOKEN y /auth/google-negocio/iniciar en
google_auth.py / main.py) - con permiso amplio de Drive ('drive', no
'drive.file'), necesario porque estas carpetas ya existían de antes y
no las creó esta aplicación.
"""

import os
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_NEGOCIO_REFRESH_TOKEN = os.environ.get("GOOGLE_NEGOCIO_REFRESH_TOKEN", "")

SCOPES = ["https://www.googleapis.com/auth/drive"]

MESES_MAYUSCULAS = [
    "ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO",
    "JULIO", "AGOSTO", "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE",
]


def log(msg, e="→"):
    print(f"[facturacion_drive] {e} {msg}")


def obtener_servicio_drive_negocio():
    if not GOOGLE_NEGOCIO_REFRESH_TOKEN:
        raise RuntimeError(
            "Falta GOOGLE_NEGOCIO_REFRESH_TOKEN - visita /auth/google-negocio/iniciar "
            "una vez con la cuenta de negocio y copia el token que te enseñe a Railway."
        )
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_NEGOCIO_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


def _nombre_carpeta_mes(fecha):
    """'9.SEPTIEMBRE' - sin cero delante, tal como ya las tienes creadas."""
    return f"{fecha.month}.{MESES_MAYUSCULAS[fecha.month - 1]}"


def buscar_carpeta(service, nombre, carpeta_padre_id=None):
    """
    Busca una carpeta por su nombre EXACTO, opcionalmente dentro de otra
    carpeta - nunca la crea, solo la busca. Devuelve su id o None.
    """
    condiciones = [
        f"name = '{nombre}'",
        "mimeType = 'application/vnd.google-apps.folder'",
        "trashed = false",
    ]
    if carpeta_padre_id:
        condiciones.append(f"'{carpeta_padre_id}' in parents")
    resultado = service.files().list(q=" and ".join(condiciones), fields="files(id)", pageSize=1).execute()
    encontrados = resultado.get("files", [])
    return encontrados[0]["id"] if encontrados else None


def obtener_o_crear_carpeta(service, nombre, carpeta_padre_id=None):
    """
    Igual que buscar_carpeta, pero si no existe la crea - para meses/años
    futuros que todavía no tengas hechos a mano.
    """
    carpeta_id = buscar_carpeta(service, nombre, carpeta_padre_id)
    if carpeta_id:
        return carpeta_id
    metadata = {"name": nombre, "mimeType": "application/vnd.google-apps.folder"}
    if carpeta_padre_id:
        metadata["parents"] = [carpeta_padre_id]
    carpeta = service.files().create(body=metadata, fields="id").execute()
    log(f"Carpeta nueva creada: {nombre}", "📁")
    return carpeta["id"]


def obtener_carpeta_destino(service, tipo, fecha):
    """
    tipo: 'ticket' o 'factura'. fecha: datetime.date (o datetime) de la
    venta. Devuelve el id de la carpeta del mes correspondiente,
    buscando primero (respetando lo que ya tengas hecho a mano) y
    creando solo lo que falte (años/meses nuevos que aún no existan).
    """
    nombre_raiz = "Tickets" if tipo == "ticket" else "Facturas"
    raiz_id = obtener_o_crear_carpeta(service, nombre_raiz)
    anio_id = obtener_o_crear_carpeta(service, str(fecha.year), raiz_id)
    mes_id = obtener_o_crear_carpeta(service, _nombre_carpeta_mes(fecha), anio_id)
    return mes_id


def subir_documento(ruta_local, nombre_archivo, tipo, fecha):
    """
    Punto de entrada único - sube ruta_local (un PDF ya generado) a la
    carpeta del mes que corresponde según 'tipo' ('ticket'/'factura') y
    'fecha'. Devuelve (url_publica, file_id).
    """
    service = obtener_servicio_drive_negocio()
    carpeta_id = obtener_carpeta_destino(service, tipo, fecha)
    metadata = {"name": nombre_archivo, "parents": [carpeta_id]}
    media = MediaFileUpload(ruta_local, resumable=True)
    archivo = service.files().create(body=metadata, media_body=media, fields="id, webViewLink").execute()
    file_id = archivo["id"]
    url = archivo.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
    log(f"{tipo} subido: {nombre_archivo} → {url}", "🧾")
    return url, file_id
