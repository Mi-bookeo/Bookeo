"""
Bookeo · supabase_client.py
Funciones para leer/guardar datos de clientes y pedidos relacionados
con la conexión de Google Drive.
"""

import os
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
    Crea (o actualiza si ya existe) la fila de 'pedidos' para este libro.
    'datos' puede traer: cliente_id, formato, orientacion, paginas.
    fecha_pedido se pone sola (default now() en la tabla); fecha_pago se
    queda en NULO hasta que se pague de verdad - eso todavía no está
    conectado, se hace mañana junto con Stripe.

    Si la fila no existe todavía, hay que rellenar 'precio' (NOT NULL sin
    valor por defecto) con un valor provisional para que el insert no
    falle - se sobrescribe con el importe real al pagar de verdad. Si la
    fila ya existe, no se toca (para no pisar un precio ya calculado).
    """
    existe = supabase.table("pedidos").select("id").eq("id", pedido_id).execute()
    fila = {k: v for k, v in datos.items() if v is not None}
    fila["id"] = pedido_id
    if not existe.data:
        fila.setdefault("precio", 0)
    supabase.table("pedidos").upsert(fila, on_conflict="id").execute()


def marcar_pedido_pagado(numero_pedido):
    """
    Se llama SOLO desde el webhook de Stripe, cuando el pago se confirma
    de verdad (checkout.session.completed) - pone fecha_pago a AHORA.
    Antes de esto la fila ya existe (creada sin pagar en
    crear_o_actualizar_pedido_inicial, al iniciar el pago), así que esto
    es una actualización, no una creación.
    """
    supabase.table("pedidos").update({"fecha_pago": datetime.datetime.utcnow().isoformat()}).eq("id", numero_pedido).execute()


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
    supabase.table("pedidos").update({"aviso_pago_enviado": True}).eq("id", numero_pedido).execute()


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
    supabase.table("pedidos").update({"valoracion_enviada": True}).eq("id", numero_pedido).execute()


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
    unidad_url, editor_url.

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
    supabase.table("libros").upsert(fila, on_conflict="pedido_id").execute()


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
    Busca 'codigo' tanto en 'cupon' (el propio del cliente) como en
    'cupon_amigo' (el de recomendación) - NO borra ni marca nada como
    usado, esto solo CONSULTA si es válido, para poder enseñar el
    descuento en pantalla antes de pagar. El borrado/canje de verdad
    pasa en marcar_cupon_usado(), solo cuando el pago se confirma.

    formato: el formato del libro sobre el que se quiere aplicar (el más
    caro del carrito) - el código de amigo solo vale en mediano/grande,
    el propio vale en cualquiera.

    Devuelve None si no existe o no es válido para ese formato/fecha;
    si es válido, un dict con el % de descuento, el tipo, y la fila a la
    que pertenece (para poder canjearlo después).
    """
    codigo = (codigo or "").strip().upper()
    if not codigo:
        return None

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
    """
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