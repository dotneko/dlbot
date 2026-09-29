"""Filename sanitization for downloaded media.

Policy:
  1. Normalize the text (NFKC) so exotic spaces/symbols decompose.
  2. Strip emoji, pictographs, and misc symbol blocks by explicit ranges.
  3. Drop every remaining symbol and control character (Unicode S*/C*).
  4. Drop other punctuation, keeping only letters and digits.
  5. Turn every whitespace character into ``_``.
  6. Collapse repeated ``_``, trim ``_``/``.``, truncate to a max length.
  7. Fall back to the video ID when nothing survives (e.g. an emoji-only title).

Example: ``"New Episode 😊 #12 (HD)!"`` -> ``"New_Episode_12_HD"``
"""

from __future__ import annotations

import logging
import re
import unicodedata

logger = logging.getLogger("dlbot.filenames")

#: Default maximum length for the generated stem (prefix + title + suffix).
MAX_STEM_LENGTH = 150

#: Explicit emoji / pictograph / symbol blocks (plus invisible joiners).
_EMOJI_RANGES = (
    "\U0001F000-\U0001FAFF"  # Mahjong..Symbols & Pictographs Extended-A (covers most emoji)
    "\U0001F1E6-\U0001F1FF"  # Regional indicators (flag sequences)
    "\u2600-\u26FF"          # Misc symbols (☀ ⚡ ...)
    "\u2700-\u27BF"          # Dingbats (✨ ...)
    "\u2B00-\u2BFF"          # Misc symbols & arrows (⭐ ...)
    "\u2190-\u21FF"          # Arrows
    "\u2300-\u23FF"          # Technical (⏰ ...)
    "\u25A0-\u25FF"          # Geometric shapes
    "\uFE00-\uFE0F"          # Variation selectors
    "\u200D"                 # Zero-width joiner
    "\u20E3"                 # Combining enclosing keycap
    "\u2049\u203C"           # Bang / double exclamation
    "\u00A9\u00AE"           # (c) / (r)
    "\u2122"                 # Trademark
)
_EMOJI_RE = re.compile(f"[{_EMOJI_RANGES}]")


def sanitize_filename(text: str, max_length: int = MAX_STEM_LENGTH) -> str:
    """Return a filesystem-safe stem from ``text`` (no extension).

    Spaces become ``_``; emoji/symbols/punctuation are removed; letters and
    digits (from any language) are kept.
    """
    if not text:
        return ""

    result = unicodedata.normalize("NFKC", text)
    result = _EMOJI_RE.sub("", result)

    kept: list[str] = []
    for ch in result:
        if ch.isspace():
            kept.append("_")
            continue
        category = unicodedata.category(ch)
        if category.startswith("L") or category.startswith("N"):
            kept.append(ch)
        # Everything else (symbols, punctuation, control chars) is dropped.

    result = "".join(kept)
    result = re.sub(r"_{2,}", "_", result)
    result = result.strip("_.")

    if len(result) > max_length:
        result = result[:max_length].rstrip("_.")

    return result


def fallback_stem(video_id: str) -> str:
    """Use the video ID as a safe stem when the title sanitizes to nothing."""
    return sanitize_filename(video_id) or "media"


def make_stem(prefix: str, title: str, suffix: str, video_id: str = "",
              max_length: int = MAX_STEM_LENGTH) -> str:
    """Compose the final stem: each piece is sanitized, then joined with ``_``.

    Empty pieces are dropped and the combined result truncated to
    ``max_length``. Falls back to the video ID (or ``"media"``) when empty.
    """
    parts = [sanitize_filename(piece, max_length) for piece in (prefix, title, suffix)]
    stem = "_".join(part for part in parts if part)

    if not stem and video_id:
        stem = fallback_stem(video_id)
    if not stem:
        stem = "media"

    if len(stem) > max_length:
        stem = stem[:max_length].rstrip("_.") or "media"

    if stem != f"{prefix}{title}{suffix}".strip():
        logger.debug("Filename sanitized: %r -> %r", f"{prefix}{title}{suffix}", stem)

    return stem
