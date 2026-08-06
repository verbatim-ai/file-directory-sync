"""Row shapes and enumerations for the local state database."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class RemoteStatus(StrEnum):
    """Document lifecycle statuses as reported by the platform."""

    AWAITING_UPLOAD = "AWAITING_UPLOAD"
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in (RemoteStatus.READY, RemoteStatus.FAILED)


class SyncState(StrEnum):
    """Where a local file sits in this job's own state machine.

    NEW             discovered and recorded, nothing sent yet
    PENDING_UPLOAD  init done, presigned URL held, bytes not PUT yet
    UPLOADED        bytes PUT to storage, commit not called yet
    COMMITTED       commit accepted, ingestion in flight
    SYNCED          platform reports READY for the current content hash
    FAILED          gave up on this file; see last_error / attempts
    MISSING_LOCAL   the local file is gone; awaiting remote deletion
    """

    NEW = "NEW"
    PENDING_UPLOAD = "PENDING_UPLOAD"
    UPLOADED = "UPLOADED"
    COMMITTED = "COMMITTED"
    SYNCED = "SYNCED"
    FAILED = "FAILED"
    MISSING_LOCAL = "MISSING_LOCAL"


@dataclass(frozen=True)
class SyncRun:
    id: int
    started_at: str
    status: str
    mode: str
    corpus_id: str
    root_dir: str
    dry_run: bool
    finished_at: str | None = None
    config_path: str | None = None
    files_scanned: int = 0
    files_uploaded: int = 0
    files_updated: int = 0
    files_deleted: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    error: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> SyncRun:
        return cls(
            id=row["id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            status=row["status"],
            mode=row["mode"],
            config_path=row["config_path"],
            corpus_id=row["corpus_id"],
            root_dir=row["root_dir"],
            dry_run=bool(row["dry_run"]),
            files_scanned=row["files_scanned"],
            files_uploaded=row["files_uploaded"],
            files_updated=row["files_updated"],
            files_deleted=row["files_deleted"],
            files_skipped=row["files_skipped"],
            files_failed=row["files_failed"],
            error=row["error"],
        )


@dataclass(frozen=True)
class FileRecord:
    """A local file and the corpus document it maps to."""

    id: int
    corpus_id: str
    rel_path: str
    abs_path: str
    filename: str
    sync_state: str
    first_seen_at: str
    last_seen_at: str
    content_type: str | None = None
    size: int | None = None
    mtime_ns: int | None = None
    #: Hash of what is on disk, refreshed on every scan.
    content_hash: str | None = None
    #: Hash of what the backend holds, advanced only on a successful commit.
    synced_hash: str | None = None
    document_id: str | None = None
    remote_status: str | None = None
    upload_url: str | None = None
    upload_url_expires_at: str | None = None
    attempts: int = 0
    last_error: str | None = None
    last_synced_at: str | None = None
    last_seen_run_id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> FileRecord:
        return cls(
            id=row["id"],
            corpus_id=row["corpus_id"],
            rel_path=row["rel_path"],
            abs_path=row["abs_path"],
            filename=row["filename"],
            content_type=row["content_type"],
            size=row["size"],
            mtime_ns=row["mtime_ns"],
            content_hash=row["content_hash"],
            synced_hash=row["synced_hash"],
            document_id=row["document_id"],
            remote_status=row["remote_status"],
            sync_state=row["sync_state"],
            upload_url=row["upload_url"],
            upload_url_expires_at=row["upload_url_expires_at"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            last_synced_at=row["last_synced_at"],
            last_seen_run_id=row["last_seen_run_id"],
        )

    def content_changed(self, size: int, mtime_ns: int) -> bool:
        """Fast path: has anything cheap about the file moved since last run?

        A False here means the expensive hash can be skipped.
        """
        return self.size != size or self.mtime_ns != mtime_ns


@dataclass(frozen=True)
class Event:
    id: int
    run_id: int | None
    file_id: int | None
    ts: str
    level: str
    event_type: str
    message: str | None
    details: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Event:
        return cls(
            id=row["id"],
            run_id=row["run_id"],
            file_id=row["file_id"],
            ts=row["ts"],
            level=row["level"],
            event_type=row["event_type"],
            message=row["message"],
            details=row["details"],
        )
