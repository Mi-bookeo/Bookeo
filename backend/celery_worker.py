"""
BOOKEO · Celery Worker
Procesa la generación de libros en paralelo.
Cada pedido es una tarea independiente — hasta 20 clientes
generando su libro al mismo tiempo sin esperar al otro.
"""

import os
from celery import Celery

# Redis incluido en Railway Plan Hobby — sin coste extra
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

app = Celery("bookeo", broker=REDIS_URL, backend=REDIS_URL)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Europe/Madrid",
    enable_utc=True,
    task_track_started=True,
    # Tiempo máximo por libro: 15 minutos
    task_time_limit=900,
    task_soft_time_limit=840,
)

@app.task(bind=True, name="generar_libro")
def generar_libro(self, datos: dict):
    """
    Tarea Celery para generar un libro.
    Se lanza desde main.py cuando el cliente confirma el pago.
    
    datos = {
        "pedido_id": "uuid",
        "fotos_rutas": [...],
        "videos_rutas": [...],
        "titulo": "Mi álbum",
        "nombre_cliente": "Ana",
        "qr_urls": {"video.mp4": "https://drive.google.com/..."},
        "carpeta_sal": "/tmp/pedido_uuid",
        "carpeta_temp": "/tmp/pedido_uuid/temp",
    }
    """
    import sys
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from bookeo_crear_libro import crear_libro

    pedido_id = datos.get("pedido_id", "sin_id")

    try:
        # Actualizar estado en Supabase
        self.update_state(state="PROGRESS", meta={"estado": "generando", "pedido_id": pedido_id})

        ruta_pdf = crear_libro(
            fotos_rutas    = datos["fotos_rutas"],
            videos_rutas   = datos.get("videos_rutas", []),
            titulo         = datos["titulo"],
            nombre_cliente = datos["nombre_cliente"],
            qr_urls        = datos.get("qr_urls", {}),
            carpeta_sal    = datos["carpeta_sal"],
            carpeta_temp   = datos.get("carpeta_temp", datos["carpeta_sal"] + "/temp"),
        )

        return {"ok": True, "pdf": ruta_pdf, "pedido_id": pedido_id}

    except Exception as e:
        self.update_state(state="FAILURE", meta={"error": str(e), "pedido_id": pedido_id})
        raise
