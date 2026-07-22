"""
Bookeo · google_auth.py
Gestiona el flujo OAuth de Google Drive para clientes (no el tuyo de facturas).
Cada cliente autoriza su propia cuenta; el refresh token resultante
se guarda en Supabase asociado a su pedido_id.
"""

import os
from google_auth_oauthlib.flow import Flow

GOOGLE_CLIENTES_CLIENT_ID     = os.environ.get("GOOGLE_CLIENTES_CLIENT_ID", "")
GOOGLE_CLIENTES_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENTES_CLIENT_SECRET", "")
REDIRECT_URI = "https://bookeo-production.up.railway.app/auth/google/callback"

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

CLIENT_CONFIG = {
    "web": {
        "client_id": GOOGLE_CLIENTES_CLIENT_ID,
        "client_secret": GOOGLE_CLIENTES_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [REDIRECT_URI],
    }
}


def generar_url_autorizacion(pedido_id):
    """
    Genera la URL a la que se redirige al cliente para que autorice
    su cuenta de Google. El pedido_id viaja en el parámetro 'state'
    para poder identificar a qué pedido pertenece cuando Google
    redirija de vuelta.
    """
    flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    url, _ = flow.authorization_url(
        access_type="offline",       # necesario para obtener refresh_token
        prompt="consent",            # fuerza a que Google dé refresh_token siempre
        state=pedido_id,
    )
    return url


def intercambiar_codigo_por_token(code):
    """
    Cambia el código de autorización que envía Google por el
    access_token + refresh_token del cliente.
    """
    flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    flow.fetch_token(code=code)
    credentials = flow.credentials
    return credentials.refresh_token