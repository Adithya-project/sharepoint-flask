# app/__init__.py — application factory
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_mail import Mail

# Load .env from the project root (one level above the app/ package).
# python-dotenv is already in requirements.txt.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

db = SQLAlchemy()
login_manager = LoginManager()
mail = Mail()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access that page."
login_manager.login_message_category = "warning"

BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")


def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    # Database — defaults to local SQLite for dev; set DATABASE_URL in
    # production (Render provides this automatically for its Postgres addon).
    # Render's DATABASE_URL starts with "postgres://" but SQLAlchemy 2.x /
    # psycopg require "postgresql://" — normalize it here.
    db_url = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "data.sqlite")
    )
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Upload dir — defaults to a local folder for dev; set UPLOAD_DIR in
    # production to point at a mounted persistent disk (e.g. "/data/uploads"
    # on Render), or uploaded files won't survive a redeploy/restart.
    app.config["UPLOAD_DIR"] = os.environ.get("UPLOAD_DIR", UPLOAD_DIR)
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB upload cap

    # Gemini — used by app/ai.py for command parsing, embeddings, summarization.
    app.config["GEMINI_API_KEY"] = os.environ.get("GEMINI_API_KEY", "")
    app.config["GEMINI_MODEL"] = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
    app.config["GEMINI_EMBED_MODEL"] = os.environ.get(
        "GEMINI_EMBED_MODEL", "text-embedding-004"
    )

    # Mail — Gmail SMTP with an app password. See app/mail.py for setup steps.
    app.config["MAIL_SERVER"] = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    app.config["MAIL_PORT"] = int(os.environ.get("MAIL_PORT", 465))
    app.config["MAIL_USE_TLS"] = os.environ.get("MAIL_USE_TLS", "false").lower() == "true"
    app.config["MAIL_USE_SSL"] = os.environ.get("MAIL_USE_SSL", "true").lower() == "true"
    app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME", "")
    app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD", "")
    app.config["MAIL_DEFAULT_SENDER"] = os.environ.get(
        "MAIL_DEFAULT_SENDER", os.environ.get("MAIL_USERNAME", "")
    )

    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    mail.init_app(app)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from app.auth import auth_bp
    from app.sites import sites_bp
    from app.files import files_bp
    from app.ai_routes import ai_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(sites_bp)
    app.register_blueprint(files_bp)
    app.register_blueprint(ai_bp)

    with app.app_context():
        db.create_all()

    return app
