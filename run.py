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

    # Stop all workers (and their Chromium trees) on any exit path,
    # including SIGTERM — nothing may outlive the app
    from app.workers import install_shutdown_cleanup
    install_shutdown_cleanup()

    host = app.config.get("FLASK_HOST", "0.0.0.0")
    port = app.config.get("FLASK_PORT", 5000)
    logger = logging.getLogger("ndi-streamer")
    debug = app.config.get("FLASK_ENV") == "development"
    # The Werkzeug debugger is a remote Python console: never serve it on a
    # network interface, whatever .env says
    if debug and host not in ("127.0.0.1", "localhost", "::1"):
        logger.error(
            "FLASK_ENV=development ignored: the debugger is only enabled on a "
            "loopback FLASK_HOST (current host=%s). Running in production mode.",
            host,
        )
        debug = False

    if app.config.get("SECRET_KEY") == "dev-secret-key":
        logger.warning(
            "SECRET_KEY is using the default value. Set SECRET_KEY in .env "
            "to a long random string before running in production."
        )

    app.run(host=host, port=port, debug=debug)
