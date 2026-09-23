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
    # "development" enables the Werkzeug debugger + reloader, but run.py only
    # honours it on a loopback FLASK_HOST (the debugger console is remote
    # code execution for anyone who can reach it)
    FLASK_ENV = os.getenv("FLASK_ENV", "production")
    # Optional DNS-rebinding guard: comma-separated hostnames/IPs the UI and
    # API may be reached by (e.g. "ndi-server,ndi-server.local,10.0.0.5").
    # Empty = accept any Host header (the default, for LAN convenience).
    ALLOWED_HOSTS = [h.strip().lower() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()]

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
    # Upper bounds on uploaded content, so a small crafted file can't make the
    # server (or a signage worker) decode gigabytes: images above this many
    # megapixels are rejected (8K UHD is ~33 MP), decks are cut at this many
    # pages
    MAX_IMAGE_MEGAPIXELS = int(os.getenv("MAX_IMAGE_MEGAPIXELS", "100"))
    MAX_DECK_PAGES = int(os.getenv("MAX_DECK_PAGES", "300"))

    # Signage state that must SURVIVE a reboot (playlist JSON handed to
    # workers, impression logs) — generated files, not user content
    SIGNAGE_STATE_FOLDER = os.path.join(BASE_DIR, "signage_state")

    # Throwaway runtime files rewritten constantly — the now-playing status
    # JSON (1 write/s per signage instance). Defaults to tmpfs (/dev/shm)
    # where available so these writes land in RAM instead of wearing the
    # SSD; falls back to the persistent state folder (e.g. on Windows).
    _rt_default = ("/dev/shm/webretriever2_runtime"
                   if os.path.isdir("/dev/shm") else SIGNAGE_STATE_FOLDER)
    SIGNAGE_RUNTIME_FOLDER = os.getenv("SIGNAGE_RUNTIME_FOLDER", _rt_default)

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

    # Preview thumbnails for the web UI — pure throwaway state, rewritten up
    # to every 2s per running instance (4/s while a popup preview streams),
    # so they default to tmpfs (/dev/shm) where available: the constant
    # small writes land in RAM, not on the SSD.
    _pv_default = ("/dev/shm/webretriever2_previews"
                   if os.path.isdir("/dev/shm") else os.path.join(BASE_DIR, "previews"))
    PREVIEW_FOLDER = os.getenv("PREVIEW_FOLDER", _pv_default)
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
