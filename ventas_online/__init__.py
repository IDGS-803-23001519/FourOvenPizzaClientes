from flask import Blueprint

ventas_online = Blueprint(
    'ventas_online',
    __name__,
    template_folder='templates',
    static_folder='static'
)

from . import routes