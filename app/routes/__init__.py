"""
REST API routes for NDI output instances, global settings, and media library.
"""

import os
import uuid
import logging
from flask import Blueprint, request, jsonify, current_app, send_from_directory
from werkzeug.utils import secure_filename
from PIL import Image as PILImage

from app.models import db, OutputInstance, GlobalSettings, MediaFile
from app.workers import manager
from app.logging_config import log_event

api = Blueprint("api", __name__, url_prefix="/api")
logger = logging.getLogger(__name__)


def _allowed_file(filename):
    return "." in filename and \
        filename.rsplit(".", 1)[1].lower() in current_app.config.get("ALLOWED_EXTENSIONS", set())


def _build_text_settings(inst):
    return {
        "content": inst.text_content, "font": inst.text_font,
        "size": inst.text_size, "color": inst.text_color,
        "bg_color": inst.text_bg_color, "align": inst.text_align,
    }


def _start_worker(inst, settings=None):
    if not settings:
        settings = GlobalSettings.query.first()
    text_settings = _build_text_settings(inst) if inst.source_type == "text" else None

    # Resolve source value for media-backed images
    source_value = inst.source_value
    if inst.source_type == "image" and inst.media_file:
        source_value = f"http://127.0.0.1:{current_app.config.get('FLASK_PORT', 5000)}/api/media/{inst.media_file_id}/file"

    return manager.start_instance(
        instance_id=inst.id,
        ndi_name=inst.ndi_source_name,
        source_type=inst.source_type,
        source_value=source_value,
        width=inst.width, height=inst.height,
        capture_fps=inst.capture_fps,
        output_fps=settings.output_fps if settings else 60,
        refresh_interval=inst.refresh_interval,
        browser_recycle_hours=current_app.config.get("BROWSER_RECYCLE_HOURS", 4),
        text_settings=text_settings,
    )


# =========================================================================
# Global Settings
# =========================================================================

@api.route("/settings", methods=["GET"])
def get_settings():
    settings = GlobalSettings.query.first()
    if not settings:
        settings = GlobalSettings(ndi_hostname="NDI-STREAMER", output_fps=60)
        db.session.add(settings)
        db.session.commit()
    return jsonify(settings.to_dict())


@api.route("/settings", methods=["PUT"])
def update_settings():
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    settings = GlobalSettings.query.first()
    if not settings:
        settings = GlobalSettings()
        db.session.add(settings)

    changed = []
    if "ndi_hostname" in data and data["ndi_hostname"] != settings.ndi_hostname:
        settings.ndi_hostname = data["ndi_hostname"]
        changed.append(f"hostname={data['ndi_hostname']}")
    if "output_fps" in data:
        try:
            fps = int(data["output_fps"])
        except (ValueError, TypeError):
            return jsonify({"error": "output_fps must be a number"}), 400
        if fps != settings.output_fps:
            settings.output_fps = fps
            changed.append(f"output_fps={fps}")

    db.session.commit()
    if changed:
        log_event("SETTINGS_CHANGED", " ".join(changed))
    return jsonify(settings.to_dict())


# =========================================================================
# Global Start / Stop
# =========================================================================

@api.route("/start-all", methods=["POST"])
def start_all():
    settings = GlobalSettings.query.first()
    instances = OutputInstance.query.filter_by(enabled=True).all()
    started = []

    for inst in instances:
        if not manager.is_running(inst.id):
            _start_worker(inst, settings)
            inst.running = True
            started.append(inst.id)

    if settings:
        settings.all_running = True
    db.session.commit()
    log_event("ALL_STARTED", f"count={len(started)}")
    return jsonify({"started": started, "count": len(started)})


@api.route("/stop-all", methods=["POST"])
def stop_all():
    manager.stop_all()
    OutputInstance.query.update({OutputInstance.running: False})
    settings = GlobalSettings.query.first()
    if settings:
        settings.all_running = False
    db.session.commit()
    return jsonify({"message": "All instances stopped"})


# =========================================================================
# Output Instances CRUD
# =========================================================================

@api.route("/instances", methods=["GET"])
def list_instances():
    instances = OutputInstance.query.order_by(OutputInstance.created_at).all()
    for inst in instances:
        actual = manager.is_running(inst.id)
        if inst.running != actual:
            inst.running = actual
    db.session.commit()
    return jsonify([i.to_dict() for i in instances])


@api.route("/instances", methods=["POST"])
def create_instance():
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400
    if not data.get("name"):
        return jsonify({"error": "Name is required"}), 400

    if OutputInstance.query.filter_by(name=data["name"]).first():
        return jsonify({"error": "Name already exists"}), 409

    inst = OutputInstance(
        name=data["name"],
        source_type=data.get("source_type", "webpage"),
        source_value=data.get("source_value", ""),
        media_file_id=data.get("media_file_id"),
        text_content=data.get("text_content", ""),
        text_font=data.get("text_font", "Arial"),
        text_size=data.get("text_size", 48),
        text_color=data.get("text_color", "#FFFFFF"),
        text_bg_color=data.get("text_bg_color", "#000000"),
        text_align=data.get("text_align", "center"),
        width=data.get("width", 1920),
        height=data.get("height", 1080),
        capture_fps=data.get("capture_fps", 30),
        refresh_interval=data.get("refresh_interval", 0),
        enabled=data.get("enabled", True),
    )
    db.session.add(inst)
    db.session.commit()
    log_event("INSTANCE_CREATED", f"id={inst.id} name='{inst.name}'")
    return jsonify(inst.to_dict()), 201


@api.route("/instances/<int:instance_id>", methods=["GET"])
def get_instance(instance_id):
    inst = OutputInstance.query.get_or_404(instance_id)
    inst.running = manager.is_running(inst.id)
    db.session.commit()
    return jsonify(inst.to_dict())


@api.route("/instances/<int:instance_id>", methods=["PUT"])
def update_instance(instance_id):
    inst = OutputInstance.query.get_or_404(instance_id)
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400
    was_running = manager.is_running(inst.id)
    needs_restart = False

    for field in [
        "name", "source_type", "source_value", "media_file_id",
        "text_content", "text_font", "text_size", "text_color",
        "text_bg_color", "text_align",
        "width", "height", "capture_fps", "refresh_interval", "enabled",
    ]:
        if field in data:
            old = getattr(inst, field)
            new = data[field]
            if old != new:
                setattr(inst, field, new)
                if field != "enabled":
                    needs_restart = True

    db.session.commit()
    log_event("INSTANCE_UPDATED", f"id={inst.id} name='{inst.name}'")

    if was_running and needs_restart:
        manager.stop_instance(inst.id)
        if inst.enabled:
            _start_worker(inst)
            inst.running = True
        else:
            inst.running = False
        db.session.commit()

    return jsonify(inst.to_dict())


@api.route("/instances/<int:instance_id>", methods=["DELETE"])
def delete_instance(instance_id):
    inst = OutputInstance.query.get_or_404(instance_id)
    name = inst.name
    if manager.is_running(inst.id):
        manager.stop_instance(inst.id)
    db.session.delete(inst)
    db.session.commit()
    log_event("INSTANCE_DELETED", f"id={instance_id} name='{name}'")
    return jsonify({"message": f"Instance '{name}' deleted"})


# =========================================================================
# Per-instance Start / Stop / Refresh
# =========================================================================

@api.route("/instances/<int:instance_id>/start", methods=["POST"])
def start_instance(instance_id):
    inst = OutputInstance.query.get_or_404(instance_id)
    if manager.is_running(inst.id):
        return jsonify({"message": "Already running"}), 200

    _start_worker(inst)
    inst.running = True
    db.session.commit()
    return jsonify(inst.to_dict())


@api.route("/instances/<int:instance_id>/stop", methods=["POST"])
def stop_instance(instance_id):
    inst = OutputInstance.query.get_or_404(instance_id)
    manager.stop_instance(inst.id)
    inst.running = False
    db.session.commit()
    return jsonify(inst.to_dict())


@api.route("/instances/<int:instance_id>/refresh", methods=["POST"])
def refresh_instance(instance_id):
    inst = OutputInstance.query.get_or_404(instance_id)
    if manager.is_running(inst.id):
        manager.stop_instance(inst.id)
    _start_worker(inst)
    inst.running = True
    db.session.commit()
    log_event("INSTANCE_REFRESHED", f"id={inst.id} name='{inst.name}'")
    return jsonify(inst.to_dict())


# =========================================================================
# Media Library
# =========================================================================

@api.route("/media", methods=["GET"])
def list_media():
    files = MediaFile.query.order_by(MediaFile.uploaded_at.desc()).all()
    return jsonify([f.to_dict() for f in files])


@api.route("/media", methods=["POST"])
def upload_media():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    if not _allowed_file(file.filename):
        return jsonify({"error": "File type not allowed"}), 400

    original_name = secure_filename(file.filename)
    if not original_name or "." not in original_name:
        return jsonify({"error": "Invalid filename"}), 400
    ext = original_name.rsplit(".", 1)[1].lower()
    unique_name = f"{uuid.uuid4().hex}.{ext}"

    upload_dir = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, unique_name)
    file.save(filepath)

    file_size = os.path.getsize(filepath)
    width_px = None
    height_px = None
    mime_type = file.content_type

    try:
        with PILImage.open(filepath) as img:
            width_px, height_px = img.size
    except Exception:
        pass

    media = MediaFile(
        filename=unique_name,
        original_name=original_name,
        mime_type=mime_type,
        file_size=file_size,
        width_px=width_px,
        height_px=height_px,
    )
    db.session.add(media)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        # Remove orphaned file from disk
        try:
            os.remove(filepath)
        except OSError:
            pass
        return jsonify({"error": "Failed to save media record"}), 500
    log_event("MEDIA_UPLOADED", f"id={media.id} name='{original_name}' size={file_size}")
    return jsonify(media.to_dict()), 201


@api.route("/media/<int:media_id>", methods=["GET"])
def get_media(media_id):
    media = MediaFile.query.get_or_404(media_id)
    return jsonify(media.to_dict())


@api.route("/media/<int:media_id>/file", methods=["GET"])
def serve_media_file(media_id):
    media = MediaFile.query.get_or_404(media_id)
    upload_dir = current_app.config["UPLOAD_FOLDER"]
    return send_from_directory(upload_dir, media.filename, mimetype=media.mime_type)


@api.route("/media/<int:media_id>", methods=["DELETE"])
def delete_media(media_id):
    media = MediaFile.query.get_or_404(media_id)

    # Unlink from any instances using this media
    instances = OutputInstance.query.filter_by(media_file_id=media_id).all()
    for inst in instances:
        inst.media_file_id = None
        inst.source_value = ""
    db.session.commit()

    # Delete DB record first, then file (avoids orphaned DB records if file delete fails)
    original_name = media.original_name
    filename = media.filename
    db.session.delete(media)
    db.session.commit()

    filepath = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
    except OSError as e:
        logger.warning(f"Failed to delete media file {filepath}: {e}")

    log_event("MEDIA_DELETED", f"id={media_id} name='{original_name}'")
    return jsonify({"message": "Deleted", "unlinked_instances": [i.id for i in instances]})


# =========================================================================
# Status
# =========================================================================

@api.route("/status", methods=["GET"])
def status():
    running_ids = manager.get_running_ids()
    return jsonify({
        "running_instances": running_ids,
        "running_count": len(running_ids),
        "total_instances": OutputInstance.query.count(),
        "media_count": MediaFile.query.count(),
    })


@api.route("/health", methods=["GET"])
def health():
    """Per-instance health details including heartbeat age."""
    instances = OutputInstance.query.all()
    health_data = []
    for inst in instances:
        info = manager.get_instance_health(inst.id)
        health_data.append({
            "id": inst.id,
            "name": inst.name,
            "running": manager.is_running(inst.id),
            "health": info,
        })
    return jsonify(health_data)
