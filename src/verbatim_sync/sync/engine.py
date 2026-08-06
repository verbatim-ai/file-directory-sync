"""Execute a sync plan against the corpus.

Upload is a three-step flow, and the local state machine mirrors it so an
interrupted run can pick up where it stopped:

    init/re-init  -> PENDING_UPLOAD  (document id + presigned URL held)
    PUT bytes     -> UPLOADED
    commit        -> COMMITTED       (ingestion queued)
    poll READY    -> SYNCED

``synced_hash`` is advanced only once commit succeeds, so it always answers
"what does the corpus actually hold?" even if ingestion later fails.
"""

from __future__ import annotations

import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from verbatim_sync.api.documents import DocumentInit, DocumentsApi
from verbatim_sync.config import Config
from verbatim_sync.db.models import FileRecord, RemoteStatus, SyncState
from verbatim_sync.db.repository import Repository
from verbatim_sync.errors import ApiError
from verbatim_sync.logging_setup import get_logger
from verbatim_sync.scan import hash_file
from verbatim_sync.sync.planner import Action, ActionKind, SyncPlan, relative_to_root

logger = get_logger("sync.engine")

#: Metadata key carrying the file's full local path, so the corpus itself holds
#: enough to rebuild the local database. See `rebuild`.
FULLPATH_METADATA_KEY = "sync_fullpath"

#: A presigned URL this close to expiry is not worth trying.
_EXPIRY_MARGIN_SECONDS = 30

#: Pushing bytes cannot share the timeout sized for JSON control-plane calls.
_UPLOAD_TIMEOUT_FLOOR_SECONDS = 300.0


@dataclass
class SyncResult:
    counters: Counter[str]
    failures: list[str]

    @property
    def ok(self) -> bool:
        return not self.failures


def run(
    config: Config,
    repository: Repository,
    documents: DocumentsApi,
    run_id: int,
    plan: SyncPlan,
) -> SyncResult:
    """Apply every action in ``plan``. One failing file does not stop the run.

    Files are processed by a pool of ``sync.threads`` workers. Each action
    concerns exactly one file and one document, so they are independent; the
    shared pieces — the database, the token cache and the HTTP client — are
    each individually thread-safe. Counters are aggregated here in the calling
    thread rather than incremented from workers, which keeps them exact
    without another lock.
    """
    counters: Counter[str] = Counter(
        scanned=plan.scanned_count,
        skipped=len(plan.skipped),
        failed=len(plan.unreadable),
    )
    failures = list(plan.unreadable)

    # NOOPs touch nothing remote; doing them inline avoids pool overhead.
    pending = []
    for action in plan.actions:
        if action.kind is ActionKind.NOOP:
            counters["unchanged"] += 1
            _mark_seen(repository, action, run_id)
        else:
            pending.append(action)

    if not pending:
        return SyncResult(counters=counters, failures=failures)

    workers = max(1, min(config.sync.threads, len(pending)))
    logger.info(
        "Applying %d change(s) with %d worker thread(s)", len(pending), workers
    )

    handlers = {
        ActionKind.UPLOAD: _upload,
        ActionKind.REPLACE: _replace,
        ActionKind.RESUME: _resume,
        ActionKind.DELETE: _delete,
    }

    def apply(action: Action) -> tuple[Action, str | None]:
        handler = handlers[action.kind]
        try:
            handler(config, repository, documents, run_id, action)
        except (ApiError, OSError) as exc:
            logger.error("%s failed for %s: %s", action.kind, action.rel_path, exc)
            _record_failure(config, repository, run_id, action, str(exc))
            return action, str(exc)
        return action, None

    if workers == 1:
        outcomes = [apply(action) for action in pending]
    else:
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="verbatim-sync"
        ) as pool:
            # Anything other than ApiError/OSError is a bug, not a bad file, and
            # surfaces here rather than being swallowed per worker.
            outcomes = [
                future.result()
                for future in as_completed(
                    [pool.submit(apply, action) for action in pending]
                )
            ]

    for action, error in outcomes:
        if error is None:
            counters[_COUNTER_FOR[action.kind]] += 1
        else:
            counters["failed"] += 1
            failures.append(action.rel_path)

    # Worker completion order is nondeterministic; sort so the run summary and
    # the recorded error read the same way twice.
    failures.sort()
    return SyncResult(counters=counters, failures=failures)


_COUNTER_FOR = {
    ActionKind.UPLOAD: "uploaded",
    ActionKind.REPLACE: "updated",
    ActionKind.RESUME: "updated",
    ActionKind.DELETE: "deleted",
}


# --------------------------------------------------------------------- actions


def _upload(
    config: Config,
    repository: Repository,
    documents: DocumentsApi,
    run_id: int,
    action: Action,
) -> None:
    """A file the corpus has never seen: init, push, commit."""
    scanned = action.scanned
    assert scanned is not None

    record = _record_scan(repository, config, action, run_id)
    logger.info("Uploading %s (%s, %d bytes)", action.rel_path, action.content_type, scanned.size)

    init = documents.init_upload(
        corpus_id=config.corpus.id,
        filename=scanned.filename,
        content_type=action.content_type or "application/octet-stream",
        lang=config.corpus.lang,
        provider=config.corpus.provider,
        doc_create=scanned.created_at,
        doc_update=scanned.modified_at,
        metadata={FULLPATH_METADATA_KEY: str(scanned.abs_path)},
    )
    _store_init(repository, record.id, init)
    _push_and_commit(config, repository, documents, run_id, action, record.id, init)


def _replace(
    config: Config,
    repository: Repository,
    documents: DocumentsApi,
    run_id: int,
    action: Action,
) -> None:
    """A file whose content moved: re-init the same document, push, commit."""
    scanned = action.scanned
    assert scanned is not None
    assert action.record is not None
    document_id = action.record.document_id
    assert document_id is not None

    record = _record_scan(repository, config, action, run_id)
    logger.info("Updating %s [%s] (%s)", action.rel_path, document_id, action.reason)

    try:
        init = documents.reinit_upload(document_id)
    except ApiError as exc:
        # 404: the document was removed in the backoffice. 409: it is stuck in
        # a non-replaceable state. Either way the mapping is stale, so start
        # over rather than leaving the file permanently unsyncable.
        if exc.status_code not in (404, 409):
            raise
        logger.warning(
            "Cannot re-init %s [%s] (HTTP %s); uploading as a new document",
            action.rel_path,
            document_id,
            exc.status_code,
        )
        repository.set_synced_hash(record.id, None)
        _upload(config, repository, documents, run_id, action)
        return

    _store_init(repository, record.id, init)
    _push_and_commit(config, repository, documents, run_id, action, record.id, init)


def _resume(
    config: Config,
    repository: Repository,
    documents: DocumentsApi,
    run_id: int,
    action: Action,
) -> None:
    """Continue a transfer an earlier run left mid-flight."""
    record = action.record
    assert record is not None

    if record.sync_state == SyncState.COMMITTED:
        # Bytes are in and ingestion was queued; only the outcome is unknown.
        logger.info("Resuming %s [%s]: polling ingestion", action.rel_path, record.document_id)
        _finalise(config, repository, run_id, action, record.id, documents)
        return

    if record.sync_state == SyncState.UPLOADED:
        logger.info("Resuming %s [%s]: committing", action.rel_path, record.document_id)
        _commit_and_finalise(
            config, repository, documents, run_id, action, record.id, record.document_id
        )
        return

    # PENDING_UPLOAD: the presigned URL may still be usable.
    if record.upload_url and _url_is_live(record.upload_url_expires_at):
        logger.info("Resuming %s [%s]: re-using the presigned URL", action.rel_path, record.document_id)
        _push(config, documents, action, record.upload_url)
        repository.set_sync_state(record.id, SyncState.UPLOADED)
        repository.clear_upload_url(record.id)
        _commit_and_finalise(
            config, repository, documents, run_id, action, record.id, record.document_id
        )
        return

    logger.info(
        "Resuming %s [%s]: presigned URL expired, re-initialising",
        action.rel_path,
        record.document_id,
    )
    _replace(config, repository, documents, run_id, action)


def _delete(
    config: Config,
    repository: Repository,
    documents: DocumentsApi,
    run_id: int,
    action: Action,
) -> None:
    """A file that vanished: drop its document, then forget the row."""
    record = action.record
    assert record is not None

    if record.document_id:
        logger.info("Deleting %s [%s] from the corpus", action.rel_path, record.document_id)
        try:
            documents.delete(record.document_id)
        except ApiError as exc:
            if exc.status_code != 404:
                raise
            # Already gone on the backend; the row is what is stale.
            logger.warning(
                "Document %s for %s was already absent", record.document_id, action.rel_path
            )
    else:
        logger.info("Removing %s from the local database (never uploaded)", action.rel_path)

    repository.record_event(
        run_id=run_id,
        event_type="file.deleted",
        message=action.rel_path,
        file_id=record.id,
        details={"document_id": record.document_id},
    )
    repository.delete_file(record.id)


# ------------------------------------------------------------------- transfer


def _push_and_commit(
    config: Config,
    repository: Repository,
    documents: DocumentsApi,
    run_id: int,
    action: Action,
    file_id: int,
    init: DocumentInit,
) -> None:
    _push(config, documents, action, init.upload_url)
    repository.set_sync_state(file_id, SyncState.UPLOADED)
    repository.clear_upload_url(file_id)
    _commit_and_finalise(
        config, repository, documents, run_id, action, file_id, init.document.id
    )


def _push(
    config: Config, documents: DocumentsApi, action: Action, upload_url: str
) -> None:
    """Stream the file to storage. The URL is single use — never log it."""
    scanned = action.scanned
    assert scanned is not None
    content_type = action.content_type or "application/octet-stream"

    with scanned.abs_path.open("rb") as handle:
        documents.upload_content(
            upload_url,
            handle,
            content_type,
            timeout=max(config.api.timeout_seconds, _UPLOAD_TIMEOUT_FLOOR_SECONDS),
        )
    logger.debug("Pushed %d bytes for %s", scanned.size, action.rel_path)


def _commit_and_finalise(
    config: Config,
    repository: Repository,
    documents: DocumentsApi,
    run_id: int,
    action: Action,
    file_id: int,
    document_id: str | None,
) -> None:
    assert document_id is not None
    try:
        document = documents.commit(document_id)
        remote_status = document.status
    except ApiError as exc:
        if exc.status_code != 409:
            raise
        # The corpus already holds this content. Nothing to ingest, and the
        # mapping is correct — treat it as synced rather than retrying forever.
        logger.warning(
            "%s is already present in the corpus (HTTP 409); recording as synced",
            action.rel_path,
        )
        _mark_synced(repository, run_id, action, file_id, RemoteStatus.READY)
        return

    repository.set_sync_state(file_id, SyncState.COMMITTED, remote_status=remote_status)
    # The bytes are in the backend now, whatever ingestion decides next.
    repository.set_synced_hash(file_id, action.content_hash)
    _finalise(config, repository, run_id, action, file_id, documents, document_id)


def _finalise(
    config: Config,
    repository: Repository,
    run_id: int,
    action: Action,
    file_id: int,
    documents: DocumentsApi,
    document_id: str | None = None,
) -> None:
    """Wait for ingestion to settle, if the config asks us to."""
    document_id = document_id or (action.record.document_id if action.record else None)
    assert document_id is not None

    if not config.sync.poll_status:
        _mark_synced(repository, run_id, action, file_id, RemoteStatus.PROCESSING)
        return

    status = _poll(documents, document_id, config.sync.poll_timeout_seconds)
    if status is None:
        logger.warning(
            "%s [%s] still ingesting after %ds; leaving it to the next run",
            action.rel_path,
            document_id,
            config.sync.poll_timeout_seconds,
        )
        return

    if status.status == RemoteStatus.READY:
        _mark_synced(repository, run_id, action, file_id, RemoteStatus.READY)
        return

    raise ApiError(
        f"ingestion failed for {action.rel_path} [{document_id}]: "
        f"{status.status_msg or 'no reason given'}"
    )


def _poll(documents: DocumentsApi, document_id: str, timeout_seconds: int):
    """Poll until the document reaches a terminal status, or time runs out."""
    deadline = time.monotonic() + timeout_seconds
    delay = 1.0
    while True:
        status = documents.status(document_id)
        if status.is_terminal:
            return status
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        delay = min(delay * 1.5, 15.0)


# -------------------------------------------------------------------- helpers


def _record_scan(
    repository: Repository, config: Config, action: Action, run_id: int
) -> FileRecord:
    """Make sure a row exists for this file, carrying what the scan observed."""
    scanned = action.scanned
    assert scanned is not None
    return repository.upsert_file(
        corpus_id=config.corpus.id,
        rel_path=scanned.rel_path,
        abs_path=str(scanned.abs_path),
        filename=scanned.filename,
        content_type=action.content_type,
        size=scanned.size,
        mtime_ns=scanned.mtime_ns,
        content_hash=action.content_hash,
        run_id=run_id,
    )


def _mark_seen(repository: Repository, action: Action, run_id: int) -> None:
    if action.record is not None:
        repository.mark_seen(action.record.id, run_id)


def _store_init(repository: Repository, file_id: int, init: DocumentInit) -> None:
    repository.set_document_id(
        file_id,
        init.document.id,
        remote_status=init.document.status,
        upload_url=init.upload_url,
        upload_url_expires_at=init.expires_at,
    )
    repository.set_sync_state(file_id, SyncState.PENDING_UPLOAD)


def _mark_synced(
    repository: Repository,
    run_id: int,
    action: Action,
    file_id: int,
    remote_status: RemoteStatus,
) -> None:
    repository.set_synced_hash(file_id, action.content_hash)
    repository.set_sync_state(
        file_id, SyncState.SYNCED, remote_status=remote_status, synced=True
    )
    repository.reset_attempts(file_id)
    repository.record_event(
        run_id=run_id,
        event_type="file.synced",
        message=action.rel_path,
        file_id=file_id,
        details={
            "document_id": action.document_id,
            "content_hash": action.content_hash,
            "action": action.kind.value,
        },
    )
    logger.info("Synced %s", action.rel_path)


def _record_failure(
    config: Config, repository: Repository, run_id: int, action: Action, error: str
) -> None:
    # A brand new file has no record on the action, but _record_scan will have
    # created one before the failure — look it up so the error is not lost.
    record = action.record or repository.get_file_by_rel_path(
        config.corpus.id, action.rel_path
    )
    if record is None:
        return
    repository.increment_attempts(record.id, error)
    repository.set_sync_state(record.id, SyncState.FAILED, last_error=error)
    repository.record_event(
        run_id=run_id,
        level="ERROR",
        event_type="file.failed",
        message=f"{action.rel_path}: {error}",
        file_id=record.id,
    )


def _url_is_live(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        deadline = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return (deadline - datetime.now(UTC)).total_seconds() > _EXPIRY_MARGIN_SECONDS


# -------------------------------------------------------------------- rebuild


def rebuild(
    config: Config, repository: Repository, documents: DocumentsApi, run_id: int
) -> Counter[str]:
    """Rebuild the local database from what the corpus already holds.

    Every document uploaded by this job carries its full local path in the
    ``sync_fullpath`` metadata key, which is what makes recovery possible after
    the local database is lost or corrupted.

    A document whose local file is present and the same size is recorded as
    synced. One whose size differs is recorded with its document id but no
    synced hash, so the next run replaces it rather than silently assuming the
    two agree.
    """
    counters: Counter[str] = Counter()
    root = config.source.root_dir

    logger.info("Rebuilding local state for corpus %s from the backend", config.corpus.id)
    for document in documents.iter_all(config.corpus.id):
        counters["documents"] += 1
        full_path = (document.metadata or {}).get(FULLPATH_METADATA_KEY)

        if not full_path:
            counters["no_metadata"] += 1
            logger.warning(
                "Document %s (%s) has no %s metadata; not managed by this job",
                document.id,
                document.filename,
                FULLPATH_METADATA_KEY,
            )
            continue

        rel_path = relative_to_root(str(full_path), root)
        if rel_path is None:
            counters["outside_tree"] += 1
            logger.warning(
                "Document %s points at %s, outside %s; skipping",
                document.id,
                full_path,
                root,
            )
            continue

        path = Path(full_path)
        if not path.is_file():
            counters["missing_local"] += 1
            logger.warning(
                "Document %s points at %s, which does not exist locally; "
                "leaving it in the corpus",
                document.id,
                full_path,
            )
            continue

        stat_result = path.stat()
        try:
            content_hash = hash_file(path)
        except OSError as exc:
            counters["unreadable"] += 1
            logger.error("Cannot read %s: %s", full_path, exc)
            continue

        record = repository.upsert_file(
            corpus_id=config.corpus.id,
            rel_path=rel_path,
            abs_path=str(path),
            filename=path.name,
            content_type=document.content_type,
            size=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
            content_hash=content_hash,
            run_id=run_id,
        )
        repository.set_document_id(
            record.id, document.id, remote_status=document.status
        )

        # The backend reports size only once ingestion has run.
        size_matches = document.size is None or document.size == stat_result.st_size
        if size_matches:
            counters["restored"] += 1
            repository.set_synced_hash(record.id, content_hash)
            repository.set_sync_state(
                record.id,
                SyncState.SYNCED,
                remote_status=document.status,
                synced=True,
            )
            logger.info("Restored %s [%s] as synced", rel_path, document.id)
        else:
            counters["stale"] += 1
            repository.set_synced_hash(record.id, None)
            repository.set_sync_state(
                record.id, SyncState.NEW, remote_status=document.status
            )
            logger.info(
                "Restored %s [%s]; local size %d differs from the corpus's %d, "
                "so the next run will update it",
                rel_path,
                document.id,
                stat_result.st_size,
                document.size,
            )

        repository.record_event(
            run_id=run_id,
            event_type="db.restored",
            message=rel_path,
            file_id=record.id,
            details={"document_id": document.id, "synced": size_matches},
        )

    logger.info(
        "Rebuild finished: %d document(s) seen, %d restored as synced, %d needing "
        "update, %d without %s, %d missing locally, %d outside the tree",
        counters["documents"],
        counters["restored"],
        counters["stale"],
        counters["no_metadata"],
        FULLPATH_METADATA_KEY,
        counters["missing_local"],
        counters["outside_tree"],
    )
    return counters
