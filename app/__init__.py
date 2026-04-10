import os
import logging
from flask import Flask, send_from_directory
from flask_migrate import Migrate
from app.config import Config
from app.models import db, GlobalSettings
from app.routes import api
from app.logging_config import setup_logging


def create_app(config_class=Config):
    app = Flask(
        __name__,
        static_folder="static",
        static_url_path="/static",
    )
    app.config.from_object(config_class)

    # Logging (including syslog)
    setup_logging(app)

    # Extensions
    db.init_app(app)
    Migrate(app, db)

    # Ensure upload directory exists
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    # Register API
    app.register_blueprint(api)

    # Serve frontend SPA
    @app.route("/")
    @app.route("/<path:path>")
    def serve_frontend(path=""):
        if path and path.startswith("api/"):
            return {"error": "Not found"}, 404
        return send_from_directory(app.static_folder, "index.html")

    # Initialize DB + default settings
    with app.app_context():
        db.create_all()
        if not GlobalSettings.query.first():
            settings = GlobalSettings(
                ndi_hostname=app.config.get("NDI_HOSTNAME", "NDI-STREAMER"),
                output_fps=app.config.get("NDI_OUTPUT_FPS", 60),
            )
            db.session.add(settings)
            db.session.commit()

    return app
