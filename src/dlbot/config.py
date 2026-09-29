"""Configuration loaded from environment variables (optionally a local .env).

All values can be overridden by real environment variables. A `.env` file at
the project root is read for convenience (real env vars take precedence).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Project root = the directory that contains ``src/`` (one level above this package).
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: `KEY=VALUE` lines, `#` comments, no quoting tricks."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            os.environ.setdefault(key, value)


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"ERROR: environment variable {name}={raw!r} is not an integer") from exc


def _env_int_opt(name: str) -> int | None:
    raw = _env_str(name, "")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"ERROR: environment variable {name}={raw!r} is not an integer") from exc


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of every tunable the bot/worker reads."""

    # Discord
    discord_token: str
    discord_channel_id: int | None
    discord_guild_id: int | None

    # Storage
    database_path: Path
    download_dir: Path
    log_dir: Path

    # Worker
    poll_interval: float
    max_attempts: int
    cookies_file: Path | None

    # Logging
    log_level: str


def load_settings() -> Settings:
    """Build :class:`Settings` from the environment / .env file."""
    _load_dotenv(PROJECT_ROOT / ".env")

    log_level = (_env_str("DLBOT_LOG_LEVEL", "INFO").upper()) or "INFO"

    db_path = Path(_env_str("DLBOT_DATABASE", "data/dlbot.db"))
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    download_dir = Path(_env_str("DLBOT_DOWNLOAD_DIR", "downloads"))
    if not download_dir.is_absolute():
        download_dir = PROJECT_ROOT / download_dir

    log_dir = Path(_env_str("DLBOT_LOG_DIR", "logs"))
    if not log_dir.is_absolute():
        log_dir = PROJECT_ROOT / log_dir

    cookies_raw = _env_str("DLBOT_COOKIES_FILE", "")
    cookies_file: Path | None = None
    if cookies_raw:
        cookies_file = Path(cookies_raw).expanduser()
        if not cookies_file.is_absolute():
            cookies_file = PROJECT_ROOT / cookies_file

    return Settings(
        discord_token=_env_str("DISCORD_TOKEN", ""),
        discord_channel_id=_env_int_opt("DISCORD_CHANNEL_ID"),
        discord_guild_id=_env_int_opt("DISCORD_GUILD_ID"),
        database_path=db_path,
        download_dir=download_dir,
        log_dir=log_dir,
        poll_interval=float(_env_int("DLBOT_WORKER_POLL_INTERVAL", 3)),
        max_attempts=_env_int("DLBOT_MAX_ATTEMPTS", 2),
        cookies_file=cookies_file,
        log_level=log_level,
    )
