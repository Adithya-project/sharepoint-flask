# TeamSpace (Python/Flask edition)

A SharePoint-style collaboration platform: team **sites**, nested **folders**, file
**upload/download**, per-site **roles** (owner / editor / viewer), file **comments**, and
**search** across everything you have access to.

This is the Python rebuild of the original Node.js version — same feature set, same permission
model, now on **Flask**.

## Tech stack
- **Backend:** Flask (application factory + blueprints)
- **ORM / DB:** Flask-SQLAlchemy + SQLite (zero setup, single file)
- **Auth:** Flask-Login + Werkzeug's `generate_password_hash`/`check_password_hash`
- **Templates:** Jinja2 + Bootstrap 5 (server-rendered, no frontend build step)
- **File uploads:** Flask's built-in `request.files` + `werkzeug.utils.secure_filename`

## Project structure
```
sharepoint-flask/
├── run.py                  # entry point (flask run / python run.py)
├── requirements.txt
├── app/
│   ├── __init__.py          # app factory: config, db, login_manager, blueprint registration
│   ├── models.py             # User, Site, SiteMember, Folder, File, Comment
│   ├── decorators.py          # require_site_role() — permission enforcement
│   ├── auth.py                 # register / login / logout blueprint
│   ├── sites.py                 # dashboard, sites, folders, members, search blueprint
│   └── files.py                  # upload / download / delete / comments blueprint
├── templates/                     # Jinja2 templates (Bootstrap UI)
├── static/css/style.css
└── uploads/                        # uploaded files land here (gitignored)
```

## Getting started

1. **Create a virtual environment (recommended):**
   ```bash
   python3 -m venv venv
   source venv/bin/activate   # Windows: venv\Scripts\activate
   ```

2. **Install dependencies** (requires internet access on your machine):
   ```bash
   pip install -r requirements.txt
   ```

3. **Run it:**
   ```bash
   python run.py
   ```
   or with the Flask CLI (auto-reload):
   ```bash
   export FLASK_APP=run.py       # Windows: set FLASK_APP=run.py
   export FLASK_DEBUG=1
   flask run
   ```

4. Open **http://localhost:5000** in your browser.

5. Register an account — the **first user to register** gets `is_admin=True` in the database
   (the flag exists in the model but isn't wired to any admin UI yet — good extension task).

`data.sqlite` is created automatically on first run via `db.create_all()` inside the app factory —
no separate database server needed.

## How the permission model works
- Every site has an **owner** (the creator).
- Owners can invite other registered users to a site by email, assigning `viewer`, `editor`, or `owner`.
- `viewer` → browse folders, download files, read/write comments.
- `editor` → viewer rights + create folders, upload/delete files.
- `owner` → editor rights + manage members.

This is enforced server-side with the `@require_site_role(min_role)` decorator in
`app/decorators.py`, applied per-route — so it can't be bypassed from the client. It loads the
`Site`, computes the current user's role via `Site.role_for()`, and aborts with `403` if the role
isn't high enough.

## Differences from the Node.js version
Functionally identical, but idiomatically Flask:
- **Routing:** Flask blueprints (`auth`, `sites`, `files`) instead of Express routers.
- **DB access:** SQLAlchemy ORM models instead of raw `better-sqlite3` SQL.
- **Permissions:** a Python decorator (`@require_site_role`) instead of Express middleware.
- **Sessions:** Flask-Login's cookie-based sessions instead of `express-session`.
- **Templates:** Jinja2 (`{% %}` / `{{ }}`) instead of EJS (`<% %>` / `<%= %>`) — logic is a
  near 1:1 translation.

## Ideas to extend
- **File versioning:** the `File` model already has a `version` column — wire up "replace file"
  to increment it and keep history instead of overwriting.
- **Real full-text search:** SQLite FTS5, or index file *contents* (not just filenames) for
  PDFs/docs using something like `pypdf` or `python-docx`.
- **Flask-WTF** for CSRF-protected forms and cleaner validation instead of raw `request.form`.
- **Blueprints for an admin panel** gated on `User.is_admin`.
- **Dockerize it** with a `Dockerfile` + `docker-compose.yml`.
- **Swap SQLite for Postgres** (just change `SQLALCHEMY_DATABASE_URI`) if you want a "built for
  scale" resume bullet.

## Notes
- Change `SECRET_KEY` (via the `SECRET_KEY` environment variable) before deploying anywhere real —
  it currently defaults to a dev placeholder in `app/__init__.py`.
- Max upload size is capped at 50MB (`MAX_CONTENT_LENGTH` in `app/__init__.py`).
