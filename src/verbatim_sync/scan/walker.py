"""Walk the configured directory tree."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from verbatim_sync.config import SourceConfig
from verbatim_sync.logging_setup import get_logger

logger = get_logger("scan.walker")


@dataclass(frozen=True)
class ScannedFile:
    """A candidate file, before content-type and size filtering."""

    abs_path: Path
    rel_path: str  # POSIX separators, relative to root_dir
    size: int
    mtime_ns: int
    ctime_ns: int = 0

    @property
    def filename(self) -> str:
        return self.abs_path.name

    @property
    def modified_at(self) -> str:
        """Last-modified time as ISO-8601 UTC, for the API's ``docUpdate``."""
        return _iso(self.mtime_ns)

    @property
    def created_at(self) -> str:
        """Creation time as ISO-8601 UTC, for the API's ``docCreate``."""
        return _iso(self.ctime_ns or self.mtime_ns)


def _iso(nanoseconds: int) -> str:
    return (
        datetime.fromtimestamp(nanoseconds / 1_000_000_000, UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def matches_any(rel_path: str, patterns: tuple[str, ...]) -> bool:
    """Glob match against the path relative to the root.

    ``**/`` is treated as "at any depth", so ``**/*.pdf`` matches both
    ``a.pdf`` and ``deep/nested/a.pdf`` — the behaviour a reader of the config
    file expects, which plain fnmatch does not give.
    """
    for pattern in patterns:
        if fnmatch(rel_path, pattern):
            return True
        if pattern.startswith("**/") and fnmatch(rel_path, pattern[3:]):
            return True
        # A bare directory pattern such as "archive" should also exclude
        # everything beneath it.
        if fnmatch(rel_path, f"{pattern.rstrip('/')}/*"):
            return True
    return False


def walk(config: SourceConfig) -> Iterator[ScannedFile]:
    """Yield every file under ``root_dir`` that survives include/exclude.

    Directories matching an exclude pattern are not descended into at all, so
    excluding a large tree costs nothing. Unreadable entries are logged and
    skipped rather than aborting the run.
    """
    root = config.root_dir
    visited_dirs: set[tuple[int, int]] = set()
    stack: list[Path] = [root]

    while stack:
        current = stack.pop()

        if config.follow_symlinks:
            # Following symlinks can revisit a directory forever; identity by
            # (st_dev, st_ino) breaks the cycle.
            try:
                stat_result = current.stat()
            except OSError as exc:
                logger.warning("Cannot stat directory %s: %s", current, exc)
                continue
            key = (stat_result.st_dev, stat_result.st_ino)
            if key in visited_dirs:
                logger.warning("Skipping symlink loop at %s", current)
                continue
            visited_dirs.add(key)

        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            logger.warning("Cannot read directory %s: %s", current, exc)
            continue

        for entry in entries:
            entry_path = Path(entry.path)
            rel_path = str(PurePosixPath(entry_path.relative_to(root)))

            if config.exclude and matches_any(rel_path, config.exclude):
                logger.debug("Excluded by pattern: %s", rel_path)
                continue

            try:
                is_dir = entry.is_dir(follow_symlinks=config.follow_symlinks)
                is_file = entry.is_file(follow_symlinks=config.follow_symlinks)
            except OSError as exc:
                logger.warning("Cannot inspect %s: %s", rel_path, exc)
                continue

            if is_dir:
                stack.append(entry_path)
                continue
            if not is_file:
                logger.debug("Not a regular file, skipping: %s", rel_path)
                continue

            if config.include and not matches_any(rel_path, config.include):
                logger.debug("Not matched by include patterns: %s", rel_path)
                continue

            try:
                stat_result = entry.stat(follow_symlinks=config.follow_symlinks)
            except OSError as exc:
                logger.warning("Cannot stat %s: %s", rel_path, exc)
                continue

            yield ScannedFile(
                abs_path=entry_path,
                rel_path=rel_path,
                size=stat_result.st_size,
                mtime_ns=stat_result.st_mtime_ns,
                # birthtime where the platform records it (macOS, some BSDs),
                # otherwise ctime, which is the closest thing available.
                ctime_ns=getattr(
                    stat_result, "st_birthtime_ns", stat_result.st_ctime_ns
                ),
            )
