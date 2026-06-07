import os
import secrets
import smtplib
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from email.message import EmailMessage

import click
from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from config import Config

app = Flask(__name__)
app.config.from_object(Config)

db = SQLAlchemy(app)
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
    email = db.Column(db.String(255))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def two_factor_enabled(self):
        return bool(self.email)


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

    people = db.relationship(
        "EntryPerson", back_populates="entry", cascade="all, delete-orphan"
    )

    @property
    def directors(self):
        return [link.person for link in self.people if link.role == "director"]

    @property
    def cast(self):
        return [link.person for link in self.people if link.role == "actor"]

    @property
    def cast_links(self):
        return [link for link in self.people if link.role == "actor"]

    @property
    def creator_csv(self):
        return ", ".join(p.name for p in self.directors)

    @property
    def cast_csv(self):
        return ", ".join(f"{l.person.name} ({l.character})" if l.character else l.person.name for l in self.cast_links)

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


class Person(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    # Nombre normalizado (minúsculas, sin acentos) para unicidad y dedup.
    normalized_name = db.Column(db.String(120), nullable=False, unique=True, index=True)

    # Hoja de vida básica — todo nullable: lo no hallado queda en blanco, nunca se inventa.
    photo = db.Column(db.String(100))          # filename en static/uploads, igual que Entry.image
    birth_date = db.Column(db.Date)
    death_date = db.Column(db.Date)            # NULL = sigue vivo
    nationality = db.Column(db.String(80))

    # Trazabilidad de fuente para que la skill re-consulte y detecte datos viejos.
    tmdb_id = db.Column(db.Integer)
    wikidata_qid = db.Column(db.String(20))
    photo_source = db.Column(db.String(20))    # 'tmdb' | 'commons' | 'wikipedia' | 'upload'
    photo_ref = db.Column(db.String(200))
    enriched_at = db.Column(db.DateTime)

    links = db.relationship(
        "EntryPerson", back_populates="person", cascade="all, delete-orphan"
    )

    @property
    def is_alive(self):
        return self.death_date is None

    @property
    def age(self):
        """Edad calculada en runtime (None si falta birth_date)."""
        if not self.birth_date:
            return None
        end = self.death_date or date.today()
        years = end.year - self.birth_date.year
        if (end.month, end.day) < (self.birth_date.month, self.birth_date.day):
            years -= 1
        return years

    @property
    def director_links(self):
        return [link for link in self.links if link.role == "director"]

    @property
    def actor_links(self):
        return [link for link in self.links if link.role == "actor"]

    def __repr__(self):
        return f"<Person {self.name}>"


class EntryPerson(db.Model):
    __tablename__ = "entry_person"
    entry_id = db.Column(db.Integer, db.ForeignKey("entry.id"), primary_key=True)
    person_id = db.Column(db.Integer, db.ForeignKey("person.id"), primary_key=True)
    role = db.Column(db.String(20), primary_key=True)  # 'director' | 'actor'
    character = db.Column(db.String(120))              # personaje interpretado (solo actores)

    entry = db.relationship("Entry", back_populates="people")
    person = db.relationship("Person", back_populates="links")


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


_FEM_SUFFIXES = (("esa", "es"), ("ana", "ano"), ("ina", "ino"), ("ena", "eno"), ("ica", "ico"), ("usa", "uso"), ("ola", "ol"))


def nationality_key(nat):
    """Clave de agrupación de nacionalidades: sin acentos y con género plegado a masculino
    (Británico/Británica/Britanico → 'britanico'). Las compuestas se dejan tal cual (sin acentos)."""
    n = normalize_text(nat).strip()
    if "-" in n:
        return n
    for fem, masc in _FEM_SUFFIXES:
        if n.endswith(fem):
            return n[: -len(fem)] + masc
    if len(n) > 3 and n.endswith("a"):
        return n[:-1] + "o"
    return n


def parse_date(value):
    """Parsea 'YYYY-MM-DD'. Devuelve date, o None si está vacío o es inválido."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def get_or_create_person(name):
    """Find-or-create por nombre normalizado. Idempotente. Devuelve (person, created)."""
    clean = (name or "").strip()
    if not clean:
        return None, False
    norm = normalize_text(clean)
    person = Person.query.filter_by(normalized_name=norm).first()
    if person:
        return person, False
    person = Person(name=clean, normalized_name=norm)
    db.session.add(person)
    try:
        db.session.flush()  # asigna id antes de vincular; la unique constraint protege de duplicados
    except IntegrityError:
        db.session.rollback()
        return Person.query.filter_by(normalized_name=norm).first(), False
    return person, True


def split_character(raw):
    """Separa 'Nombre (Personaje)' → (nombre, personaje|None). Sin paréntesis → (nombre, None)."""
    raw = raw.strip()
    if raw.endswith(")") and "(" in raw:
        name, _, rest = raw.partition("(")
        return name.strip(), (rest[:-1].strip() or None)
    return raw, None


def sync_entry_people(entry, creator_csv, cast_csv):
    """Reemplaza los enlaces de la entrada según los CSV del form (find-or-create).

    Directores: solo nombres. Reparto: 'Nombre (Personaje)' con personaje opcional.
    """
    entry.people.clear()
    db.session.flush()  # ejecuta los DELETE de enlaces viejos antes de re-insertar
    seen = set()
    for raw in (creator_csv or "").split(","):
        person, _ = get_or_create_person(raw)
        if not person or (person.id, "director") in seen:
            continue
        seen.add((person.id, "director"))
        entry.people.append(EntryPerson(person=person, role="director"))
    for raw in (cast_csv or "").split(","):
        name, character = split_character(raw)
        person, _ = get_or_create_person(name)
        if not person or (person.id, "actor") in seen:
            continue
        seen.add((person.id, "actor"))
        entry.people.append(EntryPerson(person=person, role="actor", character=character))


def ranked_people(counter, limit):
    """Counter[person_id] → [(Person, count, pct_vs_top)] para barras con avatar."""
    items = counter.most_common(limit)
    if not items:
        return []
    top = items[0][1]
    people = {p.id: p for p in Person.query.filter(Person.id.in_([pid for pid, _ in items]))}
    return [(people[pid], count, round(count / top * 100)) for pid, count in items]


def save_person_photo(person):
    """Guarda la foto (URL o archivo) como person_{id}.{ext} y borra la anterior si cambia."""
    old, new = person.photo, None
    url = request.form.get("photo_url", "").strip()
    if url:
        try:
            new = download_poster(url, f"person_{person.id}", max_dim=PERSON_PHOTO_DIM)
        except Exception as exc:
            flash(f"No se pudo descargar la foto: {exc}", "danger")
            return
    else:
        file = request.files.get("photo")
        if file and file.filename and allowed_file(file.filename):
            ext = os.path.splitext(file.filename)[1].lower() or ".jpg"
            new = secure_filename(f"person_{person.id}{ext}")
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], new))
            downscale_image(os.path.join(app.config["UPLOAD_FOLDER"], new), PERSON_PHOTO_DIM)
    if new:
        if old and old != new:
            old_path = os.path.join(app.config["UPLOAD_FOLDER"], old)
            if os.path.exists(old_path):
                os.remove(old_path)
        person.photo = new
        person.photo_source = "upload"


def send_email(to, subject, body):
    if not app.config.get("MAIL_HOST"):
        raise RuntimeError("El envío de correo no está configurado.")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"Dakit <{app.config['MAIL_FROM']}>"
    message["To"] = to
    message.set_content(body)
    with smtplib.SMTP(app.config["MAIL_HOST"], app.config["MAIL_PORT"], timeout=20) as server:
        server.starttls()
        server.login(app.config["MAIL_USERNAME"], app.config["MAIL_PASSWORD"])
        server.send_message(message)


def issue_code(email, subject):
    """Genera un código, lo envía por correo y devuelve {hash, exp} para la sesión."""
    code = f"{secrets.randbelow(1000000):06d}"
    send_email(email, subject, f"Tu código de acceso a Dakit es:\n\n    {code}\n\nVence en 10 minutos. Si no fuiste tú, ignora este correo.")
    return {"hash": generate_password_hash(code), "exp": (datetime.now() + timedelta(minutes=10)).timestamp()}


def code_matches(data, entered):
    return bool(data) and datetime.now().timestamp() < data.get("exp", 0) and check_password_hash(data.get("hash", ""), (entered or "").strip())


def mask_email(email):
    if not email or "@" not in email:
        return email or ""
    name, domain = email.split("@", 1)
    return f"{name[0]}***@{domain}"


def save_image(file):
    if file and file.filename and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
        return filename
    return None


PERSON_PHOTO_DIM = 500  # px máx del lado mayor para fotos de personas (avatares/fichas)


def downscale_image(path, max_dim):
    """Reduce la imagen a max_dim px en su lado mayor (solo si es mayor). Re-guarda en sitio."""
    try:
        from PIL import Image
        ext = os.path.splitext(path)[1].lower()
        fmt = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP", ".gif": "GIF"}.get(ext, "JPEG")
        with Image.open(path) as img:
            if max(img.size) <= max_dim:
                return
            img.thumbnail((max_dim, max_dim))
            if fmt == "JPEG" and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            tmp = path + ".tmp"
            img.save(tmp, format=fmt)
        os.replace(tmp, path)
    except Exception:
        pass  # si Pillow falla, conserva la imagen original sin romper la descarga


def download_poster(url, basename, max_dim=None):
    from urllib.parse import urlparse
    from urllib.request import Request, urlopen

    url = url.replace(" ", "%20")  # tolera nombres de archivo con espacios (Wikimedia)
    ext = (os.path.splitext(urlparse(url).path)[1] or ".jpg").lower()
    if ext.lstrip(".") not in app.config["ALLOWED_EXTENSIONS"]:
        ext = ".jpg"
    filename = secure_filename(f"{basename}{ext}")
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    http_request = Request(url, headers={"User-Agent": "DakitBot/1.0 (https://dakit.dycloud.co; biblioteca personal)"})
    with urlopen(http_request, timeout=20) as response:
        data = response.read()
    with open(filepath, "wb") as handle:
        handle.write(data)
    if max_dim:
        downscale_image(filepath, max_dim)
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
@login_required
def index():
    q = request.args.get("q", "").strip()
    type_filter = request.args.get("type", "").strip()
    year_filter = request.args.get("year", type=int)

    query = Entry.query.options(joinedload(Entry.people).joinedload(EntryPerson.person))
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
            or needle in normalize_text(e.creator_csv)
            or needle in normalize_text(e.cast_csv)
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
@login_required
def dashboard():
    entries = Entry.query.all()
    total = len(entries)

    type_counter = Counter()
    platform_counter = Counter()
    genre_counter = Counter()
    decade_counter = Counter()
    years = []

    for entry in entries:
        type_counter[entry.type] += 1
        if entry.platform:
            platform_counter[entry.platform] += 1
        for genre in entry.genres:
            genre_counter[genre.name] += 1
        if entry.year:
            years.append(entry.year)
            decade_counter[(entry.year // 10) * 10] += 1

    director_counter = Counter()
    actor_counter = Counter()
    actor_characters = defaultdict(set)
    char_per_person = defaultdict(Counter)
    char_titles = defaultdict(set)
    entry_people = Counter()
    for entry_id, person_id, role, character in db.session.query(
        EntryPerson.entry_id, EntryPerson.person_id, EntryPerson.role, EntryPerson.character
    ):
        entry_people[entry_id] += 1
        if role == "director":
            director_counter[person_id] += 1
        else:
            actor_counter[person_id] += 1
            if character:
                ch = character.strip()
                actor_characters[person_id].add(ch.lower())
                char_per_person[person_id][ch] += 1
                char_titles[ch].add(entry_id)
    versatility_counter = Counter({pid: len(chars) for pid, chars in actor_characters.items()})
    actor_metric = "characters" if request.args.get("actors") == "characters" else "appearances"

    nat_groups = defaultdict(Counter)
    for p in Person.query.filter(Person.nationality.isnot(None)):
        raw = p.nationality.strip()
        nat_groups[nationality_key(raw)][raw] += 1
    nat_ranked = sorted(nat_groups.items(), key=lambda kv: sum(kv[1].values()), reverse=True)[:12]
    nat_top = sum(nat_ranked[0][1].values()) if nat_ranked else 1
    top_nationalities = [
        (max(sp, key=lambda s: (normalize_text(s) == key, any(ord(c) > 127 for c in s), sp[s])),
         sum(sp.values()), round(sum(sp.values()) / nat_top * 100))
        for key, sp in nat_ranked
    ]

    # Icónicos: top apariciones como un mismo personaje (>= 2)
    iconic_raw = sorted(
        ((pid, *counter.most_common(1)[0]) for pid, counter in char_per_person.items()),
        key=lambda item: item[2], reverse=True,
    )
    iconic_raw = [x for x in iconic_raw if x[2] >= 2][:12]
    iconic_top = iconic_raw[0][2] if iconic_raw else 1
    iconic_people = {p.id: p for p in Person.query.filter(Person.id.in_([x[0] for x in iconic_raw]))}
    top_iconic = [(iconic_people[pid], ch, n, round(n / iconic_top * 100)) for pid, ch, n in iconic_raw]

    # Personajes que cruzan más títulos (>= 2)
    recurring_counter = Counter({ch: len(titles) for ch, titles in char_titles.items() if len(titles) >= 2})

    # Obras más corales (más personas registradas)
    coral_raw = entry_people.most_common(8)
    coral_map = {e.id: e for e in Entry.query.filter(Entry.id.in_([eid for eid, _ in coral_raw]))}
    top_coral = [(coral_map[eid], n) for eid, n in coral_raw if eid in coral_map]

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
        top_directors=ranked_people(director_counter, 40),
        top_actors=ranked_people(versatility_counter if actor_metric == "characters" else actor_counter, 40),
        actor_metric=actor_metric,
        top_nationalities=top_nationalities,
        top_iconic=top_iconic,
        top_characters=ranked_with_pct(recurring_counter, 10),
        top_coral=top_coral,
        top_genres=ranked_with_pct(genre_counter, 10),
        top_platforms=ranked_with_pct(platform_counter, 8),
        by_type=ranked_with_pct(type_counter, 10),
        by_decade=sorted(decade_counter.items()),
        top_series=series_with_seasons[:8],
        unique_directors=len(director_counter),
        unique_actors=len(actor_counter),
    )


@app.route("/add", methods=["POST"])
@login_required
def add():
    entry = Entry(
        title=request.form["title"],
        title_alt=request.form.get("title_alt", "").strip() or None,
        year=request.form.get("year", type=int),
        platform=request.form["platform"],
        type=request.form["type"],
        image=save_image(request.files.get("image")),
    )
    entry.genres = selected_genres()
    try:
        db.session.add(entry)
        sync_entry_people(entry, request.form.get("creator", ""), request.form.get("main_cast", ""))
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
        entry.year = request.form.get("year", type=int)
        entry.platform = request.form["platform"]
        entry.type = request.form["type"]
        entry.genres = selected_genres()
        sync_entry_people(entry, request.form.get("creator", ""), request.form.get("main_cast", ""))
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
@login_required
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


# ─────────────────────────────── Personas ──────────────────────────────

@app.route("/people")
@login_required
def people_list():
    people = Person.query.options(joinedload(Person.links)).order_by(Person.name).all()
    return render_template("people.html", people=people)


@app.route("/person/<int:person_id>")
@login_required
def person_detail(person_id):
    person = db.get_or_404(Person, person_id)
    return render_template("person.html", person=person)


@app.route("/person/<int:person_id>/edit", methods=["POST"])
@login_required
def person_edit(person_id):
    person = db.get_or_404(Person, person_id)
    save_person_photo(person)
    person.birth_date = parse_date(request.form.get("birth_date"))
    person.death_date = parse_date(request.form.get("death_date"))
    person.nationality = request.form.get("nationality", "").strip() or None
    db.session.commit()
    flash("Ficha actualizada.", "success")
    return redirect(url_for("person_detail", person_id=person.id))


@app.route("/person/<int:person_id>/delete", methods=["POST"])
@login_required
def person_delete(person_id):
    person = db.get_or_404(Person, person_id)
    if person.photo:
        photo_path = os.path.join(app.config["UPLOAD_FOLDER"], person.photo)
        if os.path.exists(photo_path):
            os.remove(photo_path)
    db.session.delete(person)  # cascade limpia sus enlaces EntryPerson
    db.session.commit()
    flash(f"«{person.name}» eliminada.", "info")
    return redirect(url_for("people_list"))


# ───────────────────────────── Autenticación ───────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        user = User.query.filter_by(username=request.form["username"]).first()
        if user and user.check_password(request.form["password"]):
            if user.two_factor_enabled:
                try:
                    otp = issue_code(user.email, "Tu código de acceso a Dakit")
                except Exception as exc:
                    flash(f"No se pudo enviar el código a tu correo: {exc}", "danger")
                    return render_template("login.html")
                session["login_otp"] = {"user": user.id, **otp}
                session["pending_2fa_next"] = request.args.get("next", "")
                return redirect(url_for("login_2fa"))
            login_user(user)
            flash(f"Bienvenido, {user.username}.", "success")
            next_page = request.args.get("next")
            if next_page and next_page.startswith("/"):
                return redirect(next_page)
            return redirect(url_for("index"))
        flash("Usuario o contraseña incorrectos.", "danger")
    return render_template("login.html")


@app.route("/login/2fa", methods=["GET", "POST"])
def login_2fa():
    data = session.get("login_otp")
    user = db.session.get(User, data["user"]) if data else None
    if user is None or not user.two_factor_enabled:
        session.pop("login_otp", None)
        return redirect(url_for("login"))
    if request.method == "POST":
        if code_matches(data, request.form.get("code", "")):
            login_user(user)
            session.pop("login_otp", None)
            next_page = session.pop("pending_2fa_next", "")
            flash(f"Bienvenido, {user.username}.", "success")
            if next_page and next_page.startswith("/"):
                return redirect(next_page)
            return redirect(url_for("index"))
        flash("Código incorrecto o expirado.", "danger")
    return render_template("two_factor_verify.html", email=mask_email(user.email))


@app.route("/login/2fa/resend", methods=["POST"])
def login_2fa_resend():
    data = session.get("login_otp")
    user = db.session.get(User, data["user"]) if data else None
    if user is None or not user.two_factor_enabled:
        return redirect(url_for("login"))
    try:
        session["login_otp"] = {"user": user.id, **issue_code(user.email, "Tu código de acceso a Dakit")}
        flash("Te reenviamos un nuevo código.", "info")
    except Exception as exc:
        flash(f"No se pudo reenviar el código: {exc}", "danger")
    return redirect(url_for("login_2fa"))


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("index"))


@app.route("/2fa/setup", methods=["GET", "POST"])
@login_required
def two_factor_setup():
    if request.method == "POST":
        if request.form.get("code"):
            data = session.get("setup_otp")
            if data and code_matches(data, request.form.get("code", "")):
                current_user.email = data["email"]
                db.session.commit()
                session.pop("setup_otp", None)
                flash("Listo. Tu correo de acceso quedó actualizado.", "success")
                return redirect(url_for("two_factor_setup"))
            flash("Código incorrecto o expirado.", "danger")
        else:
            email = request.form.get("email", "").strip()
            if "@" not in email:
                flash("Ingresa un correo válido.", "danger")
            elif email == current_user.email:
                flash("Ese ya es tu correo de acceso.", "info")
            else:
                try:
                    session["setup_otp"] = {"email": email, **issue_code(email, "Verifica tu correo en Dakit")}
                    flash(f"Te enviamos un código a {email} para confirmar el cambio.", "info")
                except Exception as exc:
                    flash(f"No se pudo enviar el correo: {exc}", "danger")

    return render_template(
        "two_factor_setup.html",
        email=current_user.email,
        pending=session.get("setup_otp", {}).get("email"),
    )


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
    email = request.form.get("email", "").strip()
    if not username or not password or "@" not in email:
        flash("Usuario, contraseña y correo válido son obligatorios.", "danger")
    elif User.query.filter_by(username=username).first():
        flash(f"El usuario «{username}» ya existe.", "danger")
    else:
        user = User(username=username, email=email)
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


@app.cli.command("apply-enrichment")
@click.argument("json_path")
def apply_enrichment(json_path):
    """Aplica datos investigados (foto/nacimiento/nacionalidad) a las personas desde un JSON.

    Cada item: {name|normalized_name, birth_date, death_date, nationality,
    photo_url, photo_source, tmdb_id, wikidata_qid}. Idempotente; solo escribe lo provisto.
    """
    import json
    with open(json_path, encoding="utf-8") as handle:
        records = json.load(handle)
    updated = photos = 0
    for rec in records:
        norm = normalize_text(rec.get("name") or rec.get("normalized_name") or "")
        person = Person.query.filter_by(normalized_name=norm).first()
        if not person:
            click.echo(f"  (sin match: {rec.get('name')})")
            continue
        if rec.get("birth_date"):
            person.birth_date = parse_date(rec["birth_date"])
        if rec.get("death_date"):
            person.death_date = parse_date(rec["death_date"])
        if rec.get("nationality"):
            person.nationality = rec["nationality"].strip()
        if rec.get("tmdb_id"):
            person.tmdb_id = rec["tmdb_id"]
        if rec.get("wikidata_qid"):
            person.wikidata_qid = rec["wikidata_qid"]
        if rec.get("photo_url"):
            try:
                person.photo = download_poster(rec["photo_url"], f"person_{person.id}", max_dim=PERSON_PHOTO_DIM)
                person.photo_source = rec.get("photo_source") or "tmdb"
                person.photo_ref = rec.get("photo_ref")
                photos += 1
            except Exception as exc:
                click.echo(f"  ⚠️ foto {person.name}: {exc}")
        person.enriched_at = datetime.now()
        updated += 1
    db.session.commit()
    click.echo(f"Enriquecidas: {updated} personas, {photos} fotos descargadas.")


@app.cli.command("add-person")
@click.option("--name", required=True)
@click.option("--birth", default="", help="Nacimiento YYYY-MM-DD.")
@click.option("--death", default="", help="Fallecimiento YYYY-MM-DD (vacío = vive).")
@click.option("--nationality", default="")
@click.option("--photo-url", "photo_url", default="", help="URL de la foto (se descarga).")
def add_person(name, birth, death, nationality, photo_url):
    """Crea o actualiza una persona (find-or-create por nombre normalizado)."""
    person, created = get_or_create_person(name)
    if birth:
        person.birth_date = parse_date(birth)
    if death:
        person.death_date = parse_date(death)
    if nationality:
        person.nationality = nationality.strip()
    if photo_url:
        try:
            person.photo = download_poster(photo_url, f"person_{person.id}", max_dim=PERSON_PHOTO_DIM)
            person.photo_source = "upload"
        except Exception as exc:
            click.echo(f"⚠️ No se pudo descargar la foto: {exc}")
    person.enriched_at = datetime.now()
    db.session.commit()
    click.echo(f"Persona «{person.name}» {'creada' if created else 'actualizada'}.")


@app.cli.command("set-person-photo")
@click.option("--name", required=True)
@click.option("--photo-url", "photo_url", required=True)
def set_person_photo(name, photo_url):
    """Descarga y asigna la foto de una persona existente."""
    person = Person.query.filter_by(normalized_name=normalize_text(name)).first()
    if not person:
        click.echo(f"No existe la persona «{name}».")
        return
    try:
        person.photo = download_poster(photo_url, f"person_{person.id}", max_dim=PERSON_PHOTO_DIM)
        person.photo_source = "upload"
        person.enriched_at = datetime.now()
        db.session.commit()
        click.echo(f"Foto de «{person.name}» actualizada.")
    except Exception as exc:
        click.echo(f"⚠️ No se pudo descargar la foto: {exc}")


@app.cli.command("link-person")
@click.option("--name", required=True)
@click.option("--entry-title", "entry_title", required=True)
@click.option("--role", type=click.Choice(["director", "actor"]), required=True)
@click.option("--character", default="", help="Personaje interpretado (solo actores).")
def link_person(name, entry_title, role, character):
    """Vincula una persona EXISTENTE a una entrada existente (aparición). Idempotente."""
    person = Person.query.filter_by(normalized_name=normalize_text(name)).first()
    if not person:
        click.echo(f"No existe la persona «{name}» (créala con add-person antes de vincular).")
        return
    entry = Entry.query.filter(db.func.lower(Entry.title) == entry_title.lower()).first()
    if not entry:
        click.echo(f"No existe la entrada «{entry_title}».")
        return
    char = character.strip() or None
    link = EntryPerson.query.filter_by(entry_id=entry.id, person_id=person.id, role=role).first()
    if link:
        if char and link.character != char:
            link.character = char
            db.session.commit()
            click.echo(f"Personaje actualizado: {person.name} como «{char}» en «{entry.title}».")
        else:
            click.echo(f"Ya estaba vinculado: {person.name} · «{entry.title}» · {role}.")
        return
    db.session.add(EntryPerson(entry_id=entry.id, person_id=person.id, role=role, character=char))
    db.session.commit()
    click.echo(f"Vinculado: {person.name} → «{entry.title}» ({role}){f' como «{char}»' if char else ''}.")


@app.cli.command("apply-links")
@click.argument("json_path")
@click.option("--no-create", is_flag=True, help="Solo actualiza el personaje de vínculos existentes; no crea vínculos nuevos.")
def apply_links(json_path, no_create):
    """Crea/actualiza vínculos persona↔entrada en lote desde [{name, entry_title, role, character?}].

    Solo personas y entradas existentes; idempotente. Con --no-create solo refresca personajes.
    """
    import json
    with open(json_path, encoding="utf-8") as handle:
        items = json.load(handle)
    people = {p.normalized_name: p for p in Person.query.all()}
    entries = {normalize_text(e.title): e for e in Entry.query.all()}
    created = updated = skipped = missing = 0
    for it in items:
        role = it.get("role")
        if role not in ("director", "actor"):
            continue
        person = people.get(normalize_text(it.get("name", "")))
        entry = entries.get(normalize_text(it.get("entry_title", "")))
        if not person or not entry:
            missing += 1
            continue
        char = (it.get("character") or "").strip() or None
        link = EntryPerson.query.filter_by(entry_id=entry.id, person_id=person.id, role=role).first()
        if link:
            if char and link.character != char:
                link.character = char
                updated += 1
            else:
                skipped += 1
            continue
        if no_create:
            missing += 1
            continue
        db.session.add(EntryPerson(entry_id=entry.id, person_id=person.id, role=role, character=char))
        created += 1
    db.session.commit()
    click.echo(f"Vínculos nuevos: {created}. Personajes actualizados: {updated}. Sin cambio: {skipped}. Sin match: {missing}.")


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
@click.option("--cast", "main_cast", default="", help="Reparto principal: actores separados por coma.")
@click.option("--year", required=True, type=int)
@click.option("--platform", required=True)
@click.option("--type", "type_", required=True)
@click.option("--genres", default="", help="Nombres de géneros separados por coma.")
@click.option("--image-url", default="", help="URL de un póster para descargar.")
def add_entry(title, title_alt, creator, main_cast, year, platform, type_, genres, image_url):
    """Registra una entrada en la biblioteca desde la línea de comandos."""
    entry = Entry(
        title=title, title_alt=title_alt.strip() or None,
        year=year, platform=platform, type=type_,
    )
    db.session.add(entry)
    sync_entry_people(entry, creator, main_cast)

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
@click.option("--email", default="", help="Correo donde llegará el código de acceso (2FA).")
def create_user(username, password, email):
    """Crea un usuario: flask create-user <usuario> <clave> [--email correo]."""
    if User.query.filter_by(username=username).first():
        click.echo(f"El usuario '{username}' ya existe.")
        return
    user = User(username=username, email=email.strip() or None)
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
