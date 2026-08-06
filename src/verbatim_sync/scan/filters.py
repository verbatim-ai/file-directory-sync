"""Decide whether a scanned file is in scope, and say why when it is not."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from verbatim_sync.config import FiltersConfig

# mimetypes is driven by the host's mime.types and disagrees between machines
# for exactly the formats that matter here, so the platform-supported types are
# pinned explicitly and consulted first.
EXTENSION_CONTENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".odt": "application/vnd.oasis.opendocument.text",
    ".rtf": "application/rtf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".htm": "text/html",
    ".html": "text/html",
    ".csv": "text/csv",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    ".xls": "application/vnd.ms-excel",
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
}


class SkipReason(StrEnum):
    """Why a file was left out. Logged verbatim, so each value stands alone."""

    UNKNOWN_CONTENT_TYPE = "content type could not be determined"
    CONTENT_TYPE_NOT_ALLOWED = "content type is not in filters.content_types"
    TOO_LARGE = "file is larger than filters.max_file_size"
    TOO_SMALL = "file is smaller than filters.min_file_size"


@dataclass(frozen=True)
class FilterResult:
    accepted: bool
    content_type: str | None = None
    reason: SkipReason | None = None
    detail: str | None = None


def resolve_content_type(path: str | Path) -> str | None:
    """Best-effort MIME type from the filename extension."""
    suffix = Path(path).suffix.lower()
    if suffix in EXTENSION_CONTENT_TYPES:
        return EXTENSION_CONTENT_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed.lower() if guessed else None


def apply_filters(path: str | Path, size: int, config: FiltersConfig) -> FilterResult:
    """Gate a file on content type and size.

    Size is checked before type so an oversized file reports the size problem,
    which is the one the operator can act on.
    """
    if size > config.max_file_size:
        return FilterResult(
            accepted=False,
            reason=SkipReason.TOO_LARGE,
            detail=f"{size} bytes > {config.max_file_size} bytes",
        )
    if size < config.min_file_size:
        return FilterResult(
            accepted=False,
            reason=SkipReason.TOO_SMALL,
            detail=f"{size} bytes < {config.min_file_size} bytes",
        )

    content_type = resolve_content_type(path)
    if content_type is None:
        return FilterResult(
            accepted=False,
            reason=SkipReason.UNKNOWN_CONTENT_TYPE,
            detail=f"unrecognised extension {Path(path).suffix or '(none)'!r}",
        )

    # An empty content_types list means "accept whatever the platform accepts";
    # --check is where that list is validated against GET /v1/doc/accept.
    if config.content_types and content_type not in config.content_types:
        return FilterResult(
            accepted=False,
            content_type=content_type,
            reason=SkipReason.CONTENT_TYPE_NOT_ALLOWED,
            detail=content_type,
        )

    return FilterResult(accepted=True, content_type=content_type)
