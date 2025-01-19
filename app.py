from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
import os
from werkzeug.utils import secure_filename
from flask_migrate import Migrate

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///dakit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Configuración para la subida de archivos
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif'}
db = SQLAlchemy(app)
migrate = Migrate(app, db)

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

# Tabla de asociación para la relación muchos a muchos
entry_genres = db.Table('entry_genres',
    db.Column('entry_id', db.Integer, db.ForeignKey('entry.id'), primary_key=True),
    db.Column('genre_id', db.Integer, db.ForeignKey('genre.id'), primary_key=True)
)

# Modelo Genre
class Genre(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)

    def __repr__(self):
        return f'<Genre {self.name}>'

# Modelo Entry
class Entry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    creator = db.Column(db.String(100), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    platform = db.Column(db.String(50), nullable=False)
    type = db.Column(db.String(20), nullable=False)
    image = db.Column(db.String(100), nullable=True) 

    # Relación muchos a muchos con Genre
    genres = db.relationship('Genre', secondary=entry_genres, lazy='subquery',
                             backref=db.backref('entries', lazy=True))

    def __repr__(self):
        return f'<{self.id} - {self.title}>'
    
with app.app_context():
    db.create_all()

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        title = request.form['title']
        creator = request.form['creator']
        year = request.form['year']
        platform = request.form['platform']
        type = request.form['type']
        genre_ids = request.form.getlist('genres')

        image_file = request.files['image']
        if image_file and allowed_file(image_file.filename):
            filename = secure_filename(image_file.filename)
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)
        else:
            filename = None

        new_entry = Entry(
            title=title,
            creator=creator,
            year=year,
            platform=platform,
            type=type,
            image=filename
        )

        db.session.add(new_entry)

        for genre_id in genre_ids:
            genre = Genre.query.get(genre_id)
            if genre:
                new_entry.genres.append(genre)

        try:
            db.session.commit()
            return redirect(url_for('index'))
        except:
            return 'There was an issue adding your movie'
    else:
        entries = Entry.query.order_by(Entry.title).all()
        genres = Genre.query.order_by(Genre.name).all()
        return render_template('index.html', entries=entries, genres=genres)


@app.route('/delete/<int:id>')
def delete(id):
    entry_to_delete = Entry.query.get_or_404(id)

    try:
        if entry_to_delete.image:
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], entry_to_delete.image)
            if os.path.exists(image_path):
                os.remove(image_path)

        db.session.delete(entry_to_delete)
        db.session.commit()
        return redirect(url_for('index'))   
    except:
        return 'There was a problem deleting that movie'


@app.route('/update/<int:id>', methods=['GET', 'POST'])
def update(id):
    entry = Entry.query.get_or_404(id)
    genres = Genre.query.order_by(Genre.name).all()

    if request.method == 'POST':
        entry.title = request.form['title']
        entry.creator = request.form['creator']
        entry.year = request.form['year']
        entry.platform = request.form['platform']
        entry.type = request.form['type']
        genre_ids = request.form.getlist('genres')

        # Manejar la imagen
        image_file = request.files['image']
        if image_file and allowed_file(image_file.filename):
            filename = secure_filename(image_file.filename)
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            image_file.save(image_path)
            entry.image = filename 

        entry.genres = []
        for genre_id in genre_ids:
            genre = Genre.query.get(genre_id)
            if genre:
                entry.genres.append(genre)

        try:
            db.session.commit()
            return redirect(url_for('index'))
        except:
            return 'There was an issue updating your movie'

    else:
        return render_template('update_item.html', entry=entry, genres=genres)


@app.route('/genres', methods=['GET', 'POST'])
def genres():
    if request.method == 'POST':
        name = request.form['name']
        new_genre = Genre(name=name)

        try:
            db.session.add(new_genre)
            db.session.commit()
            return redirect(url_for('genres'))
        except Exception as e:
            return f'Error al agregar el género: {e}'
    else:
        genres = Genre.query.order_by(Genre.name).all()
        return render_template('genres.html', genres=genres)
    

@app.route('/genres/delete/<int:id>')
def delete_genre(id):
    genre_to_delete = Genre.query.get_or_404(id)

    try:
        db.session.delete(genre_to_delete)
        db.session.commit()
        return redirect(url_for('genres'))
    except Exception as e:
        return f'Error al eliminar el género: {e}'



if __name__ == '__main__':
    app.run(debug=True)