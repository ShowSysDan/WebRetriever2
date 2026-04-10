#!/usr/bin/env python3
"""NDI Streamer - Main entry point.

IMPORTANT: The create_app() call MUST be inside the __main__ guard.
On Windows, Python uses 'spawn' for multiprocessing, which re-imports
this module in every child process. If create_app() runs at module
level, each NDI worker would spin up its own Flask app + DB connection.
"""

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()  # Required for Windows executables

    from app import create_app

    app = create_app()
    app.run(
        host="0.0.0.0",
        port=app.config.get("FLASK_PORT", 5000),
        debug=app.config.get("FLASK_ENV") == "development",
    )
