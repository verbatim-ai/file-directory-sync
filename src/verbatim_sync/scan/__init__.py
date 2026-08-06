"""Discover and qualify local files."""

from verbatim_sync.scan.filters import (
    FilterResult,
    SkipReason,
    apply_filters,
    resolve_content_type,
)
from verbatim_sync.scan.hashing import hash_file
from verbatim_sync.scan.walker import ScannedFile, matches_any, walk

__all__ = [
    "FilterResult",
    "ScannedFile",
    "SkipReason",
    "apply_filters",
    "hash_file",
    "matches_any",
    "resolve_content_type",
    "walk",
]
