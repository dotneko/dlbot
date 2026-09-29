"""Queue worker: claims pending jobs, downloads them, records the outcome.

Runs as its own process (``uv run dlbot worker``) or as a subprocess of the
bot in combined mode (``uv run dlbot``).
"""

from __future__ import annotations

import shutil
import signal
import time
from pathlib import Path

from .config import Settings
from .database import Database, QueueJob
from .download import check_ffmpeg, download
from .filenames import make_stem
from .logging_config import get_logger, log_exception, setup_logging

logger = get_logger("worker")


class Worker:
    """Long-running process that drains the download queue."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.database_path)
        self.download_dir = settings.download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.work_root = self.download_dir / ".tmp"
        self.work_root.mkdir(parents=True, exist_ok=True)
        self._stop = False

    def _request_stop(self, signum, _frame) -> None:
        if not self._stop:
            logger.info("Received signal %s, stopping after current job...", signum)
            self._stop = True

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._request_stop)
        signal.signal(signal.SIGINT, self._request_stop)

    # --- single-job pipeline ---------------------------------------------------

    def _final_path(self, job: QueueJob, stem: str, ext: str) -> Path:
        target_dir = self.download_dir / job.subdir if job.subdir else self.download_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / f"{stem}.{ext}"

    def process_job(self, job: QueueJob) -> None:
        """Download one job, update the DB to success/failed (with retry)."""
        logger.info(
            "job %d: processing %s %r (format=%s, attempt %d/%d)",
            job.id, job.platform or "media", job.url, job.media_type,
            job.attempt, job.max_attempts,
        )
        work_dir = self.work_root / f"job-{job.id}"
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            result = download(
                job_id=job.id,
                url=job.url,
                media_type=job.media_type,
                work_dir=work_dir,
                cookies_file=self.settings.cookies_file,
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the job row
            log_exception(logger, exc)
            retry = job.attempt < job.max_attempts
            self.db.mark_failed(job.id, str(exc)[:2000], retry=retry)
            if retry:
                logger.warning("job %d: attempt %d failed, requeued: %s",
                               job.id, job.attempt, exc)
            else:
                logger.error("job %d: permanently failed after %d attempts: %s",
                             job.id, job.attempt, exc)
            self._cleanup(work_dir)
            return

        # Move the finished file to its final sanitized name (optionally in a subdir).
        stem = make_stem(job.prefix, result.title, job.suffix, result.video_id or "")
        ext = result.file_path.suffix.lstrip(".") or "mp4"
        final_path = self._final_path(job, stem, ext)
        try:
            if final_path.exists():
                # Keep history: append a counter instead of overwriting.
                counter = 2
                while True:
                    candidate = final_path.with_name(
                        f"{final_path.stem}_{counter}{final_path.suffix}"
                    )
                    if not candidate.exists():
                        final_path = candidate
                        break
                    counter += 1
            shutil.move(str(result.file_path), str(final_path))
        except OSError as exc:
            log_exception(logger, exc)
            self.db.mark_failed(
                job.id, f"Move to {final_path} failed: {exc}"[:2000],
                retry=job.attempt < job.max_attempts,
            )
            self._cleanup(work_dir)
            return

        self.db.mark_success(
            job.id,
            title=result.title,
            video_id=result.video_id,
            file_path=str(final_path),
            file_size=result.file_size,
        )
        logger.info(
            "job %d: SUCCESS -> %s (%d bytes, stem=%r)",
            job.id, final_path, result.file_size, stem,
        )
        self._cleanup(work_dir)

    def _cleanup(self, work_dir: Path) -> None:
        shutil.rmtree(work_dir, ignore_errors=True)

    # --- main loop ---------------------------------------------------------------

    def run(self) -> int:
        logger.info(
            "worker starting (db=%s, download_dir=%s, poll=%ss, max_attempts=%d)",
            self.settings.database_path, self.download_dir,
            self.settings.poll_interval, self.settings.max_attempts,
        )
        if not check_ffmpeg():
            logger.warning(
                "ffmpeg not found — video (mp4 merge) and audio (mp3) jobs "
                "will fail until it is installed"
            )
        self.install_signal_handlers()

        while not self._stop:
            job = self.db.claim_next_job()
            if job is None:
                if not self.db.pending_count():
                    self._sleep_interruptible(self.settings.poll_interval)
                continue
            try:
                self.process_job(job)
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                log_exception(logger, exc)
                self.db.mark_failed(
                    job.id, f"Unhandled worker error: {exc}"[:2000],
                    retry=job.attempt < job.max_attempts,
                )
        logger.info("worker stopped")
        return 0

    def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep in small slices so SIGTERM/SIGINT are handled promptly."""
        end = time.monotonic() + seconds
        while not self._stop:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.5, remaining))


def run_worker(settings: Settings) -> int:
    """Entry point used by ``dlbot worker``."""
    setup_logging("worker", settings)
    return Worker(settings).run()