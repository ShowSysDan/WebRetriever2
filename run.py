#!/usr/bin/env python3
"""NDI Streamer - Main entry point.

IMPORTANT: The create_app() call MUST be inside the __main__ guard.
On Windows, Python uses 'spawn' for multiprocessing, which re-imports
this module in every child process. If create_app() runs at module
level, each NDI worker would spin up its own Flask app + DB connection.
"""

if __name__ == "__main__":
    import logging
    import multiprocessing
    multiprocessing.freeze_support()  # Required for Windows executables

    from app import create_app

    app = create_app()

    host = app.config.get("FLASK_HOST", "0.0.0.0")
    port = app.config.get("FLASK_PORT", 5000)
    debug = app.config.get("FLASK_ENV") == "development"

    logger = logging.getLogger("ndi-streamer")
    if app.config.get("SECRET_KEY") == "dev-secret-key":
        logger.warning(
            "SECRET_KEY is using the default value. Set SECRET_KEY in .env "
            "to a long random string before running in production."
        )
    if debug and host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "FLASK_ENV=development with host=%s exposes the Flask debugger "
            "on the network. Set FLASK_ENV=production in .env for deployment.",
            host,
        )

    app.run(host=host, port=port, debug=debug)
