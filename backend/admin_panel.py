"""
admin_panel.py  ->  endpoint /admin/panel para el panel de Bookeo (panel.html)

Va en la misma carpeta que main.py (carpeta backend).
Variables necesarias en Railway (servicio Bookeo):
  ADMIN_PANEL_KEY   clave del panel (la inventas tú)
  SUPABASE_URL      \
  SUPABASE_KEY      /  si en tu proyecto se llaman distinto, cambia los dos nombres de abajo
  REDIS_URL         (ya la tienes por Celery/Redis)
"""
import hmac
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException

router = APIRouter()
MADRID = ZoneInfo("Europe/Madrid")
CLAVE_LATIDO_BEAT = "bookeo:beat:latido"


def _supabase():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _fecha(valor):
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00")).astimezone(MADRID)
    except Exception:
        return None


def _precio(valor):
    try:
        return float(valor or 0)
    except Exception:
        return 0.0


def _texto_fecha(dt, ahora):
    if not dt:
        return "sin fecha"
    dias = (ahora.date() - dt.date()).days
    if dias == 0:
        return f"hoy {dt:%H:%M}"
    if dias == 1:
        return "ayer"
    return f"{dt:%d/%m}"


def _estado_worker():
    """True si algún worker de Celery contesta; False si no; None si no se puede comprobar."""
    try:
        import celery_worker
        app = getattr(celery_worker, "celery_app", None) or getattr(celery_worker, "app", None)
        if app is None:
            return None
        return bool(app.control.inspect(timeout=1.5).ping())
    except Exception:
        return False


def _estado_beat():
    """True si el latido de Beat es reciente; None si nunca se ha registrado."""
    try:
        import redis
        r = redis.from_url(os.environ["REDIS_URL"])
        v = r.get(CLAVE_LATIDO_BEAT)
        if v is None:
            return None
        return (time.time() - float(v)) < 180
    except Exception:
        return None


@router.get("/admin/panel")
def admin_panel(x_admin_key: str = Header(default="")):
    clave = os.environ.get("ADMIN_PANEL_KEY", "")
    if not clave:
        raise HTTPException(status_code=503, detail="Panel sin configurar")
    if not hmac.compare_digest(x_admin_key.encode(), clave.encode()):
        raise HTTPException(status_code=401, detail="Clave incorrecta")

    ahora = datetime.now(MADRID)
    inicio_hoy = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    inicio_mes = inicio_hoy.replace(day=1)
    hace_30 = ahora - timedelta(days=30)

    db = _supabase()
    cols = "id, precio, titulo_libro, fecha_pedido, fecha_pago"

    pagados_mes = (db.table("pedidos").select(cols)
                   .gte("fecha_pago", inicio_mes.astimezone(timezone.utc).isoformat())
                   .execute().data or [])
    pendientes = (db.table("pedidos").select(cols)
                  .is_("fecha_pago", "null")
                  .gte("fecha_pedido", hace_30.astimezone(timezone.utc).isoformat())
                  .execute().data or [])
    ultimos = (db.table("pedidos").select(cols)
               .order("fecha_pedido", desc=True).limit(10)
               .execute().data or [])

    def suma(filas):
        return {"importe": round(sum(_precio(f.get("precio")) for f in filas), 2), "pedidos": len(filas)}

    pagados_hoy = [f for f in pagados_mes if (_fecha(f.get("fecha_pago")) or ahora) >= inicio_hoy]

    return {
        "servicios": [
            {"nombre": "Web", "ok": True},
            {"nombre": "Worker", "ok": _estado_worker()},
            {"nombre": "Beat", "ok": _estado_beat()},
        ],
        "ventas": {
            "hoy": suma(pagados_hoy),
            "mes": suma(pagados_mes),
            "pendientes": suma(pendientes),
        },
        "pedidos": [
            {
                "numero": f.get("id"),
                "titulo": f.get("titulo_libro") or "Sin título",
                "precio": _precio(f.get("precio")),
                "estado": "pagado" if f.get("fecha_pago") else "pendiente",
                "fecha": _texto_fecha(_fecha(f.get("fecha_pedido")), ahora),
            }
            for f in ultimos
        ],
    }
