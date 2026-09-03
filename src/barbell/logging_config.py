"""
Rich-based console logging setup.

Call setup_logging() once at every entrypoint (CLI, scheduler, scripts).
Level is controlled by BARBELL_LOG_LEVEL in the environment / .env file.

Usage:
    from barbell.logging_config import setup_logging
    setup_logging()
    import logging
    log = logging.getLogger(__name__)
    log.info("ready")
"""

from __future__ import annotations

import logging
import os

from rich.logging import RichHandler

_CONFIGURED = False


def setup_logging(level: str | None = None) -> logging.Logger:
    """
    Configure the root logger with a RichHandler.

    Safe to call multiple times — only configures on the first call.

    Args:
        level: Override log level string (e.g. "DEBUG"). If None, reads
               BARBELL_LOG_LEVEL from the environment, defaulting to "INFO".

    Returns:
        The root logger (convenience — callers can also use logging.getLogger).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return logging.getLogger()

    resolved_level = level or os.environ.get("BARBELL_LOG_LEVEL", "INFO")
    numeric_level = getattr(logging, resolved_level.upper(), logging.INFO)

    handler = RichHandler(
        level=numeric_level,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
        show_path=True,
        markup=True,
    )
    handler.setLevel(numeric_level)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Remove any existing handlers (e.g. pytest caplog adds one)
    root.handlers.clear()
    root.addHandler(handler)

    # Silence noisy third-party loggers at WARNING unless DEBUG is requested
    if numeric_level > logging.DEBUG:
        for noisy in ("httpx", "httpcore", "urllib3", "alpaca", "google_genai"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    return root


def get_logger(name: str) -> logging.Logger:
    """Convenience wrapper — call setup_logging() first, then get a named logger."""
    return logging.getLogger(name)
