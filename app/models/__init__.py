from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class GlobalSettings(db.Model):
    __tablename__ = "global_settings"

    id = db.Column(db.Integer, primary_key=True)
    ndi_hostname = db.Column(db.String(128), nullable=False, default="NDI-STREAMER")
    output_fps = db.Column(db.Integer, nullable=False, default=60)
    all_running = db.Column(db.Boolean, nullable=False, default=False)
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
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class MediaFile(db.Model):
    """Uploaded image/file library."""
    __tablename__ = "media_files"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(256), nullable=False)
    original_name = db.Column(db.String(256), nullable=False)
    mime_type = db.Column(db.String(64), nullable=True)
    file_size = db.Column(db.Integer, nullable=True)  # bytes
    width_px = db.Column(db.Integer, nullable=True)
    height_px = db.Column(db.Integer, nullable=True)
    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id": self.id,
            "filename": self.filename,
            "original_name": self.original_name,
            "mime_type": self.mime_type,
            "file_size": self.file_size,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "url": f"/api/media/{self.id}/file",
            "uploaded_at": self.uploaded_at.isoformat() if self.uploaded_at else None,
        }


class OutputInstance(db.Model):
    __tablename__ = "output_instances"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    source_type = db.Column(db.String(16), nullable=False, default="webpage")
    source_value = db.Column(db.Text, nullable=False, default="")

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
            "width": self.width,
            "height": self.height,
            "capture_fps": self.capture_fps,
            "refresh_interval": self.refresh_interval,
            "enabled": self.enabled,
            "running": self.running,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        return d

    @property
    def ndi_source_name(self):
        """NDI source name — just the instance name.
        NDI protocol automatically prefixes with MACHINE_NAME."""
        return self.name
