# run.py — entry point (local dev only; Render runs gunicorn instead, see Procfile)
import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(debug=debug, port=int(os.environ.get("PORT", 5000)))
