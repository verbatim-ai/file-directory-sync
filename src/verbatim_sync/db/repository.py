"""Typed access to the local state database.

Every SQL statement in the project lives here, so the sync engine works in
terms of records and states rather than rows and column names.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from verbatim_sync.db.models import Event, FileRecord, RunStatus, SyncRun, SyncState
from verbatim_sync.errors import DbError


def utc_now() -> str:
    """Timestamps are stored as ISO-8601 UTC text, matching the API."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Repository:
    """Typed, thread-safe access to the local state database.

    The sync engine runs a pool of worker threads over one connection, so every
    statement is serialised here with a re-entrant lock. Methods that issue
    more than one statement hold the lock across all of them, which is what
    makes composites such as :meth:`upsert_file` atomic.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._lock = threading.RLock()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group several writes so a crash cannot leave a half-applied change."""
        with self._lock:
            try:
                self._connection.execute("BEGIN")
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    # Every helper below executes *and consumes* its result while holding the
    # lock. Handing a live cursor back to the caller would let another worker
    # run statements on the shared connection mid-fetch, which silently yields
    # empty result sets rather than raising.

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._lock:
            try:
                self._connection.execute(sql, params)
            except sqlite3.Error as exc:
                raise DbError(f"database operation failed: {exc}") from exc

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            try:
                return self._connection.execute(sql, params).fetchone()
            except sqlite3.Error as exc:
                raise DbError(f"database operation failed: {exc}") from exc

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            try:
                return self._connection.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                raise DbError(f"database operation failed: {exc}") from exc

    def _insert(self, sql: str, params: tuple[Any, ...] = ()) -> int | None:
        with self._lock:
            try:
                return self._connection.execute(sql, params).lastrowid
            except sqlite3.Error as exc:
                raise DbError(f"database operation failed: {exc}") from exc

    # ------------------------------------------------------------------ runs

    def start_run(
        self,
        *,
        mode: str,
        corpus_id: str,
        root_dir: str,
        dry_run: bool = False,
        config_path: str | None = None,
    ) -> int:
        run_id = self._insert(
            """
            INSERT INTO sync_run
                (started_at, status, mode, config_path, corpus_id, root_dir, dry_run)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                RunStatus.RUNNING.value,
                mode,
                config_path,
                corpus_id,
                root_dir,
                int(dry_run),
            ),
        )
        if run_id is None:  # pragma: no cover - sqlite always sets this on INSERT
            raise DbError("could not create sync_run row")
        return run_id

    def finish_run(
        self,
        run_id: int,
        *,
        status: RunStatus,
        counters: dict[str, int] | None = None,
        error: str | None = None,
    ) -> None:
        counters = counters or {}
        self._execute(
            """
            UPDATE sync_run SET
                finished_at    = ?,
                status         = ?,
                files_scanned  = ?,
                files_uploaded = ?,
                files_updated  = ?,
                files_deleted  = ?,
                files_skipped  = ?,
                files_failed   = ?,
                error          = ?
            WHERE id = ?
            """,
            (
                utc_now(),
                status.value,
                counters.get("scanned", 0),
                counters.get("uploaded", 0),
                counters.get("updated", 0),
                counters.get("deleted", 0),
                counters.get("skipped", 0),
                counters.get("failed", 0),
                error,
                run_id,
            ),
        )

    def get_run(self, run_id: int) -> SyncRun | None:
        row = self._fetchone("SELECT * FROM sync_run WHERE id = ?", (run_id,))
        return SyncRun.from_row(row) if row else None

    def last_runs(self, corpus_id: str, limit: int = 10) -> list[SyncRun]:
        rows = self._fetchall(
            """
            SELECT * FROM sync_run
            WHERE corpus_id = ?
            ORDER BY started_at DESC, id DESC
            LIMIT ?
            """,
            (corpus_id, limit),
        )
        return [SyncRun.from_row(row) for row in rows]

    def abandoned_runs(self, corpus_id: str) -> list[SyncRun]:
        """Runs still marked RUNNING — a previous invocation died mid-flight."""
        rows = self._fetchall(
            "SELECT * FROM sync_run WHERE corpus_id = ? AND status = ?",
            (corpus_id, RunStatus.RUNNING.value),
        )
        return [SyncRun.from_row(row) for row in rows]

    # ----------------------------------------------------------------- files

    def get_file_by_rel_path(self, corpus_id: str, rel_path: str) -> FileRecord | None:
        row = self._fetchone(
            "SELECT * FROM file WHERE corpus_id = ? AND rel_path = ?",
            (corpus_id, rel_path),
        )
        return FileRecord.from_row(row) if row else None

    def upsert_file(
        self,
        *,
        corpus_id: str,
        rel_path: str,
        abs_path: str,
        filename: str,
        content_type: str | None,
        size: int,
        mtime_ns: int,
        content_hash: str | None,
        run_id: int | None = None,
    ) -> FileRecord:
        """Record a file observed on disk, inserting or refreshing its row.

        Only the observed-on-disk columns are touched: ``document_id``,
        ``sync_state`` and the upload bookkeeping belong to the sync engine and
        are preserved here. Content that actually changed is signalled by the
        returned record differing from the stored hash — the caller decides
        what that means for ``sync_state``.
        """
        now = utc_now()
        # Insert and read-back must not interleave with another worker.
        with self._lock:
            self._execute(
                """
            INSERT INTO file (
                corpus_id, rel_path, abs_path, filename, content_type,
                size, mtime_ns, content_hash, sync_state,
                first_seen_at, last_seen_at, last_seen_run_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (corpus_id, rel_path) DO UPDATE SET
                abs_path         = excluded.abs_path,
                filename         = excluded.filename,
                content_type     = excluded.content_type,
                size             = excluded.size,
                mtime_ns         = excluded.mtime_ns,
                content_hash     = COALESCE(excluded.content_hash, file.content_hash),
                last_seen_at     = excluded.last_seen_at,
                last_seen_run_id = excluded.last_seen_run_id
            """,
                (
                    corpus_id,
                    rel_path,
                    abs_path,
                    filename,
                    content_type,
                    size,
                    mtime_ns,
                    content_hash,
                    SyncState.NEW.value,
                    now,
                    now,
                    run_id,
                ),
            )
            record = self.get_file_by_rel_path(corpus_id, rel_path)
        if record is None:  # pragma: no cover - the upsert guarantees a row
            raise DbError(f"file row vanished after upsert: {rel_path}")
        return record

    def mark_seen(self, file_id: int, run_id: int) -> None:
        self._execute(
            "UPDATE file SET last_seen_at = ?, last_seen_run_id = ? WHERE id = ?",
            (utc_now(), run_id, file_id),
        )

    def set_document_id(
        self,
        file_id: int,
        document_id: str,
        *,
        remote_status: str | None = None,
        upload_url: str | None = None,
        upload_url_expires_at: str | None = None,
    ) -> None:
        """Bind a local file to its document UID in the corpus."""
        self._execute(
            """
            UPDATE file SET
                document_id           = ?,
                remote_status         = COALESCE(?, remote_status),
                upload_url            = ?,
                upload_url_expires_at = ?
            WHERE id = ?
            """,
            (document_id, remote_status, upload_url, upload_url_expires_at, file_id),
        )

    def set_sync_state(
        self,
        file_id: int,
        state: SyncState,
        *,
        remote_status: str | None = None,
        last_error: str | None = None,
        synced: bool = False,
    ) -> None:
        self._execute(
            """
            UPDATE file SET
                sync_state     = ?,
                remote_status  = COALESCE(?, remote_status),
                last_error     = ?,
                last_synced_at = CASE WHEN ? THEN ? ELSE last_synced_at END
            WHERE id = ?
            """,
            (state.value, remote_status, last_error, int(synced), utc_now(), file_id),
        )

    def set_synced_hash(self, file_id: int, content_hash: str | None) -> None:
        """Record what the backend now holds. Only call after a commit succeeds."""
        self._execute(
            "UPDATE file SET synced_hash = ? WHERE id = ?", (content_hash, file_id)
        )

    def clear_upload_url(self, file_id: int) -> None:
        self._execute(
            "UPDATE file SET upload_url = NULL, upload_url_expires_at = NULL "
            "WHERE id = ?",
            (file_id,),
        )

    def increment_attempts(self, file_id: int, last_error: str | None = None) -> int:
        row = self._fetchone(
            "UPDATE file SET attempts = attempts + 1, last_error = ? "
            "WHERE id = ? RETURNING attempts",
            (last_error, file_id),
        )
        return int(row["attempts"]) if row else 0

    def reset_attempts(self, file_id: int) -> None:
        self._execute(
            "UPDATE file SET attempts = 0, last_error = NULL WHERE id = ?", (file_id,)
        )

    def files_not_seen_in_run(self, corpus_id: str, run_id: int) -> list[FileRecord]:
        """Rows absent from disk during ``run_id`` — deletion candidates."""
        rows = self._fetchall(
            """
            SELECT * FROM file
            WHERE corpus_id = ?
              AND (last_seen_run_id IS NULL OR last_seen_run_id != ?)
              AND sync_state != ?
            ORDER BY rel_path
            """,
            (corpus_id, run_id, SyncState.MISSING_LOCAL.value),
        )
        return [FileRecord.from_row(row) for row in rows]

    def files_in_state(self, corpus_id: str, *states: SyncState) -> list[FileRecord]:
        if not states:
            return []
        placeholders = ", ".join("?" for _ in states)
        rows = self._fetchall(
            f"SELECT * FROM file WHERE corpus_id = ? "
            f"AND sync_state IN ({placeholders}) ORDER BY rel_path",
            (corpus_id, *(state.value for state in states)),
        )
        return [FileRecord.from_row(row) for row in rows]

    def all_files(self, corpus_id: str) -> list[FileRecord]:
        rows = self._fetchall(
            "SELECT * FROM file WHERE corpus_id = ? ORDER BY rel_path", (corpus_id,)
        )
        return [FileRecord.from_row(row) for row in rows]

    def delete_file(self, file_id: int) -> None:
        self._execute("DELETE FROM file WHERE id = ?", (file_id,))

    def count_files(self, corpus_id: str) -> int:
        row = self._fetchone(
            "SELECT COUNT(*) AS n FROM file WHERE corpus_id = ?", (corpus_id,)
        )
        return int(row["n"])

    # ----------------------------------------------------------- statistics

    def count_by_state(self, corpus_id: str) -> dict[str, int]:
        rows = self._fetchall(
            "SELECT sync_state, COUNT(*) AS n FROM file WHERE corpus_id = ? "
            "GROUP BY sync_state",
            (corpus_id,),
        )
        return {row["sync_state"]: int(row["n"]) for row in rows}

    def bytes_in_state(self, corpus_id: str, state: SyncState) -> int:
        row = self._fetchone(
            "SELECT COALESCE(SUM(size), 0) AS total FROM file "
            "WHERE corpus_id = ? AND sync_state = ?",
            (corpus_id, state.value),
        )
        return int(row["total"])

    def last_synced_at(self, corpus_id: str) -> str | None:
        row = self._fetchone(
            "SELECT MAX(last_synced_at) AS ts FROM file WHERE corpus_id = ?",
            (corpus_id,),
        )
        return row["ts"]

    def last_run_of_modes(self, corpus_id: str, modes: tuple[str, ...]) -> SyncRun | None:
        """The most recent finished run in one of ``modes``.

        Used for figures a run computes rather than the file table stores, such
        as how many files the filters excluded.
        """
        if not modes:
            return None
        placeholders = ", ".join("?" for _ in modes)
        row = self._fetchone(
            f"SELECT * FROM sync_run WHERE corpus_id = ? AND mode IN ({placeholders}) "
            f"AND finished_at IS NOT NULL ORDER BY started_at DESC, id DESC LIMIT 1",
            (corpus_id, *modes),
        )
        return SyncRun.from_row(row) if row else None

    # ---------------------------------------------------------------- events

    def record_event(
        self,
        *,
        run_id: int | None,
        event_type: str,
        message: str | None = None,
        level: str = "INFO",
        file_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._execute(
            """
            INSERT INTO event (run_id, file_id, ts, level, event_type, message, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                file_id,
                utc_now(),
                level,
                event_type,
                message,
                json.dumps(details, default=str) if details else None,
            ),
        )

    def events_for_run(self, run_id: int, limit: int = 1000) -> list[Event]:
        rows = self._fetchall(
            "SELECT * FROM event WHERE run_id = ? ORDER BY id LIMIT ?",
            (run_id, limit),
        )
        return [Event.from_row(row) for row in rows]
