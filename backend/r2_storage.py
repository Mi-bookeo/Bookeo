"""
BOOKEO · Almacenamiento temporal de fotos/vídeos de pedidos en Cloudflare R2
Permite que el servicio web (main.py) y el worker de Celery
(celery_worker.py / crear_libro_railway.py) compartan archivos aunque
corran en contenedores distintos, ya que Railway no soporta discos
compartidos entre servicios.
"""

import os
import boto3
from botocore.config import Config

R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_ENDPOINT_URL = os.environ.get("R2_ENDPOINT_URL")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "bookeo-fotos-temp")

_cliente = None


def obtener_cliente_r2():
    global _cliente
    if _cliente is None:
        _cliente = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT_URL,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
    return _cliente


def subir_a_r2(ruta_local, pedido_id, nombre_archivo):
    """Sube un archivo local a R2 bajo la clave pedido_id/nombre_archivo.
    Devuelve la clave (string) para guardarla y mandarla luego al worker."""
    cliente = obtener_cliente_r2()
    clave = f"{pedido_id}/{nombre_archivo}"
    cliente.upload_file(ruta_local, R2_BUCKET_NAME, clave)
    return clave


def descargar_de_r2(clave, destino_local):
    """Descarga un objeto de R2 a una ruta local del contenedor actual."""
    cliente = obtener_cliente_r2()
    cliente.download_file(R2_BUCKET_NAME, clave, destino_local)


def generar_url_descarga(clave, expira_segundos=3600 * 24, nombre_descarga=None):
    """Genera un enlace temporal para que el navegador del cliente descargue
    un archivo directamente de R2, sin pasar por el servidor. Por defecto
    caduca en 24 horas."""
    cliente = obtener_cliente_r2()
    params = {"Bucket": R2_BUCKET_NAME, "Key": clave}
    if nombre_descarga:
        params["ResponseContentDisposition"] = f'attachment; filename="{nombre_descarga}"'
    return cliente.generate_presigned_url(
        "get_object", Params=params, ExpiresIn=expira_segundos
    )


def borrar_carpeta_pedido_r2(pedido_id):
    """Borra todos los objetos de R2 bajo el prefijo pedido_id/.
    Se usará más adelante al confirmarse el pago (Stripe webhook)."""
    cliente = obtener_cliente_r2()
    paginator = cliente.get_paginator("list_objects_v2")
    claves = []
    for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=f"{pedido_id}/"):
        for obj in page.get("Contents", []):
            claves.append({"Key": obj["Key"]})
    if claves:
        cliente.delete_objects(Bucket=R2_BUCKET_NAME, Delete={"Objects": claves})
