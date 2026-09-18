"""Logging setup. One place to control jiffy's verbosity."""

import logging
import os
import sys

_CONFIGURED = False

LEVELS = {
    "quiet": logging.WARNING,
    "normal": logging.INFO,
    "verbose": logging.DEBUG,
}


def setup(level="normal", *, quiet=False):
    """Configure the 'jiffy' logger once. Returns the logger."""
    global _CONFIGURED
    logger = logging.getLogger("jiffy")
    if _CONFIGURED:
        return logger
    if quiet:
        level = "quiet"
    resolved = LEVELS.get(str(level).lower())
    if resolved is None:
        resolved = getattr(logging, str(level).upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.setLevel(resolved)
    logger.propagate = False
    _CONFIGURED = True
    return logger


def get(name):
    return logging.getLogger(f"jiffy.{name}")


def level_from_env():
    return os.getenv("JIFFY_LOG", "normal")
