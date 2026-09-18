"""
resend_client.py
Envío de correos transaccionales de Bookeo vía Resend (dominio ya
verificado - RESEND_API_KEY y el remitente vienen de las variables de
entorno de Railway, ya puestas: pedidos@mibookeo.es).

El estilo visual (_plantilla_base/_boton_cta) está calcado del correo de
lanzamiento que ya tenías hecho (cabecera verde #7db898 con el nombre
Bookeo, tarjeta blanca redondeada con sombra, botón verde en píldora,
footer con contacto) - así los 5 correos se sienten de la misma marca,
aunque cada uno tenga su propio contenido.

Los 6 tipos de correo del negocio:

1. Confirmación de pedido (al pagar) - YA CONECTADO, se llama desde
   /crear-pedido/confirmar-pago-simulado mientras Stripe no está
   conectado de verdad; en cuanto lo esté, el disparo pasa al webhook de
   Stripe, nunca a un botón que pulsa el propio cliente sin verificación
   real de por medio.
2. Envío con nº de seguimiento - lista para usar, se disparará cuando se
   conecte Gelato y él avise de que el pedido ha salido de fábrica.
3. Aviso de pago fallido - lista para usar, se disparará desde el
   webhook de Stripe cuando un intento de pago no se complete.
4. Aviso a los 7 días sin pagar (con borrado de contenido) - lista para
   usar, necesita una tarea programada (Celery beat) que revise
   periódicamente pedidos creados hace 7 días y sin pagar.
5. Valoración + 2 cupones, a los 7 días de la entrega - lista para
   usar, con dos códigos distintos (10% fidelidad sin caducar, 15% de
   recomendación para regalar, caduca en 60 días) - depende de saber la
   fecha de entrega real, que a su vez depende del punto 2. Los propios
   códigos hay que generarlos y guardarlos en una tabla aparte (ver
   supabase_client.py) antes de llamar a esta función.
6. Recompensa por recomendación usada - lista para usar, se dispara
   cuando el webhook de Stripe detecta que se ha usado un código de
   recomendación de tipo 5 (buscando en esa misma tabla de quién era) -
   entrega un cupón nuevo del 20% al cliente que recomendó.
"""
import os
import base64
import requests

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM = os.environ.get("RESEND_FROM", "Bookeo <pedidos@mibookeo.es>")
RESEND_URL = "https://api.resend.com/emails"

VERDE = "#7db898"
OSCURO = "#1a1a2e"
GRIS = "#5a5a6a"
GRIS_CLARO = "#9a9aaa"


def _enviar(to, subject, html, adjuntos=None):
    """
    adjuntos: lista de {"filename": str, "content_bytes": bytes} - Resend
    exige el contenido en base64, la conversión se hace aquí para que el
    resto del código solo maneje bytes normales.
    """
    if not RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY no está configurada en este entorno")

    payload = {
        "from": RESEND_FROM,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if adjuntos:
        payload["attachments"] = [
            {"filename": a["filename"], "content": base64.b64encode(a["content_bytes"]).decode("ascii")}
            for a in adjuntos
        ]

    resp = requests.post(
        RESEND_URL,
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _plantilla_base(titulo, cuerpo_html):
    # Mismo esqueleto visual que el correo de lanzamiento que ya tenías:
    # cabecera verde con el nombre Bookeo, tarjeta blanca redondeada con
    # sombra, footer con contacto - solo cambia el título y el cuerpo de
    # cada correo.
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{titulo} · Bookeo</title>
</head>
<body style="margin:0;padding:0;background:#f5f2ea;font-family:'Plus Jakarta Sans',Arial,sans-serif;">

<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f2ea;padding:32px 16px;">
  <tr><td align="center">

    <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:20px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,0.10);">

      <tr>
        <td align="center" style="background:{VERDE};padding:28px 32px 24px;">
          <span style="color:#ffffff;font-family:Georgia,serif;font-size:24px;font-weight:800;letter-spacing:0.02em;">Bookeo</span>
          <p style="margin:14px 0 0;color:rgba(255,255,255,0.85);font-size:13px;letter-spacing:0.04em;">
            mibookeo.es · Libros de fotos con vídeos QR
          </p>
        </td>
      </tr>

      <tr>
        <td align="center" style="padding:32px 32px 8px;">
          <h1 style="margin:0;font-size:24px;color:{OSCURO};font-weight:800;line-height:1.2;">
            {titulo}
          </h1>
        </td>
      </tr>

      <tr>
        <td style="padding:12px 40px 24px;">
          {cuerpo_html}
        </td>
      </tr>

      <tr>
        <td style="padding:0 32px;">
          <div style="height:1px;background:#e8e4dc;"></div>
        </td>
      </tr>

      <tr>
        <td align="center" style="padding:22px 32px 28px;">
          <p style="margin:0;font-size:12px;color:{GRIS_CLARO};line-height:1.8;">
            Bookeo · mibookeo.es<br>
            <a href="mailto:info@mibookeo.es" style="color:{VERDE};text-decoration:none;">info@mibookeo.es</a>
            &nbsp;·&nbsp;
            <a href="https://www.mibookeo.es" style="color:{VERDE};text-decoration:none;">www.mibookeo.es</a>
          </p>
        </td>
      </tr>

    </table>

  </td></tr>
</table>

</body>
</html>"""


def _boton_cta(texto, url):
    # Mismo botón verde en píldora que el "✦ Crear mi álbum ahora" del
    # correo de lanzamiento.
    return f"""
      <div style="text-align:center;padding:16px 0 4px;">
        <a href="{url}"
           style="display:inline-block;background:{VERDE};color:#ffffff;text-decoration:none;
                  font-size:16px;font-weight:800;padding:16px 44px;border-radius:50px;
                  box-shadow:0 6px 24px rgba(125,184,152,0.4);letter-spacing:0.02em;">
          {texto}
        </a>
      </div>"""


def _parrafo(texto):
    return f'<p style="margin:0 0 16px;font-size:14px;color:{GRIS};line-height:1.7;text-align:center;">{texto}</p>'


def _enlace_texto(url):
    # Enlace en texto plano, además del botón - por si el correo llega a
    # un cliente de correo que no pinta bien el botón (pasa en algunos
    # webmail corporativos), siempre hay una forma de pulsarlo.
    return f"""
      <p style="margin:8px 0 0;font-size:12px;color:{GRIS_CLARO};text-align:center;">
        Aquí tienes el enlace para finalizar tu pedido:<br>
        <a href="{url}" style="color:{VERDE};word-break:break-all;">{url}</a>
      </p>"""


# ═══════════════════════════════════════════════════════
#  1. CONFIRMACIÓN DE PEDIDO (YA CONECTADO)
# ═══════════════════════════════════════════════════════

def enviar_correo_confirmacion_pedido(correo, numero_pedido, libros, total_texto, envio, pdf_adjuntos=None):
    """
    libros: [{"titulo": str, "cantidad": int}, ...]
    envio: dict con nombre/direccion/ciudad/codigo_postal/provincia
    pdf_adjuntos: [{"filename": "mi_libro.pdf", "content_bytes": b"..."}]
    """
    filas_libros = "".join(
        f'<tr><td style="padding:6px 0;color:{OSCURO};font-size:14px;">📖 {l["titulo"]} '
        f'<span style="color:{GRIS_CLARO};font-size:12px;">× {l.get("cantidad", 1)}</span></td></tr>'
        for l in libros
    )
    cuerpo = f"""
      {_parrafo("¡Gracias por tu pedido! Lo hemos recibido correctamente y ya estamos preparándolo.")}
      <p style="font-size:13px;color:{GRIS_CLARO};margin:0 0 4px;text-align:center;">Nº de pedido</p>
      <p style="font-size:20px;color:{OSCURO};font-weight:800;font-family:Georgia,serif;margin:0 0 20px;text-align:center;">{numero_pedido}</p>
      <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">{filas_libros}</table>
      <table style="width:100%;border-collapse:collapse;border-top:2px solid {OSCURO};">
        <tr><td style="padding-top:10px;font-weight:700;color:{OSCURO};">Total</td>
            <td style="padding-top:10px;font-weight:700;color:{OSCURO};text-align:right;">{total_texto}</td></tr>
      </table>
      <div style="margin-top:20px;padding-top:16px;border-top:1px solid #e8e4dc;">
        <p style="font-size:13px;color:{GRIS_CLARO};margin:0 0 8px;font-weight:700;">Dirección de envío</p>
        <p style="font-size:13px;color:#2e2e3a;line-height:1.5;margin:0;">
          {envio.get('nombre', '')}<br>
          {envio.get('direccion', '')}<br>
          {envio.get('codigo_postal', '')} {envio.get('ciudad', '')}, {envio.get('provincia', '')}
        </p>
      </div>
      {_parrafo("Adjuntamos una copia de tu libro en PDF para que lo revises con calma. Te avisaremos por aquí en cuanto lo enviemos, con tu número de seguimiento.")}
    """
    html = _plantilla_base("Pedido confirmado 🎉", cuerpo)
    return _enviar(correo, f"Bookeo · Pedido {numero_pedido} confirmado", html, adjuntos=pdf_adjuntos)


# ═══════════════════════════════════════════════════════
#  2. ENVÍO CON Nº DE SEGUIMIENTO
# ═══════════════════════════════════════════════════════

def enviar_correo_envio(correo, numero_pedido, transportista, numero_seguimiento, url_seguimiento=None, titulo_libro=None):
    """
    titulo_libro: opcional - el título que el cliente le puso a su libro
    (p.ej. "Tailandia"). Si se pasa, personaliza el título del correo
    ("Tu Bookeo de Tailandia ya está en camino"); si no se tiene a mano
    (o llega vacío), se usa el texto genérico de siempre - nunca falla
    por falta de este dato.
    """
    nombre_album = f"Bookeo de {titulo_libro}" if titulo_libro else "libro"
    cuerpo = f"""
      {_parrafo(f"¡Tu {nombre_album} <strong style='color:{OSCURO};'>{numero_pedido}</strong> ya está en camino!")}
      <p style="font-size:13px;color:{GRIS_CLARO};margin:20px 0 4px;text-align:center;">Transportista</p>
      <p style="font-size:16px;color:{OSCURO};font-weight:700;margin:0 0 16px;text-align:center;">{transportista}</p>
      <p style="font-size:13px;color:{GRIS_CLARO};margin:0 0 4px;text-align:center;">Nº de seguimiento</p>
      <p style="font-size:20px;color:{OSCURO};font-weight:800;font-family:Georgia,serif;margin:0 0 8px;text-align:center;">{numero_seguimiento}</p>
      {_boton_cta("📦 Seguir mi envío", url_seguimiento) if url_seguimiento else ""}
      {_parrafo("Normalmente llega en 5–7 días hábiles desde este momento. Cuando lo recibas, nos encantaría saber qué tal ha quedado.")}
    """
    titulo_correo = f"Tu {nombre_album} está en camino 📦" if titulo_libro else "Tu libro está en camino 📦"
    html = _plantilla_base(titulo_correo, cuerpo)
    return _enviar(correo, f"Bookeo · Pedido {numero_pedido} enviado", html)


# ═══════════════════════════════════════════════════════
#  3. AVISO DE PAGO FALLIDO
# ═══════════════════════════════════════════════════════

def enviar_correo_pago_fallido(correo, numero_pedido, url_reintentar, motivo=None):
    detalle_motivo = f'<p style="font-size:12px;color:{GRIS_CLARO};margin:8px 0 0;text-align:center;">Motivo: {motivo}</p>' if motivo else ""
    cuerpo = f"""
      {_parrafo(f"No hemos podido cobrar tu pedido <strong style='color:{OSCURO};'>{numero_pedido}</strong>. Tu libro sigue guardado tal cual lo dejaste, solo falta completar el pago.")}
      {detalle_motivo}
      {_boton_cta("Reintentar el pago", url_reintentar)}
      {_enlace_texto(url_reintentar)}
      {_parrafo("Si el problema persiste, prueba con otra tarjeta o escríbenos y te ayudamos.")}
    """
    html = _plantilla_base("No se pudo completar tu pago", cuerpo)
    return _enviar(correo, f"Bookeo · Pago pendiente del pedido {numero_pedido}", html)


# ═══════════════════════════════════════════════════════
#  4. AVISO A LOS 7 DÍAS SIN PAGAR (CON BORRADO DE CONTENIDO)
# ═══════════════════════════════════════════════════════

def enviar_correo_aviso_pago_pendiente(correo, numero_pedido, url_reintentar, dias_para_borrado=1):
    plural = "día" if dias_para_borrado == 1 else "días"
    cuerpo = f"""
      {_parrafo(f"Tu álbum <strong style='color:{OSCURO};'>{numero_pedido}</strong> lleva una semana esperando el pago.")}
      {_parrafo(f"Por espacio, en <strong style='color:{OSCURO};'>{dias_para_borrado} {plural}</strong> tendremos que borrar las fotos y vídeos que subiste si el pedido sigue sin pagarse - tu diseño y el libro montado se perderían.")}
      {_boton_cta("Completar mi pedido", url_reintentar)}
      {_enlace_texto(url_reintentar)}
      {_parrafo("Si ya no quieres seguir con este álbum, no hace falta que hagas nada.")}
    """
    html = _plantilla_base("Tu álbum está a punto de borrarse ⏳", cuerpo)
    return _enviar(correo, f"Bookeo · Tu pedido {numero_pedido} se borrará pronto", html)


# ═══════════════════════════════════════════════════════
#  5. PETICIÓN DE VALORACIÓN + 2 CUPONES (A LOS 7 DÍAS DE LA ENTREGA)
# ═══════════════════════════════════════════════════════

def enviar_correo_valoracion_cupones(correo, numero_pedido, url_valoracion, codigo_fidelidad, codigo_recomendacion, titulo_libro=None):
    """
    titulo_libro: opcional - mismo mecanismo que en enviar_correo_envio.
    Si se tiene, el saludo queda "Ya has recibido tu Bookeo de Tailandia"
    en vez del genérico "¿Qué tal ha quedado tu álbum?" - si no, cae en
    el texto de siempre sin que falle nada.
    """
    nombre_album = f"Bookeo de {titulo_libro}" if titulo_libro else "álbum"
    saludo = (
        f"Ya has recibido tu {nombre_album} <strong style='color:{OSCURO};'>{numero_pedido}</strong> - ¿qué tal ha quedado? Nos encantaría saber tu opinión."
        if titulo_libro else
        f"¿Qué tal ha quedado tu álbum <strong style='color:{OSCURO};'>{numero_pedido}</strong>? Nos encantaría saber tu opinión."
    )
    cuerpo = f"""
      {_parrafo(saludo)}
      {_boton_cta("⭐ Dejar mi valoración", url_valoracion)}
      <div style="margin-top:28px;padding-top:20px;border-top:1px solid #e8e4dc;">
        {_parrafo("Como agradecimiento, aquí tienes dos regalos:")}

        <table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 12px;background:#f5f2ea;border-radius:12px;">
          <tr><td style="padding:16px 20px;text-align:center;">
            <p style="margin:0 0 4px;font-size:12px;color:{GRIS_CLARO};">Para tu próximo álbum · 10% de descuento · sin caducidad</p>
            <p style="margin:0;font-size:20px;font-weight:800;font-family:Georgia,serif;color:{VERDE};letter-spacing:0.05em;">{codigo_fidelidad}</p>
            <p style="margin:6px 0 0;font-size:10px;color:{GRIS_CLARO};">Mantén pulsado el código para copiarlo</p>
          </td></tr>
        </table>

        <table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 12px;background:#f5f2ea;border-radius:12px;">
          <tr><td style="padding:16px 20px;text-align:center;">
            <p style="margin:0 0 4px;font-size:12px;color:{GRIS_CLARO};">Para regalar a un amigo o familiar · 15% en formato mediano o grande · válido 60 días</p>
            <p style="margin:0;font-size:20px;font-weight:800;font-family:Georgia,serif;color:{VERDE};letter-spacing:0.05em;">{codigo_recomendacion}</p>
            <p style="margin:6px 0 0;font-size:10px;color:{GRIS_CLARO};">Mantén pulsado el código para copiarlo</p>
          </td></tr>
        </table>

        {_parrafo("Si tu amigo usa este segundo código, te avisaremos y te llevas tú también un 20% de regalo.")}
      </div>
    """
    titulo_correo = f"¿Qué tal tu {nombre_album}? 💌" if titulo_libro else "¿Qué tal tu álbum? 💌"
    html = _plantilla_base(titulo_correo, cuerpo)
    return _enviar(correo, f"Bookeo · ¿Qué tal tu {nombre_album}? + regalo para ti", html)


# ═══════════════════════════════════════════════════════
#  6. RECOMPENSA POR RECOMENDACIÓN USADA (nuevo)
# ═══════════════════════════════════════════════════════

def enviar_correo_recompensa_recomendacion(correo, codigo_recompensa):
    """
    Se dispara cuando el código de recomendación de este cliente (el que
    le dimos en el correo de valoración, ver arriba) lo usa de verdad un
    amigo o familiar en su compra - premia al cliente ORIGINAL con un
    cupón nuevo del 20%.
    """
    cuerpo = f"""
      {_parrafo("¡Buenas noticias! Alguien a quien recomendaste Bookeo acaba de hacer su pedido con tu código.")}
      {_parrafo("Como agradecimiento, aquí tienes un cupón del 20% para tu próximo álbum:")}
      <table width="100%" cellpadding="0" cellspacing="0" style="margin:16px 0;background:#f5f2ea;border-radius:12px;">
        <tr><td style="padding:16px 20px;text-align:center;">
          <p style="margin:0 0 4px;font-size:12px;color:{GRIS_CLARO};">20% de descuento</p>
          <p style="margin:0;font-size:20px;font-weight:800;font-family:Georgia,serif;color:{VERDE};letter-spacing:0.05em;">{codigo_recompensa}</p>
          <p style="margin:6px 0 0;font-size:10px;color:{GRIS_CLARO};">Mantén pulsado el código para copiarlo</p>
        </td></tr>
      </table>
      {_parrafo("Gracias por contarle a alguien lo que hacemos - significa mucho para nosotros.")}
    """
    html = _plantilla_base("¡Tu recomendación ha dado sus frutos! 🎁", cuerpo)
    return _enviar(correo, "Bookeo · Un 20% de regalo por tu recomendación", html)
