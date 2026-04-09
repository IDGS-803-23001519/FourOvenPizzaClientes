from flask import Flask, render_template, request, redirect, url_for
from flask import flash
from flask_wtf.csrf import CSRFProtect
from config import DevelopmentConfig
from models import db  
from ordenar import ordenar

app = Flask(__name__)  
app.config.from_object(DevelopmentConfig)

db.init_app(app)  

csrf = CSRFProtect() 
csrf.init_app(app)  

app.register_blueprint(ordenar)

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.route("/")
def index():
    return render_template("index.html")

@app.route('/inicio')
def inicio():
    return render_template('Inicio/inicio.html')

@app.route('/ordenar')
def ordenar():
    return render_template('ordenar/ordenarAhora.html')

@app.route('/registrar')
def registrar():
     return render_template('registrar/registrarVenta.html')

if __name__ == '__main__':
    app.run(debug=True, port=8000)