"""
REST API routes for NDI output instances, global settings, and media library.
"""

import os
import re
import glob
import json
import time
import uuid
import shutil
import signal
import logging
import tempfile
import threading
import subprocess
from datetime import datetime
from collections import Counter

from flask import Blueprint, Response, request, jsonify, current_app, send_from_directory, send_file, abort
from werkzeug.utils import secure_filename
from PIL import Image as PILImage

from app.models import (
    db, OutputInstance, GlobalSettings, MediaFile, generate_media_uid,
    SignageGroup, SignageItem,
)
from app.workers import manager
from app.transcode import transcoder, ffmpeg_available
from app.logging_config import log_event

api = Blueprint("api", __name__, url_prefix="/api")
logger = logging.getLogger(__name__)


def _run_process_group(cmd, timeout):
    """Run a helper tool in its own process group and reap the WHOLE group
    on timeout.

    subprocess.run's timeout kill only reaches the direct child. soffice is
    a wrapper that forks soffice.bin — killing just the wrapper orphans the
    real process, which then holds the LibreOffice profile lock and breaks
    every later conversion until someone kills it by hand. Group kill is
    POSIX-only; elsewhere this behaves like subprocess.run (single kill).
    Raises subprocess.TimeoutExpired after cleanup, like run() does."""
    posix = hasattr(os, "setsid")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=posix)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if posix:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        proc.kill()
        proc.wait(timeout=10)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _allowed_file(filename):
    return "." in filename and \
        filename.rsplit(".", 1)[1].lower() in current_app.config.get("ALLOWED_EXTENSIONS", set())


def _build_text_settings(inst):
    return {
        "content": inst.text_content, "font": inst.text_font,
        "size": inst.text_size, "color": inst.text_color,
        "bg_color": inst.text_bg_color, "align": inst.text_align,
    }


def _build_video_settings(inst):
    return {
        "loop": bool(inst.video_loop),
        "hold": inst.video_hold or "last",
        "autoplay": bool(inst.video_autoplay),
    }


def _start_worker(inst, settings=None):
    if not settings:
        settings = GlobalSettings.query.first()
    text_settings = _build_text_settings(inst) if inst.source_type == "text" else None
    video_settings = _build_video_settings(inst) if inst.source_type == "video" else None
    signage_settings = None
    if inst.source_type == "signage":
        # The worker never touches the DB — it reads the playlist from a
        # JSON file we (re)write here and on every playlist mutation
        _write_signage_playlist(inst)
        signage_settings = _signage_paths(inst.id)

    # Resolve source value for media-backed images
    source_value = inst.source_value
    if inst.source_type == "image" and inst.media_file:
        source_value = f"http://127.0.0.1:{current_app.config.get('FLASK_PORT', 5000)}/api/media/{inst.media_file_id}/file"
    elif inst.source_type == "video" and inst.media_file:
        # Video is decoded directly by the worker (OpenCV/FFmpeg) — hand it
        # the local file path (optimized playback copy when one exists),
        # not an HTTP URL
        source_value = os.path.join(
            current_app.config["UPLOAD_FOLDER"], inst.media_file.playback_filename
        )

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
        video_settings=video_settings,
        signage_settings=signage_settings,
        preview_dir=current_app.config.get("PREVIEW_FOLDER"),
        preview_interval=current_app.config.get("PREVIEW_INTERVAL", 2.0),
    )


# =========================================================================
# Signage plumbing — playlist file, schedule resolution, impression folding
# =========================================================================

def _signage_paths(instance_id):
    """Filesystem paths for one signage instance's runtime state.

    Playlist and impressions must survive a reboot and live in the
    persistent state folder; the now-playing status is rewritten every
    second and goes to the runtime folder (tmpfs where available, so the
    constant writes land in RAM instead of wearing the disk)."""
    state_dir = current_app.config["SIGNAGE_STATE_FOLDER"]
    runtime_dir = current_app.config.get("SIGNAGE_RUNTIME_FOLDER", state_dir)
    return {
        "playlist_path": os.path.join(state_dir, f"playlist_{instance_id}.json"),
        "status_path": os.path.join(runtime_dir, f"status_{instance_id}.json"),
        "impressions_path": os.path.join(state_dir, f"impressions_{instance_id}.log"),
    }


def _signage_playlist_entries(inst):
    """Flatten the playlist into play order: [(item, group_or_None), ...].

    Top-level order interleaves groups and ungrouped items by sort_order; a
    group expands to its items (by their sort_order within the group)."""
    groups = sorted(inst.signage_groups, key=lambda g: (g.sort_order, g.id))
    ungrouped = [i for i in inst.signage_items if i.group_id is None]
    entries = sorted(
        [("group", g) for g in groups] + [("item", i) for i in ungrouped],
        key=lambda e: (e[1].sort_order, e[1].id),
    )
    out = []
    for kind, obj in entries:
        if kind == "group":
            for it in sorted(obj.items, key=lambda i: (i.sort_order, i.id)):
                out.append((it, obj))
        else:
            out.append((obj, None))
    return out


def _resolve_signage_item(item, group, inst):
    """Bake one playlist item's effective settings into a plain dict for the
    worker. Resolution rules:
      - duration: item → group → (video's own length) → instance default.
        A duration set on the item/group is "explicit": a video holds its
        last frame to fill the slot instead of ending early.
      - crossfade: item → group → instance default. This is the OUTGOING
        fade — the transition into the next item starts this many seconds
        before the slot ends (before a video file ends).
      - date window: intersection of item and group windows.
      - daily window: item's if set, else group's.
      - enabled: item AND group.
    """
    media = item.media_file
    if media is None:
        return None
    is_video = media.is_video

    duration = item.duration_s
    explicit = duration is not None
    if duration is None and group is not None and group.duration_s is not None:
        duration = group.duration_s
        explicit = True
    if duration is None and is_video and media.duration_s:
        duration = media.duration_s
    if duration is None:
        duration = inst.signage_duration if inst.signage_duration is not None else 8.0

    crossfade = item.crossfade_s
    if crossfade is None and group is not None:
        crossfade = group.crossfade_s
    if crossfade is None:
        crossfade = inst.signage_crossfade if inst.signage_crossfade is not None else 1.0

    start = item.start_at
    end = item.end_at
    if group is not None:
        if group.start_at and (start is None or group.start_at > start):
            start = group.start_at
        if group.end_at and (end is None or group.end_at < end):
            end = group.end_at

    if item.daily_start and item.daily_end:
        daily_start, daily_end = item.daily_start, item.daily_end
    elif group is not None and group.daily_start and group.daily_end:
        daily_start, daily_end = group.daily_start, group.daily_end
    else:
        daily_start = daily_end = None

    return {
        "id": item.id,
        "media_id": media.id,
        "name": media.original_name,
        "kind": "video" if is_video else "image",
        "path": os.path.join(current_app.config["UPLOAD_FOLDER"], media.playback_filename),
        "duration": round(float(duration), 3),
        "duration_explicit": explicit,
        "crossfade": round(float(crossfade), 3),
        "start_at": start.isoformat() if start else None,
        "end_at": end.isoformat() if end else None,
        "daily_start": daily_start,
        "daily_end": daily_end,
        "enabled": bool(item.enabled) and (bool(group.enabled) if group else True),
    }


def _write_signage_playlist(inst):
    """Atomically (re)write the playlist JSON the worker reads."""
    items = []
    for item, group in _signage_playlist_entries(inst):
        resolved = _resolve_signage_item(item, group, inst)
        if resolved is not None:
            items.append(resolved)

    state_dir = current_app.config["SIGNAGE_STATE_FOLDER"]
    os.makedirs(state_dir, exist_ok=True)
    path = _signage_paths(inst.id)["playlist_path"]
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"instance_id": inst.id, "items": items}, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sync_signage(inst):
    """Push playlist changes to a running worker without restarting it."""
    if inst.source_type != "signage":
        return
    _write_signage_playlist(inst)
    if manager.is_running(inst.id):
        manager.signage_command(inst.id, "reload")


# The worker appends one line per impression (open/append/close per event);
# folding atomically rotates the log so no append can be lost, then adds the
# counts to the DB. The lock keeps concurrent API threads from double-folding.
_impressions_lock = threading.Lock()


def _fold_impressions(instance_id):
    path = _signage_paths(instance_id)["impressions_path"]
    with _impressions_lock:
        if not os.path.exists(path):
            return
        rotated = path + ".folding"
        try:
            os.replace(path, rotated)
        except OSError:
            return
        counts = Counter()
        try:
            with open(rotated, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.isdigit():
                        counts[int(line)] += 1
        finally:
            try:
                os.remove(rotated)
            except OSError:
                pass
        if not counts:
            return
        for item_id, n in counts.items():
            item = db.session.get(SignageItem, item_id)
            if item is not None:
                item.impressions = (item.impressions or 0) + n
        db.session.commit()


_DAILY_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _parse_dt(value, field):
    """Parse an optional schedule datetime ('' / null clears it).

    Accepts the datetime-local format (YYYY-MM-DDTHH:MM[:SS]); stored naive
    and interpreted in the server's local timezone."""
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        abort(400, description=f"{field} must be an ISO datetime (YYYY-MM-DDTHH:MM)")


def _parse_daily(value, field):
    """Parse an optional 'HH:MM' daily-window bound ('' / null clears it)."""
    if value in (None, ""):
        return None
    value = str(value)[:5]
    if not _DAILY_RE.match(value):
        abort(400, description=f"{field} must be HH:MM (24h)")
    return value


def _parse_opt_float(value, field, lo=0.0, hi=86400.0):
    """Parse an optional non-negative float ('' / null clears the override)."""
    if value in (None, ""):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        abort(400, description=f"{field} must be a number")
    if not (lo <= f <= hi):
        abort(400, description=f"{field} must be between {lo} and {hi}")
    return f


def _next_top_sort(inst):
    """Sort key placing a new entry at the end of the top-level playlist."""
    tops = [g.sort_order for g in inst.signage_groups] + \
           [i.sort_order for i in inst.signage_items if i.group_id is None]
    return (max(tops) + 10) if tops else 0


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
    d = settings.to_dict()
    # Server clock for the UI header — signage schedules run on the server's
    # local wall-clock, so show it where people set them. The browser ticks
    # it forward between polls from the epoch + offset here.
    now = datetime.now().astimezone()
    d["server_time_ms"] = int(now.timestamp() * 1000)
    d["server_tz"] = now.tzname() or ""
    d["server_tz_offset_min"] = int(now.utcoffset().total_seconds() // 60)
    return jsonify(d)


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

def _instance_dict(inst):
    """Instance dict augmented with live playback state (video sources) and
    NDI receiver stats (connection count + tally) while running."""
    d = inst.to_dict()
    d["video_state"] = manager.get_video_state(inst.id)
    d["ndi"] = manager.get_ndi_stats(inst.id)
    return d


@api.route("/instances", methods=["GET"])
def list_instances():
    instances = OutputInstance.query.order_by(OutputInstance.created_at).all()
    for inst in instances:
        actual = manager.is_running(inst.id)
        if inst.running != actual:
            inst.running = actual
    db.session.commit()
    return jsonify([_instance_dict(i) for i in instances])


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
        video_loop=data.get("video_loop", False),
        video_hold=data.get("video_hold", "last"),
        video_autoplay=data.get("video_autoplay", False),
        signage_duration=_parse_opt_float(data.get("signage_duration", 8.0), "signage_duration", 0.5),
        signage_crossfade=_parse_opt_float(data.get("signage_crossfade", 1.0), "signage_crossfade", 0.0, 30.0),
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
    return jsonify(_instance_dict(inst))


@api.route("/instances/<int:instance_id>", methods=["PUT"])
def update_instance(instance_id):
    inst = OutputInstance.query.get_or_404(instance_id)
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400
    was_running = manager.is_running(inst.id)
    needs_restart = False
    signage_dirty = False

    # Signage timing defaults are baked into the playlist file, so changing
    # them needs a playlist rewrite + live reload — not a worker restart
    signage_live_fields = {"signage_duration", "signage_crossfade"}

    for field in [
        "name", "source_type", "source_value", "media_file_id",
        "text_content", "text_font", "text_size", "text_color",
        "text_bg_color", "text_align",
        "video_loop", "video_hold", "video_autoplay",
        "signage_duration", "signage_crossfade",
        "width", "height", "capture_fps", "refresh_interval", "enabled",
    ]:
        if field in data:
            old = getattr(inst, field)
            new = data[field]
            if field == "signage_duration":
                new = _parse_opt_float(new, field, 0.5)
            elif field == "signage_crossfade":
                new = _parse_opt_float(new, field, 0.0, 30.0)
            if old != new:
                setattr(inst, field, new)
                if field in signage_live_fields:
                    signage_dirty = True
                elif field != "enabled":
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
    elif signage_dirty:
        _sync_signage(inst)

    return jsonify(inst.to_dict())


@api.route("/instances/<int:instance_id>", methods=["DELETE"])
def delete_instance(instance_id):
    inst = OutputInstance.query.get_or_404(instance_id)
    name = inst.name
    if manager.is_running(inst.id):
        manager.stop_instance(inst.id)
    db.session.delete(inst)
    db.session.commit()

    # Remove the stale preview thumbnail so deleted instances don't
    # accumulate orphaned files in the previews directory
    preview_dir = current_app.config.get("PREVIEW_FOLDER")
    if preview_dir:
        try:
            os.remove(os.path.join(preview_dir, f"{instance_id}.jpg"))
        except OSError:
            pass

    # Signage runtime state files (playlist/status/impressions) are keyed by
    # instance id — clean them up so they can't leak or be inherited by a
    # future instance that reuses the id
    for path in _signage_paths(instance_id).values():
        try:
            os.remove(path)
        except OSError:
            pass

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
# Video playback control (play/stop/load over plain HTTP for show controllers)
#
# Instances can be addressed by numeric id or by name, media files by
# numeric id, permanent uid, or original filename — and GET is accepted
# alongside POST so simple controllers (Companion, Crestron, a browser
# bookmark) can fire commands with a bare URL:
#   http://<host>:5000/api/instances/Walk-In%20Video/video/play
#   http://<host>:5000/api/instances/3/video/play/intro.mp4
#   http://<host>:5000/api/instances/3/video/load/9f3c21ab
#   http://<host>:5000/api/instances/3/video/stop?hold=first
#
# Switching media on a running instance is a hot-swap inside the worker
# process (no restart, no NDI sender teardown): the new file is opened and
# verified before it replaces the old one, so the stream never drops and
# the switch is near-instant. `load` cues a video on its first frame so a
# later `play` starts with zero load latency.
# =========================================================================

def _resolve_instance(ref):
    """Look up an instance by numeric id or by (URL-decoded) name."""
    if ref.isdigit():
        inst = db.session.get(OutputInstance, int(ref))
    else:
        inst = OutputInstance.query.filter_by(name=ref).first()
    if not inst:
        abort(404, description=f"No instance '{ref}'")
    return inst


def _resolve_media(ref):
    """Look up a media file by numeric id, permanent uid, or original filename."""
    media = None
    if ref.isdigit():
        media = db.session.get(MediaFile, int(ref))
    if media is None:
        media = MediaFile.query.filter_by(uid=ref).first()
    if media is None:
        media = MediaFile.query.filter_by(original_name=ref).first()
    if not media:
        abort(404, description=f"No media file '{ref}'")
    return media


def _media_path(media):
    """Path workers should decode — the optimized playback copy when one
    exists, else the original upload."""
    return os.path.join(current_app.config["UPLOAD_FOLDER"], media.playback_filename)


def _hold_param():
    """Validated optional ?hold=first|last query parameter."""
    hold = request.args.get("hold")
    if hold is not None:
        hold = hold.lower()
        if hold not in ("first", "last"):
            abort(400, description="hold must be 'first' or 'last'")
    return hold


def _apply_video_selection(inst, media_ref, hold):
    """Persist a media switch and/or hold change on the instance.
    Returns the resolved MediaFile (or None if no media_ref given)."""
    media = None
    if media_ref:
        media = _resolve_media(media_ref)
        if not media.is_video:
            abort(400, description=f"Media '{media.original_name}' is not a video")
        if inst.media_file_id != media.id:
            inst.media_file_id = media.id
            inst.source_value = ""
    if hold and inst.video_hold != hold:
        inst.video_hold = hold
    db.session.commit()
    return media


def _video_response(inst, media, state, **extra):
    return jsonify({
        "id": inst.id, "name": inst.name, "video_state": state,
        "media": media.to_dict() if media else None,
        **extra,
    })


@api.route("/instances/<ref>/video/play", methods=["GET", "POST"])
@api.route("/instances/<ref>/video/play/<media_ref>", methods=["GET", "POST"])
def video_play(ref, media_ref=None):
    """Play from the first frame — optionally switching to a different media
    file first (`/video/play/<media>` or `?media=`, by id/uid/filename)."""
    inst = _resolve_instance(ref)
    if inst.source_type != "video":
        return jsonify({"error": f"Instance '{inst.name}' is not a video source"}), 400

    media_ref = media_ref or request.args.get("media")
    hold = _hold_param()
    media = _apply_video_selection(inst, media_ref, hold)

    if not manager.is_running(inst.id):
        # Auto-start the NDI output — the fresh worker already picks up the
        # (possibly just-switched) file and hold from the DB, so a plain
        # play command is all it needs
        _start_worker(inst)
        inst.running = True
        db.session.commit()
        ok = manager.video_command(inst.id, "play")
    elif media:
        # Hot-swap inside the running worker and play immediately
        ok = manager.video_command(inst.id, "load_play",
                                   path=_media_path(media), hold=hold)
    else:
        ok = manager.video_command(inst.id, "play", hold=hold)

    if not ok:
        return jsonify({"error": "Worker not ready, try again"}), 503
    log_event("VIDEO_PLAY", f"id={inst.id} name='{inst.name}'"
              + (f" media={media.id}" if media else ""))
    return _video_response(inst, media or inst.media_file, "playing")


@api.route("/instances/<ref>/video/load", methods=["GET", "POST"])
@api.route("/instances/<ref>/video/load/<media_ref>", methods=["GET", "POST"])
def video_load(ref, media_ref=None):
    """Cue a video without playing it: load the file (hot-swap if the worker
    is running) and hold on its first frame, so a later `play` is instant."""
    inst = _resolve_instance(ref)
    if inst.source_type != "video":
        return jsonify({"error": f"Instance '{inst.name}' is not a video source"}), 400

    media_ref = media_ref or request.args.get("media")
    hold = _hold_param()
    media = _apply_video_selection(inst, media_ref, hold)
    if media is None:
        media = inst.media_file
        if media is None:
            return jsonify({"error": "No media specified and none assigned to instance"}), 400

    if not manager.is_running(inst.id):
        _start_worker(inst)
        inst.running = True
        db.session.commit()
        if inst.video_autoplay:
            # Cue means hold, even for autoplay instances — the stop lands
            # before the first decode, so the first frame stays on air
            manager.video_command(inst.id, "stop")
        ok = True
    else:
        ok = manager.video_command(inst.id, "load",
                                   path=_media_path(media), hold=hold)

    if not ok:
        return jsonify({"error": "Worker not ready, try again"}), 503
    log_event("VIDEO_LOAD", f"id={inst.id} name='{inst.name}' media={media.id}")
    return _video_response(inst, media, "stopped", cued=True)


@api.route("/instances/<ref>/video/stop", methods=["GET", "POST"])
def video_stop(ref):
    """Stop playback and hold — `?hold=first|last` overrides the hold frame."""
    inst = _resolve_instance(ref)
    if inst.source_type != "video":
        return jsonify({"error": f"Instance '{inst.name}' is not a video source"}), 400

    hold = _hold_param()
    if hold and inst.video_hold != hold:
        inst.video_hold = hold
        db.session.commit()

    if not manager.is_running(inst.id):
        return jsonify({"id": inst.id, "name": inst.name, "video_state": None,
                        "message": "Instance not running"})

    manager.video_command(inst.id, "stop", hold=hold)
    log_event("VIDEO_STOP", f"id={inst.id} name='{inst.name}'"
              + (f" hold={hold}" if hold else ""))
    return _video_response(inst, inst.media_file, "stopped")


@api.route("/instances/<ref>/video/status", methods=["GET"])
def video_status(ref):
    inst = _resolve_instance(ref)
    if inst.source_type != "video":
        return jsonify({"error": f"Instance '{inst.name}' is not a video source"}), 400
    return jsonify({
        "id": inst.id,
        "name": inst.name,
        "running": manager.is_running(inst.id),
        "video_state": manager.get_video_state(inst.id),
        "video_loop": bool(inst.video_loop),
        "video_hold": inst.video_hold or "last",
        "video_autoplay": bool(inst.video_autoplay),
        "media": inst.media_file.to_dict() if inst.media_file else None,
    })


@api.route("/instances/<int:instance_id>/preview", methods=["GET"])
def instance_preview(instance_id):
    """Serve the latest preview thumbnail for an instance."""
    preview_dir = current_app.config.get("PREVIEW_FOLDER")
    if not preview_dir:
        return jsonify({"error": "Previews not configured"}), 404
    preview_path = os.path.join(preview_dir, f"{instance_id}.jpg")
    if os.path.exists(preview_path):
        return send_file(preview_path, mimetype="image/jpeg")
    return "", 204


# How the preview stream is paced. POLL is how often the generator checks
# the preview file for a new frame; BOOST_HOLD is how far ahead it keeps the
# worker's HD deadline (comfortably more than one poll); MAX_S caps a single
# stream so an abandoned-but-connected popup can't hold a server thread
# forever (the popup <img> auto-reconnects).
PREVIEW_STREAM_POLL = 0.12
PREVIEW_STREAM_BOOST_HOLD = 6.0
PREVIEW_STREAM_MAX_S = 4 * 3600
PREVIEW_STREAM_STOPPED_GRACE = 5.0


@api.route("/instances/<ref>/preview/stream", methods=["GET"])
def instance_preview_stream(ref):
    """Live MJPEG preview (multipart/x-mixed-replace) for the popup viewer.

    Pushes the instance's preview JPEG whenever the worker writes a new one.
    While at least one stream is connected the worker is kept in "boost"
    mode (854px @ ~4fps instead of the 320px/2s list thumbnails). The
    stream ends shortly after the instance stops; the popup page reconnects
    when it starts again."""
    inst = _resolve_instance(ref)
    preview_dir = current_app.config.get("PREVIEW_FOLDER")
    if not preview_dir:
        return jsonify({"error": "Previews not configured"}), 404
    path = os.path.join(preview_dir, f"{inst.id}.jpg")
    instance_id = inst.id

    def generate():
        last_mtime = None
        last_frame = None
        last_yield = 0.0
        stopped_since = None
        deadline = time.monotonic() + PREVIEW_STREAM_MAX_S

        def part(frame):
            return (b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                    + frame + b"\r\n")

        while time.monotonic() < deadline:
            now = time.monotonic()
            if manager.boost_preview(instance_id, PREVIEW_STREAM_BOOST_HOLD):
                stopped_since = None
            else:
                # Not running: keep serving the last frame briefly (worker
                # may be restarting), then end the stream
                if stopped_since is None:
                    stopped_since = now
                elif now - stopped_since > PREVIEW_STREAM_STOPPED_GRACE:
                    break
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                mtime = None
            if mtime is not None and mtime != last_mtime:
                last_mtime = mtime
                try:
                    with open(path, "rb") as f:
                        frame = f.read()
                except OSError:
                    frame = None
                if frame:
                    last_frame = frame
                    last_yield = now
                    yield part(frame)
            elif last_frame is not None and now - last_yield >= 2.0:
                # Static content produces no new frames — re-send the last
                # one as a keepalive so a closed popup's disconnect is
                # noticed here (GeneratorExit on the failed write) instead
                # of the thread idling until the stream deadline
                last_yield = now
                yield part(last_frame)
            time.sleep(PREVIEW_STREAM_POLL)

    resp = Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")
    resp.headers["Cache-Control"] = "no-store"
    # The stream never ends at a content boundary — disable proxy buffering
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


# =========================================================================
# Media Library
# =========================================================================

@api.route("/media", methods=["GET"])
def list_media():
    """Media listing, each entry augmented with its signage playlist usage
    (which instances' playlists it's in, and inside which group, if any) so
    the library can filter on group membership."""
    files = MediaFile.query.order_by(MediaFile.uploaded_at.desc()).all()

    inst_names = {i.id: i.name for i in
                  db.session.query(OutputInstance.id, OutputInstance.name).all()}
    group_names = {g.id: g.name for g in
                   db.session.query(SignageGroup.id, SignageGroup.name).all()}
    usage = {}
    for it in SignageItem.query.all():
        usage.setdefault(it.media_file_id, []).append({
            "item_id": it.id,
            "instance_id": it.instance_id,
            "instance_name": inst_names.get(it.instance_id),
            "group_id": it.group_id,
            "group_name": group_names.get(it.group_id) if it.group_id else None,
        })

    out = []
    for f in files:
        d = f.to_dict()
        d["signage_usage"] = usage.get(f.id, [])
        # Whether the UI should offer an Optimize action (eligible under the
        # current mode and not already queued/optimized/playback-ready)
        d["can_optimize"] = (
            f.optimize_status not in ("pending", "processing", "done", "native")
            and _needs_optimize(f)
        )
        out.append(d)
    return jsonify(out)


def _probe_media(filepath, ext):
    """(width, height, duration_s) probed from an image or video on disk."""
    width_px = height_px = duration_s = None
    if ext in current_app.config.get("VIDEO_EXTENSIONS", set()):
        try:
            import cv2
            cap = cv2.VideoCapture(filepath)
            if cap.isOpened():
                width_px = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None
                height_px = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None
                fps = cap.get(cv2.CAP_PROP_FPS)
                frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                if fps and fps > 0 and frames and frames > 0:
                    duration_s = round(frames / fps, 2)
            cap.release()
        except Exception:
            pass
    else:
        try:
            with PILImage.open(filepath) as img:
                width_px, height_px = img.size
        except Exception:
            pass
    return width_px, height_px, duration_s


def _create_media_record(filepath, unique_name, original_name, ext, mime_type=None,
                         origin="library", origin_name=None):
    """Create + commit a MediaFile row for a file already in the uploads dir.
    Returns (media, None) on success, (None, (response, status)) on failure —
    the on-disk file is removed on failure so nothing is orphaned."""
    if not mime_type or mime_type == "application/octet-stream":
        # Some clients don't send a useful content type — guess from the
        # extension so browsers can play videos served back to them
        import mimetypes
        mime_type = mimetypes.guess_type(original_name)[0] or mime_type

    file_size = os.path.getsize(filepath)
    width_px, height_px, duration_s = _probe_media(filepath, ext)

    media = MediaFile(
        uid=generate_media_uid(),
        filename=unique_name,
        original_name=original_name,
        mime_type=mime_type,
        file_size=file_size,
        width_px=width_px,
        height_px=height_px,
        duration_s=duration_s,
        origin=origin,
        origin_name=origin_name,
    )
    db.session.add(media)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        try:
            os.remove(filepath)
        except OSError:
            pass
        return None, (jsonify({"error": "Failed to save media record"}), 500)
    log_event("MEDIA_UPLOADED", f"id={media.id} name='{original_name}' size={file_size}")
    # Best-effort eager poster thumbnail (the /thumb endpoint regenerates on
    # miss, so a failure here is invisible)
    _generate_thumb(media)
    return media, None


def _store_media_upload(file, origin="library"):
    """Validate + store an uploaded image/video and create its MediaFile.
    Returns (media, None) or (None, (response, status))."""
    if file.filename == "":
        return None, (jsonify({"error": "No file selected"}), 400)
    if not _allowed_file(file.filename):
        return None, (jsonify({"error": "File type not allowed"}), 400)

    original_name = secure_filename(file.filename)
    if not original_name or "." not in original_name:
        return None, (jsonify({"error": "Invalid filename"}), 400)
    ext = original_name.rsplit(".", 1)[1].lower()
    unique_name = f"{uuid.uuid4().hex}.{ext}"

    upload_dir = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, unique_name)
    file.save(filepath)

    return _create_media_record(
        filepath, unique_name, original_name, ext,
        mime_type=file.content_type, origin=origin,
    )


def _needs_optimize(media):
    """Whether this video should get a playback copy under the current mode."""
    if not media.is_video:
        return False
    mode = current_app.config.get("VIDEO_OPTIMIZE", "oversized")
    if mode == "off":
        return False
    if mode == "all":
        return True
    tw = current_app.config.get("VIDEO_TARGET_WIDTH", 1920)
    th = current_app.config.get("VIDEO_TARGET_HEIGHT", 1080)
    return (media.width_px or 0) > tw or (media.height_px or 0) > th


def _maybe_optimize(media):
    """Queue a background transcode when the mode calls for one."""
    if media is not None and _needs_optimize(media):
        transcoder.enqueue(media.id)
        db.session.commit()


@api.route("/media", methods=["POST"])
def upload_media():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    media, err = _store_media_upload(request.files["file"])
    if err:
        return err
    _maybe_optimize(media)
    return jsonify(media.to_dict()), 201


@api.route("/media/<int:media_id>/optimize", methods=["POST"])
def optimize_media(media_id):
    """Queue (or re-queue) a video for background optimization — for files
    uploaded before this feature existed, or after installing ffmpeg."""
    media = MediaFile.query.get_or_404(media_id)
    if not media.is_video:
        return jsonify({"error": "Only videos can be optimized"}), 400
    if media.optimize_status in ("pending", "processing"):
        return jsonify(media.to_dict())  # already on its way
    status = transcoder.enqueue(media.id)
    db.session.commit()
    if status == "skipped":
        return jsonify({"error": "ffmpeg is not installed on the server "
                        "(apt install ffmpeg)"}), 501
    return jsonify(media.to_dict()), 202


@api.route("/media/<int:media_id>", methods=["GET"])
def get_media(media_id):
    media = MediaFile.query.get_or_404(media_id)
    return jsonify(media.to_dict())


def _thumb_path(media_id):
    return os.path.join(current_app.config["THUMB_FOLDER"], f"{media_id}.jpg")


def _generate_thumb(media):
    """Create the poster JPEG for a media file. Returns the path or None.

    Videos: one decoded frame (~0.5s in) via OpenCV — the browser never has
    to load and seek a full video element just to draw a grid card, which is
    what made library thumbnails flaky. Images: a plain downscale."""
    thumb_w = current_app.config.get("THUMB_WIDTH", 320)
    src = _media_path(media)  # playback copy when available (faster to open)
    dest = _thumb_path(media.id)
    try:
        if media.is_video:
            import cv2
            cap = cv2.VideoCapture(src)
            if not cap.isOpened():
                cap.release()
                return None
            # A frame slightly in beats a black/blank first frame
            fps = cap.get(cv2.CAP_PROP_FPS) or 30
            cap.set(cv2.CAP_PROP_POS_FRAMES, min(int(fps * 0.5), 30))
            ok, frame = cap.read()
            if not ok or frame is None:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = cap.read()
            cap.release()
            if not ok or frame is None:
                return None
            h, w = frame.shape[:2]
            tw = min(thumb_w, w)
            th = max(1, int(h * tw / w))
            frame = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
            # Atomic write so a concurrent request never sees a partial file
            tmp = dest + ".part.jpg"
            if not cv2.imwrite(tmp, frame, [cv2.IMWRITE_JPEG_QUALITY, 80]):
                return None
            os.replace(tmp, dest)
        else:
            with PILImage.open(src) as img:
                img = img.convert("RGB")
                img.thumbnail((thumb_w, thumb_w * 4), PILImage.Resampling.LANCZOS)
                tmp = dest + ".part.jpg"
                img.save(tmp, "JPEG", quality=80)
            os.replace(tmp, dest)
        return dest
    except Exception as e:
        logger.debug(f"Thumbnail generation failed for media {media.id}: {e}")
        try:
            os.remove(dest + ".part.jpg")
        except OSError:
            pass
        return None


@api.route("/media/<int:media_id>/thumb", methods=["GET"])
def media_thumb(media_id):
    """Poster thumbnail (JPEG). Generated on first request and cached on
    disk — media content never changes for an id, so clients may cache."""
    media = MediaFile.query.get_or_404(media_id)
    path = _thumb_path(media.id)
    if not os.path.exists(path):
        path = _generate_thumb(media)
    if not path or not os.path.exists(path):
        return "", 204
    resp = send_file(path, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@api.route("/media/<int:media_id>/download", methods=["GET"])
def download_media(media_id):
    """Download the file as an attachment with its human filename.
    Default is the ORIGINAL upload; ?optimized=1 downloads the transcoded
    playback copy (when one exists)."""
    media = MediaFile.query.get_or_404(media_id)
    upload_dir = current_app.config["UPLOAD_FOLDER"]
    if request.args.get("optimized"):
        if not (media.optimized_filename and media.optimize_status == "done"):
            return jsonify({"error": "No optimized copy for this file"}), 404
        stem = media.original_name.rsplit(".", 1)[0]
        return send_from_directory(
            upload_dir, media.optimized_filename, as_attachment=True,
            download_name=f"{stem} (optimized).mp4",
        )
    return send_from_directory(
        upload_dir, media.filename, as_attachment=True,
        download_name=media.original_name,
    )


@api.route("/media/<int:media_id>/file", methods=["GET"])
def serve_media_file(media_id):
    """Serve the media file. For optimized videos this is the playback copy
    (what actually goes on air, and lighter for browser previews too);
    ?original=1 fetches the untouched upload."""
    media = MediaFile.query.get_or_404(media_id)
    upload_dir = current_app.config["UPLOAD_FOLDER"]
    if request.args.get("original"):
        return send_from_directory(upload_dir, media.filename, mimetype=media.mime_type)
    filename = media.playback_filename
    mime = "video/mp4" if filename != media.filename else media.mime_type
    return send_from_directory(upload_dir, filename, mimetype=mime)


@api.route("/media/<int:media_id>", methods=["DELETE"])
def delete_media(media_id):
    media = MediaFile.query.get_or_404(media_id)

    # Unlink from any instances using this media. Running instances are
    # stopped first — otherwise a video worker keeps an open handle to the
    # deleted file and plays a ghost copy forever (the disk space isn't
    # reclaimed until that handle closes)
    instances = OutputInstance.query.filter_by(media_file_id=media_id).all()
    stopped = []
    for inst in instances:
        if manager.is_running(inst.id):
            manager.stop_instance(inst.id)
            inst.running = False
            stopped.append(inst.id)
        inst.media_file_id = None
        inst.source_value = ""
    db.session.commit()
    if stopped:
        log_event("MEDIA_IN_USE_STOPPED", f"media_id={media_id} stopped_instances={stopped}")

    # Remove signage playlist items that reference this media. Running
    # signage workers get a live playlist reload (no restart needed): if the
    # deleted file is on air, the worker's open handle keeps the frames valid
    # until it transitions to the next item.
    sig_items = SignageItem.query.filter_by(media_file_id=media_id).all()
    affected_signage = {it.instance_id for it in sig_items}
    for it in sig_items:
        db.session.delete(it)
    if sig_items:
        db.session.commit()
        for iid in affected_signage:
            sig_inst = db.session.get(OutputInstance, iid)
            if sig_inst is not None:
                _sync_signage(sig_inst)

    # Delete DB record first, then files (avoids orphaned DB records if file delete fails)
    original_name = media.original_name
    filenames = [media.filename]
    if media.optimized_filename:
        filenames.append(media.optimized_filename)
    db.session.delete(media)
    db.session.commit()

    for filename in filenames:
        filepath = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except OSError as e:
            logger.warning(f"Failed to delete media file {filepath}: {e}")
    try:
        os.remove(_thumb_path(media_id))
    except OSError:
        pass

    log_event("MEDIA_DELETED", f"id={media_id} name='{original_name}'")
    return jsonify({"message": "Deleted", "unlinked_instances": [i.id for i in instances]})


# =========================================================================
# Signage — playlist management, presentation upload, live control
# =========================================================================

def _get_signage_instance(instance_id):
    inst = OutputInstance.query.get_or_404(instance_id)
    if inst.source_type != "signage":
        abort(400, description=f"Instance '{inst.name}' is not a signage source")
    return inst


def _get_owned_item(item_id):
    item = SignageItem.query.get_or_404(item_id)
    return item, db.session.get(OutputInstance, item.instance_id)


def _apply_item_fields(item, data):
    """Apply editable SignageItem fields from a JSON body (validated)."""
    if "duration_s" in data:
        item.duration_s = _parse_opt_float(data["duration_s"], "duration_s", 0.5)
    if "crossfade_s" in data:
        item.crossfade_s = _parse_opt_float(data["crossfade_s"], "crossfade_s", 0.0, 30.0)
    if "start_at" in data:
        item.start_at = _parse_dt(data["start_at"], "start_at")
    if "end_at" in data:
        item.end_at = _parse_dt(data["end_at"], "end_at")
    if "daily_start" in data:
        item.daily_start = _parse_daily(data["daily_start"], "daily_start")
    if "daily_end" in data:
        item.daily_end = _parse_daily(data["daily_end"], "daily_end")
    if "enabled" in data:
        item.enabled = bool(data["enabled"])


@api.route("/instances/<int:instance_id>/signage", methods=["GET"])
def signage_playlist(instance_id):
    """Full playlist state: groups, items (with folded impression counts),
    and the instance's timing defaults."""
    inst = _get_signage_instance(instance_id)
    _fold_impressions(inst.id)
    groups = sorted(inst.signage_groups, key=lambda g: (g.sort_order, g.id))
    items = sorted(inst.signage_items, key=lambda i: (i.sort_order, i.id))
    return jsonify({
        "instance_id": inst.id,
        "defaults": {
            "duration": inst.signage_duration if inst.signage_duration is not None else 8.0,
            "crossfade": inst.signage_crossfade if inst.signage_crossfade is not None else 1.0,
        },
        "groups": [g.to_dict() for g in groups],
        "items": [i.to_dict() for i in items],
        "running": manager.is_running(inst.id),
    })


@api.route("/instances/<int:instance_id>/signage/items", methods=["POST"])
def signage_add_items(instance_id):
    """Append media library files to the playlist.
    Body: {"media_file_ids": [..], "group_id": optional}."""
    inst = _get_signage_instance(instance_id)
    data = request.get_json() or {}
    media_ids = data.get("media_file_ids") or []
    if not isinstance(media_ids, list) or not media_ids:
        return jsonify({"error": "media_file_ids (non-empty list) required"}), 400

    group = None
    if data.get("group_id") is not None:
        group = SignageGroup.query.get_or_404(data["group_id"])
        if group.instance_id != inst.id:
            return jsonify({"error": "Group belongs to a different instance"}), 400

    if group is not None:
        in_group = [i.sort_order for i in group.items]
        next_sort = (max(in_group) + 10) if in_group else 0
    else:
        next_sort = _next_top_sort(inst)

    created = []
    for mid in media_ids:
        media = db.session.get(MediaFile, mid)
        if media is None:
            db.session.rollback()
            return jsonify({"error": f"No media file {mid}"}), 404
        item = SignageItem(
            instance_id=inst.id,
            group_id=group.id if group else None,
            media_file_id=media.id,
            sort_order=next_sort,
        )
        next_sort += 10
        db.session.add(item)
        created.append(item)
    db.session.commit()
    _sync_signage(inst)
    log_event("SIGNAGE_ITEMS_ADDED", f"instance={inst.id} count={len(created)}")
    return jsonify([i.to_dict() for i in created]), 201


@api.route("/signage/items/<int:item_id>", methods=["PUT"])
def signage_update_item(item_id):
    item, inst = _get_owned_item(item_id)
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400
    _apply_item_fields(item, data)
    if "group_id" in data:
        gid = data["group_id"]
        if gid is not None:
            group = SignageGroup.query.get_or_404(gid)
            if group.instance_id != item.instance_id:
                return jsonify({"error": "Group belongs to a different instance"}), 400
        item.group_id = gid
    db.session.commit()
    if inst is not None:
        _sync_signage(inst)
    return jsonify(item.to_dict())


@api.route("/signage/items/<int:item_id>", methods=["DELETE"])
def signage_delete_item(item_id):
    item, inst = _get_owned_item(item_id)
    db.session.delete(item)
    db.session.commit()
    if inst is not None:
        _sync_signage(inst)
    log_event("SIGNAGE_ITEM_DELETED", f"id={item_id}")
    return jsonify({"message": "Deleted"})


@api.route("/instances/<int:instance_id>/signage/items/update", methods=["POST"])
def signage_update_items(instance_id):
    """Batch-edit playlist items in one call (one playlist reload).
    Body: {"item_ids": [..], "set": {fields}} — `set` takes the same fields
    as a single-item PUT; only the fields present are changed, and null
    clears an override back to inherited."""
    inst = _get_signage_instance(instance_id)
    data = request.get_json() or {}
    ids = data.get("item_ids") or []
    fields = data.get("set") or {}
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "item_ids (non-empty list) required"}), 400
    if not isinstance(fields, dict) or not fields:
        return jsonify({"error": "set (object of fields to change) required"}), 400

    updated = []
    for iid in ids:
        item = db.session.get(SignageItem, iid)
        if item is not None and item.instance_id == inst.id:
            _apply_item_fields(item, fields)
            updated.append(item)
    db.session.commit()
    _sync_signage(inst)
    log_event("SIGNAGE_ITEMS_UPDATED", f"instance={inst.id} count={len(updated)}")
    return jsonify([i.to_dict() for i in updated])


@api.route("/instances/<int:instance_id>/signage/items/delete", methods=["POST"])
def signage_delete_items(instance_id):
    """Batch delete. Body: {"item_ids": [..]}."""
    inst = _get_signage_instance(instance_id)
    data = request.get_json() or {}
    ids = data.get("item_ids") or []
    deleted = 0
    for iid in ids:
        item = db.session.get(SignageItem, iid)
        if item is not None and item.instance_id == inst.id:
            db.session.delete(item)
            deleted += 1
    db.session.commit()
    _sync_signage(inst)
    log_event("SIGNAGE_ITEMS_DELETED", f"instance={inst.id} count={deleted}")
    return jsonify({"deleted": deleted})


@api.route("/instances/<int:instance_id>/signage/groups", methods=["POST"])
def signage_create_group(instance_id):
    """Create a group; optionally move existing items into it.
    Body: {"name": str, "item_ids": optional [..]}."""
    inst = _get_signage_instance(instance_id)
    data = request.get_json() or {}
    name = (data.get("name") or "Group").strip()[:128] or "Group"

    group = SignageGroup(
        instance_id=inst.id, name=name, sort_order=_next_top_sort(inst)
    )
    db.session.add(group)
    db.session.flush()  # need group.id for the items

    moved = 0
    for idx, iid in enumerate(data.get("item_ids") or []):
        item = db.session.get(SignageItem, iid)
        if item is not None and item.instance_id == inst.id:
            item.group_id = group.id
            item.sort_order = idx * 10
            moved += 1
    db.session.commit()
    _sync_signage(inst)
    log_event("SIGNAGE_GROUP_CREATED", f"instance={inst.id} group={group.id} items={moved}")
    return jsonify(group.to_dict()), 201


@api.route("/signage/groups/<int:group_id>", methods=["PUT"])
def signage_update_group(group_id):
    group = SignageGroup.query.get_or_404(group_id)
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400
    if "name" in data:
        group.name = (str(data["name"]).strip() or group.name)[:128]
    _apply_item_fields(group, data)  # same schedule/timing field set
    db.session.commit()
    inst = db.session.get(OutputInstance, group.instance_id)
    if inst is not None:
        _sync_signage(inst)
    return jsonify(group.to_dict())


@api.route("/signage/groups/<int:group_id>", methods=["DELETE"])
def signage_delete_group(group_id):
    """Delete a group. Its items are deleted too, unless ?keep_items=1
    detaches them back to the top level (ungroup)."""
    group = SignageGroup.query.get_or_404(group_id)
    inst = db.session.get(OutputInstance, group.instance_id)
    keep = request.args.get("keep_items") in ("1", "true", "yes")

    items = sorted(group.items, key=lambda i: (i.sort_order, i.id))
    if keep:
        # Detach in play order at the group's old position
        for offset, item in enumerate(items):
            item.group_id = None
            item.sort_order = group.sort_order + offset
    else:
        for item in items:
            db.session.delete(item)
    db.session.delete(group)
    db.session.commit()
    if inst is not None:
        _sync_signage(inst)
    log_event("SIGNAGE_GROUP_DELETED",
              f"id={group_id} items={'kept' if keep else 'deleted'} count={len(items)}")
    return jsonify({"message": "Deleted", "items_kept": keep, "item_count": len(items)})


@api.route("/instances/<int:instance_id>/signage/reorder", methods=["POST"])
def signage_reorder(instance_id):
    """Persist a full playlist ordering from the UI.
    Body: {"order": [{"type": "item"|"group", "id": n}, ...],   # top level
           "group_items": {"<group_id>": [item ids in order]}}."""
    inst = _get_signage_instance(instance_id)
    data = request.get_json() or {}

    for idx, ent in enumerate(data.get("order") or []):
        etype, eid = ent.get("type"), ent.get("id")
        if etype == "group":
            group = db.session.get(SignageGroup, eid)
            if group is not None and group.instance_id == inst.id:
                group.sort_order = idx * 10
        elif etype == "item":
            item = db.session.get(SignageItem, eid)
            if item is not None and item.instance_id == inst.id:
                item.group_id = None
                item.sort_order = idx * 10

    for gid, item_ids in (data.get("group_items") or {}).items():
        try:
            gid = int(gid)
        except (TypeError, ValueError):
            continue
        group = db.session.get(SignageGroup, gid)
        if group is None or group.instance_id != inst.id:
            continue
        for idx, iid in enumerate(item_ids or []):
            item = db.session.get(SignageItem, iid)
            if item is not None and item.instance_id == inst.id:
                item.group_id = gid
                item.sort_order = idx * 10

    db.session.commit()
    _sync_signage(inst)
    return jsonify({"message": "Reordered"})


def _convert_presentation(src_path, ext):
    """Rasterize a presentation/PDF into one PNG per slide/page.

    Returns (workdir, [png paths in slide order]). The caller must remove
    workdir. Raises RuntimeError with a user-facing message when the
    required tools are missing or conversion fails."""
    workdir = tempfile.mkdtemp(prefix="signage_conv_")
    try:
        pdf_path = src_path
        if ext != "pdf":
            soffice = shutil.which("soffice") or shutil.which("libreoffice")
            if not soffice:
                raise RuntimeError(
                    "LibreOffice is not installed — required to convert "
                    "presentations. Install it (apt install libreoffice-impress) "
                    "or upload a PDF export instead."
                )
            result = _run_process_group(
                [soffice, "--headless", "--convert-to", "pdf",
                 "--outdir", workdir, src_path],
                timeout=300,
            )
            pdfs = glob.glob(os.path.join(workdir, "*.pdf"))
            if result.returncode != 0 or not pdfs:
                logger.error(f"soffice conversion failed: {result.stderr[:500]}")
                raise RuntimeError("Presentation conversion failed — is the file valid?")
            pdf_path = pdfs[0]

        if not shutil.which("pdftoppm"):
            raise RuntimeError(
                "pdftoppm is not installed — required to render slides. "
                "Install poppler-utils (apt install poppler-utils)."
            )
        dpi = current_app.config.get("PRESENTATION_RENDER_DPI", 150)
        result = subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi), pdf_path,
             os.path.join(workdir, "slide")],
            capture_output=True, timeout=300,
        )
        pngs = sorted(glob.glob(os.path.join(workdir, "slide*.png")))
        if result.returncode != 0 or not pngs:
            logger.error(f"pdftoppm failed: {result.stderr[:500]}")
            raise RuntimeError("Slide rendering failed — is the file valid?")
        return workdir, pngs
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


@api.route("/instances/<int:instance_id>/signage/upload", methods=["POST"])
def signage_upload(instance_id):
    """Upload content straight into the playlist.

    Images/videos become a single appended item. Presentations (ppt, pptx,
    odp) and PDFs are rasterized into one image per slide and appended as a
    ready-made group named after the file — schedule or delete the whole
    deck as one unit."""
    inst = _get_signage_instance(instance_id)
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    file = request.files["file"]

    original_name = secure_filename(file.filename or "")
    if not original_name or "." not in original_name:
        return jsonify({"error": "Invalid filename"}), 400
    ext = original_name.rsplit(".", 1)[1].lower()

    # Plain image/video → media library + one playlist item
    if ext in current_app.config.get("ALLOWED_EXTENSIONS", set()):
        media, err = _store_media_upload(file, origin="signage")
        if err:
            return err
        item = SignageItem(
            instance_id=inst.id, media_file_id=media.id,
            sort_order=_next_top_sort(inst),
        )
        db.session.add(item)
        db.session.commit()
        _maybe_optimize(media)
        _sync_signage(inst)
        return jsonify({"items": [item.to_dict()], "group": None}), 201

    if ext not in current_app.config.get("PRESENTATION_EXTENSIONS", set()):
        return jsonify({"error": "File type not allowed"}), 400

    # Presentation/PDF → one PNG per slide, grouped
    src_dir = tempfile.mkdtemp(prefix="signage_upload_")
    workdir = None
    try:
        src_path = os.path.join(src_dir, original_name)
        file.save(src_path)
        try:
            workdir, pngs = _convert_presentation(src_path, ext)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 501
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Presentation conversion timed out"}), 504

        upload_dir = current_app.config["UPLOAD_FOLDER"]
        os.makedirs(upload_dir, exist_ok=True)
        deck_name = original_name.rsplit(".", 1)[0]

        # Register all slide images first (each commits its own MediaFile),
        # then build the group + items in a single commit — so a failure
        # mid-deck can't leave a half-populated group in the playlist
        medias = []
        for idx, png in enumerate(pngs, start=1):
            unique_name = f"{uuid.uuid4().hex}.png"
            dest = os.path.join(upload_dir, unique_name)
            shutil.move(png, dest)
            media, err = _create_media_record(
                dest, unique_name,
                f"{deck_name} — slide {idx:02d}.png", "png",
                mime_type="image/png",
                origin="deck", origin_name=original_name,
            )
            if err:
                return err
            medias.append(media)

        group = SignageGroup(
            instance_id=inst.id, name=deck_name[:128],
            sort_order=_next_top_sort(inst),
        )
        db.session.add(group)
        db.session.flush()

        items = []
        for idx, media in enumerate(medias):
            item = SignageItem(
                instance_id=inst.id, group_id=group.id,
                media_file_id=media.id, sort_order=idx * 10,
            )
            db.session.add(item)
            items.append(item)
        db.session.commit()
        _sync_signage(inst)
        log_event("SIGNAGE_DECK_UPLOADED",
                  f"instance={inst.id} deck='{deck_name}' slides={len(items)}")
        return jsonify({
            "group": group.to_dict(),
            "items": [i.to_dict() for i in items],
        }), 201
    finally:
        shutil.rmtree(src_dir, ignore_errors=True)
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


@api.route("/instances/<ref>/signage/status", methods=["GET"])
def signage_status(ref):
    """Live now-playing / up-next state, read from the worker's status file."""
    inst = _resolve_instance(ref)
    if inst.source_type != "signage":
        return jsonify({"error": f"Instance '{inst.name}' is not a signage source"}), 400
    _fold_impressions(inst.id)
    running = manager.is_running(inst.id)
    status = None
    if running:
        try:
            with open(_signage_paths(inst.id)["status_path"], "r", encoding="utf-8") as f:
                status = json.load(f)
        except (OSError, ValueError):
            status = None
    return jsonify({
        "id": inst.id, "name": inst.name, "running": running, "status": status,
    })


@api.route("/instances/<ref>/receivers", methods=["GET"])
def instance_receivers(ref):
    """Who is pulling this NDI source: peer IPs (and reverse-DNS hostnames)
    of established TCP connections to the worker's NDI listening ports.

    Complements the SDK's connection count on /api/instances: the SDK says
    HOW MANY receivers are connected, the sockets say WHO. Every receiver
    keeps a reliable TCP control connection open regardless of the video
    transport (TCP, UDP, multicast), so all of them appear here; hostnames
    are filled in by a cached background reverse-DNS lookup and may be null
    on the first request. `sdk_receivers` is the SDK's own count for
    cross-checking (null when it can't report)."""
    inst = _resolve_instance(ref)
    if not manager.is_running(inst.id):
        return jsonify({"id": inst.id, "name": inst.name, "running": False,
                        "supported": True, "receivers": [], "sdk_receivers": None})
    data = manager.get_receiver_endpoints(inst.id) \
        or {"supported": False, "reason": "Instance not running"}
    ndi = manager.get_ndi_stats(inst.id) or {}
    return jsonify({"id": inst.id, "name": inst.name, "running": True,
                    "sdk_receivers": ndi.get("receivers"), **data})


# How the signage event stream is paced. POLL is how often the generator
# checks the (RAM-resident) status file; the worker force-writes it the
# frame a transition happens, so item changes reach the browser within
# ~POLL seconds. KEEPALIVE comments stop proxies dropping quiet streams;
# MAX_S caps a stream so an abandoned tab can't hold a server thread
# forever (EventSource reconnects automatically).
SIGNAGE_EVENTS_POLL = 0.2
SIGNAGE_EVENTS_KEEPALIVE = 15.0
SIGNAGE_EVENTS_MAX_S = 4 * 3600


@api.route("/instances/<ref>/signage/events", methods=["GET"])
def signage_events(ref):
    """Real-time now-playing state as a Server-Sent Events stream.

    Pushes the same payload as /signage/status whenever it changes (item
    transitions land in ~200ms; the countdown ticks with the worker's 1s
    status writes). One-directional by design — commands stay on the plain
    HTTP endpoints — which is why SSE fits better than a WebSocket: no
    extra dependencies, works through proxies, and the browser's
    EventSource reconnects on its own."""
    inst = _resolve_instance(ref)
    if inst.source_type != "signage":
        return jsonify({"error": f"Instance '{inst.name}' is not a signage source"}), 400
    status_path = _signage_paths(inst.id)["status_path"]
    instance_id = inst.id

    def generate():
        last_payload = None
        last_sent = 0.0
        deadline = time.monotonic() + SIGNAGE_EVENTS_MAX_S
        while time.monotonic() < deadline:
            running = manager.is_running(instance_id)
            status = None
            if running:
                try:
                    with open(status_path, "r", encoding="utf-8") as f:
                        status = json.load(f)
                except (OSError, ValueError):
                    status = None
            payload = json.dumps(
                {"id": instance_id, "running": running, "status": status},
                sort_keys=True,
            )
            now = time.monotonic()
            if payload != last_payload:
                last_payload = payload
                last_sent = now
                yield f"data: {payload}\n\n"
            elif now - last_sent >= SIGNAGE_EVENTS_KEEPALIVE:
                last_sent = now
                yield ": keepalive\n\n"
            time.sleep(SIGNAGE_EVENTS_POLL)

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # nginx: don't buffer the stream
    })


@api.route("/instances/<ref>/signage/skip", methods=["GET", "POST"])
def signage_skip(ref):
    """Skip to the next playlist item now. GET is accepted alongside POST so
    simple controllers can fire it with a bare URL, like the video API."""
    inst = _resolve_instance(ref)
    if inst.source_type != "signage":
        return jsonify({"error": f"Instance '{inst.name}' is not a signage source"}), 400
    if not manager.signage_command(inst.id, "skip"):
        return jsonify({"error": "Instance not running"}), 409
    log_event("SIGNAGE_SKIP", f"id={inst.id} name='{inst.name}'")
    return jsonify({"id": inst.id, "name": inst.name, "message": "Skipping to next item"})


# =========================================================================
# Webcams
# =========================================================================

@api.route("/webcams", methods=["GET"])
def list_webcams():
    """Detect V4L2 video capture devices connected to this machine."""
    from app.workers.webcam_utils import detect_webcams
    return jsonify(detect_webcams())


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
