"""Logging setup shared by the bot and worker processes.

Each component (``bot`` / ``worker``) gets:
  * a console handler at the configured level (``DLBOT_LOG_LEVEL``), and
  * a rotating file handler at DEBUG in ``logs/<component>.log``.

Discord's loggers are tuned so that connection/API problems are visible:
``discord.http`` logs at DEBUG (it logs response bodies on errors, which is
invaluable for debugging), while chatty gateways (heartbeats) are silenced.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings

#: Loggers whose normal output is chatty and unhelpful for debugging this bot.
_QUIET_LOGGERS = ("discord.gateway", "discord.utils", "asyncio", "urllib3")
#: Loggers we specifically want at DEBUG for error visibility.
_DEBUG_LOGGERS = ("discord.http", "discord.rpc")

_FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)-8s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def setup_logging(component: str, settings: "Settings") -> None:
    """Configure the root logger and Discord loggers for one process."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Start clean so repeated setup (tests, hot reloads) doesn't duplicate output.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(getattr(logging, settings.log_level, logging.INFO))
    console.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(console)

    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        root.warning("Could not create log dir %s; file logging disabled", settings.log_dir)
    else:
        file_handler = logging.handlers.RotatingFileHandler(
            settings.log_dir / f"{component}.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
        root.addHandler(file_handler)

    # Turn off noisy libraries; surface Discord API errors in detail.
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in _DEBUG_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, e.g. ``get_logger("worker")``."""
    return logging.getLogger(f"dlbot.{name}")


def log_exception(logger: logging.Logger, exc: BaseException) -> None:
    """Log an exception with the full stack trace (never drops the traceback)."""
    logger.error(
        "%s: %s", type(exc).__name__, exc,
        exc_info=exc,
        extra={"traceback": exc},
    )


def tail_recent_logs(settings: "Settings", component: str, max_lines: int = 40) -> str:
    """Return the last ``max_lines`` lines of a component log file (for /logtail).

    Returns an empty string when the file does not exist.
    """
    log_path: Path = settings.log_dir / f"{component}.log"
    if not log_path.is_file():
        return ""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if not lines:
        return ""
    selected = lines[-max_lines:]
    joined = "\n".join(selected)
    # Keep it well under Discord's 2000-char message limit.
    if len(joined) > 1900:
        joined = joined[-1900:]
    return joined
