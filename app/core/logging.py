"""
Logging setup.

`logging.basicConfig` silently does nothing when the root logger already has
handlers — which is exactly what happens when the process manager (gunicorn,
a reloader worker, some platform runners) configures logging before importing
the app. The visible symptom is "logging stopped working": the custom format
disappears and INFO records vanish because the pre-existing root level is
WARNING. `force=True` removes that failure mode by replacing whatever is
already installed.
"""

import logging
import sys

LOG_FORMAT = "%(asctime)s │ %(levelname)-8s │ %(name)s │ %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO

    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
        stream=sys.stdout,
        force=True,
    )

    # Our own loggers get the level explicitly, so they survive anything that
    # later fiddles with the root logger.
    logging.getLogger("app").setLevel(level)

    # Uvicorn installs its own handlers; let its records flow through ours
    # instead so every line in the platform log reads the same way.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # SQLAlchemy echoes every statement at INFO when debug is on; that is
    # deafening, so keep it at WARNING unless explicitly debugging.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if debug else logging.WARNING
    )
