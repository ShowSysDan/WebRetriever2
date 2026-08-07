import os
import logging
from flask import Flask, send_from_directory
from flask_migrate import Migrate
from sqlalchemy import inspect as sa_inspect, text
from app.config import Config
from app.models import db, GlobalSettings, OutputInstance, MediaFile, generate_media_uid
from app.routes import api
from app.logging_config import setup_logging


def _add_missing_columns():
    """Lightweight auto-migration: add columns that exist in the models but
    not yet in the database (create_all only creates missing tables, it never
    alters existing ones). New columns are declared nullable so a plain
    ADD COLUMN works on SQLite and PostgreSQL alike; code treats NULL as the
    field's default."""
    logger = logging.getLogger(__name__)
    inspector = sa_inspect(db.engine)
    for table in db.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue  # create_all handles brand-new tables
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(db.engine.dialect)
            db.session.execute(text(
                f'ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}'
            ))
            logger.info(f"DB migrated: added {table.name}.{column.name} ({col_type})")
    db.session.commit()


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
        _add_missing_columns()
        # Backfill permanent uids for media uploaded before the uid column
        # existed (new uploads get one at upload time)
        backfilled = 0
        for media in MediaFile.query.filter(MediaFile.uid.is_(None)).all():
            media.uid = generate_media_uid()
            backfilled += 1
        if backfilled:
            db.session.commit()
            logging.getLogger(__name__).info(
                f"DB migrated: assigned uids to {backfilled} existing media file(s)"
            )
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
