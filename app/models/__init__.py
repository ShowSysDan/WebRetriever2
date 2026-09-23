import uuid
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def generate_media_uid():
    """Short random token for MediaFile.uid — unique among existing rows.

    Unlike the integer primary key (which SQLite may reuse after the
    highest row is deleted), a uid is never reassigned, so external
    controllers can reference media permanently."""
    while True:
        uid = uuid.uuid4().hex[:8]
        if not MediaFile.query.filter_by(uid=uid).first():
            return uid


class GlobalSettings(db.Model):
    __tablename__ = "global_settings"

    id = db.Column(db.Integer, primary_key=True)
    ndi_hostname = db.Column(db.String(128), nullable=False, default="NDI-STREAMER")
    output_fps = db.Column(db.Integer, nullable=False, default=60)
    all_running = db.Column(db.Boolean, nullable=False, default=False)
    # Built-in Overview multiview stream on/off (nullable for ADD COLUMN
    # auto-migration; NULL = off). Restored at boot like running outputs.
    overview_enabled = db.Column(db.Boolean, nullable=True, default=False)
    updated_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "ndi_hostname": self.ndi_hostname,
            "output_fps": self.output_fps,
            "all_running": self.all_running,
            "overview_enabled": bool(self.overview_enabled),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class MediaFile(db.Model):
    """Uploaded image/file library."""
    __tablename__ = "media_files"

    id = db.Column(db.Integer, primary_key=True)
    # Permanent external identifier: assigned once at upload, never reused.
    # (SQLite can hand a deleted row's integer id to the next insert when the
    # highest row is removed — uids can't shift or be recycled that way.)
    # Nullable so existing DBs auto-migrate with ADD COLUMN; backfilled at
    # startup in create_app.
    uid = db.Column(db.String(12), nullable=True, unique=True, index=True)
    filename = db.Column(db.String(256), nullable=False)
    original_name = db.Column(db.String(256), nullable=False)
    mime_type = db.Column(db.String(64), nullable=True)
    file_size = db.Column(db.Integer, nullable=True)  # bytes
    width_px = db.Column(db.Integer, nullable=True)
    height_px = db.Column(db.Integer, nullable=True)
    # Video files only — probed with OpenCV on upload (nullable: added after
    # 0.2.0, and images have no duration)
    duration_s = db.Column(db.Float, nullable=True)
    # How the file got here (nullable for ADD COLUMN auto-migration; NULL =
    # uploaded before 1.3.0, treated as "library"):
    #   "library" — Media Library upload zone
    #   "signage" — Signage tab upload zone
    #   "deck"    — rasterized slide from a presentation/PDF upload
    origin = db.Column(db.String(16), nullable=True)
    # For deck slides: the source deck's filename, so a deck's slides can be
    # filtered/grouped together in the library
    origin_name = db.Column(db.String(256), nullable=True)

    # Background video optimization (nullable for ADD COLUMN auto-migration).
    # Oversized videos get an H.264 playback copy sized for the outputs;
    # the original stays on disk. optimize_status: NULL (never considered) |
    # "pending" | "processing" | "done" | "failed" | "skipped" (no ffmpeg).
    optimized_filename = db.Column(db.String(256), nullable=True)
    optimize_status = db.Column(db.String(12), nullable=True)
    optimized_width = db.Column(db.Integer, nullable=True)
    optimized_height = db.Column(db.Integer, nullable=True)

    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    @property
    def is_video(self):
        if self.mime_type and self.mime_type.startswith("video/"):
            return True
        ext = self.filename.rsplit(".", 1)[-1].lower() if "." in self.filename else ""
        return ext in {"mp4", "mov", "m4v", "mkv", "webm", "avi", "mpg", "mpeg"}

    @property
    def playback_filename(self):
        """The file workers should actually decode: the optimized playback
        copy once it exists, otherwise the original upload."""
        if self.optimized_filename and self.optimize_status == "done":
            return self.optimized_filename
        return self.filename

    def to_dict(self):
        return {
            "id": self.id,
            "uid": self.uid,
            "filename": self.filename,
            "original_name": self.original_name,
            "mime_type": self.mime_type,
            "file_size": self.file_size,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "duration_s": self.duration_s,
            "is_video": self.is_video,
            "origin": self.origin or "library",
            "origin_name": self.origin_name,
            "optimize_status": self.optimize_status,
            "optimized_width": self.optimized_width,
            "optimized_height": self.optimized_height,
            "url": f"/api/media/{self.id}/file",
            "thumb_url": f"/api/media/{self.id}/thumb",
            "download_url": f"/api/media/{self.id}/download",
            "download_optimized_url": (
                f"/api/media/{self.id}/download?optimized=1"
                if self.optimized_filename and self.optimize_status == "done" else None
            ),
            "uploaded_at": self.uploaded_at.isoformat() if self.uploaded_at else None,
        }


class OutputInstance(db.Model):
    __tablename__ = "output_instances"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    source_type = db.Column(db.String(16), nullable=False, default="webpage")
    source_value = db.Column(db.Text, nullable=False, default="")

    # Signage source defaults (nullable for ADD COLUMN auto-migration; code
    # treats NULL as the default). Per-item / per-group values override these.
    signage_duration = db.Column(db.Float, nullable=True, default=8.0)    # seconds per still
    signage_crossfade = db.Column(db.Float, nullable=True, default=1.0)   # seconds

    signage_groups = db.relationship(
        "SignageGroup", backref="instance", lazy=True,
        cascade="all, delete-orphan",
    )
    signage_items = db.relationship(
        "SignageItem", backref="instance", lazy=True,
        cascade="all, delete-orphan",
    )

    # Link to media library (for image source type)
    media_file_id = db.Column(db.Integer, db.ForeignKey("media_files.id"), nullable=True)
    media_file = db.relationship("MediaFile", backref="instances")

    # Text source settings
    text_content = db.Column(db.Text, nullable=True, default="")
    text_font = db.Column(db.String(128), nullable=True, default="Arial")
    text_size = db.Column(db.Integer, nullable=True, default=48)
    text_color = db.Column(db.String(16), nullable=True, default="#FFFFFF")
    text_bg_color = db.Column(db.String(16), nullable=True, default="#000000")
    text_align = db.Column(db.String(16), nullable=True, default="center")

    # Video source settings (nullable so existing DBs can be auto-migrated
    # with a simple ADD COLUMN — code treats NULL as the default)
    video_loop = db.Column(db.Boolean, nullable=True, default=False)      # loop vs play once
    video_hold = db.Column(db.String(8), nullable=True, default="last")   # "last" | "first" frame while stopped
    video_autoplay = db.Column(db.Boolean, nullable=True, default=False)  # start playing when instance starts

    # Resolution
    width = db.Column(db.Integer, nullable=False, default=1920)
    height = db.Column(db.Integer, nullable=False, default=1080)

    # FPS: capture rate (NDI output is always global output_fps)
    capture_fps = db.Column(db.Integer, nullable=False, default=30)

    # Auto-refresh: reload content every N seconds (0 = disabled)
    refresh_interval = db.Column(db.Integer, nullable=False, default=0)

    # State
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    running = db.Column(db.Boolean, nullable=False, default=False)

    # Timestamps
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self):
        d = {
            "id": self.id,
            "name": self.name,
            "source_type": self.source_type,
            "source_value": self.source_value,
            "media_file_id": self.media_file_id,
            "media_file": self.media_file.to_dict() if self.media_file else None,
            "text_content": self.text_content,
            "text_font": self.text_font,
            "text_size": self.text_size,
            "text_color": self.text_color,
            "text_bg_color": self.text_bg_color,
            "text_align": self.text_align,
            "video_loop": bool(self.video_loop),
            "video_hold": self.video_hold or "last",
            "video_autoplay": bool(self.video_autoplay),
            "width": self.width,
            "height": self.height,
            "capture_fps": self.capture_fps,
            "refresh_interval": self.refresh_interval,
            "enabled": self.enabled,
            "running": self.running,
            "signage_duration": self.signage_duration if self.signage_duration is not None else 8.0,
            "signage_crossfade": self.signage_crossfade if self.signage_crossfade is not None else 1.0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if self.source_type == "signage":
            d["signage_item_count"] = len(self.signage_items)
        return d

    @property
    def ndi_source_name(self):
        """NDI source name — just the instance name.
        NDI protocol automatically prefixes with MACHINE_NAME."""
        return self.name


class SignageGroup(db.Model):
    """A named bundle of signage items inside one instance's playlist.

    A group occupies a single slot in the top-level play order (its items
    play consecutively) and carries schedule/timing settings that apply to
    every item in it — so 10 uploaded slides can be scheduled, transitioned
    and deleted as one unit.

    Scheduling semantics (see routes._build_signage_playlist): the group's
    date window intersects with each item's own window; the group's daily
    time window and duration/crossfade act as defaults an item can override.
    """
    __tablename__ = "signage_groups"

    id = db.Column(db.Integer, primary_key=True)
    instance_id = db.Column(
        db.Integer, db.ForeignKey("output_instances.id"), nullable=False, index=True
    )
    name = db.Column(db.String(128), nullable=False, default="Group")
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    # Defaults for items in the group (NULL = fall through to instance default)
    duration_s = db.Column(db.Float, nullable=True)
    crossfade_s = db.Column(db.Float, nullable=True)

    # Schedule window — naive datetimes interpreted in the SERVER's local
    # timezone (signage schedules mean wall-clock time on the box)
    start_at = db.Column(db.DateTime, nullable=True)
    end_at = db.Column(db.DateTime, nullable=True)
    # Daily time-of-day window, "HH:MM" strings; start > end wraps midnight
    daily_start = db.Column(db.String(5), nullable=True)
    daily_end = db.Column(db.String(5), nullable=True)

    enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # No delete-orphan here: "ungroup" detaches items without deleting them.
    # Group deletion handles its items explicitly in the route.
    items = db.relationship("SignageItem", backref="group", lazy=True)

    def to_dict(self):
        return {
            "id": self.id,
            "instance_id": self.instance_id,
            "name": self.name,
            "sort_order": self.sort_order,
            "duration_s": self.duration_s,
            "crossfade_s": self.crossfade_s,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "daily_start": self.daily_start,
            "daily_end": self.daily_end,
            "enabled": self.enabled,
            "item_count": len(self.items),
        }


class SignageItem(db.Model):
    """One entry in a signage instance's playlist: a media file plus playback
    timing, an optional schedule window, and an impression counter."""
    __tablename__ = "signage_items"

    id = db.Column(db.Integer, primary_key=True)
    instance_id = db.Column(
        db.Integer, db.ForeignKey("output_instances.id"), nullable=False, index=True
    )
    group_id = db.Column(
        db.Integer, db.ForeignKey("signage_groups.id"), nullable=True, index=True
    )
    media_file_id = db.Column(
        db.Integer, db.ForeignKey("media_files.id"), nullable=False
    )
    media_file = db.relationship("MediaFile", backref="signage_items")

    # Order within the top level (group_id NULL) or within the group
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    # NULL = inherit from group, then instance default. For videos the
    # fallback is the file's own duration.
    duration_s = db.Column(db.Float, nullable=True)
    crossfade_s = db.Column(db.Float, nullable=True)

    # Schedule window — naive local datetimes + optional daily "HH:MM" window
    start_at = db.Column(db.DateTime, nullable=True)
    end_at = db.Column(db.DateTime, nullable=True)
    daily_start = db.Column(db.String(5), nullable=True)
    daily_end = db.Column(db.String(5), nullable=True)

    enabled = db.Column(db.Boolean, nullable=False, default=True)

    # Times this item went on air (folded in from the worker's impression log)
    impressions = db.Column(db.Integer, nullable=False, default=0)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        m = self.media_file
        return {
            "id": self.id,
            "instance_id": self.instance_id,
            "group_id": self.group_id,
            "media_file_id": self.media_file_id,
            "media_file": m.to_dict() if m else None,
            "sort_order": self.sort_order,
            "duration_s": self.duration_s,
            "crossfade_s": self.crossfade_s,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "daily_start": self.daily_start,
            "daily_end": self.daily_end,
            "enabled": self.enabled,
            "impressions": self.impressions or 0,
        }
