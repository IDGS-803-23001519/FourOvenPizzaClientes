from flask import Flask, render_template
from flask_wtf.csrf import CSRFProtect
from config import DevelopmentConfig
from models import db
from ventas_online.routes import ventas_online

app = Flask(__name__)
app.config.from_object(DevelopmentConfig)
db.init_app(app)

csrf = CSRFProtect()
csrf.init_app(app)

# IMPORTANTE: url_prefix='/tienda' + eximir rutas AJAX del CSRF
app.register_blueprint(ventas_online, url_prefix='/tienda')
csrf.exempt(ventas_online)   # todas las rutas del blueprint están protegidas
                              # por validación propia; el CSRF lo maneja el JS

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.route("/")
def index():
    return render_template("index.html")

@app.route('/inicio')
def inicio():
    return render_template('Inicio/inicio.html')

if __name__ == '__main__':
    app.run(debug=True, port=8000)