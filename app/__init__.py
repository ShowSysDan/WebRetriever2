import os
import logging
from flask import Flask, send_from_directory
from flask_migrate import Migrate
from app.config import Config
from app.models import db, GlobalSettings, OutputInstance
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

    # Ensure upload and preview directories exist
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["PREVIEW_FOLDER"], exist_ok=True)

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

    # Auto-start instances that were running before shutdown
    with app.app_context():
        from app.routes import _start_worker
        previously_running = OutputInstance.query.filter_by(running=True).all()
        if previously_running:
            settings = GlobalSettings.query.first()
            logger = logging.getLogger(__name__)
            logger.info(f"Auto-starting {len(previously_running)} previously running instance(s)")
            for inst in previously_running:
                try:
                    _start_worker(inst, settings)
                    logger.info(f"Auto-started: {inst.name}")
                except Exception as e:
                    logger.error(f"Failed to auto-start {inst.name}: {e}")
                    inst.running = False
            db.session.commit()

    return app
