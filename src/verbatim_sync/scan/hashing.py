"""Content hashing for change detection.

SHA-256 of the file content is the authoritative answer to "did this change?",
and it mirrors what the platform does on commit, where a document whose content
already exists in the corpus is rejected as a duplicate.

Hashing costs a full read, so callers use the cheap ``(size, mtime_ns)`` pair
first and only fall through to here when that pair moved.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK_SIZE = 1024 * 1024


def hash_file(path: str | Path, chunk_size: int = CHUNK_SIZE) -> str:
    """Streaming SHA-256 of a file, as lowercase hex."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
