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


def _remove_overview_leftovers(app):
    """Clean up after the Overview stream (added in 1.10.0, removed in
    1.10.2) on boxes that ran it: its runtime files and its settings
    column. Idempotent and best-effort — each step is skipped quietly if
    there is nothing to do or it can't be done."""
    logger = logging.getLogger(__name__)
    removed = 0
    preview_dir = app.config.get("PREVIEW_FOLDER")
    if preview_dir and os.path.isdir(preview_dir):
        # Per-worker NDI endpoint files (<id>.ndi.json) the Overview read,
        # and its own thumbnail (0.jpg — it ran under reserved id 0, which
        # no database row can have)
        for name in os.listdir(preview_dir):
            if name.endswith(".ndi.json") or name == "0.jpg":
                try:
                    os.remove(os.path.join(preview_dir, name))
                    removed += 1
                except OSError:
                    pass
    runtime_dir = app.config.get("SIGNAGE_RUNTIME_FOLDER")
    for name in ("multiview_layout.json", "overview_status.json"):
        try:
            os.remove(os.path.join(runtime_dir, name))
            removed += 1
        except (OSError, TypeError):
            pass
    if removed:
        logger.info(f"Removed {removed} leftover Overview stream file(s)")

    # The global_settings.overview_enabled column: unused now. DROP COLUMN
    # needs SQLite 3.35+ (any PostgreSQL); older SQLite keeps the column,
    # which is harmless since nothing reads it.
    try:
        cols = {c["name"] for c in sa_inspect(db.engine).get_columns("global_settings")}
        if "overview_enabled" in cols:
            db.session.execute(text("ALTER TABLE global_settings DROP COLUMN overview_enabled"))
            db.session.commit()
            logger.info("DB migrated: dropped unused global_settings.overview_enabled")
    except Exception as e:
        db.session.rollback()
        logger.debug(f"Could not drop global_settings.overview_enabled (harmless): {e}")


def _install_security_hooks(app):
    """Browser-facing hardening for an auth-less LAN tool.

    Show controllers (Companion, Crestron, QLab, curl) send plain requests
    with no Origin / Sec-Fetch-* headers, so they are unaffected. What this
    stops is a web page on some OTHER site driving the API through a LAN
    browser (CSRF: auto-submitted forms, fetch with a simple content type)
    and, when ALLOWED_HOSTS is set, DNS rebinding. The documented GET cue
    URLs stay open on purpose — they are the show-control interface."""
    from urllib.parse import urlsplit
    from flask import request, abort

    unsafe = {"POST", "PUT", "PATCH", "DELETE"}

    @app.before_request
    def _guard_requests():
        allowed = app.config.get("ALLOWED_HOSTS") or []
        if allowed:
            host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in allowed and host not in ("localhost", "127.0.0.1", "::1"):
                abort(403, description="Host not allowed (see ALLOWED_HOSTS)")
        if request.method in unsafe:
            # Modern browsers label every request with where it came from —
            # authoritative when present (and immune to a reverse proxy
            # rewriting Host or dropping the port)
            site = request.headers.get("Sec-Fetch-Site")
            if site is not None:
                if site in ("cross-site", "same-site"):
                    abort(403, description="Cross-site request blocked")
                return None
            # Older browsers: fall back to the Origin header, comparing
            # hostnames only (a proxy's Host may omit a non-default port)
            origin = request.headers.get("Origin")
            if origin is not None:
                req_host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
                if origin == "null" or (urlsplit(origin).hostname or "") != req_host:
                    abort(403, description="Cross-origin request blocked")

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        # Clickjacking guard for the management UI (its buttons stop outputs).
        # The preview popup and the API stay frameable/embeddable — people
        # put /preview/<id> and the MJPEG stream into other dashboards.
        if not request.path.startswith(("/api/", "/preview/")):
            resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        # User-uploaded files are served inline (the UI and players embed
        # them); if one is opened as a document — an SVG with a <script>, or
        # a file with a spoofed type — the sandbox stops it running on this
        # origin. Has no effect on <img>/<video> embedding.
        if request.path.startswith("/api/media/"):
            resp.headers["Content-Security-Policy"] = "sandbox"
        return resp


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

    # SQLite multi-user hardening: with several people using the UI at once
    # (each browser polls + edits), the default rollback journal makes
    # concurrent writes fail fast with "database is locked". WAL lets
    # readers and a writer coexist, and busy_timeout makes a second writer
    # wait its turn instead of erroring. No-op for PostgreSQL.
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        from sqlalchemy import event
        with app.app_context():
            engine = db.engine

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    # Ensure upload, preview, thumbnail, and signage state directories exist
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["PREVIEW_FOLDER"], exist_ok=True)
    os.makedirs(app.config["THUMB_FOLDER"], exist_ok=True)
    os.makedirs(app.config["SIGNAGE_STATE_FOLDER"], exist_ok=True)
    os.makedirs(app.config["SIGNAGE_RUNTIME_FOLDER"], exist_ok=True)

    # Register API
    _install_security_hooks(app)
    app.register_blueprint(api)

    # Live preview popup window (one page for all instances; it reads the
    # instance id from its own URL). Registered before the SPA catch-all.
    @app.route("/preview/<int:instance_id>")
    def preview_popup(instance_id):
        return send_from_directory(app.static_folder, "preview.html")

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
        _remove_overview_leftovers(app)
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

    # Background video optimizer — re-queues transcodes interrupted by a
    # restart and serves new uploads from here on
    from app.transcode import transcoder
    transcoder.init_app(app)

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
