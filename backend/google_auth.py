"""
Bookeo · google_auth.py
Gestiona el flujo OAuth de Google Drive para clientes (no el tuyo de facturas).

El cliente autoriza su cuenta → Google nos da un code → lo cambiamos
por un refresh_token + el EMAIL del cliente (para poder crearlo o
identificarlo en Supabase sin pedirle el email por separado).
"""

import os
import requests
from google_auth_oauthlib.flow import Flow

GOOGLE_CLIENTES_CLIENT_ID     = os.environ.get("GOOGLE_CLIENTES_CLIENT_ID", "")
GOOGLE_CLIENTES_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENTES_CLIENT_SECRET", "")
REDIRECT_URI = "https://bookeo-production.up.railway.app/auth/google/callback"

# 'openid' y 'email' → para poder identificar al cliente por su correo
# 'drive.file' → para poder crear/gestionar solo los archivos que sube Bookeo
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
]

CLIENT_CONFIG = {
    "web": {
        "client_id": GOOGLE_CLIENTES_CLIENT_ID,
        "client_secret": GOOGLE_CLIENTES_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [REDIRECT_URI],
    }
}


def generar_url_autorizacion():
    """
    Genera la URL a la que se redirige al cliente para que autorice
    su cuenta de Google. Ya no hace falta pasar cliente_id ni pedido_id
    en el 'state', porque todavía no existen en este punto — se crean
    DESPUÉS, cuando recibamos su email.
    """
    flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    return url


def intercambiar_codigo_por_token_y_email(code):
    """
    Cambia el código de autorización por el refresh_token del cliente,
    y además consulta su email usando el access_token recién obtenido.
    Devuelve (refresh_token, email).
    """
    flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    flow.fetch_token(code=code)
    credentials = flow.credentials

    # Pedimos el email del cliente usando su access_token
    resp = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {credentials.token}"},
    )
    resp.raise_for_status()
    email = resp.json().get("email")

    return credentials.refresh_token, email
