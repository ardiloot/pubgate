import logging
import os
import sys

import colorlog

_ENV_VAR = "PUBGATE_LOG_LEVEL"


def setup_logging() -> None:
    env_level = os.environ.get(_ENV_VAR, "INFO").upper()
    level = getattr(logging, env_level, None)
    if level is None:
        level = logging.INFO

    handler = logging.StreamHandler(sys.stderr)
    if hasattr(sys.stderr, "isatty") and sys.stderr.isatty():
        handler.setFormatter(
            colorlog.ColoredFormatter(
                "%(log_color)s[%(levelname)-7s]:%(reset)s %(message)s",
                log_colors={
                    "DEBUG": "cyan",
                    "INFO": "green",
                    "WARNING": "yellow",
                    "ERROR": "red",
                    "CRITICAL": "bold_red",
                },
            )
        )
    else:
        handler.setFormatter(logging.Formatter("[%(levelname)-7s]: %(message)s"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)

    # App logger gets the user-requested level.
    logging.getLogger("pubgate").setLevel(level)

    # Route warnings.warn() through logging.
    logging.captureWarnings(True)
