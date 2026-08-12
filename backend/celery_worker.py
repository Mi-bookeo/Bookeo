"""
BOOKEO · Celery Worker
Procesa la generación de libros en paralelo.
Cada pedido es una tarea independiente.

Dos tareas:
  - calcular_paginas_libro  → FASE RÁPIDA (editor). Calcula qué foto va en
    qué página, sin dibujar PDF, y publica cada página por Redis pub/sub
    según se van resolviendo, para que main.py las reenvíe por WebSocket
    al editor y el cliente las vea aparecer una a una.
  - generar_libro           → FASE FINAL (PDF de verdad). Si recibe
    'estructura_editada' (lo que el cliente dejó en el editor), la usa tal
    cual. Si no, recalcula desde cero - así el flujo actual sin editor
    sigue funcionando exactamente igual que hasta ahora.
"""

import os
import json
from celery import Celery
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from r2_storage import subir_a_r2, generar_url_descarga

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


def _cliente_redis():
    """Cliente Redis para publicar en el canal del editor. 'redis' viaja
    junto con Celery casi siempre, pero si el requirements.txt no lo tiene
    explícito, hay que añadirlo (redis>=4)."""
    import redis
    return redis.from_url(REDIS_URL)


def _canal_editor(pedido_id):
    return f"bookeo:editor:{pedido_id}"


def _url_foto_r2(nombre, fotos_r2):
    """URL descargable en R2 para una foto - el editor corre en el
    navegador del cliente, así que las rutas locales del disco del worker
    no le sirven de nada, necesita una URL de verdad."""
    clave = fotos_r2.get(nombre)
    if not clave:
        return None
    try:
        return generar_url_descarga(clave, nombre_descarga=nombre)
    except Exception:
        return None


def _pagina_con_urls(pagina, fotos_r2):
    """Copia una página añadiendo 'url' (R2) a cada foto, sin tocar 'ruta'
    (esa la seguirá necesitando el worker más adelante para el PDF final)."""
    pagina_copia = dict(pagina)
    fotos_con_url = []
    for foto in pagina.get("fotos", []):
        foto_copia = dict(foto)
        foto_copia["url"] = _url_foto_r2(foto.get("nombre", ""), fotos_r2)
        fotos_con_url.append(foto_copia)
    pagina_copia["fotos"] = fotos_con_url
    return pagina_copia


@app.task(bind=True, name="calcular_paginas_libro")
def calcular_paginas_libro(self, datos: dict):
    """
    FASE DE CÁLCULO (rápida, para el editor).
    Calcula la estructura completa del libro - sin dibujar PDF - y va
    publicando cada página por Redis pub/sub según se resuelven, para que
    main.py las reenvíe por WebSocket al editor y aparezcan progresivamente.
    """
    from crear_libro_railway import calcular_estructura_libro_completo

    pedido_id = datos.get("pedido_id", "sin_id")
    fotos_r2 = datos.get("fotos_r2", {})
    redis_cliente = _cliente_redis()
    canal = _canal_editor(pedido_id)

    try:
        self.update_state(state="PROGRESS", meta={"estado": "calculando_paginas", "pedido_id": pedido_id})

        resultado = calcular_estructura_libro_completo(
            diseño=datos["diseño"],
            fotos=datos["fotos"],
            videos_rutas=datos["videos_rutas"],
            qr_urls=datos["qr_urls"],
            portada_elegida=datos["portada_elegida"],
            carpeta_temp=datos["carpeta_temp"],
            formato=datos["formato"],
            orientacion=datos["orientacion"],
            caso_reparto=datos["caso_reparto"],
            paginas_objetivo=datos["paginas_objetivo"],
            fotos_r2=fotos_r2,
            videos_r2=datos.get("videos_r2", {}),
            pedido_id=pedido_id,
        )

        paginas = resultado["paginas"]
        total = len(paginas)
        paginas_con_url = [_pagina_con_urls(p, fotos_r2) for p in paginas]

        # GUARDAR UNA "FOTO FIJA" EN REDIS ANTES DE PUBLICAR NADA.
        # Este calculo es tan rapido (no dibuja nada) que puede terminar
        # antes de que el navegador termine de cargar editor.html y abra el
        # WebSocket - el pub/sub de Redis NO guarda mensajes para quien
        # llega tarde, se perderian para siempre. Por eso se guarda tambien
        # una copia persistente (con caducidad) que el WebSocket puede leer
        # si llega despues de que esto ya haya terminado.
        clave_snapshot = f"bookeo:editor:snapshot:{pedido_id}"
        snapshot = {
            "tipo": "completo",
            "pedido_id": pedido_id,
            "total_paginas": total,
            "AW": resultado["AW"], "AH": resultado["AH"],
            "titulo": resultado["titulo"], "subtitulo": resultado["subtitulo"],
            "paginas": paginas_con_url,
        }
        redis_cliente.set(clave_snapshot, json.dumps(snapshot), ex=3600)  # 1 hora de margen

        # El cálculo en sí ya está hecho (es rápido, no dibuja nada), pero
        # se publica página a página para que el editor las vaya pintando
        # una a una, en vez de esperar a recibir el JSON entero de golpe.
        # Esto sigue sirviendo para el caso normal: navegador ya conectado
        # y esperando en directo.
        for indice, pagina in enumerate(paginas_con_url):
            mensaje = {
                "tipo": "pagina",
                "pedido_id": pedido_id,
                "indice": indice,
                "total_paginas": total,
                "pagina": pagina,
            }
            redis_cliente.publish(canal, json.dumps(mensaje))

        mensaje_final = {
            "tipo": "completo",
            "pedido_id": pedido_id,
            "total_paginas": total,
            "AW": resultado["AW"], "AH": resultado["AH"],
            "titulo": resultado["titulo"], "subtitulo": resultado["subtitulo"],
        }
        redis_cliente.publish(canal, json.dumps(mensaje_final))

        return {
            "ok": True,
            "pedido_id": pedido_id,
            "total_paginas": total,
            "paginas": paginas,   # estructura completa -> se guarda como borrador inicial
            "AW": resultado["AW"], "AH": resultado["AH"],
            "titulo": resultado["titulo"], "subtitulo": resultado["subtitulo"],
        }

    except Exception as e:
        try:
            redis_cliente.publish(canal, json.dumps({
                "tipo": "error", "pedido_id": pedido_id, "error": str(e)
            }))
        except Exception:
            pass
        self.update_state(state="FAILURE", meta={"error": str(e), "pedido_id": pedido_id})
        raise

    finally:
        try:
            redis_cliente.close()
        except Exception:
            pass


@app.task(bind=True, name="generar_libro")
def generar_libro(self, datos: dict):
    """
    Tarea Celery para generar el PDF completo de un libro.

    Si 'datos' trae 'estructura_editada' (la lista de páginas que devolvió
    el editor con los cambios del cliente ya aplicados), se usa tal cual -
    el PDF final respeta exactamente lo que el cliente dejó editado.

    Si no la trae (flujo sin editor, como hasta ahora), se recalcula desde
    cero - se comporta exactamente igual que antes de este cambio.
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
            estructura_editada=datos.get("estructura_editada"),
        )

        # Subir el PDF final a R2 para que el cliente pueda verlo/descargarlo
        # de verdad - antes se devolvía solo la ruta local del worker, que
        # no es accesible desde el navegador del cliente.
        nombre_pdf = os.path.basename(ruta_pdf)
        clave_r2_pdf = subir_a_r2(ruta_pdf, pedido_id, nombre_pdf)
        url_descarga = generar_url_descarga(clave_r2_pdf, nombre_descarga=nombre_pdf)

        return {
            "ok": True,
            "pdf": ruta_pdf,
            "pdf_url": url_descarga,
            "pedido_id": pedido_id,
        }

    except Exception as e:
        self.update_state(state="FAILURE", meta={"error": str(e), "pedido_id": pedido_id})
        raise