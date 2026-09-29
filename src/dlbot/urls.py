"""Validation of user-supplied URLs and the ``subdir`` command parameter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

#: Platforms yt-dlp can handle that this bot supports.
PLATFORM_YOUTUBE = "youtube"
PLATFORM_INSTAGRAM = "instagram"


class InvalidURLError(ValueError):
    """Raised when a URL is not a supported YouTube/Instagram video link."""


class InvalidSubdirError(ValueError):
    """Raised when the ``subdir`` parameter is not a safe single folder name."""


@dataclass(frozen=True)
class ParsedMediaURL:
    """A validated media URL plus its detected platform."""

    url: str
    platform: str  # "youtube" | "instagram"


#: Hosts (or host suffixes) mapped to a platform name.
_HOST_MAP: dict[str, str] = {
    "youtube.com": PLATFORM_YOUTUBE,
    "youtu.be": PLATFORM_YOUTUBE,
    "m.youtube.com": PLATFORM_YOUTUBE,
    "music.youtube.com": PLATFORM_YOUTUBE,
    "www.youtube.com": PLATFORM_YOUTUBE,
    "youtube-nocookie.com": PLATFORM_YOUTUBE,
    "instagram.com": PLATFORM_INSTAGRAM,
    "www.instagram.com": PLATFORM_INSTAGRAM,
    "m.instagram.com": PLATFORM_INSTAGRAM,
}

#: Instagram path segments that mark a single video post (Reel / video post / TV).
_INSTAGRAM_VIDEO_PATHS = ("reel", "reels", "p", "tv")


def _platform_for_host(host: str) -> str | None:
    host = host.lower().rstrip(".")
    if host in _HOST_MAP:
        return _HOST_MAP[host]
    # Accept any subdomain of a known base (e.g. "amp.youtube.com").
    for suffix, platform in _HOST_MAP.items():
        if host.endswith("." + suffix):
            return platform
    return None


def parse_media_url(url: str) -> ParsedMediaURL:
    """Validate ``url`` and return it tagged with its platform.

    Raises :class:`InvalidURLError` for non-HTTP(S) URLs, unsupported
    hosts, YouTube playlists, and Instagram links that are not video posts.
    """
    if not isinstance(url, str):
        raise InvalidURLError("URL must be a string")

    candidate = url.strip()
    if not candidate:
        raise InvalidURLError("URL is empty")

    # Tolerate bare domains like "youtube.com/watch?v=..." with a scheme.
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", candidate):
        candidate = "https://" + candidate

    try:
        parsed = urlparse(candidate)
    except ValueError as exc:
        raise InvalidURLError(f"Unparseable URL: {exc}") from exc

    if parsed.scheme not in ("http", "https"):
        raise InvalidURLError("URL must use http or https")

    host = (parsed.hostname or "").lower()
    platform = _platform_for_host(host)
    if platform is None:
        raise InvalidURLError(
            f"Unsupported site {host or '<missing>'} — only YouTube and Instagram video links are supported"
        )

    if platform == PLATFORM_YOUTUBE:
        # Refuse playlists: enqueuing them would dump hundreds of files.
        if parsed.query and "list=" in parsed.query:
            raise InvalidURLError("YouTube playlists are not supported — share the video link instead")
        path = parsed.path.strip("/")
        if path.startswith("playlist"):
            raise InvalidURLError("YouTube playlists are not supported — share the video link instead")
    else:  # instagram
        path = parsed.path.rstrip("/")
        segments = [s for s in path.split("/") if s]
        if not segments or not segments[0].lower() in _INSTAGRAM_VIDEO_PATHS:
            raise InvalidURLError(
                "Instagram links must point at a Reel or video post "
                "(instagram.com/reel/... or instagram.com/p/...)"
            )

    return ParsedMediaURL(url=candidate, platform=platform)


#: Characters that are always allowed in a folder name (letters, digits, '-', '_', '.').
_SUBDIR_SAFE = re.compile(r"^[\w.-]+$", re.UNICODE)


def validate_subdir(subdir: str) -> str:
    """Validate the ``subdir`` parameter; return the cleaned folder name.

    The value must be a single directory name (no path separators, no
    ``.``/``..``, no absolute paths) — this is what keeps a bad parameter
    from escaping the download directory.
    """
    value = (subdir or "").strip()
    if not value:
        raise InvalidSubdirError("subdir is empty")

    # Strip any drive letter / absolute-path lookalikes up front.
    if value.startswith(("/", "\\", "~")):
        raise InvalidSubdirError(f"subdir {value!r} must be a single folder name (no leading / or ~)")

    if re.match(r"^[A-Za-z]:[\\\\/]", value):  # e.g. C:\ or C:/
        raise InvalidSubdirError(f"subdir {value!r} must be a single folder name (no drive letter)")

    if "\\" in value or "/" in value:
        raise InvalidSubdirError(f"subdir {value!r} cannot contain path separators — use a single folder name")

    if value in (".", "..") or value.endswith("."):
        raise InvalidSubdirError(f"subdir {value!r} is not a valid folder name")

    if "\x00" in value:
        raise InvalidSubdirError("subdir contains invalid characters")

    if len(value) > 100:
        raise InvalidSubdirError("subdir must be at most 100 characters")

    if not _SUBDIR_SAFE.match(value):
        raise InvalidSubdirError(
            f"subdir {value!r} contains invalid characters — use letters, numbers, '-', '_' or '.'"
        )

    return value
