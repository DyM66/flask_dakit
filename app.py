import os
import unicodedata
from collections import Counter
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
    # Título alternativo oficial (inglés para anime, español para films en inglés, etc.).
    # Mapeado a la columna existente "title_es" para no migrar el schema.
    title_alt = db.Column("title_es", db.String(200))
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


class Season(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(db.Integer, db.ForeignKey("entry.id"), nullable=False)
    number = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(120))
    year = db.Column(db.Integer)
    director = db.Column(db.String(100))
    image = db.Column(db.String(100))

    entry = db.relationship(
        "Entry",
        backref=db.backref("seasons", cascade="all, delete-orphan", order_by="Season.number"),
    )

    def __repr__(self):
        return f"<Season {self.number} of entry {self.entry_id}>"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ─────────────────────────────── Utilidades ────────────────────────────

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]


def normalize_text(value):
    """Minúsculas y sin acentos, para búsquedas tolerantes."""
    text = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def save_image(file):
    if file and file.filename and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
        return filename
    return None


def download_poster(url, basename):
    from urllib.parse import urlparse
    from urllib.request import Request, urlopen

    ext = (os.path.splitext(urlparse(url).path)[1] or ".jpg").lower()
    if ext.lstrip(".") not in app.config["ALLOWED_EXTENSIONS"]:
        ext = ".jpg"
    filename = secure_filename(f"{basename}{ext}")
    http_request = Request(url, headers={"User-Agent": "dakit"})
    with urlopen(http_request, timeout=20) as response:
        data = response.read()
    with open(os.path.join(app.config["UPLOAD_FOLDER"], filename), "wb") as handle:
        handle.write(data)
    return filename


def selected_genres():
    ids = request.form.getlist("genres")
    return Genre.query.filter(Genre.id.in_(ids)).all()


def resolve_poster(basename):
    """Obtiene el póster del form: descarga si hay image_url, o guarda el archivo subido."""
    image_url = request.form.get("image_url", "").strip()
    if image_url:
        try:
            return download_poster(image_url, basename)
        except Exception as exc:
            flash(f"No se pudo descargar el póster: {exc}", "danger")
            return None
    return save_image(request.files.get("image"))


def ranked_with_pct(counter, limit):
    """Convierte un Counter en [(nombre, conteo, porcentaje_vs_top)] para barras."""
    items = counter.most_common(limit)
    if not items:
        return []
    top = items[0][1]
    return [(name, count, round(count / top * 100)) for name, count in items]


# ─────────────────────────────── Catálogo ──────────────────────────────

@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    type_filter = request.args.get("type", "").strip()
    year_filter = request.args.get("year", type=int)

    query = Entry.query
    if type_filter:
        query = query.filter(Entry.type == type_filter)
    if year_filter:
        query = query.filter(Entry.year == year_filter)
    entries = query.order_by(Entry.title).all()

    if q:
        needle = normalize_text(q)
        entries = [
            e for e in entries
            if needle in normalize_text(e.title)
            or needle in normalize_text(e.title_alt)
            or needle in normalize_text(e.creator)
        ]
    types = [t for (t,) in db.session.query(Entry.type).distinct().order_by(Entry.type)]
    years = [y for (y,) in db.session.query(Entry.year).distinct().order_by(Entry.year.desc())]
    return render_template(
        "index.html",
        entries=entries,
        types=types,
        years=years,
        filters={"q": q, "type": type_filter, "year": year_filter},
    )


@app.route("/dashboard")
def dashboard():
    entries = Entry.query.all()
    total = len(entries)

    type_counter = Counter()
    platform_counter = Counter()
    director_counter = Counter()
    genre_counter = Counter()
    decade_counter = Counter()
    years = []

    for entry in entries:
        type_counter[entry.type] += 1
        if entry.platform:
            platform_counter[entry.platform] += 1
        for name in (entry.creator or "").split(","):
            name = name.strip()
            if name:
                director_counter[name] += 1
        for genre in entry.genres:
            genre_counter[genre.name] += 1
        if entry.year:
            years.append(entry.year)
            decade_counter[(entry.year // 10) * 10] += 1

    series_with_seasons = sorted(
        ((e.title, len(e.seasons)) for e in entries if e.seasons),
        key=lambda pair: pair[1],
        reverse=True,
    )

    return render_template(
        "dashboard.html",
        total=total,
        total_seasons=Season.query.count(),
        total_seasons_series=len(series_with_seasons),
        oldest=min(years) if years else None,
        newest=max(years) if years else None,
        top_directors=ranked_with_pct(director_counter, 10),
        top_genres=ranked_with_pct(genre_counter, 10),
        top_platforms=ranked_with_pct(platform_counter, 8),
        by_type=ranked_with_pct(type_counter, 10),
        by_decade=sorted(decade_counter.items()),
        top_series=series_with_seasons[:8],
        unique_directors=len(director_counter),
    )


@app.route("/add", methods=["POST"])
@login_required
def add():
    entry = Entry(
        title=request.form["title"],
        title_alt=request.form.get("title_alt", "").strip() or None,
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
        entry.title_alt = request.form.get("title_alt", "").strip() or None
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


# ─────────────────────────────── Temporadas ────────────────────────────

@app.route("/entry/<int:id>/seasons/add", methods=["POST"])
@login_required
def add_season(id):
    entry = db.get_or_404(Entry, id)
    number = request.form.get("number", type=int)
    season = Season(
        entry_id=entry.id,
        number=number,
        name=request.form.get("name", "").strip() or None,
        year=request.form.get("year", type=int),
        director=request.form.get("director", "").strip() or None,
        image=resolve_poster(f"{entry.title}_T{number}"),
    )
    try:
        db.session.add(season)
        db.session.commit()
        flash(f"Temporada {season.number} agregada a «{entry.title}».", "success")
    except Exception:
        db.session.rollback()
        flash("No se pudo agregar la temporada.", "danger")
    return redirect(url_for("update", id=entry.id))


@app.route("/seasons/edit/<int:id>", methods=["POST"])
@login_required
def edit_season(id):
    season = db.get_or_404(Season, id)
    season.number = request.form.get("number", type=int)
    season.year = request.form.get("year", type=int)
    season.name = request.form.get("name", "").strip() or None
    season.director = request.form.get("director", "").strip() or None
    new_image = resolve_poster(f"{season.entry.title}_T{season.number}")
    if new_image:
        season.image = new_image
    try:
        db.session.commit()
        flash(f"Temporada {season.number} actualizada.", "success")
    except Exception:
        db.session.rollback()
        flash("No se pudo actualizar la temporada.", "danger")
    return redirect(url_for("update", id=season.entry_id))


@app.route("/seasons/delete/<int:id>", methods=["POST"])
@login_required
def delete_season(id):
    season = db.get_or_404(Season, id)
    entry_id = season.entry_id
    try:
        if season.image:
            image_path = os.path.join(app.config["UPLOAD_FOLDER"], season.image)
            if os.path.exists(image_path):
                os.remove(image_path)
        db.session.delete(season)
        db.session.commit()
        flash("Temporada eliminada.", "success")
    except Exception:
        db.session.rollback()
        flash("No se pudo eliminar la temporada.", "danger")
    return redirect(url_for("update", id=entry_id))


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
@click.option("--title", required=True, help="Título original (principal).")
@click.option("--title-alt", "title_alt", default="", help="Título alternativo oficial (si difiere del original).")
@click.option("--creator", required=True)
@click.option("--year", required=True, type=int)
@click.option("--platform", required=True)
@click.option("--type", "type_", required=True)
@click.option("--genres", default="", help="Nombres de géneros separados por coma.")
@click.option("--image-url", default="", help="URL de un póster para descargar.")
def add_entry(title, title_alt, creator, year, platform, type_, genres, image_url):
    """Registra una entrada en la biblioteca desde la línea de comandos."""
    entry = Entry(
        title=title, title_alt=title_alt.strip() or None,
        creator=creator, year=year, platform=platform, type=type_,
    )
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
        try:
            entry.image = download_poster(image_url, title)
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


@app.cli.command("add-season")
@click.option("--entry-title", required=True)
@click.option("--number", required=True, type=int)
@click.option("--year", type=int)
@click.option("--director", default="")
@click.option("--name", default="", help="Subtítulo de la temporada (opcional).")
@click.option("--image", default="", help="Archivo de póster ya en uploads, o URL http(s) para descargar.")
def add_season_cmd(entry_title, number, year, director, name, image):
    """Agrega una temporada a una serie existente (buscada por título)."""
    entry = Entry.query.filter(db.func.lower(Entry.title) == entry_title.lower()).first()
    if entry is None:
        click.echo(f"No existe la serie «{entry_title}».")
        return

    poster = None
    if image.startswith("http"):
        try:
            poster = download_poster(image, f"{entry.title}_T{number}")
        except Exception as exc:
            click.echo(f"⚠️ No se pudo descargar la imagen: {exc}")
    elif image:
        poster = image

    season = Season(
        entry_id=entry.id, number=number, name=name or None,
        year=year, director=director or None, image=poster,
    )
    db.session.add(season)
    db.session.commit()
    click.echo(f"Temporada {number} agregada a «{entry.title}».")


@app.cli.command("set-season-poster")
@click.option("--entry-title", required=True)
@click.option("--number", required=True, type=int)
@click.option("--image-url", required=True)
def set_season_poster(entry_title, number, image_url):
    """Asigna (descargando) el póster de una temporada existente."""
    entry = Entry.query.filter(db.func.lower(Entry.title) == entry_title.lower()).first()
    if entry is None:
        click.echo(f"No existe la serie «{entry_title}».")
        return
    season = Season.query.filter_by(entry_id=entry.id, number=number).first()
    if season is None:
        click.echo(f"«{entry.title}» no tiene temporada {number}.")
        return
    try:
        season.image = download_poster(image_url, f"{entry.title}_T{number}")
        db.session.commit()
        click.echo(f"Póster de la temporada {number} de «{entry.title}» actualizado.")
    except Exception as exc:
        click.echo(f"⚠️ No se pudo descargar la imagen: {exc}")


if __name__ == "__main__":
    app.run(debug=True)
