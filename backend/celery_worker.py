"""
BOOKEO · Celery Worker
Procesa la generación de libros en paralelo.
Cada pedido es una tarea independiente.
"""

import os
from celery import Celery
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

app = Celery("bookeo", broker=REDIS_URL, backend=REDIS_URL)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Europe/Madrid",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=900,
    task_soft_time_limit=840,
)


@app.task(bind=True, name="generar_libro")
def generar_libro(self, datos: dict):
    """
    Tarea Celery para generar el PDF completo de un libro.
    Se lanza desde main.py cuando el cliente confirma la portada.
    """
    from crear_libro_railway import generar_pdf_completo

    pedido_id = datos.get("pedido_id", "sin_id")

    try:
        self.update_state(state="PROGRESS", meta={"estado": "generando", "pedido_id": pedido_id})

        ruta_pdf = generar_pdf_completo(
            diseño=datos["diseño"],
            fotos=datos["fotos"],
            videos_rutas=datos["videos_rutas"],
            qr_urls=datos["qr_urls"],
            portada_elegida=datos["portada_elegida"],
            nombre_cliente=datos["nombre_cliente"],
            carpeta_sal=datos["carpeta_sal"],
            carpeta_temp=datos["carpeta_temp"],
            formato=datos["formato"],
            orientacion=datos["orientacion"],
            caso_reparto=datos["caso_reparto"],
            paginas_objetivo=datos["paginas_objetivo"],
            fotos_r2=datos.get("fotos_r2", {}),
            videos_r2=datos.get("videos_r2", {}),
        )

        return {"ok": True, "pdf": ruta_pdf, "pedido_id": pedido_id}

    except Exception as e:
        self.update_state(state="FAILURE", meta={"error": str(e), "pedido_id": pedido_id})
        raise