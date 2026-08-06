"""Command line entry point.

Run as a cron job or by hand, always with a configuration file:

    verbatim-sync --config /etc/verbatim/sync.toml

Exit codes are meaningful to cron: 0 success, 1 runtime failure, 2 bad config.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from pathlib import Path

from verbatim_sync import __version__, stats
from verbatim_sync.api.auth import Key, TokenProvider
from verbatim_sync.api.client import ApiClient
from verbatim_sync.api.documents import DocumentsApi
from verbatim_sync.config import Config, load_config
from verbatim_sync.db import Repository, connect, migrate, schema_version
from verbatim_sync.db.models import RunStatus
from verbatim_sync.errors import ConfigError, VerbatimSyncError
from verbatim_sync.logging_setup import configure_logging, get_logger
from verbatim_sync.scan import apply_filters, hash_file, walk
from verbatim_sync.sync import engine, planner

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 2

logger = get_logger("cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verbatim-sync",
        description="Synchronise a local directory tree into a Verbatim AI corpus.",
    )
    parser.add_argument(
        "--config",
        "-c",
        required=True,
        metavar="PATH",
        help="path to the TOML configuration file",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--init-db",
        action="store_true",
        help="create or migrate the local state database, then exit",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "validate the configuration, sign a token and probe the API "
            "(whoami + accepted content types), then exit"
        ),
    )
    mode.add_argument(
        "--scan-only",
        action="store_true",
        help=(
            "walk the tree, apply the filters and record state without "
            "contacting the corpus"
        ),
    )
    mode.add_argument(
        "--rebuild-db",
        action="store_true",
        help=(
            "rebuild the local database from the corpus, matching documents to "
            "local files by their sync_fullpath metadata"
        ),
    )
    mode.add_argument(
        "--stats",
        action="store_true",
        help="print sync statistics from the local database, then exit",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "report what the sync would change and exit without sending "
            "anything to the corpus"
        ),
    )
    parser.add_argument(
        "--log-file", metavar="PATH", help="override logging.file from the config"
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="force DEBUG level regardless of logging.level",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    logging_cfg = config.logging
    if args.verbose:
        logging_cfg = dataclasses.replace(logging_cfg, level="DEBUG")
    if args.log_file:
        logging_cfg = dataclasses.replace(logging_cfg, file=Path(args.log_file).resolve())

    sync_cfg = config.sync
    if args.dry_run:
        sync_cfg = dataclasses.replace(sync_cfg, dry_run=True)

    if sync_cfg.dry_run:
        # A dry run exists to be read, so it always reaches the console even
        # when the config silences it for unattended runs.
        logging_cfg = dataclasses.replace(logging_cfg, console=True)

    return dataclasses.replace(config, logging=logging_cfg, sync=sync_cfg)


def _mode_name(args: argparse.Namespace) -> str:
    if args.init_db:
        return "init-db"
    if args.check:
        return "check"
    if args.scan_only:
        return "scan-only"
    if args.rebuild_db:
        return "rebuild-db"
    if args.stats:
        return "stats"
    return "dry-run" if args.dry_run else "sync"


def _build_token_provider(config: Config) -> TokenProvider:
    key = Key.from_keystore(
        config.api.keys_dir,
        config.api.key_filename,
        config.api.key_id,
        config.api.organization_id,
    )
    return TokenProvider(key, ttl_minutes=config.api.token_ttl_minutes)


def _build_documents_api(config: Config) -> tuple[ApiClient, DocumentsApi]:
    client = ApiClient(
        config.api.base_url,
        _build_token_provider(config),
        timeout=config.api.timeout_seconds,
        max_retries=config.api.max_retries,
    )
    return client, DocumentsApi(client)


def run_init_db(config: Config) -> int:
    connection = connect(config.database.path)
    try:
        before = schema_version(connection)
        after = migrate(connection)
        if before == after:
            logger.info(
                "Database %s already at schema version %d", config.database.path, after
            )
        else:
            logger.info(
                "Database %s migrated from schema version %d to %d",
                config.database.path,
                before,
                after,
            )
    finally:
        connection.close()
    return EXIT_OK


def run_check(config: Config) -> int:
    """Prove the configuration works end to end without changing anything."""
    logger.info("Configuration file: %s", config.path)
    logger.info("Corpus: %s", config.corpus.id)
    logger.info("Source tree: %s", config.source.root_dir)
    logger.info(
        "API: %s (timeout %dms, %d retries)",
        config.api.base_url,
        config.api.timeout_ms,
        config.api.max_retries,
    )

    connection = connect(config.database.path)
    try:
        version = migrate(connection)
        logger.info("Database %s ready at schema version %d", config.database.path, version)
    finally:
        connection.close()

    tokens = _build_token_provider(config)
    logger.info(
        "Private key %s loaded: kid=%s oid=%s",
        config.api.private_key_path,
        tokens.key_id,
        tokens.organization_id,
    )

    # Verifying against the local .pub turns a mismatched key pair into a
    # precise error instead of an opaque 403 on the first call. A missing .pub
    # only means the check is unavailable; a failing one propagates.
    claims = tokens.verify_locally()
    if claims is None:
        logger.warning(
            "No public key at %s, skipping the local key self-check",
            config.api.public_key_path,
        )
    else:
        logger.info(
            "Token verifies against %s: iss=%s oid=%s ttl=%ds",
            config.api.public_key_path.name,
            claims["iss"],
            claims["oid"],
            int(claims["exp"]) - int(claims["iat"]),
        )

    client, documents = _build_documents_api(config)
    try:
        identity = documents.whoami()
        logger.info("Authenticated: %s", identity)

        accepted = documents.accepted_content_types()
        logger.info("Platform accepts %d content type(s): %s", len(accepted), ", ".join(accepted))

        unsupported = [ct for ct in config.filters.content_types if ct not in accepted]
        if unsupported:
            # Not fatal: the list may simply be newer than this build's view.
            logger.warning(
                "filters.content_types contains %d type(s) the platform does not "
                "accept; files of these types would be rejected at commit: %s",
                len(unsupported),
                ", ".join(unsupported),
            )
        elif config.filters.content_types:
            logger.info("All configured content types are accepted by the platform")
        else:
            logger.info(
                "filters.content_types is empty; every platform-accepted type "
                "will be considered in scope"
            )
    finally:
        client.close()

    logger.info("Check passed")
    return EXIT_OK


def run_scan(config: Config) -> int:
    """Walk, filter, hash and record — everything short of talking to the corpus."""
    connection = connect(config.database.path)
    try:
        migrate(connection)
        repository = Repository(connection)
        run_id = repository.start_run(
            mode="scan-only",
            corpus_id=config.corpus.id,
            root_dir=str(config.source.root_dir),
            dry_run=config.sync.dry_run,
            config_path=str(config.path) if config.path else None,
        )
        configure_logging(config.logging, run_id)

        logger.info("Scanning %s for corpus %s", config.source.root_dir, config.corpus.id)
        repository.record_event(
            run_id=run_id,
            event_type="run.started",
            message=f"scan-only over {config.source.root_dir}",
            details={"corpus_id": config.corpus.id, "dry_run": config.sync.dry_run},
        )

        counters = {"scanned": 0, "skipped": 0, "failed": 0}
        try:
            for scanned in walk(config.source):
                counters["scanned"] += 1
                result = apply_filters(scanned.abs_path, scanned.size, config.filters)

                if not result.accepted:
                    counters["skipped"] += 1
                    logger.info(
                        "Skipped %s: %s (%s)",
                        scanned.rel_path,
                        result.reason,
                        result.detail,
                    )
                    repository.record_event(
                        run_id=run_id,
                        event_type="file.skipped",
                        message=f"{scanned.rel_path}: {result.reason}",
                        details={
                            "rel_path": scanned.rel_path,
                            "reason": str(result.reason),
                            "detail": result.detail,
                            "size": scanned.size,
                        },
                    )
                    continue

                existing = repository.get_file_by_rel_path(
                    config.corpus.id, scanned.rel_path
                )
                # Hashing costs a full read, so only do it when the cheap
                # (size, mtime_ns) pair says something moved.
                if (
                    existing is not None
                    and existing.content_hash
                    and not existing.content_changed(scanned.size, scanned.mtime_ns)
                ):
                    content_hash = existing.content_hash
                    hashed = False
                else:
                    try:
                        content_hash = hash_file(scanned.abs_path)
                    except OSError as exc:
                        counters["failed"] += 1
                        logger.error("Cannot read %s: %s", scanned.rel_path, exc)
                        repository.record_event(
                            run_id=run_id,
                            level="ERROR",
                            event_type="file.unreadable",
                            message=f"{scanned.rel_path}: {exc}",
                            details={"rel_path": scanned.rel_path},
                        )
                        continue
                    hashed = True

                record = repository.upsert_file(
                    corpus_id=config.corpus.id,
                    rel_path=scanned.rel_path,
                    abs_path=str(scanned.abs_path),
                    filename=scanned.filename,
                    content_type=result.content_type,
                    size=scanned.size,
                    mtime_ns=scanned.mtime_ns,
                    content_hash=content_hash,
                    run_id=run_id,
                )
                changed = existing is not None and existing.content_hash != content_hash
                logger.info(
                    "Accepted %s (%s, %d bytes, %s%s)",
                    scanned.rel_path,
                    result.content_type,
                    scanned.size,
                    "new" if existing is None else ("changed" if changed else "unchanged"),
                    "" if hashed else ", hash reused",
                )
                repository.record_event(
                    run_id=run_id,
                    event_type="file.discovered",
                    message=scanned.rel_path,
                    file_id=record.id,
                    details={
                        "rel_path": scanned.rel_path,
                        "content_type": result.content_type,
                        "size": scanned.size,
                        "content_hash": content_hash,
                        "sync_state": record.sync_state,
                        "document_id": record.document_id,
                    },
                )

            missing = repository.files_not_seen_in_run(config.corpus.id, run_id)
            for record in missing:
                logger.info(
                    "Missing locally: %s (document_id=%s)",
                    record.rel_path,
                    record.document_id or "none",
                )
                repository.record_event(
                    run_id=run_id,
                    event_type="file.missing_local",
                    message=record.rel_path,
                    file_id=record.id,
                    details={"document_id": record.document_id},
                )

        except Exception as exc:
            repository.finish_run(run_id, status=RunStatus.FAILED, counters=counters, error=str(exc))
            raise

        repository.finish_run(run_id, status=RunStatus.SUCCESS, counters=counters)
        logger.info(
            "Scan finished: %d scanned, %d accepted, %d skipped, %d unreadable, "
            "%d missing locally",
            counters["scanned"],
            counters["scanned"] - counters["skipped"] - counters["failed"],
            counters["skipped"],
            counters["failed"],
            len(missing),
        )
        return EXIT_OK
    finally:
        connection.close()


def run_stats(config: Config) -> int:
    """Report on the local database. Never contacts the corpus."""
    connection = connect(config.database.path)
    try:
        migrate(connection)
        report = stats.render(stats.collect(config, Repository(connection)))
    finally:
        connection.close()

    # stdout, not the logger: this is a report to read or pipe, not an event.
    print(report)
    return EXIT_OK


def run_dry_run(config: Config) -> int:
    """Compute and report the plan. Touches neither the corpus nor the database."""
    connection = connect(config.database.path)
    try:
        migrate(connection)
        repository = Repository(connection)

        logger.info(
            "Dry run over %s for corpus %s — no changes will be made",
            config.source.root_dir,
            config.corpus.id,
        )
        result = planner.plan(config, repository)
        planner.log_plan(result, dry_run=True)
    finally:
        connection.close()
    return EXIT_OK


def run_sync(config: Config) -> int:
    connection = connect(config.database.path)
    try:
        migrate(connection)
        repository = Repository(connection)
        run_id = repository.start_run(
            mode="sync",
            corpus_id=config.corpus.id,
            root_dir=str(config.source.root_dir),
            dry_run=False,
            config_path=str(config.path) if config.path else None,
        )
        configure_logging(config.logging, run_id)

        for abandoned in repository.abandoned_runs(config.corpus.id):
            if abandoned.id != run_id:
                logger.warning(
                    "Run %d started at %s never finished; its files will be resumed",
                    abandoned.id,
                    abandoned.started_at,
                )

        client, documents = _build_documents_api(config)
        try:
            logger.info(
                "Syncing %s into corpus %s", config.source.root_dir, config.corpus.id
            )
            repository.record_event(
                run_id=run_id,
                event_type="run.started",
                message=f"sync over {config.source.root_dir}",
                details={"corpus_id": config.corpus.id},
            )

            result = planner.plan(config, repository)
            planner.log_plan(result, dry_run=False)

            if not config.sync.delete_remote_when_missing:
                deletions = result.of_kind(planner.ActionKind.DELETE)
                if deletions:
                    logger.info(
                        "Leaving %d document(s) in place: "
                        "sync.delete_remote_when_missing is false",
                        len(deletions),
                    )
                result.actions = [
                    action
                    for action in result.actions
                    if action.kind is not planner.ActionKind.DELETE
                ]

            outcome = engine.run(config, repository, documents, run_id, result)
            counters = dict(outcome.counters)
            logger.info(
                "Sync finished: %d scanned, %d uploaded, %d updated, %d deleted, "
                "%d unchanged, %d skipped, %d failed",
                counters.get("scanned", 0),
                counters.get("uploaded", 0),
                counters.get("updated", 0),
                counters.get("deleted", 0),
                counters.get("unchanged", 0),
                counters.get("skipped", 0),
                counters.get("failed", 0),
            )
            repository.finish_run(
                run_id,
                status=RunStatus.SUCCESS if outcome.ok else RunStatus.FAILED,
                counters=counters,
                error=(
                    None
                    if outcome.ok
                    else f"{len(outcome.failures)} file(s) failed: "
                    + ", ".join(outcome.failures[:10])
                ),
            )
            return EXIT_OK if outcome.ok else EXIT_FAILURE
        except Exception as exc:
            repository.finish_run(run_id, status=RunStatus.FAILED, error=str(exc))
            raise
        finally:
            client.close()
    finally:
        connection.close()


def run_rebuild_db(config: Config) -> int:
    """Repopulate the local database from the corpus after losing it."""
    connection = connect(config.database.path)
    try:
        migrate(connection)
        repository = Repository(connection)
        run_id = repository.start_run(
            mode="rebuild-db",
            corpus_id=config.corpus.id,
            root_dir=str(config.source.root_dir),
            config_path=str(config.path) if config.path else None,
        )
        configure_logging(config.logging, run_id)

        client, documents = _build_documents_api(config)
        try:
            counters = engine.rebuild(config, repository, documents, run_id)
            repository.finish_run(
                run_id,
                status=RunStatus.SUCCESS,
                counters={"scanned": counters.get("documents", 0)},
            )
            return EXIT_OK
        except Exception as exc:
            repository.finish_run(run_id, status=RunStatus.FAILED, error=str(exc))
            raise
        finally:
            client.close()
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Logging is configured as soon as the config parses; anything before that
    # can only reach stderr.
    try:
        config = _apply_overrides(load_config(args.config), args)
    except ConfigError as exc:
        print(f"verbatim-sync: configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    configure_logging(config.logging, run_id="-")
    logger.debug("verbatim-sync %s starting in %s mode", __version__, _mode_name(args))

    try:
        if args.init_db:
            return run_init_db(config)
        if args.check:
            return run_check(config)
        if args.scan_only:
            return run_scan(config)
        if args.rebuild_db:
            return run_rebuild_db(config)
        if args.stats:
            return run_stats(config)
        if config.sync.dry_run:
            return run_dry_run(config)
        return run_sync(config)
    except NotImplementedError as exc:
        logger.error("%s", exc)
        return EXIT_FAILURE
    except VerbatimSyncError as exc:
        logger.error("%s", exc)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return EXIT_FAILURE
    except Exception:
        logger.critical("Unexpected failure", exc_info=True)
        return EXIT_FAILURE
    finally:
        logging.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
