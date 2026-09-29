"""yt-dlp download wrapper.

Downloads go to a unique per-job temporary directory; the caller (worker)
is responsible for moving the finished file to its final, sanitized name.

  * video -> best mp4 (video + audio merged; requires ffmpeg)
  * audio -> best audio source re-encoded to mp3 (requires ffmpeg)
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp

logger = logging.getLogger("dlbot.download")


class DownloadError(Exception):
    """Raised when yt-dlp cannot download or process the requested media."""


class FfmpegMissingError(DownloadError):
    """Raised when ffmpeg is not available but the job requires it."""


def check_ffmpeg() -> bool:
    """Return True when the ``ffmpeg`` binary is on PATH (warn otherwise)."""
    if shutil.which("ffmpeg"):
        return True
    logger.warning(
        "ffmpeg was not found on PATH. Merging video+audio (mp4) and audio "
        "conversion (mp3) will fail until ffmpeg is installed."
    )
    return False


class _YtdlpLogger:
    """Object with the interface yt-dlp expects, routed to Python logging."""

    def __init__(self) -> None:
        self._log = logging.getLogger("dlbot.download.ytdlp")

    def _emit(self, level: int, msg: object) -> None:
        text = str(msg)
        if "has already been recorded" in text:  # yt-dlp noise about cached info
            return
        self._log.log(level, "yt-dlp: %s", text)

    def debug(self, msg: object) -> None:
        self._emit(logging.DEBUG, msg)

    def info(self, msg: object) -> None:
        self._emit(logging.INFO, msg)

    def warning(self, msg: object) -> None:
        self._emit(logging.WARNING, msg)

    def error(self, msg: object) -> None:
        self._emit(logging.ERROR, msg)

    def shout(self, msg: object) -> None:
        self._emit(logging.ERROR, msg)


@dataclass(frozen=True)
class DownloadResult:
    """Outcome of a successful download (file still in its temp location)."""

    file_path: Path
    file_size: int
    title: str
    video_id: str | None


def _progress_hook(job_id: int) -> Any:
    def _hook(d: dict[str, Any]) -> None:
        if d.get("status") == "downloading":
            percent = (d.get("_percent_str") or "?").strip()
            speed = (d.get("_speed_str") or "?").strip()
            logger.debug("job %d downloading %s%% at %s", job_id, percent, speed)
        elif d.get("status") == "finished":
            logger.info("job %d finished download stage", job_id)

    return _hook


def _build_options(job_id: int, work_dir: Path, media_type: str,
                   cookies_file: Path | None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "outtmpl": str(work_dir / "%(title).100B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": _YtdlpLogger(),
        "retries": 3,
        "fragment_retries": 5,
        "progress_hooks": [_progress_hook(job_id)],
    }
    if cookies_file is not None:
        if not Path(cookies_file).is_file():
            raise DownloadError(f"Cookies file not found: {cookies_file}")
        options["cookiefile"] = str(cookies_file)

    if media_type == "audio":
        if not check_ffmpeg():
            raise FfmpegMissingError("ffmpeg is required to convert audio to mp3")
        options.update(
            {
                "format": "bestaudio/best",
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                ],
            }
        )
    else:  # video
        if not check_ffmpeg():
            raise FfmpegMissingError("ffmpeg is required to merge video+audio into mp4")
        options.update(
            {
                "format": "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b[ext=mp4]/b",
                "merge_output_format": "mp4",
            }
        )
    return options


def _find_output_file(work_dir: Path, info: dict[str, Any]) -> Path:
    """Locate the file yt-dlp actually produced (postprocessors may rename it)."""
    candidates: list[dict[str, Any]]
    if info.get("entries"):
        candidates = [e for e in info.get("entries", []) if e]
    else:
        candidates = [info]

    for entry in candidates:
        for rd in entry.get("requested_downloads") or []:
            filepath = rd.get("filepath")
            if filepath and Path(filepath).is_file():
                return Path(filepath)
        filepath = entry.get("filepath")
        if filepath and Path(filepath).is_file():
            return Path(filepath)

    files = [p for p in work_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
    if not files:
        raise DownloadError("Download finished but no output file was found")
    return max(files, key=lambda p: p.stat().st_size)


def download(
    *,
    job_id: int,
    url: str,
    media_type: str,
    work_dir: Path,
    cookies_file: Path | None = None,
) -> DownloadResult:
    """Download ``url`` into ``work_dir`` and return the finished file.

    The file is left in ``work_dir`` under a temporary name; the worker moves
    it to the final sanitized path in the download directory.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    options = _build_options(job_id, work_dir, media_type, cookies_file)

    logger.info("job %d: starting yt-dlp (%s) for %s", job_id, media_type, url)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadError(str(exc)) from exc

    if not info:
        raise DownloadError("yt-dlp returned no info for the URL")
    if info.get("_type") == "playlist":
        raise DownloadError("The URL resolved to a playlist, which is not supported")

    file_path = _find_output_file(work_dir, info)
    title = str(info.get("title") or file_path.stem)
    video_id = info.get("id")

    result = DownloadResult(
        file_path=file_path,
        file_size=file_path.stat().st_size,
        title=title,
        video_id=str(video_id) if video_id else None,
    )
    logger.info(
        "job %d: downloaded %s (%d bytes), title=%r",
        job_id, file_path.name, result.file_size, title,
    )
    return result
