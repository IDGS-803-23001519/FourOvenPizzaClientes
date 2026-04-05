from flask import Blueprint

ordenar = Blueprint(
    'ordenar',
    __name__,
    template_folder='templates',
    static_folder='static'
)

from . import routes