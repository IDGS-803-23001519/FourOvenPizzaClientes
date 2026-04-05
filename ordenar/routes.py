from flask import render_template, session, url_for
from . import ordenar
from models import Productos, db

@ordenar.route('/ordenar')
def menu():
    """
    Vista principal del menú de pizzas.
    Muestra todas las pizzas activas (estatus=1).
    """
    try:
        productos = Productos.query.filter_by(estatus=1).order_by(Productos.tamaño, Productos.nombre).all()
        
        productos_list = []
        for p in productos:
            productos_list.append({
                'idProducto': p.idProducto,
                'nombre': p.nombre,
                'precio': float(p.precio),
                'tamaño': p.tamaño,
                'stock': float(p.stock)
            })
        
        return render_template('ordenar/ordenarAhora.html', 
                             productos=productos_list)
    except Exception as e:
        print(f"Error al cargar menú: {e}")
        return render_template('ordenar/ordenarAhora.html', 
                             productos=[])