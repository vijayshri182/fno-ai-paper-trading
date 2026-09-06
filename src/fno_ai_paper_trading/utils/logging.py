"""Structured logging setup.

Logging is intentionally kept dependency-free and only ever emits the fields the
application explicitly provides. The paper trading system stores no secrets in
configuration, and this module never logs environment values.
"""
from __future__ import annotations

import logging
import sys

_DEFAULT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO", name: str = "fno_ai_paper_trading") -> logging.Logger:
    """Configure the root logger and return a named logger.

    ``level`` accepts standard names such as ``DEBUG``, ``INFO``, ``WARNING``.
    Invalid values fall back to ``INFO``.
    """
    numeric_level = getattr(logging, level.strip().upper(), None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    logging.basicConfig(
        level=numeric_level,
        format=_DEFAULT_FORMAT,
        datefmt=_DEFAULT_DATE_FORMAT,
        stream=sys.stdout,
        force=True,
    )
    logger = logging.getLogger(name)
    logger.debug("Logging configured at level %s", level)
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a logger for the given module name."""
    return logging.getLogger(name)