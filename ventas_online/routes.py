"""
ventas_online/routes.py
=======================
Blueprint: ventas_online
Prefijo: /tienda

Sin AJAX/fetch - todo server-side con redirects tradicionales.
Reserva temporal de stock al entrar al checkout.
Timer de 5 minutos en la página de checkout (no en seguimiento).
Entrega a domicilio - sin contador en seguimiento.
"""

import json
import threading
import base64
from datetime import datetime, timedelta

from flask import (
    Blueprint, redirect, render_template,
    request, session, url_for, flash, current_app
)
from sqlalchemy import text

from models import (
    db, Productos, Ventas, DetalleVenta, VentaStockReservado,
    OrdenesProduccion, DetalleProduccion, Recetas, DetalleReceta,
    MateriasPrimas, BitacoraEventos
)

ventas_online = Blueprint("ventas_online", __name__, template_folder="templates")

# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────
RESERVA_MINUTOS   = 5
USUARIO_ONLINE_ID = 3   # usuario "Ventas"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers internos
# ─────────────────────────────────────────────────────────────────────────────

def _get_tamano(prod):
    """Obtiene el tamaño del producto de forma segura."""
    return getattr(prod, 'tamano', None) or getattr(prod, 'tamaño', '')


def _imagen_b64(imagen_blob):
    """Convierte BLOB de imagen a cadena base64 para usar en <img src>."""
    if not imagen_blob:
        return None
    try:
        img_bytes = bytes(imagen_blob)
        return "data:image/png;base64," + base64.b64encode(img_bytes).decode()
    except Exception:
        return None


def _productos_menu():
    """
    Retorna productos activos con receta completa (≥4 ingredientes).
    Incluye imagen convertida a base64.
    """
    rows = db.session.execute(text("""
        SELECT
            p.idProducto,
            p.nombre,
            p.precio,
            p.`tamaño`  AS tamano,
            p.stock,
            p.imagen
        FROM productos p
        WHERE p.estatus = 1
          AND EXISTS (
                SELECT 1
                FROM recetas r
                JOIN detalleReceta dr ON dr.idReceta = r.idReceta
                WHERE r.idProducto = p.idProducto
                GROUP BY r.idReceta
                HAVING COUNT(dr.idDetalleR) >= 4
          )
        ORDER BY p.nombre, p.`tamaño`
    """)).mappings().all()

    result = []
    for r in rows:
        d = dict(r)
        d["precio"] = float(d["precio"] or 0)
        d["stock"]  = float(d["stock"]  or 0)
        d["imagen_b64"] = _imagen_b64(d.get("imagen"))
        d.pop("imagen", None)
        result.append(d)
    return result


def _calcular_faltante(id_producto, cantidad_pedida):
    """Retorna (stock_a_usar, faltante) para un producto y cantidad dados."""
    prod = Productos.query.get(id_producto)
    if not prod:
        return 0, cantidad_pedida
    stock_actual = float(prod.stock or 0)
    stock_a_usar = min(int(stock_actual), cantidad_pedida)
    faltante     = max(0, cantidad_pedida - int(stock_actual))
    return stock_a_usar, faltante


def _receta_de(id_producto):
    """Retorna el idReceta del producto o None."""
    row = db.session.execute(
        text("SELECT idReceta FROM recetas WHERE idProducto = :id LIMIT 1"),
        {"id": id_producto}
    ).fetchone()
    return row[0] if row else None


def _consumo_mp_dict(id_receta, cantidad):
    """
    Retorna {idMateriaP: {nombre, stock, requerido}} para producir `cantidad` unidades.
    """
    rows = db.session.execute(text("""
        SELECT mp.idMateriaP, mp.nombre,
               CAST(mp.stock AS DECIMAL(10,2)) AS stock,
               dr.cantidad * :cant AS requerido
        FROM detalleReceta dr
        JOIN materiasPrimas mp ON mp.idMateriaP = dr.idMateriaP
        WHERE dr.idReceta = :receta
    """), {"receta": id_receta, "cant": cantidad}).mappings().all()
    return {int(r["idMateriaP"]): dict(r) for r in rows}


def _validar_carrito(carrito):
    """
    Valida disponibilidad de stock y materias primas para el carrito completo.
    Retorna {"ok": bool, "errores": [...], "detalle": [...]}
    """
    mp_requerida = {}   # {idMateriaP: total_requerido}
    mp_info      = {}   # {idMateriaP: {nombre, stock}}
    errores      = []
    detalle      = []

    for item in carrito:
        id_producto = int(item["idProducto"])
        cantidad    = int(item["cantidad"])

        prod = Productos.query.get(id_producto)
        if not prod or not prod.estatus:
            errores.append({
                "idProducto": id_producto,
                "tipo": "no_disponible",
                "mensaje": f"El producto #{id_producto} ya no está disponible."
            })
            detalle.append({"idProducto": id_producto, "stock_a_usar": 0,
                            "faltante": cantidad, "stock_actual": 0})
            continue

        stock_a_usar, faltante = _calcular_faltante(id_producto, cantidad)
        detalle.append({
            "idProducto":   id_producto,
            "stock_a_usar": stock_a_usar,
            "faltante":     faltante,
            "stock_actual": float(prod.stock or 0)
        })

        if faltante > 0:
            id_receta = _receta_de(id_producto)
            if not id_receta:
                errores.append({
                    "idProducto": id_producto,
                    "tipo": "sin_receta",
                    "mensaje": (
                        f"Lo sentimos, <strong>{prod.nombre} ({_get_tamano(prod)})</strong> "
                        f"no tiene stock suficiente y no podemos prepararla en este momento."
                    )
                })
                continue

            for mp_id, mp in _consumo_mp_dict(id_receta, faltante).items():
                if mp_id not in mp_requerida:
                    mp_requerida[mp_id] = 0.0
                    mp_info[mp_id] = {"nombre": mp["nombre"], "stock": float(mp["stock"])}
                mp_requerida[mp_id] += float(mp["requerido"])

    for mp_id, requerido in mp_requerida.items():
        disponible = mp_info[mp_id]["stock"]
        if disponible < requerido:
            errores.append({
                "idProducto": None,
                "tipo": "mp_insuficiente",
                "mensaje": (
                    f"No contamos con suficientes ingredientes para preparar tu pedido. "
                    f"El insumo <strong>{mp_info[mp_id]['nombre']}</strong> está agotado. "
                    f"Por favor reduce las cantidades o elige otras opciones."
                )
            })

    return {"ok": len(errores) == 0, "errores": errores, "detalle": detalle}


def _liberar_reserva_temporal(id_venta):
    """
    Libera stock reservado temporalmente (antes de confirmar la venta).
    Solo opera sobre ventas en estado 'Reservado_temp'.
    """
    try:
        venta = Ventas.query.get(id_venta)
        if not venta or venta.estado != "Reservado_temp":
            return

        reservas = VentaStockReservado.query.filter_by(idVenta=id_venta).all()
        for r in reservas:
            # Cancelar orden de producción vinculada
            if r.idOrdenProduccion:
                orden = OrdenesProduccion.query.get(r.idOrdenProduccion)
                if orden and orden.estado == "En proceso":
                    orden.estado = "Cancelada"
                    # Devolver materias primas descontadas
                    consumo = db.session.execute(text("""
                        SELECT dr.idMateriaP, SUM(dr.cantidad * dp.cantidad) AS total
                        FROM detalleProduccion dp
                        JOIN recetas r ON r.idProducto = dp.idProducto
                        JOIN detalleReceta dr ON dr.idReceta = r.idReceta
                        WHERE dp.idOrden = :oid
                        GROUP BY dr.idMateriaP
                    """), {"oid": orden.idOrden}).mappings().all()
                    for c in consumo:
                        mp = MateriasPrimas.query.get(c["idMateriaP"])
                        if mp:
                            mp.stock = float(mp.stock or 0) + float(c["total"])

            # Devolver stock del producto reservado
            if r.cantidadReservada and r.cantidadReservada > 0:
                prod = Productos.query.get(r.idProducto)
                if prod:
                    prod.stock = float(prod.stock or 0) + r.cantidadReservada

        VentaStockReservado.query.filter_by(idVenta=id_venta).delete()
        # Eliminar detalle y venta temporal
        DetalleVenta.query.filter_by(idVenta=id_venta).delete()
        db.session.delete(venta)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"[ventas_online] Error liberando reserva temporal venta #{id_venta}: {e}")


def _timer_vencimiento_checkout(app, id_venta_temp, segundos):
    """
    Lanza hilo que libera la reserva temporal si el usuario
    no completa el checkout en el tiempo dado.
    """
    def _liberar():
        import time
        time.sleep(segundos)
        with app.app_context():
            _liberar_reserva_temporal(id_venta_temp)

    t = threading.Thread(target=_liberar, daemon=True)
    t.start()


# ─────────────────────────────────────────────────────────────────────────────
# Rutas
# ─────────────────────────────────────────────────────────────────────────────

@ventas_online.route("/menu")
def menu():
    """
    Página principal del menú.
    Si venía del carrito, limpia cualquier reserva temporal anterior.
    """
    # Si hay una reserva temporal activa, liberarla al volver al menú
    id_temp = session.get("reserva_temp_id")
    if id_temp:
        _liberar_reserva_temporal(int(id_temp))
        session.pop("reserva_temp_id",     None)
        session.pop("reserva_temp_expira", None)

    productos = _productos_menu()

    # Carrito guardado en sesión
    carrito_raw = session.get("carrito_online", "[]")
    try:
        carrito = json.loads(carrito_raw)
    except Exception:
        carrito = []

    # Validar carrito actual para mostrar badges de producción
    detalle_stock = {}
    if carrito:
        validacion = _validar_carrito(carrito)
        for d in validacion.get("detalle", []):
            detalle_stock[d["idProducto"]] = d

    errores_flash = session.pop("carrito_errores", [])

    return render_template(
        "ordenar/ordenarAhora.html",
        productos=productos,
        carrito=carrito,
        detalle_stock=detalle_stock,
        errores=errores_flash,
    )


@ventas_online.route("/agregar", methods=["POST"])
def agregar_al_carrito():
    """Agrega un producto al carrito (server-side) y valida disponibilidad."""
    id_producto = int(request.form.get("idProducto", 0))
    cantidad    = int(request.form.get("cantidad", 1))

    if not id_producto or cantidad < 1:
        flash("Producto no válido.", "danger")
        return redirect(url_for("ventas_online.menu"))

    prod = Productos.query.get(id_producto)
    if not prod or not prod.estatus:
        flash("El producto seleccionado no está disponible.", "danger")
        return redirect(url_for("ventas_online.menu"))

    # Leer carrito actual
    carrito_raw = session.get("carrito_online", "[]")
    try:
        carrito = json.loads(carrito_raw)
    except Exception:
        carrito = []

    # Actualizar cantidad
    encontrado = False
    for item in carrito:
        if int(item["idProducto"]) == id_producto:
            item["cantidad"] += cantidad
            encontrado = True
            break
    if not encontrado:
        carrito.append({"idProducto": id_producto, "cantidad": cantidad})

    # Validar el carrito completo
    validacion = _validar_carrito(carrito)
    if not validacion["ok"]:
        # Revertir la adición del producto problemático
        for item in carrito:
            if int(item["idProducto"]) == id_producto:
                item["cantidad"] -= cantidad
                if item["cantidad"] <= 0:
                    carrito = [i for i in carrito if int(i["idProducto"]) != id_producto]
                break
        session["carrito_errores"] = [e["mensaje"] for e in validacion["errores"]]
    
    session["carrito_online"] = json.dumps(carrito)
    return redirect(url_for("ventas_online.menu"))


@ventas_online.route("/actualizar", methods=["POST"])
def actualizar_carrito():
    """Actualiza cantidad o elimina un item del carrito."""
    id_producto = int(request.form.get("idProducto", 0))
    accion      = request.form.get("accion", "")  # "aumentar", "disminuir", "eliminar"

    carrito_raw = session.get("carrito_online", "[]")
    try:
        carrito = json.loads(carrito_raw)
    except Exception:
        carrito = []

    nuevo_carrito = []
    for item in carrito:
        if int(item["idProducto"]) == id_producto:
            if accion == "aumentar":
                item["cantidad"] += 1
                nuevo_carrito.append(item)
            elif accion == "disminuir":
                item["cantidad"] -= 1
                if item["cantidad"] > 0:
                    nuevo_carrito.append(item)
                # si llega a 0, se elimina
            elif accion == "eliminar":
                pass  # no agregar = eliminar
            else:
                nuevo_carrito.append(item)
        else:
            nuevo_carrito.append(item)

    # Validar carrito actualizado
    if nuevo_carrito:
        validacion = _validar_carrito(nuevo_carrito)
        if not validacion["ok"]:
            session["carrito_errores"] = [e["mensaje"] for e in validacion["errores"]]

    session["carrito_online"] = json.dumps(nuevo_carrito)
    return redirect(url_for("ventas_online.menu"))


@ventas_online.route("/limpiar-carrito", methods=["POST"])
def limpiar_carrito():
    """Vacía el carrito."""
    session.pop("carrito_online", None)
    return redirect(url_for("ventas_online.menu"))


@ventas_online.route("/checkout")
def checkout():
    """
    Página de checkout / datos del cliente.

    Al entrar aquí:
      1. Se valida el carrito completo.
      2. Se descuenta el stock de forma TEMPORAL (estado 'Reservado_temp').
      3. Se inicia un timer de 5 min; si vence sin confirmación, se libera el stock.
      4. Se muestra un countdown de 5 min al usuario.
    """
    # Si ya había una reserva temporal activa, liberarla antes de crear una nueva
    id_temp_anterior = session.get("reserva_temp_id")
    if id_temp_anterior:
        _liberar_reserva_temporal(int(id_temp_anterior))
        session.pop("reserva_temp_id",     None)
        session.pop("reserva_temp_expira", None)

    carrito_raw = session.get("carrito_online", "[]")
    try:
        carrito = json.loads(carrito_raw)
    except Exception:
        carrito = []

    if not carrito:
        flash("Tu carrito está vacío.", "warning")
        return redirect(url_for("ventas_online.menu"))

    # Validar disponibilidad
    validacion = _validar_carrito(carrito)
    if not validacion["ok"]:
        session["carrito_errores"] = [e["mensaje"] for e in validacion["errores"]]
        return redirect(url_for("ventas_online.menu"))

    # ── Reservar stock temporalmente ──────────────────────────────────────────
    try:
        faltantes = []

        for item in carrito:
            id_producto = int(item["idProducto"])
            cantidad    = int(item["cantidad"])
            prod = Productos.query.get(id_producto)
            if not prod:
                raise ValueError(f"Producto #{id_producto} no encontrado.")
            stock_a_usar, faltante_prod = _calcular_faltante(id_producto, cantidad)
            if stock_a_usar > 0:
                prod.stock = float(prod.stock or 0) - stock_a_usar
            if faltante_prod > 0:
                faltantes.append({"idProducto": id_producto, "cantidad": faltante_prod})

        # Crear venta temporal en BD para poder referenciarla
        venta_temp = Ventas(
            idUsuario=USUARIO_ONLINE_ID,
            nombreCliente="[TEMP - Pendiente de confirmar]",
            tipo="En línea",
            metodoPago="Efectivo",
            estado="Reservado_temp",
        )
        db.session.add(venta_temp)
        db.session.flush()
        id_venta_temp = venta_temp.idVenta

        # Detalle temporal
        for item in carrito:
            id_producto = int(item["idProducto"])
            cantidad    = int(item["cantidad"])
            prod = Productos.query.get(id_producto)
            db.session.add(DetalleVenta(
                idVenta=id_venta_temp,
                idProducto=id_producto,
                cantidad=cantidad,
                precio=prod.precio,
            ))

        # Reservas de stock
        for item in carrito:
            id_producto   = int(item["idProducto"])
            cantidad      = int(item["cantidad"])
            faltante_prod = next(
                (f["cantidad"] for f in faltantes if f["idProducto"] == id_producto), 0
            )
            db.session.add(VentaStockReservado(
                idVenta=id_venta_temp,
                idProducto=id_producto,
                cantidadReservada=cantidad - faltante_prod,
                cantidadFaltante=faltante_prod,
            ))

        db.session.flush()

        # Crear orden de producción temporal si hay faltantes
        if faltantes:
            for f in faltantes:
                id_receta = _receta_de(f["idProducto"])
                if id_receta:
                    db.session.execute(text("""
                        UPDATE materiasPrimas mp
                        JOIN detalleReceta dr ON dr.idMateriaP = mp.idMateriaP
                        SET mp.stock = mp.stock - (dr.cantidad * :cant)
                        WHERE dr.idReceta = :receta
                    """), {"cant": f["cantidad"], "receta": id_receta})

            orden_temp = OrdenesProduccion(
                idUsuario=USUARIO_ONLINE_ID,
                estado="En proceso",
                origen="Venta",
                idVentaOrigen=id_venta_temp,
            )
            db.session.add(orden_temp)
            db.session.flush()

            for f in faltantes:
                db.session.add(DetalleProduccion(
                    idProducto=f["idProducto"],
                    idOrden=orden_temp.idOrden,
                    cantidad=f["cantidad"],
                ))

            db.session.execute(text("""
                UPDATE ventaStockReservado
                SET idOrdenProduccion = :oid
                WHERE idVenta = :vid
            """), {"oid": orden_temp.idOrden, "vid": id_venta_temp})

        db.session.commit()

        # Guardar en sesión
        expira = datetime.now() + timedelta(minutes=RESERVA_MINUTOS)
        session["reserva_temp_id"]     = id_venta_temp
        session["reserva_temp_expira"] = expira.isoformat()

        # Iniciar timer de cancelación automática
        _timer_vencimiento_checkout(
            current_app._get_current_object(),
            id_venta_temp,
            RESERVA_MINUTOS * 60
        )

    except Exception as e:
        db.session.rollback()
        flash(f"Error al apartar tu pedido: {str(e)}", "danger")
        return redirect(url_for("ventas_online.menu"))

    # Construir datos para el template
    productos_carrito = []
    total = 0.0
    for item in carrito:
        prod = Productos.query.get(int(item["idProducto"]))
        if not prod:
            continue
        cantidad = int(item["cantidad"])
        subtotal = float(prod.precio) * cantidad
        total   += subtotal
        faltante = next(
            (f["cantidad"] for f in faltantes if f["idProducto"] == prod.idProducto), 0
        )
        productos_carrito.append({
            "idProducto": prod.idProducto,
            "nombre":     prod.nombre,
            "tamano":     _get_tamano(prod),
            "precio":     float(prod.precio),
            "cantidad":   cantidad,
            "subtotal":   subtotal,
            "faltante":   faltante,
        })

    segundos_restantes = RESERVA_MINUTOS * 60

    return render_template(
        "registrar/registrarVenta.html",
        productos_carrito=productos_carrito,
        total=total,
        segundos_restantes=segundos_restantes,
        segundos_totales=RESERVA_MINUTOS * 60,
    )


@ventas_online.route("/confirmar-pedido", methods=["POST"])
def confirmar_pedido():
    """
    Confirma la compra:
      - Valida que la reserva temporal siga vigente.
      - Actualiza la venta temporal con los datos reales del cliente.
      - Cambia el estado de 'Reservado_temp' a 'En proceso' o 'Lista para entregar'.
      - Limpia el carrito de la sesión.
    """
    id_venta_temp = session.get("reserva_temp_id")
    expira_iso    = session.get("reserva_temp_expira")

    if not id_venta_temp:
        flash("Tu tiempo de compra venció. Por favor vuelve a armar tu pedido.", "warning")
        return redirect(url_for("ventas_online.menu"))

    # Verificar que no haya vencido
    if expira_iso:
        try:
            expira_dt = datetime.fromisoformat(expira_iso)
            if datetime.now() > expira_dt:
                _liberar_reserva_temporal(int(id_venta_temp))
                session.pop("reserva_temp_id",     None)
                session.pop("reserva_temp_expira", None)
                flash("El tiempo para completar tu pedido venció. Tu stock fue liberado. Por favor vuelve a intentarlo.", "warning")
                return redirect(url_for("ventas_online.menu"))
        except Exception:
            pass

    # Datos del formulario
    nombre_cliente = request.form.get("nombreCliente", "").strip()
    colonia        = request.form.get("colonia",  "").strip()
    calle          = request.form.get("calle",    "").strip()
    numero         = request.form.get("numero",   "").strip()
    cp             = request.form.get("cp",       "").strip()
    metodo_pago    = request.form.get("metodoPago", "Efectivo").strip()

    # Validar campos obligatorios
    errores = []
    if not nombre_cliente:
        errores.append("Nombre completo")
    if not colonia:
        errores.append("Colonia")
    if not calle:
        errores.append("Calle")
    if not numero:
        errores.append("Número")
    if not cp:
        errores.append("Código Postal")
    if metodo_pago not in ("Efectivo", "Tarjeta"):
        errores.append("Método de pago válido")

    if errores:
        flash("Faltan los siguientes campos: " + ", ".join(errores), "danger")
        # Redirigir de vuelta al checkout SIN liberar la reserva
        return redirect(url_for("ventas_online.checkout_datos"))

    direccion = ", ".join(filter(None, [colonia, calle, numero, cp]))

    try:
        venta = Ventas.query.get(int(id_venta_temp))
        if not venta or venta.estado != "Reservado_temp":
            flash("El pedido ya no está disponible. Por favor vuelve a intentarlo.", "warning")
            session.pop("reserva_temp_id",     None)
            session.pop("reserva_temp_expira", None)
            return redirect(url_for("ventas_online.menu"))

        # Determinar estado final
        hay_produccion = VentaStockReservado.query.filter(
            VentaStockReservado.idVenta == venta.idVenta,
            VentaStockReservado.cantidadFaltante > 0
        ).count() > 0

        estado_final = "En proceso" if hay_produccion else "Lista para entregar"

        # Actualizar venta con datos reales
        venta.nombreCliente = f"{nombre_cliente} | Dir: {direccion}"
        venta.metodoPago    = metodo_pago
        venta.estado        = estado_final

        # Bitácora
        db.session.add(BitacoraEventos(
            usuarioId=USUARIO_ONLINE_ID,
            nombreUsuario="Sistema Web",
            modulo="VentasOnline",
            accion="CREAR",
            referencial="venta",
            referencia=f"ID:{venta.idVenta} | Cliente: {nombre_cliente}",
            ip=request.remote_addr,
        ))

        db.session.commit()

        # Limpiar sesión
        id_venta_final = venta.idVenta
        session.pop("reserva_temp_id",     None)
        session.pop("reserva_temp_expira", None)
        session.pop("carrito_online",      None)

        # Guardar id de venta para la página de seguimiento
        session["venta_online_id"] = id_venta_final

        return redirect(url_for("ventas_online.seguimiento", id_venta=id_venta_final))

    except Exception as e:
        db.session.rollback()
        flash(f"Error al confirmar tu pedido: {str(e)}", "danger")
        return redirect(url_for("ventas_online.checkout_datos"))


@ventas_online.route("/checkout/datos")
def checkout_datos():
    """
    Muestra el formulario de datos del cliente.
    Solo accesible si hay una reserva temporal activa.
    """
    id_venta_temp = session.get("reserva_temp_id")
    expira_iso    = session.get("reserva_temp_expira")

    if not id_venta_temp:
        flash("Tu carrito está vacío o el tiempo venció.", "warning")
        return redirect(url_for("ventas_online.menu"))

    # Calcular segundos restantes
    segundos_restantes = RESERVA_MINUTOS * 60
    if expira_iso:
        try:
            delta = (datetime.fromisoformat(expira_iso) - datetime.now()).total_seconds()
            segundos_restantes = max(0, int(delta))
        except Exception:
            pass

    if segundos_restantes <= 0:
        _liberar_reserva_temporal(int(id_venta_temp))
        session.pop("reserva_temp_id",     None)
        session.pop("reserva_temp_expira", None)
        flash("El tiempo para completar tu pedido venció. Por favor vuelve a intentarlo.", "warning")
        return redirect(url_for("ventas_online.menu"))

    # Obtener detalle de la venta temporal
    detalle = db.session.execute(text("""
        SELECT dv.cantidad, dv.precio,
               (dv.cantidad * dv.precio) AS subtotal,
               p.idProducto, p.nombre AS nombre_producto,
               p.`tamaño` AS tamano,
               COALESCE(vsr.cantidadFaltante, 0) AS faltante
        FROM detalleVenta dv
        JOIN productos p ON p.idProducto = dv.idProducto
        LEFT JOIN ventaStockReservado vsr
               ON vsr.idVenta = dv.idVenta AND vsr.idProducto = dv.idProducto
        WHERE dv.idVenta = :id
        ORDER BY p.nombre
    """), {"id": id_venta_temp}).mappings().all()

    total = sum(float(d["subtotal"]) for d in detalle)

    productos_carrito = [{
        "idProducto": d["idProducto"],
        "nombre":     d["nombre_producto"],
        "tamano":     d["tamano"],
        "precio":     float(d["precio"]),
        "cantidad":   d["cantidad"],
        "subtotal":   float(d["subtotal"]),
        "faltante":   int(d["faltante"]),
    } for d in detalle]

    return render_template(
        "registrar/registrarVenta.html",
        productos_carrito=productos_carrito,
        total=total,
        segundos_restantes=segundos_restantes,
        segundos_totales=RESERVA_MINUTOS * 60,
    )


@ventas_online.route("/volver-menu", methods=["POST"])
def volver_menu():
    """
    El usuario decide volver al menú desde el checkout.
    Se libera la reserva temporal.
    """
    id_temp = session.get("reserva_temp_id")
    if id_temp:
        _liberar_reserva_temporal(int(id_temp))
        session.pop("reserva_temp_id",     None)
        session.pop("reserva_temp_expira", None)
    return redirect(url_for("ventas_online.menu"))


@ventas_online.route("/pedido/<int:id_venta>")
def seguimiento(id_venta):
    """
    Página de seguimiento del pedido.
    Entrega a domicilio: sin countdown, solo estado.
    """
    venta = Ventas.query.get_or_404(id_venta)

    detalle = db.session.execute(text("""
        SELECT dv.idDetalleV, dv.cantidad, dv.precio,
               (dv.cantidad * dv.precio) AS subtotal,
               p.idProducto, p.nombre AS nombre_producto,
               p.`tamaño` AS tamano
        FROM detalleVenta dv
        JOIN productos p ON p.idProducto = dv.idProducto
        WHERE dv.idVenta = :id
        ORDER BY p.nombre
    """), {"id": id_venta}).mappings().all()

    total = sum(float(d["subtotal"]) for d in detalle)

    return render_template(
        "ordenar/seguimiento.html",
        venta=venta,
        detalle=list(detalle),
        total=total,
    )


@ventas_online.route("/estado/<int:id_venta>")
def estado_pedido(id_venta):
    """Endpoint para consultar el estado del pedido (para auto-refresh de la página)."""
    venta = Ventas.query.get(id_venta)
    if not venta:
        return {"estado": "no_encontrada"}, 404
    return {"estado": venta.estado}