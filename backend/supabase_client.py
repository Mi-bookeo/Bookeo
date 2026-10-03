"""
Bookeo · supabase_client.py
Funciones para leer/guardar datos de clientes y pedidos relacionados
con la conexión de Google Drive.
"""

import os
import re
import random
import string
import datetime
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ═══════════════════════════════════════════════════════
#  CLIENTE — buscar o crear por email
# ═══════════════════════════════════════════════════════

def obtener_o_crear_cliente(email):
    """
    Busca un cliente por su email. Si ya existe, devuelve su id.
    Si no existe, lo crea y devuelve el id recién generado.
    """
    resp = supabase.table("clientes").select("id").eq("email", email).execute()

    if resp.data:
        return resp.data[0]["id"]

    nuevo = supabase.table("clientes").insert({"email": email}).execute()
    return nuevo.data[0]["id"]


def obtener_o_crear_cliente_por_drive(email_drive):
    """
    Busca un cliente por su CORREO DE GOOGLE DRIVE (columna
    google_drive_email) - a propósito, NO por el correo principal
    (columna 'email'). El correo de la cuenta de Google que se conecta
    para guardar vídeos puede ser distinto del que el cliente escribe
    luego en los datos de envío/facturación - son la MISMA persona con
    dos direcciones distintas, no dos clientes.

    OJO: antes esto se buscaba/creaba con obtener_o_crear_cliente(email),
    usando el correo de Drive como si fuera el correo principal - así,
    en cuanto el cliente escribía un correo distinto en los datos de
    envío, se creaba una fila NUEVA y duplicada para la misma persona,
    en vez de completar la que ya existía.

    Se busca por las DOS columnas a la vez (google_drive_email O email) -
    cubre el caso de que el cliente ya hubiera rellenado antes sus datos
    de envío con este MISMO correo (su fila ya existe, con 'email'
    puesto pero 'google_drive_email' todavía vacío) y ahora conecte
    Drive usando esa misma cuenta - sin este OR, no se encontraría esa
    fila (todavía no tiene nada en 'google_drive_email') y se crearía
    una segunda de más.

    Si no existe ningún cliente con ese correo en ninguna de las dos
    columnas, se crea una fila "en blanco" (columna 'email' vacía por
    ahora) - se rellena más adelante, cuando el cliente pase por la
    pantalla de pago y escriba sus datos de contacto (ver
    guardar_datos_contacto_cliente), que es cuando de verdad sabemos con
    qué correo comunicarnos con él.
    """
    resp = supabase.table("clientes").select("id").or_(
        f"google_drive_email.eq.{email_drive},email.eq.{email_drive}"
    ).execute()
    if resp.data:
        return resp.data[0]["id"]
    nuevo = supabase.table("clientes").insert({"google_drive_email": email_drive}).execute()
    return nuevo.data[0]["id"]


def obtener_cliente_drive(cliente_id):
    """Devuelve refresh_token y carpeta_drive_id principal del cliente, si existen."""
    # OJO: antes usaba .single(), que lanza un error (PGRST116) si no
    # encuentra NINGUNA fila con ese cliente_id - por ejemplo si el
    # navegador del cliente tenía guardado en localStorage un cliente_id
    # de una sesión antigua que ya no existe en la base de datos. Eso
    # tumbaba la petición entera con un 500 en vez de simplemente seguir
    # sin datos de Drive. Ahora se comprueba la lista de resultados sin
    # .single(), igual que el resto de funciones de este archivo.
    resp = supabase.table("clientes").select(
        "google_refresh_token, carpeta_drive_id, google_drive_email"
    ).eq("id", cliente_id).execute()
    return resp.data[0] if resp.data else None


def borrar_refresh_token_cliente(cliente_id):
    """
    Borra el refresh_token guardado de un cliente porque se ha detectado
    que ya no es válido (revocado en Google, caducado, etc.) - así no se
    queda un token muerto en la base de datos que haga pensar que Drive
    sigue conectado cuando en realidad ya no lo está.
    """
    supabase.table("clientes").update({"google_refresh_token": None}).eq("id", cliente_id).execute()


def guardar_refresh_token_cliente(cliente_id, refresh_token, email=None):
    """Guarda el refresh_token tras el login OAuth del cliente."""
    datos = {"google_refresh_token": refresh_token}
    if email:
        datos["google_drive_email"] = email
    supabase.table("clientes").update(datos).eq("id", cliente_id).execute()


def guardar_carpeta_principal_cliente(cliente_id, carpeta_drive_id):
    """Guarda el ID de la carpeta principal 'Mibookeo (NO BORRAR)' del cliente."""
    supabase.table("clientes").update(
        {"carpeta_drive_id": carpeta_drive_id}
    ).eq("id", cliente_id).execute()


# ═══════════════════════════════════════════════════════
#  PEDIDO — una fila por cada LIBRO (tabla 'pedidos')
# ═══════════════════════════════════════════════════════
# OJO: el "pedido_id" que usa el resto del código (uno por cada álbum que
# el cliente crea) ES el mismo id de la fila en 'pedidos' (columna 'id') -
# no hay traducción entre uno y otro. Si el cliente compra 2 libros en el
# mismo pago, son 2 filas en 'pedidos' (no una) que luego se agrupan por
# tener el mismo stripe_id/numero_pedido - eso se asigna más adelante, al
# confirmar el pago de verdad (solo ahí se conoce el carrito completo).
#
# La fila se crea aquí, en el momento en que el PDF termina de generarse
# -no cuando paga- para que un libro que se queda a medias (el cliente lo
# ve pero no paga) quede igualmente registrado, con fecha_pedido puesta y
# fecha_pago en NULO. Un futuro correo de recordatorio solo tiene que
# buscar los que tengan fecha_pago NULO y fecha_pedido de hace X días.
#
# OJO 2: esto NO tiene nada que ver con la subcarpeta de Drive de los
# vídeos - esa se busca/crea directamente en Google Drive (ver
# subir_drive.py), sin tocar esta tabla para nada, así que subir un vídeo
# nunca depende de si esta fila existe todavía o no.

def crear_o_actualizar_pedido_inicial(pedido_id, datos):
    """
    Crea (o actualiza si ya existe) la fila de 'pedidos' para este pedido.
    'datos' puede traer: cliente_id, precio, correo, titulo_libro,
    cantidad_libros (total de ejemplares del pedido, copias incluidas -
    el formato y las páginas van en 'libros', uno por cada libro, no
    aquí: un pedido puede llevar varios libros distintos).
    fecha_pedido se pone sola (default now() en la tabla); fecha_pago se
    queda en NULO hasta que se pague de verdad - eso todavía no está
    conectado, se hace mañana junto con Stripe.

    OJO: 'id' es un UUID interno autogenerado por la tabla
    (gen_random_uuid()) - nunca se escribe a mano. El código de pedido
    con el que trabaja todo el resto del sistema ("BK-20260926-...") va
    en la columna 'numero_pedido', que es la que hay que usar para
    buscar/actualizar esta fila desde fuera.

    Si la fila no existe todavía, hay que rellenar 'precio' (NOT NULL sin
    valor por defecto) con un valor provisional para que el insert no
    falle - se sobrescribe con el importe real al pagar de verdad. Si la
    fila ya existe, no se toca (para no pisar un precio ya calculado).
    """
    existe = supabase.table("pedidos").select("id").eq("numero_pedido", pedido_id).execute()
    fila = {k: v for k, v in datos.items() if v is not None}
    fila["numero_pedido"] = pedido_id
    if not existe.data:
        fila.setdefault("precio", 0)
    supabase.table("pedidos").upsert(fila, on_conflict="numero_pedido").execute()


def marcar_pedido_pagado(numero_pedido, stripe_id=None):
    """
    Se llama SOLO desde el webhook de Stripe, cuando el pago se confirma
    de verdad (checkout.session.completed) - pone fecha_pago a AHORA y
    guarda el id de la sesión de Stripe (stripe_id) para poder relacionar
    el pedido con su pago exacto - funciona igual en modo test (empieza
    por 'cs_test_') que en modo real ('cs_live_'), así que no hace falta
    tocar nada al pasar a real.
    Antes de esto la fila ya existe (creada sin pagar en
    crear_o_actualizar_pedido_inicial, al iniciar el pago), así que esto
    es una actualización, no una creación.
    """
    datos = {"fecha_pago": datetime.datetime.utcnow().isoformat()}
    if stripe_id:
        datos["stripe_id"] = stripe_id
    supabase.table("pedidos").update(datos).eq("numero_pedido", numero_pedido).execute()


def obtener_pedido_por_numero(numero_pedido):
    """
    Una fila de 'pedidos' por su numero_pedido ("BK-..."), o None si no
    existe. Se usa desde el webhook de Gelato (aviso de envío) para
    recuperar el correo y el título del libro, que ahí no vienen - Gelato
    solo manda su propio orderReferenceId (que es nuestro numero_pedido)
    y los datos de seguimiento.
    """
    resp = supabase.table("pedidos").select("*").eq("numero_pedido", numero_pedido).execute()
    return resp.data[0] if resp.data else None


def marcar_aviso_envio_enviado(numero_pedido):
    """
    Se llama tras mandar con éxito el correo de 'tu pedido ya está en
    camino' (ver enviar_correo_envio) - evita mandarlo dos veces si
    Gelato reintenta su aviso de envío (lo hace hasta 3 veces si no
    recibe una respuesta 2xx a tiempo).
    """
    supabase.table("pedidos").update({"aviso_envio_enviado": True}).eq("numero_pedido", numero_pedido).execute()


def marcar_pedido_gelato(numero_pedido, gelato_id):
    """
    Se llama desde el webhook de Stripe, justo después de crear el
    pedido de producción en Gelato (ver gelato_client.crear_pedido_gelato) -
    guarda su id en pedidos.gelato_id, al lado de stripe_id. Un solo
    gelato_id por pedido, aunque lleve varios libros (van todos juntos
    como items dentro de ESE pedido de Gelato).
    """
    supabase.table("pedidos").update({"gelato_id": gelato_id}).eq("numero_pedido", numero_pedido).execute()


def obtener_pedidos_sin_pagar_hace_dias(dias=7):
    """
    Pedidos creados hace 'dias' días o más, que siguen sin pagar
    (fecha_pago NULO) y a los que todavía no se avisó - para el correo
    de "tu álbum se va a borrar" (ver enviar_correo_aviso_pago_pendiente).
    """
    limite = (datetime.datetime.utcnow() - datetime.timedelta(days=dias)).isoformat()
    resp = supabase.table("pedidos").select("*").is_("fecha_pago", "null").lte("fecha_pedido", limite).eq("aviso_pago_enviado", False).execute()
    return resp.data or []


def marcar_aviso_pago_enviado(numero_pedido):
    supabase.table("pedidos").update({"aviso_pago_enviado": True}).eq("numero_pedido", numero_pedido).execute()


def obtener_pedidos_sin_pagar_para_borrar(dias=7):
    """
    Pedidos creados hace 'dias' días o más que siguen SIN pagar: sin
    fecha_pago, sin stripe_id y sin gelato_id. Son los que ya han perdido
    sus archivos en R2 (regla de ciclo de vida de 7 días), así que su fila
    ya no sirve para nada. Los pagados nunca entran aquí.
    """
    limite = (datetime.datetime.utcnow() - datetime.timedelta(days=dias)).isoformat()
    resp = supabase.table("pedidos").select("*") \
        .is_("fecha_pago", "null").is_("stripe_id", "null").is_("gelato_id", "null") \
        .lte("fecha_pedido", limite).execute()
    return resp.data or []


def borrar_pedido_sin_pagar(numero_pedido):
    """
    Borra la fila de 'pedidos' de un pedido sin pagar y, si existe, la fila
    de 'libros' con ese mismo id (el worker crea una fila en cada tabla con
    el mismo id por cada álbum generado). NO borra nada si ese id tiene un
    libro 'confirmado' (= se pagó): esa fila de 'pedidos' es una sobra del
    álbum, pero el libro confirmado se conserva para poder recuperar el
    diseño si el cliente escribe pidiendo ayuda.
    Devuelve True si se borró, False si se saltó por seguridad.
    """
    confirmado = supabase.table("libros").select("pedido_id").eq("pedido_id", numero_pedido).eq("estado", "confirmado").execute()
    if confirmado.data:
        return False
    supabase.table("libros").delete().eq("pedido_id", numero_pedido).execute()
    supabase.table("pedidos").delete().eq("numero_pedido", numero_pedido).is_("fecha_pago", "null").execute()
    return True


def obtener_pedidos_para_valorar_hace_dias(dias=7):
    """
    Pedidos PAGADOS hace 'dias' días o más, a los que todavía no se les
    mandó el correo de valoración+cupones - ver
    enviar_correo_valoracion_cupones. Se cuenta desde fecha_pago (la
    confirmación del pedido), no desde la entrega.
    """
    limite = (datetime.datetime.utcnow() - datetime.timedelta(days=dias)).isoformat()
    resp = supabase.table("pedidos").select("*").not_.is_("fecha_pago", "null").lte("fecha_pago", limite).eq("valoracion_enviada", False).execute()
    return resp.data or []


def marcar_valoracion_enviada(numero_pedido):
    supabase.table("pedidos").update({"valoracion_enviada": True}).eq("numero_pedido", numero_pedido).execute()


def obtener_pedidos_pagados_entre(fecha_inicio, fecha_fin):
    """
    Pedidos PAGADOS con fecha_pago dentro de [fecha_inicio, fecha_fin)
    (fecha_fin no incluida) - para los resúmenes de facturación mensual
    y trimestral. fecha_inicio/fecha_fin: objetos date o datetime.
    """
    resp = supabase.table("pedidos").select("*") \
        .not_.is_("fecha_pago", "null") \
        .gte("fecha_pago", fecha_inicio.isoformat()) \
        .lt("fecha_pago", fecha_fin.isoformat()) \
        .execute()
    return resp.data or []


def obtener_cupones_cliente(cliente_id):
    """Lee (sin tocar nada) la fila de cupones de un cliente, si tiene."""
    resp = supabase.table("cupones").select("*").eq("id_cliente", cliente_id).execute()
    return resp.data[0] if resp.data else None


# ═══════════════════════════════════════════════════════
#  NUMERACIÓN CORRELATIVA DE TICKETS Y FACTURAS
# ═══════════════════════════════════════════════════════

def siguiente_numero_ticket():
    """
    Devuelve el siguiente número de ticket, formateado ya como 'T0001',
    'T0002'... Nunca se reinicia. Llama a una función de Postgres (ver
    sql_contadores.sql) para que el número nunca se repita aunque
    lleguen dos pagos a la vez.
    """
    resp = supabase.rpc("siguiente_numero", {"p_tipo": "ticket", "p_anio": 0}).execute()
    numero = resp.data
    return f"T{numero:04d}"


def siguiente_numero_factura(anio):
    """
    Igual que siguiente_numero_ticket, pero para facturas - el contador
    es distinto POR AÑO (empieza en 1 cada año nuevo), de ahí el
    parámetro 'anio'. Formato final: 'F2026-0001'.
    """
    resp = supabase.rpc("siguiente_numero", {"p_tipo": "factura", "p_anio": anio}).execute()
    numero = resp.data
    return f"F{anio}-{numero:04d}"


# ═══════════════════════════════════════════════════════
#  CLIENTE — datos de contacto (pantalla de pago)
# ═══════════════════════════════════════════════════════
# OJO: la dirección de envío/facturación NO se guarda en Supabase - solo
# se usa para mandarla a Gelato en el momento de fabricar el pedido. Aquí
# solo se guarda lo mínimo del cliente: nombre, correo y teléfono.

def guardar_datos_contacto_cliente(email, nombre=None, telefono=None, cliente_id=None):
    """
    Busca (o crea) el cliente por email y actualiza su nombre/teléfono si
    se han pasado. Se llama desde pago.html al rellenar el formulario.

    Si ya se conoce el cliente_id (porque el cliente conectó Google
    Drive antes, en esta misma sesión), se actualiza ESA fila con el
    correo de los datos de envío - así la misma persona queda con sus
    dos correos en sus dos columnas (email = el de los datos de
    contacto/envío, google_drive_email = el de la cuenta de Drive), en
    vez de crear una fila nueva solo porque el correo es distinto del
    de Drive.

    Si no se conoce cliente_id (el cliente no ha conectado Drive
    todavía, o nunca lo hace porque su libro no lleva vídeos), se
    busca/crea por email como hasta ahora.
    """
    if not cliente_id:
        cliente_id = obtener_o_crear_cliente(email)
    datos = {"email": email}
    if nombre:
        datos["nombre"] = nombre
    if telefono:
        datos["telefono"] = telefono
    supabase.table("clientes").update(datos).eq("id", cliente_id).execute()
    return cliente_id


def guardar_marketing_cliente(email, marketing):
    """Guarda si el cliente acepta recibir ofertas/novedades por correo."""
    cliente_id = obtener_o_crear_cliente(email)
    supabase.table("clientes").update({"marketing": bool(marketing)}).eq("id", cliente_id).execute()


# ═══════════════════════════════════════════════════════
#  LIBRO — una fila por cada álbum creado (tabla 'libros')
# ═══════════════════════════════════════════════════════
# 'libros' guarda el DISEÑO del álbum (para poder recuperarlo si el
# cliente escribe pidiendo ayuda); 'pedidos' guarda el estado comercial
# (pagado o no, Stripe, Gelato, factura...) de ese mismo id. No se repite
# el email del cliente aquí - se llega a él por pedidos.cliente_id -> 
# clientes.email cuando haga falta.

def guardar_libro(pedido_id, datos):
    """
    Guarda (o actualiza) la fila de un libro en la tabla 'libros', para
    poder encontrarlo si un cliente escribe pidiendo volver a su diseño.
    'datos' puede traer: titulo, tipo_libro ('IA' o 'cero'), estado,
    unidad_url, editor_url, formato ('2020'/'2128'/'2828'), paginas (30
    de base + 2 por cada pack extra).

    'pedido_id' es el identificador que ya usa el resto del backend (uno
    por cada álbum creado) - es el que el cliente tiene de verdad (le
    aparece en la URL del editor), así que es lo que hay que buscar si
    escribe pidiendo ayuda. La columna 'id' propia de la fila la genera
    Supabase sola, no se toca aquí.

    OJO: se asume que la columna 'pedido_id' tiene una restricción de
    valor único en la tabla, para que el upsert actualice la misma fila
    en vez de crear una duplicada cada vez que se llama a esto para el
    mismo libro - si no la tiene, avisa y la añadimos.
    """
    fila = dict(datos)
    fila["pedido_id"] = pedido_id

    # OJO: en el código hay dos nombres para la misma columna (el worker y
    # el webhook de Stripe escriben 'url_editor', el modelo DatosLibro y
    # esta documentación dicen 'editor_url'). Solo uno existe de verdad en
    # la tabla, y con el otro el upsert falla ENTERO ("Could not find the
    # '...' column of 'libros'") - por eso los libros no se guardaban. Aquí
    # se prueba el nombre alternativo, y si una columna no existe se guarda
    # sin ella dejando un AVISO claro en el log, en vez de perder la fila
    # entera.
    alternativos = {"url_editor": "editor_url", "editor_url": "url_editor"}
    for _ in range(6):
        try:
            supabase.table("libros").upsert(fila, on_conflict="pedido_id").execute()
            return
        except Exception as e:
            m = re.search(r"Could not find the '([^']+)' column", str(e))
            if not m or m.group(1) not in fila:
                raise
            col = m.group(1)
            valor = fila.pop(col)
            alt = alternativos.get(col)
            if alt and alt not in fila:
                print(f"[LIBROS] AVISO: la columna '{col}' no existe en 'libros', se usa '{alt}'")
                fila[alt] = valor
            else:
                print(f"[LIBROS] AVISO: la columna '{col}' no existe en 'libros', se guarda el libro SIN ella")
    raise RuntimeError("No se pudo guardar el libro en 'libros' tras varios intentos")


# ═══════════════════════════════════════════════════════
#  CUPONES — fidelidad, recomendación y recompensa
# ═══════════════════════════════════════════════════════
# Una fila por cliente en la tabla 'cupones':
#   id_cliente, correo          - de quién es
#   cupon, descuento            - su cupón "propio" (empieza en 10% de
#                                  fidelidad; si su código de recomendación
#                                  lo usa un amigo, se SUSTITUYE por uno
#                                  nuevo del 20% - nunca tiene los dos a
#                                  la vez)
#   cupon_amigo, cupon_amigo_caduca - el código para regalar a un amigo o
#                                  familiar (15%, caduca a los 60 días,
#                                  solo formato mediano/grande - esa
#                                  restricción de formato se comprueba al
#                                  validar, no se guarda aquí)
#
# Los códigos llevan una parte aleatoria a propósito, NUNCA correlativa -
# un código adivinable (tipo ...001, ...002) permitiría a cualquiera con
# un código válido probar el siguiente y usar un cupón que no le
# corresponde. Con parte aleatoria, además, no hace falta llevar la
# cuenta de "cuál es el siguiente número" ni hay riesgo de que dos
# pedidos confirmados en el mismo instante generen el mismo código por
# casualidad.

def _generar_codigo_cupon(prefijo, longitud=6):
    caracteres = string.ascii_uppercase + string.digits
    sufijo = "".join(random.choices(caracteres, k=longitud))
    return f"{prefijo}-{sufijo}"


def crear_cupones_cliente(cliente_id, correo):
    """
    Se llama al confirmar un pedido de verdad (webhook de Stripe, nunca
    antes) - crea (o renueva, si ya tenía una fila de un pedido
    anterior) los dos códigos iniciales del cliente:
      - cupon: 10% de fidelidad para él mismo, sin caducidad.
      - cupon_amigo: 15% para regalar, caduca en 60 días, solo formato
        mediano/grande.
    Devuelve los dos códigos para poder mandarlos en el correo de
    valoración a los 7 días.
    """
    cupon = _generar_codigo_cupon("GRACIAS10")
    cupon_amigo = _generar_codigo_cupon("AMIGO15")
    caduca = (datetime.datetime.utcnow() + datetime.timedelta(days=60)).date().isoformat()

    fila = {
        "id_cliente": cliente_id,
        "correo": correo,
        "cupon": cupon,
        "descuento": 10,
        "cupon_amigo": cupon_amigo,
        "cupon_amigo_caduca": caduca,
    }
    existente = supabase.table("cupones").select("id").eq("id_cliente", cliente_id).execute()
    if existente.data:
        supabase.table("cupones").update(fila).eq("id_cliente", cliente_id).execute()
    else:
        supabase.table("cupones").insert(fila).execute()
    return {"cupon": cupon, "cupon_amigo": cupon_amigo}


def validar_cupon(codigo, formato):
    """
    Busca 'codigo' en tres sitios, en este orden:
      1. 'cupon' (el propio del cliente, siempre con números).
      2. 'cupon_amigo' (el de recomendación, siempre con números).
      3. 'promociones' - SOLO si el código no lleva ningún número (p.ej.
         "MARIACUERVOS") - códigos de colaboración puestos a mano por
         Abel, compartidos (no ligados a ningún cliente en concreto),
         limitados por cantidad total de usos, por fecha, o sin límite
         si no se pone ninguna de las dos cosas.

    NO borra ni marca nada como usado, esto solo CONSULTA si es válido,
    para poder enseñar el descuento en pantalla antes de pagar. El
    canje de verdad pasa en marcar_cupon_usado(), solo cuando el pago
    se confirma.

    Devuelve None si no existe o no es válido para ese formato/fecha;
    si es válido, un dict con el % de descuento, el tipo, y la fila a la
    que pertenece (para poder canjearlo después).
    """
    codigo = (codigo or "").strip().upper()
    if not codigo:
        return None

    if not any(c.isdigit() for c in codigo):
        return _validar_promocion(codigo, formato)

    resp = supabase.table("cupones").select("*").eq("cupon", codigo).execute()
    if resp.data:
        fila = resp.data[0]
        return {"tipo": "propio", "descuento": fila["descuento"], "fila_id": fila["id"]}

    resp = supabase.table("cupones").select("*").eq("cupon_amigo", codigo).execute()
    if resp.data:
        fila = resp.data[0]
        if formato not in ("2128", "2828"):
            return None
        caduca = fila.get("cupon_amigo_caduca")
        if caduca and datetime.date.fromisoformat(str(caduca)) < datetime.date.today():
            return None
        return {"tipo": "amigo", "descuento": 15, "fila_id": fila["id"]}

    return None


def _validar_promocion(codigo, formato):
    """
    Cupón de colaboración de la tabla 'promociones' - puesto a mano por
    Abel, compartido (cualquiera puede usarlo, no está ligado a ningún
    cliente), solo válido en formato mediano/grande igual que el de
    recomendación. Puede tener límite por 'cantidad' (nº total de usos),
    por 'fecha_limite', las dos cosas a la vez, o ninguna (sin límite).
    """
    resp = supabase.table("promociones").select("*").eq("nombre_cupon", codigo).execute()
    if not resp.data:
        return None
    promo = resp.data[0]

    if formato not in ("2128", "2828"):
        return None

    cantidad = promo.get("cantidad")
    if cantidad is not None and (promo.get("cupones_usados") or 0) >= cantidad:
        return None

    fecha_limite = promo.get("fecha_limite")
    if fecha_limite and datetime.date.fromisoformat(str(fecha_limite)) < datetime.date.today():
        return None

    return {
        "tipo": "promocion",
        "descuento": promo["descuento"],
        "fila_id": promo["id"],
        # Columna nueva 'envio_gratis' (bool, por defecto false) - si está
        # a true, este código además de su descuento normal quita el
        # coste de envío del pedido entero.
        "envio_gratis": bool(promo.get("envio_gratis")),
    }


def marcar_cupon_usado(fila_id, tipo):
    """
    Se llama SOLO cuando Stripe confirma que el pago se ha completado de
    verdad (nunca al aplicar el código en pantalla, para no gastar un
    cupón de alguien que aplica el código y luego no llega a pagar).

    - tipo 'propio': se borra 'cupon'/'descuento' de esa fila (de un
      solo uso).
    - tipo 'amigo': se borra 'cupon_amigo'/'cupon_amigo_caduca', Y de
      paso se SUSTITUYE el cupón propio del cliente que recomendó (lo
      tuviera ya gastado o no) por uno nuevo del 20% - nunca acumula dos
      cupones a la vez, el de fidelidad se "convierte" en el de
      recompensa. Devuelve el correo y el nuevo código para poder mandar
      el correo de recompensa desde quien llame a esta función.

    Si tras el cambio la fila se queda sin 'cupon' NI 'cupon_amigo', se
    borra la fila entera - hasta su próximo pedido no vuelve a tener
    ningún cupón.

    - tipo 'promocion': no toca la tabla 'cupones' en absoluto - suma 1
      a 'cupones_usados' en la tabla 'promociones' (usando una función
      de Postgres para que dos canjes a la vez nunca se pisen entre
      sí), sin borrar la fila nunca (el código de colaboración sigue
      existiendo aunque se agote su cantidad, solo deja de ser válido).
    """
    if tipo == "promocion":
        supabase.rpc("incrementar_uso_promocion", {"p_id": fila_id}).execute()
        return None

    resp = supabase.table("cupones").select("*").eq("id", fila_id).execute()
    if not resp.data:
        return None
    fila = resp.data[0]
    resultado = None

    if tipo == "propio":
        supabase.table("cupones").update({"cupon": None, "descuento": None}).eq("id", fila_id).execute()
    elif tipo == "amigo":
        nuevo_codigo = _generar_codigo_cupon("RECOMPENSA20")
        supabase.table("cupones").update({
            "cupon_amigo": None,
            "cupon_amigo_caduca": None,
            "cupon": nuevo_codigo,
            "descuento": 20,
        }).eq("id", fila_id).execute()
        resultado = {"correo": fila["correo"], "codigo_recompensa": nuevo_codigo}

    restante = supabase.table("cupones").select("cupon, cupon_amigo").eq("id", fila_id).execute()
    if restante.data and not restante.data[0]["cupon"] and not restante.data[0]["cupon_amigo"]:
        supabase.table("cupones").delete().eq("id", fila_id).execute()

    return resultado


# ═══════════════════════════════════════════════════════
#  VALORACIONES — histórico de todas las valoraciones (tabla 'valoraciones')
# ═══════════════════════════════════════════════════════

def guardar_valoracion(estrellas, comentario=None, nombre=None, correo=None, numero_pedido=None):
    """
    Guarda una fila en 'valoraciones' con cada valoración recibida desde
    valorar.html, buena o mala - para tener histórico aunque las de 4-5
    estrellas no generen correo interno.
    """
    datos = {"estrellas": estrellas}
    if comentario:
        datos["comentario"] = comentario
    if nombre:
        datos["nombre"] = nombre
    if correo:
        datos["correo"] = correo
    if numero_pedido:
        datos["numero_pedido"] = numero_pedido
    supabase.table("valoraciones").insert(datos).execute()