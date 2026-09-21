import os
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///ndi_streamer.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    NDI_HOSTNAME = os.getenv("NDI_HOSTNAME", "NDI-STREAMER")
    NDI_OUTPUT_FPS = int(os.getenv("NDI_OUTPUT_FPS", "60"))
    FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
    FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))

    # Uploads — always resolve to absolute path so Flask's send_from_directory works
    _upload_folder = os.getenv("UPLOAD_FOLDER", os.path.join(BASE_DIR, "uploads"))
    UPLOAD_FOLDER = os.path.abspath(_upload_folder) if not os.path.isabs(_upload_folder) else _upload_folder
    MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "500"))
    MAX_CONTENT_LENGTH = MAX_UPLOAD_SIZE_MB * 1024 * 1024
    IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg", "tiff"}
    VIDEO_EXTENSIONS = {"mp4", "mov", "m4v", "mkv", "webm", "avi", "mpg", "mpeg"}
    ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
    # Presentations accepted by the signage upload endpoint — converted to
    # one image per slide/page (requires LibreOffice + poppler-utils)
    PRESENTATION_EXTENSIONS = {"ppt", "pptx", "odp", "pdf"}
    # DPI used when rasterizing presentation slides/PDF pages
    PRESENTATION_RENDER_DPI = int(os.getenv("PRESENTATION_RENDER_DPI", "150"))

    # Signage runtime state (playlist JSON handed to workers, now-playing
    # status, impression logs) — generated files, not user content
    SIGNAGE_STATE_FOLDER = os.path.join(BASE_DIR, "signage_state")

    # Video optimization: background one-time transcode (ffmpeg) into the
    # cheapest-to-decode playback format — H.264/yuv420p MP4, tuned
    # fastdecode, sized to the target box, audio stripped. Originals stay on
    # disk. Videos already in the perfect format are probed and marked
    # playback-ready without re-encoding. Modes:
    #   "all" (default)       — check every uploaded video (including ones
    #                           already in the library at boot) and convert
    #                           whatever isn't in the ideal playback format
    #   "oversized"           — only convert videos larger than the target
    #                           box (the 4K-chokes-playback case)
    #   "off"                 — never transcode
    _vo = os.getenv("VIDEO_OPTIMIZE", "all").lower()
    VIDEO_OPTIMIZE = {"true": "all", "false": "off"}.get(_vo, _vo)
    if VIDEO_OPTIMIZE not in ("oversized", "all", "off"):
        VIDEO_OPTIMIZE = "all"
    VIDEO_TARGET_WIDTH = int(os.getenv("VIDEO_TARGET_WIDTH", "1920"))
    VIDEO_TARGET_HEIGHT = int(os.getenv("VIDEO_TARGET_HEIGHT", "1080"))
    VIDEO_CRF = int(os.getenv("VIDEO_CRF", "20"))          # x264 quality (lower = better)
    VIDEO_PRESET = os.getenv("VIDEO_PRESET", "veryfast")   # x264 speed/size tradeoff

    # Browser recycling (hours) — restarts Chromium to prevent memory leaks
    BROWSER_RECYCLE_HOURS = float(os.getenv("BROWSER_RECYCLE_HOURS", "4"))

    # Preview thumbnails for the web UI
    PREVIEW_FOLDER = os.path.join(BASE_DIR, "previews")
    PREVIEW_INTERVAL = 2.0  # seconds between preview saves

    # Media library poster thumbnails (server-generated JPEGs — the UI never
    # loads full images/videos just to draw a grid card)
    THUMB_FOLDER = os.path.join(BASE_DIR, "thumbs")
    THUMB_WIDTH = 320

    # Syslog
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    SYSLOG_ENABLED = os.getenv("SYSLOG_ENABLED", "false").lower() == "true"
    SYSLOG_ADDRESS = os.getenv("SYSLOG_ADDRESS", "/dev/log")
    SYSLOG_FACILITY = os.getenv("SYSLOG_FACILITY", "local0")
    SYSLOG_TAG = os.getenv("SYSLOG_TAG", "ndi-streamer")
