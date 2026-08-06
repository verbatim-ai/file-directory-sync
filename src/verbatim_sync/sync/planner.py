"""Diff the scanned tree against local state to produce sync actions.

The planner is strictly read-only: it walks, filters, hashes and compares, but
writes nothing. That is what makes ``--dry-run`` honest — it runs exactly this
code and then stops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from verbatim_sync.config import Config
from verbatim_sync.db.models import FileRecord, SyncState
from verbatim_sync.db.repository import Repository
from verbatim_sync.logging_setup import get_logger
from verbatim_sync.scan import ScannedFile, SkipReason, apply_filters, hash_file, walk

logger = get_logger("sync.planner")

#: States an earlier run can leave behind mid-flight.
IN_FLIGHT_STATES = (
    SyncState.PENDING_UPLOAD,
    SyncState.UPLOADED,
    SyncState.COMMITTED,
)


class ActionKind(StrEnum):
    """What the engine will do about one file.

    UPLOAD   never pushed: init -> PUT -> commit
    REPLACE  content differs from what the backend holds: re-init -> PUT -> commit
    DELETE   gone from disk, and its document must go too
    RESUME   left mid-flight by an earlier run; continue where it stopped
    NOOP     the backend already holds this content
    """

    UPLOAD = "UPLOAD"
    REPLACE = "REPLACE"
    DELETE = "DELETE"
    RESUME = "RESUME"
    NOOP = "NOOP"


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    rel_path: str
    reason: str
    scanned: ScannedFile | None = None
    record: FileRecord | None = None
    content_type: str | None = None
    content_hash: str | None = None

    @property
    def document_id(self) -> str | None:
        return self.record.document_id if self.record else None


@dataclass(frozen=True)
class Skipped:
    rel_path: str
    reason: SkipReason
    detail: str | None


@dataclass
class SyncPlan:
    actions: list[Action] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    #: Records whose file is absent but which stay put — out of scope now,
    #: rather than deleted. Deleting them would discard a document because a
    #: size limit was tightened.
    out_of_scope: list[str] = field(default_factory=list)
    scanned_count: int = 0

    def of_kind(self, *kinds: ActionKind) -> list[Action]:
        return [action for action in self.actions if action.kind in kinds]

    @property
    def changes(self) -> list[Action]:
        """Everything that would touch the backend."""
        return [action for action in self.actions if action.kind is not ActionKind.NOOP]

    def counts(self) -> dict[str, int]:
        return {
            kind.value: len(self.of_kind(kind)) for kind in ActionKind
        } | {
            "SCANNED": self.scanned_count,
            "SKIPPED": len(self.skipped),
            "UNREADABLE": len(self.unreadable),
        }


def plan(config: Config, repository: Repository) -> SyncPlan:
    """Work out what the corpus needs to match the tree on disk."""
    corpus_id = config.corpus.id
    result = SyncPlan()
    seen: set[str] = set()

    for scanned in walk(config.source):
        result.scanned_count += 1
        gate = apply_filters(scanned.abs_path, scanned.size, config.filters)
        if not gate.accepted:
            assert gate.reason is not None
            result.skipped.append(
                Skipped(scanned.rel_path, gate.reason, gate.detail)
            )
            logger.debug(
                "Skipped %s: %s (%s)", scanned.rel_path, gate.reason, gate.detail
            )
            continue

        seen.add(scanned.rel_path)
        record = repository.get_file_by_rel_path(corpus_id, scanned.rel_path)

        content_hash = _hash(scanned, record)
        if content_hash is None:
            result.unreadable.append(scanned.rel_path)
            continue

        result.actions.append(
            _classify(scanned, record, gate.content_type, content_hash)
        )

    result.actions.extend(_plan_deletions(config, repository, seen, result))
    return result


def _hash(scanned: ScannedFile, record: FileRecord | None) -> str | None:
    """Hash the file, reusing the stored digest when nothing cheap moved."""
    if (
        record is not None
        and record.content_hash
        and not record.content_changed(scanned.size, scanned.mtime_ns)
    ):
        return record.content_hash
    try:
        return hash_file(scanned.abs_path)
    except OSError as exc:
        logger.error("Cannot read %s: %s", scanned.rel_path, exc)
        return None


def _classify(
    scanned: ScannedFile,
    record: FileRecord | None,
    content_type: str | None,
    content_hash: str,
) -> Action:
    def build(kind: ActionKind, reason: str) -> Action:
        return Action(
            kind=kind,
            rel_path=scanned.rel_path,
            reason=reason,
            scanned=scanned,
            record=record,
            content_type=content_type,
            content_hash=content_hash,
        )

    if record is None:
        return build(ActionKind.UPLOAD, "not in the local database")
    if record.document_id is None:
        return build(ActionKind.UPLOAD, "no document id recorded")
    if record.sync_state in IN_FLIGHT_STATES:
        return build(
            ActionKind.RESUME, f"left in {record.sync_state} by an earlier run"
        )
    if record.sync_state == SyncState.FAILED:
        return build(ActionKind.REPLACE, "previous attempt failed")
    if record.synced_hash is None:
        return build(ActionKind.REPLACE, "no synced content recorded")
    if content_hash != record.synced_hash:
        return build(ActionKind.REPLACE, "content changed since last sync")

    # Content matches what the backend holds. A newer mtime here means the file
    # was rewritten with identical bytes or merely touched; re-uploading would
    # be wasted work and the server would reject it as a duplicate anyway.
    if _modified_since_sync(scanned, record):
        return build(ActionKind.NOOP, "modified but content identical")
    return build(ActionKind.NOOP, "unchanged")


def _modified_since_sync(scanned: ScannedFile, record: FileRecord) -> bool:
    return record.mtime_ns is not None and scanned.mtime_ns > record.mtime_ns


def _plan_deletions(
    config: Config, repository: Repository, seen: set[str], result: SyncPlan
) -> list[Action]:
    """Rows with no matching file on disk.

    Only a file that genuinely no longer exists is deleted. One that is still
    there but no longer passes the filters is left alone and reported: dropping
    a document because ``max_file_size`` was lowered would be destructive and
    surprising.
    """
    actions: list[Action] = []
    for record in repository.all_files(config.corpus.id):
        if record.rel_path in seen:
            continue
        if (config.source.root_dir / record.rel_path).exists():
            result.out_of_scope.append(record.rel_path)
            logger.debug(
                "Still on disk but out of scope, leaving alone: %s", record.rel_path
            )
            continue
        actions.append(
            Action(
                kind=ActionKind.DELETE,
                rel_path=record.rel_path,
                reason="no longer on disk",
                record=record,
            )
        )
    return actions


def describe(action: Action) -> str:
    """One readable line per action, for the dry-run report."""
    detail = f" [{action.document_id}]" if action.document_id else ""
    size = f", {action.scanned.size} bytes" if action.scanned else ""
    return f"{action.kind.value:8} {action.rel_path}{detail} ({action.reason}{size})"


def log_plan(result: SyncPlan, dry_run: bool) -> None:
    """Report the plan at INFO, changes first."""
    prefix = "Would " if dry_run else ""
    for action in result.changes:
        logger.info("%s%s", prefix, describe(action))

    for skip in result.skipped:
        logger.info("SKIP     %s (%s: %s)", skip.rel_path, skip.reason, skip.detail)
    for rel_path in result.out_of_scope:
        logger.info(
            "KEEP     %s (still on disk but no longer in scope; not deleted)", rel_path
        )
    for rel_path in result.unreadable:
        logger.error("UNREAD   %s (could not be read)", rel_path)

    counts = result.counts()
    logger.info(
        "Plan: %d scanned, %d new, %d updated, %d removed, %d resumed, "
        "%d unchanged, %d skipped, %d unreadable",
        counts["SCANNED"],
        counts["UPLOAD"],
        counts["REPLACE"],
        counts["DELETE"],
        counts["RESUME"],
        counts["NOOP"],
        counts["SKIPPED"],
        counts["UNREADABLE"],
    )
    if dry_run and not result.changes:
        logger.info("Dry run: nothing to do, the corpus already matches the tree")
    elif dry_run:
        logger.info(
            "Dry run: %d change(s) identified, nothing was sent to the backend",
            len(result.changes),
        )


def relative_to_root(full_path: str, root_dir: Path) -> str | None:
    """POSIX rel_path for an absolute path inside the tree, else ``None``."""
    try:
        return Path(full_path).resolve().relative_to(root_dir.resolve()).as_posix()
    except (ValueError, OSError):
        return None
