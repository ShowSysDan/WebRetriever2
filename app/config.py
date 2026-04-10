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
    FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))

    # Uploads — always resolve to absolute path so Flask's send_from_directory works
    _upload_folder = os.getenv("UPLOAD_FOLDER", os.path.join(BASE_DIR, "uploads"))
    UPLOAD_FOLDER = os.path.abspath(_upload_folder) if not os.path.isabs(_upload_folder) else _upload_folder
    MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50"))
    MAX_CONTENT_LENGTH = MAX_UPLOAD_SIZE_MB * 1024 * 1024
    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg", "tiff"}

    # Browser recycling (hours) — restarts Chromium to prevent memory leaks
    BROWSER_RECYCLE_HOURS = float(os.getenv("BROWSER_RECYCLE_HOURS", "4"))

    # Syslog
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    SYSLOG_ENABLED = os.getenv("SYSLOG_ENABLED", "false").lower() == "true"
    SYSLOG_ADDRESS = os.getenv("SYSLOG_ADDRESS", "/dev/log")
    SYSLOG_FACILITY = os.getenv("SYSLOG_FACILITY", "local0")
    SYSLOG_TAG = os.getenv("SYSLOG_TAG", "ndi-streamer")
