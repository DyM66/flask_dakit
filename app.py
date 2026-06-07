import os
from datetime import date

import click
from flask import Flask, flash, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from sqlalchemy import or_
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from config import Config

app = Flask(__name__)
app.config.from_object(Config)

db = SQLAlchemy(app)
migrate = Migrate(app, db)
csrf = CSRFProtect(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Inicia sesión para administrar la biblioteca."
login_manager.login_message_category = "warning"

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)


@app.context_processor
def inject_globals():
    authed = current_user.is_authenticated
    return {
        "current_year": date.today().year,
        "all_genres": Genre.query.order_by(Genre.name).all() if authed else [],
        "all_platforms": Platform.query.order_by(Platform.name).all() if authed else [],
    }


# ─────────────────────────────── Modelos ───────────────────────────────

entry_genres = db.Table(
    "entry_genres",
    db.Column("entry_id", db.Integer, db.ForeignKey("entry.id"), primary_key=True),
    db.Column("genre_id", db.Integer, db.ForeignKey("genre.id"), primary_key=True),
)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Genre(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)

    def __repr__(self):
        return f"<Genre {self.name}>"


class Platform(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)

    def __repr__(self):
        return f"<Platform {self.name}>"


class Entry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    creator = db.Column(db.String(100), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    platform = db.Column(db.String(50), nullable=False)
    type = db.Column(db.String(20), nullable=False)
    image = db.Column(db.String(100), nullable=True)

    genres = db.relationship(
        "Genre",
        secondary=entry_genres,
        lazy="subquery",
        backref=db.backref("entries", lazy=True),
    )

    def __repr__(self):
        return f"<{self.id} - {self.title}>"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ─────────────────────────────── Utilidades ────────────────────────────

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]


def save_image(file):
    if file and file.filename and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
        return filename
    return None


def selected_genres():
    ids = request.form.getlist("genres")
    return Genre.query.filter(Genre.id.in_(ids)).all()


# ─────────────────────────────── Catálogo ──────────────────────────────

@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    type_filter = request.args.get("type", "").strip()
    year_filter = request.args.get("year", type=int)

    query = Entry.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Entry.title.ilike(like), Entry.creator.ilike(like)))
    if type_filter:
        query = query.filter(Entry.type == type_filter)
    if year_filter:
        query = query.filter(Entry.year == year_filter)

    entries = query.order_by(Entry.title).all()
    types = [t for (t,) in db.session.query(Entry.type).distinct().order_by(Entry.type)]
    years = [y for (y,) in db.session.query(Entry.year).distinct().order_by(Entry.year.desc())]
    return render_template(
        "index.html",
        entries=entries,
        types=types,
        years=years,
        filters={"q": q, "type": type_filter, "year": year_filter},
    )


@app.route("/add", methods=["POST"])
@login_required
def add():
    entry = Entry(
        title=request.form["title"],
        creator=request.form["creator"],
        year=request.form.get("year", type=int),
        platform=request.form["platform"],
        type=request.form["type"],
        image=save_image(request.files.get("image")),
    )
    entry.genres = selected_genres()
    try:
        db.session.add(entry)
        db.session.commit()
        flash(f"«{entry.title}» agregado a la biblioteca.", "success")
    except Exception:
        db.session.rollback()
        flash("No se pudo agregar la entrada.", "danger")
    return redirect(url_for("index"))


@app.route("/update/<int:id>", methods=["GET", "POST"])
@login_required
def update(id):
    entry = db.get_or_404(Entry, id)
    if request.method == "POST":
        entry.title = request.form["title"]
        entry.creator = request.form["creator"]
        entry.year = request.form.get("year", type=int)
        entry.platform = request.form["platform"]
        entry.type = request.form["type"]
        entry.genres = selected_genres()
        new_image = save_image(request.files.get("image"))
        if new_image:
            entry.image = new_image
        try:
            db.session.commit()
            flash("Entrada actualizada.", "success")
            return redirect(url_for("index"))
        except Exception:
            db.session.rollback()
            flash("No se pudo actualizar la entrada.", "danger")
    genres = Genre.query.order_by(Genre.name).all()
    return render_template("update_item.html", entry=entry, genres=genres)


@app.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete(id):
    entry = db.get_or_404(Entry, id)
    try:
        if entry.image:
            image_path = os.path.join(app.config["UPLOAD_FOLDER"], entry.image)
            if os.path.exists(image_path):
                os.remove(image_path)
        db.session.delete(entry)
        db.session.commit()
        flash(f"«{entry.title}» eliminado.", "success")
    except Exception:
        db.session.rollback()
        flash("No se pudo eliminar la entrada.", "danger")
    return redirect(url_for("index"))


# ─────────────────────────────── Géneros ───────────────────────────────

@app.route("/genres")
def genres():
    items = Genre.query.order_by(Genre.name).all()
    return render_template("genres.html", genres=items)


@app.route("/genres/add", methods=["POST"])
@login_required
def add_genre():
    name = request.form["name"].strip()
    if name:
        try:
            db.session.add(Genre(name=name))
            db.session.commit()
            flash(f"Género «{name}» agregado.", "success")
        except Exception:
            db.session.rollback()
            flash("Ese género ya existe o no es válido.", "danger")
    return redirect(url_for("genres"))


@app.route("/genres/delete/<int:id>", methods=["POST"])
@login_required
def delete_genre(id):
    genre = db.get_or_404(Genre, id)
    try:
        db.session.delete(genre)
        db.session.commit()
        flash(f"Género «{genre.name}» eliminado.", "success")
    except Exception:
        db.session.rollback()
        flash("No se pudo eliminar el género.", "danger")
    return redirect(url_for("genres"))


# ─────────────────────────────── Plataformas ───────────────────────────

@app.route("/platforms")
@login_required
def platforms():
    items = Platform.query.order_by(Platform.name).all()
    return render_template("platforms.html", platforms=items)


@app.route("/platforms/add", methods=["POST"])
@login_required
def add_platform():
    name = request.form["name"].strip()
    if name:
        try:
            db.session.add(Platform(name=name))
            db.session.commit()
            flash(f"Plataforma «{name}» agregada.", "success")
        except Exception:
            db.session.rollback()
            flash("Esa plataforma ya existe o no es válida.", "danger")
    return redirect(url_for("platforms"))


@app.route("/platforms/delete/<int:id>", methods=["POST"])
@login_required
def delete_platform(id):
    platform = db.get_or_404(Platform, id)
    try:
        db.session.delete(platform)
        db.session.commit()
        flash(f"Plataforma «{platform.name}» eliminada.", "success")
    except Exception:
        db.session.rollback()
        flash("No se pudo eliminar la plataforma.", "danger")
    return redirect(url_for("platforms"))


# ───────────────────────────── Autenticación ───────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        user = User.query.filter_by(username=request.form["username"]).first()
        if user and user.check_password(request.form["password"]):
            login_user(user)
            flash(f"Bienvenido, {user.username}.", "success")
            next_page = request.args.get("next")
            if next_page and next_page.startswith("/"):
                return redirect(next_page)
            return redirect(url_for("index"))
        flash("Usuario o contraseña incorrectos.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("index"))


# ─────────────────────────────── Usuarios ──────────────────────────────

@app.route("/users")
@login_required
def users():
    items = User.query.order_by(User.username).all()
    return render_template("users.html", users=items)


@app.route("/users/add", methods=["POST"])
@login_required
def add_user():
    username = request.form["username"].strip()
    password = request.form["password"]
    if not username or not password:
        flash("Usuario y contraseña son obligatorios.", "danger")
    elif User.query.filter_by(username=username).first():
        flash(f"El usuario «{username}» ya existe.", "danger")
    else:
        user = User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash(f"Usuario «{username}» creado.", "success")
    return redirect(url_for("users"))


@app.route("/users/delete/<int:id>", methods=["POST"])
@login_required
def delete_user(id):
    user = db.get_or_404(User, id)
    if user.id == current_user.id:
        flash("No puedes eliminar tu propia cuenta mientras la usas.", "danger")
    elif User.query.count() <= 1:
        flash("No puedes eliminar el único usuario (quedarías sin acceso).", "danger")
    else:
        db.session.delete(user)
        db.session.commit()
        flash(f"Usuario «{user.username}» eliminado.", "success")
    return redirect(url_for("users"))


# ─────────────────────────────── CLI ───────────────────────────────────

@app.cli.command("init-db")
def init_db():
    """Crea las tablas que falten en la base de datos."""
    db.create_all()
    click.echo("Esquema verificado: tablas creadas/actualizadas.")


PLATFORM_SEED = [
    "Netflix", "Disney Plus", "HBO Max", "Amazon Prime", "Apple TV+",
    "Paramount+", "Star+", "Vix", "Crunchyroll", "Movistar Plus+",
    "Claro Video", "Mubi", "Pluto TV", "Hulu", "Peacock", "YouTube",
    "AnimeFLV", "Cuevana",
    "Cine Colombia", "Royal Films", "Cinépolis", "Cinemark", "Procinal",
    "Kindle", "Audible", "Físico (papel)", "Google Play Libros",
    "Blu-ray/DVD", "Otra",
]


@app.cli.command("seed-platforms")
def seed_platforms():
    """Carga el catálogo de plataformas (idempotente)."""
    created = 0
    for name in PLATFORM_SEED:
        if not Platform.query.filter_by(name=name).first():
            db.session.add(Platform(name=name))
            created += 1
    db.session.commit()
    click.echo(f"Plataformas: {created} nuevas, {Platform.query.count()} en total.")


@app.cli.command("add-entry")
@click.option("--title", required=True)
@click.option("--creator", required=True)
@click.option("--year", required=True, type=int)
@click.option("--platform", required=True)
@click.option("--type", "type_", required=True)
@click.option("--genres", default="", help="Nombres de géneros separados por coma.")
@click.option("--image-url", default="", help="URL de un póster para descargar.")
def add_entry(title, creator, year, platform, type_, genres, image_url):
    """Registra una entrada en la biblioteca desde la línea de comandos."""
    entry = Entry(title=title, creator=creator, year=year, platform=platform, type=type_)
    db.session.add(entry)

    linked = []
    for name in (g.strip() for g in genres.split(",") if g.strip()):
        genre = Genre.query.filter(db.func.lower(Genre.name) == name.lower()).first()
        if genre is None:
            genre = Genre(name=name)
            db.session.add(genre)
        entry.genres.append(genre)
        linked.append(name)

    if image_url:
        from urllib.parse import urlparse
        from urllib.request import Request, urlopen

        try:
            ext = (os.path.splitext(urlparse(image_url).path)[1] or ".jpg").lower()
            if ext.lstrip(".") not in app.config["ALLOWED_EXTENSIONS"]:
                ext = ".jpg"
            filename = secure_filename(f"{title}{ext}")
            request_ = Request(image_url, headers={"User-Agent": "dakit"})
            with urlopen(request_, timeout=20) as response:
                data = response.read()
            with open(os.path.join(app.config["UPLOAD_FOLDER"], filename), "wb") as handle:
                handle.write(data)
            entry.image = filename
        except Exception as exc:
            click.echo(f"⚠️ No se pudo descargar la imagen: {exc}")

    db.session.commit()
    click.echo(f"Entrada «{title}» creada (géneros: {', '.join(linked) or 'ninguno'}).")


@app.cli.command("create-user")
@click.argument("username")
@click.argument("password")
def create_user(username, password):
    """Crea un usuario para administrar la biblioteca: flask create-user <usuario> <clave>."""
    if User.query.filter_by(username=username).first():
        click.echo(f"El usuario '{username}' ya existe.")
        return
    user = User(username=username)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    click.echo(f"Usuario '{username}' creado.")


if __name__ == "__main__":
    app.run(debug=True)
