"""SQLite-backed download queue (safe for concurrent bot + worker processes).

Every function opens a short-lived connection with a busy timeout and runs
its work inside a transaction. The queue relies on WAL journaling plus a
``BEGIN IMMEDIATE`` claim transaction so two workers can never claim the
same job.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# --- Job statuses ---------------------------------------------------------
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS download_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT    NOT NULL,
    platform        TEXT,
    media_type      TEXT    NOT NULL,
    prefix          TEXT    NOT NULL DEFAULT '',
    suffix          TEXT    NOT NULL DEFAULT '',
    subdir          TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'pending',
    attempt         INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 2,
    error           TEXT,
    title           TEXT,
    video_id        TEXT,
    file_path       TEXT,
    file_size       INTEGER,
    requested_by    TEXT,
    channel_id      INTEGER,
    guild_id        INTEGER,
    created_at      TEXT    NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    notified_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_dq_status_id ON download_queue (status, id);
"""

#: The columns that may be updated after enqueue.
_UPDATABLE = (
    "status", "attempt", "error", "title", "video_id", "file_path", "file_size",
    "started_at", "finished_at", "notified_at",
)


@dataclass
class QueueJob:
    """One row of the download queue."""

    id: int
    url: str
    platform: str | None
    media_type: str
    prefix: str
    suffix: str
    subdir: str
    status: str
    attempt: int
    max_attempts: int
    error: str | None
    title: str | None
    video_id: str | None
    file_path: str | None
    file_size: int | None
    requested_by: str | None
    channel_id: int | None
    guild_id: int | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    notified_at: str | None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """A thin, thread- and process-safe wrapper around the queue database."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Cheap one-time setup that also proves the file is writable.
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def init_db(self) -> None:
        """Create the schema (idempotent) and apply additive migrations."""
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(download_queue)")}
            if "subdir" not in existing:  # databases created before subdir existed
                conn.execute(
                    "ALTER TABLE download_queue ADD COLUMN subdir TEXT NOT NULL DEFAULT ''"
                )

    # --- enqueue / claims ---------------------------------------------------

    def enqueue_download(
        self,
        *,
        url: str,
        platform: str,
        media_type: str,
        prefix: str = "",
        suffix: str = "",
        subdir: str = "",
        requested_by: str | None = None,
        channel_id: int | None = None,
        guild_id: int | None = None,
        max_attempts: int = 2,
    ) -> int:
        """Insert a new job and return its id."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO download_queue
                    (url, platform, media_type, prefix, suffix, subdir, status,
                     max_attempts, requested_by, channel_id, guild_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    url, platform, media_type, prefix, suffix, subdir, STATUS_PENDING,
                    max_attempts, requested_by, channel_id, guild_id, _utcnow(),
                ),
            )
            return int(cur.lastrowid)

    def claim_next_job(self) -> QueueJob | None:
        """Atomically claim the oldest pending job and mark it processing."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            row = conn.execute(
                "SELECT * FROM download_queue WHERE status = ? ORDER BY id LIMIT 1",
                (STATUS_PENDING,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            conn.execute(
                """
                UPDATE download_queue
                SET status = ?, attempt = attempt + 1, started_at = ?
                WHERE id = ?
                """,
                (STATUS_PROCESSING, _utcnow(), row["id"]),
            )
            claimed = conn.execute(
                "SELECT * FROM download_queue WHERE id = ?", (row["id"],)
            ).fetchone()
            return self._row_to_job(claimed)

    # --- status updates -------------------------------------------------------

    def mark_success(self, job_id: int, *, title: str | None, video_id: str | None,
                     file_path: str, file_size: int) -> None:
        self._update(job_id, status=STATUS_SUCCESS, title=title, video_id=video_id,
                     file_path=file_path, file_size=file_size, error=None,
                     finished_at=_utcnow())

    def mark_failed(self, job_id: int, error: str, *, retry: bool) -> None:
        if retry:
            # Go back to pending; the row keeps its attempt count so the
            # worker can decide whether to stop retrying.
            self._update(job_id, status=STATUS_PENDING, error=error,
                         started_at=None, finished_at=None)
        else:
            self._update(job_id, status=STATUS_FAILED, error=error,
                         finished_at=_utcnow())

    def _update(self, job_id: int, **fields) -> None:
        fields = {key: value for key, value in fields.items() if key in _UPDATABLE}
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE download_queue SET {assignments} WHERE id = ?",
                (*fields.values(), job_id),
            )

    # --- bot-side queries -----------------------------------------------------

    def fetch_unnotified_completed(self) -> list[QueueJob]:
        """Finished jobs the bot has not yet announced."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM download_queue
                WHERE status IN (?, ?) AND notified_at IS NULL
                ORDER BY id
                """,
                (STATUS_SUCCESS, STATUS_FAILED),
            ).fetchall()
            return [self._row_to_job(row) for row in rows]

    def mark_notified(self, job_id: int) -> None:
        self._update(job_id, notified_at=_utcnow())

    def pending_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM download_queue WHERE status = ?",
                (STATUS_PENDING,),
            ).fetchone()
            return int(row["n"]) if row else 0

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> QueueJob:
        return QueueJob(
            id=row["id"],
            url=row["url"],
            platform=row["platform"],
            media_type=row["media_type"],
            prefix=row["prefix"],
            suffix=row["suffix"],
            subdir=row["subdir"],
            status=row["status"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            error=row["error"],
            title=row["title"],
            video_id=row["video_id"],
            file_path=row["file_path"],
            file_size=row["file_size"],
            requested_by=row["requested_by"],
            channel_id=row["channel_id"],
            guild_id=row["guild_id"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            notified_at=row["notified_at"],
        )
