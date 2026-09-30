# -*- coding: utf-8 -*-
"""
Endpoint suelto SOLO para probar gelato_client.py desde el navegador
(GET, sin necesidad de terminal ni script) - se añade a main.py con el
mismo patrón que ya usaste para admin_panel.py:

    from probar_gelato_router import router as probar_gelato_router
    app.include_router(probar_gelato_router)

Se puede borrar sin miedo en cuanto se dé por buena la conexión con
Gelato - no lo usa ninguna otra parte del sistema.

SIEMPRE crea el pedido en modo "draft" (nunca entra en producción),
pase lo que pase con GELATO_ORDENES_REALES - así esta prueba en
concreto no puede mandar un libro a imprenta por accidente aunque ese
interruptor ya esté en "1".
"""
import os
from fastapi import APIRouter, HTTPException

from gelato_client import crear_pedido_gelato

router = APIRouter()

ADMIN_REPETIR_CLAVE = os.environ.get("ADMIN_REPETIR_CLAVE", "")


@router.get("/admin/probar-gelato")
def admin_probar_gelato(clave: str, pdf_url: str, formato: str = "2020", numero_pedido: str = "PRUEBA-GELATO-1"):
    """
    Ejemplo de uso desde el navegador del móvil:

    https://bookeo-production.up.railway.app/admin/probar-gelato
      ?clave=TU_CLAVE
      &pdf_url=https://enlace-de-descarga-de-un-pdf-real-tuyo.pdf
      &formato=2128

    pdf_url tiene que ser un enlace de descarga DIRECTO a un PDF real
    (por ejemplo, el que te llega en el correo de confirmación de
    cualquier pedido tuyo de prueba - todavía válido si no pasaron los
    7 días). formato es opcional (2020/2128/2828, por defecto 2020).
    """
    if not ADMIN_REPETIR_CLAVE or clave != ADMIN_REPETIR_CLAVE:
        raise HTTPException(status_code=403, detail="Clave incorrecta")

    envio_de_prueba = {
        "nombre": "Prueba Bookeo",
        "telefono": "600000000",
        "correo": "hola@mibookeo.es",
        "direccion": "Calle de Prueba 1",
        "ciudad": "Tudela",
        "codigo_postal": "31500",
        "provincia": "Navarra",
        "pais": "ES",
    }

    try:
        resultado = crear_pedido_gelato(
            numero_pedido=numero_pedido,
            cliente_id="prueba-interna",
            formato=formato,
            cantidad=1,
            pdf_url=pdf_url,
            envio=envio_de_prueba,
            forzar_draft=True,  # blindado: esta prueba nunca crea un pedido real, pase lo que pase
        )
        return {"ok": True, "respuesta_gelato": resultado}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
