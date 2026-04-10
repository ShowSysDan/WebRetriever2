"""
Logging configuration with optional syslog forwarding.

Events tracked:
  - Instance started / stopped / crashed / restarted
  - Instance auto-refreshed
  - Global start / stop
  - Settings changed
  - Media uploaded / deleted
  - Errors and warnings
"""

import logging
import logging.handlers
import os

FACILITY_MAP = {
    "kern": logging.handlers.SysLogHandler.LOG_KERN,
    "user": logging.handlers.SysLogHandler.LOG_USER,
    "mail": logging.handlers.SysLogHandler.LOG_MAIL,
    "daemon": logging.handlers.SysLogHandler.LOG_DAEMON,
    "auth": logging.handlers.SysLogHandler.LOG_AUTH,
    "syslog": logging.handlers.SysLogHandler.LOG_SYSLOG,
    "lpr": logging.handlers.SysLogHandler.LOG_LPR,
    "news": logging.handlers.SysLogHandler.LOG_NEWS,
    "local0": logging.handlers.SysLogHandler.LOG_LOCAL0,
    "local1": logging.handlers.SysLogHandler.LOG_LOCAL1,
    "local2": logging.handlers.SysLogHandler.LOG_LOCAL2,
    "local3": logging.handlers.SysLogHandler.LOG_LOCAL3,
    "local4": logging.handlers.SysLogHandler.LOG_LOCAL4,
    "local5": logging.handlers.SysLogHandler.LOG_LOCAL5,
    "local6": logging.handlers.SysLogHandler.LOG_LOCAL6,
    "local7": logging.handlers.SysLogHandler.LOG_LOCAL7,
}


def setup_logging(app):
    """Configure logging with console + optional syslog output."""
    log_level = getattr(logging, app.config.get("LOG_LEVEL", "INFO").upper(), logging.INFO)

    # Root logger
    root = logging.getLogger()
    root.setLevel(log_level)

    # Clear any existing handlers
    root.handlers.clear()

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(log_level)
    console_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console.setFormatter(console_fmt)
    root.addHandler(console)

    # Syslog handler (optional)
    if app.config.get("SYSLOG_ENABLED"):
        syslog_addr = app.config.get("SYSLOG_ADDRESS", "/dev/log")
        facility_name = app.config.get("SYSLOG_FACILITY", "local0").lower()
        facility = FACILITY_MAP.get(facility_name, logging.handlers.SysLogHandler.LOG_LOCAL0)
        tag = app.config.get("SYSLOG_TAG", "ndi-streamer")

        # Determine if address is a socket path or host:port
        if ":" in syslog_addr and not syslog_addr.startswith("/"):
            host, port = syslog_addr.rsplit(":", 1)
            address = (host, int(port))
            socktype = None  # let it auto-detect
        else:
            address = syslog_addr
            socktype = None

        try:
            syslog_handler = logging.handlers.SysLogHandler(
                address=address,
                facility=facility,
            )
            syslog_fmt = logging.Formatter(f"{tag}: [%(levelname)s] %(name)s - %(message)s")
            syslog_handler.setFormatter(syslog_fmt)
            syslog_handler.setLevel(log_level)
            root.addHandler(syslog_handler)
            logging.getLogger(__name__).info(
                f"Syslog enabled -> {syslog_addr} (facility={facility_name}, tag={tag})"
            )
        except Exception as e:
            logging.getLogger(__name__).warning(f"Failed to initialize syslog: {e}")

    # App-specific logger
    app_logger = logging.getLogger("ndi_streamer")
    app_logger.setLevel(log_level)

    return app_logger


# Convenience: structured event logging
def log_event(event_type: str, detail: str = "", level: str = "info"):
    """
    Log a structured event for syslog consumption.

    Usage:
        log_event("INSTANCE_STARTED", "id=3 name='Weather Radar'")
        log_event("INSTANCE_CRASHED", "id=3 name='Weather Radar'", level="error")
    """
    logger = logging.getLogger("ndi_streamer.events")
    msg = f"[{event_type}] {detail}"
    getattr(logger, level.lower(), logger.info)(msg)
