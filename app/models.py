# app/models.py — SQLAlchemy models
from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from app import db

# Role hierarchy used for permission checks
ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}


class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    owned_sites = db.relationship("Site", backref="owner", lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Site(db.Model):
    __tablename__ = "sites"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    members = db.relationship("SiteMember", backref="site", cascade="all, delete-orphan")
    folders = db.relationship("Folder", backref="site", cascade="all, delete-orphan")
    files = db.relationship("File", backref="site", cascade="all, delete-orphan")

    def role_for(self, user):
        """Return this user's effective role on the site, or None."""
        if not user or not user.is_authenticated:
            return None
        if self.owner_id == user.id:
            return "owner"
        membership = SiteMember.query.filter_by(site_id=self.id, user_id=user.id).first()
        return membership.role if membership else None

    def has_role_at_least(self, user, min_role):
        role = self.role_for(user)
        return bool(role) and ROLE_RANK[role] >= ROLE_RANK[min_role]


class SiteMember(db.Model):
    __tablename__ = "site_members"

    id = db.Column(db.Integer, primary_key=True)
    site_id = db.Column(db.Integer, db.ForeignKey("sites.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="viewer")  # viewer|editor|owner

    user = db.relationship("User")

    __table_args__ = (db.UniqueConstraint("site_id", "user_id", name="uq_site_user"),)


class Folder(db.Model):
    __tablename__ = "folders"

    id = db.Column(db.Integer, primary_key=True)
    site_id = db.Column(db.Integer, db.ForeignKey("sites.id"), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("folders.id"), nullable=True)
    name = db.Column(db.String(200), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    children = db.relationship("Folder", backref=db.backref("parent", remote_side=[id]))


class File(db.Model):
    __tablename__ = "files"

    id = db.Column(db.Integer, primary_key=True)
    site_id = db.Column(db.Integer, db.ForeignKey("sites.id"), nullable=False)
    folder_id = db.Column(db.Integer, db.ForeignKey("folders.id"), nullable=True)
    stored_name = db.Column(db.String(255), nullable=False)   # random name on disk
    original_name = db.Column(db.String(255), nullable=False)  # name shown to users
    size = db.Column(db.Integer, nullable=False)
    mime_type = db.Column(db.String(120))
    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    version = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Feature 3: AI-suggested folder (not auto-applied — user must accept).
    ai_suggested_folder_id = db.Column(db.Integer, db.ForeignKey("folders.id"), nullable=True)
    ai_suggestion_reason = db.Column(db.String(255), nullable=True)
    ai_suggestion_dismissed = db.Column(db.Boolean, default=False)

    # Feature 4: cached AI summary so we don't regenerate on every view.
    summary = db.Column(db.Text, nullable=True)
    summary_kind = db.Column(db.String(20), nullable=True)  # "short" or "detailed"

    uploader = db.relationship("User")
    folder = db.relationship("Folder", backref="files", foreign_keys=[folder_id])
    suggested_folder = db.relationship("Folder", foreign_keys=[ai_suggested_folder_id])
    comments = db.relationship("Comment", backref="file", cascade="all, delete-orphan")


class Comment(db.Model):
    __tablename__ = "comments"

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("files.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    author = db.relationship("User")


# --- AI extensions (Features 2, 3, 4) ---

import json as _json


class FileEmbedding(db.Model):
    """One row per file. The vector is stored as a JSON blob of floats because
    SQLite has no native vector type and we're not pulling in numpy/sqlite-vec
    for what is a small personal-deployment app. For a real production setup
    you'd use sqlite-vec or pgvector."""
    __tablename__ = "file_embeddings"

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("files.id"), nullable=False, unique=True)
    vector_json = db.Column(db.Text, nullable=False)
    source_text = db.Column(db.Text, nullable=False)  # what was embedded
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    file = db.relationship("File", backref=db.backref("embedding", uselist=False))

    @property
    def vector(self) -> list[float]:
        return _json.loads(self.vector_json)

    @vector.setter
    def vector(self, v: list[float]) -> None:
        self.vector_json = _json.dumps(v)
