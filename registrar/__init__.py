from flask import Blueprint

registrar = Blueprint(
    'registrar',
    __name__,
    template_folder='templates',
    static_folder='static'
)

from . import routes