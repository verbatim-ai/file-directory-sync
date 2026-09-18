# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-08-06

Initial release: feature complete for one-way sync. New files are uploaded,
changed files are replaced in place, deleted files are removed from the corpus,
and the local database can be rebuilt from the corpus after a loss.

> The whole implementation landed as a single commit, so the entries below
> describe the shipped feature set rather than an incremental history.

### Added

- **CLI** (`verbatim-sync`) driven entirely by a TOML configuration file passed
  as its sole argument, so it runs unattended from cron. Modes: `--init-db`,
  `--check`, `--dry-run`, `--scan-only`, `--rebuild-db`, `--stats`, plus a full
  sync by default; `--verbose` and `--log-file` override the config. Exit codes
  are meaningful to cron — `0` success, `1` runtime failure (including any file
  that failed to sync), `2` configuration error.
- **Configuration loader** parsing TOML into validated frozen dataclasses.
  Every relative path resolves against the configuration file's own directory,
  never the process working directory, because cron does not control the latter.
  Unknown keys in any section are a hard error — a typo silently disabling a
  filter is the worst failure mode for a job nobody watches. Sizes accept both
  SI (`50MB`) and IEC (`50MiB`) suffixes as well as bare byte counts; UUIDs are
  validated at load time so a bad `key_id` fails immediately rather than as an
  opaque `403`.
- **RS512 JWT authentication** from an RSA key pair held in a keystore outside
  the project. `key_filename` (the file on disk) and `key_id` (the UUID the
  platform issued, sent as the `kid` header) are independent by design. Tokens
  are cached and re-signed shortly before expiry. `--check` verifies a freshly
  signed token against the local `.pub` before any request goes out, turning a
  mismatched pair into a precise error. The job warns if the private key is
  readable beyond its owner.
- **HTTP client** hand-written against the live API, with bounded exponential
  backoff and jitter over `429` and `5xx`. Pushing file bytes takes a separate,
  longer timeout than the JSON control-plane calls, and is deliberately *not*
  retried — a presigned URL is single use, so recovery means going back to init.
- **Three-step upload flow** — `POST /v1/doc/init` → `PUT <presigned URL>` →
  `POST /v1/doc/{id}/commit`, then polling `GET /v1/doc/{id}/status` until
  `READY` or `FAILED`. Replacing content re-inits the same document id, keeping
  the document UID stable.
- **Read-only planner** that walks, filters, hashes and classifies each file
  into `UPLOAD`, `REPLACE`, `DELETE`, `RESUME` or `NOOP` without writing
  anything. `--dry-run` runs exactly this code and then stops, which is what
  makes its report trustworthy.
- **Content-hash change detection.** `(size, mtime_ns)` is the cheap first check
  that decides whether to hash at all; a streaming SHA-256 against `synced_hash`
  — the digest of what the corpus actually holds — is authoritative. Stricter
  than comparing mtimes, which re-uploads a merely touched file and misses one
  restored from a backup with an older timestamp.
- **Resumable state machine.** `PENDING_UPLOAD` → `UPLOADED` → `COMMITTED` →
  `SYNCED` mirrors the wire flow, so a run interrupted mid-transfer continues
  from wherever it stopped, reusing the presigned URL when it is still live and
  re-initialising when it has expired.
- **Conservative deletion.** A file that genuinely left the disk has its
  document removed; a file still present but no longer passing the filters is
  reported and left alone, so lowering `max_file_size` cannot silently destroy
  documents. `sync.delete_remote_when_missing` disables remote deletion
  entirely.
- **`--rebuild-db` recovery.** Every uploaded document carries its full local
  path in the `sync_fullpath` metadata key, so the local database can be
  reconstructed from the corpus after a loss. Documents whose local file matches
  in size are restored as synced; where sizes disagree the document id is
  restored without the synced hash, so the next run updates rather than assumes.
  Rebuild reads — it never prunes.
- **Concurrency** via `sync.threads` (default `5`). Each worker takes one file
  through the whole flow, which is almost entirely network wait. The plan is
  computed before any worker starts and the shared pieces — SQLite connection,
  token cache, HTTP client — are each thread-safe, so concurrency changes the
  speed and never the result. `threads = 1` skips the pool.
- **SQLite state database** with `sync_run` (one row per invocation), `file`
  (the local-path-to-document-UID mapping) and `event` (an append-only audit
  trail that survives log rotation). WAL journaling and a busy timeout keep
  overlapping cron runs from failing on a locked database. The schema version
  lives in `PRAGMA user_version`; migrations are idempotent and applied
  automatically on every run.
- **Logging built for an unattended job**: every record carries the `run_id` so
  overlapping runs stay separable, the file handler rotates, and a redaction
  filter strips presigned URL signatures, signed JWTs, PEM private keys and
  access-token headers before they can reach any handler. `logging.format =
  "json"` emits one structured object per line. `httpx` is pinned to `WARNING`
  because it logs full request URLs — including the signed upload URL — at
  `INFO`.
- **`--stats`** reporting files synced, volume, pending, excluded and recent
  runs, answered from SQLite alone so it is safe to run mid-sync.
- **Filters** on content type and size, with the platform-supported MIME types
  pinned explicitly rather than trusting the host's `mime.types`. `--check`
  validates the configured list against `GET /v1/doc/accept` and warns about any
  type the platform will not ingest.
- **Test suite** of ~390 tests, none of which touch the network. The HTTP layer
  runs against `httpx.MockTransport` asserting the exact wire shape of every
  request; the sync engine runs against an in-memory backend modelling the real
  contract — bytes must be PUT before commit, duplicate content is rejected, and
  re-init only works from `READY` or `FAILED`.
- **Documentation**: `README.md` (design rationale and the short version),
  `USER_GUIDE.md` (installation, full configuration reference, cron setup,
  monitoring, troubleshooting) and an annotated `config.example.toml`.

## [Unreleased]

Documentation and configuration-template work since the initial release. No
behaviour of the job itself changed — only the shipped defaults in
`config.example.toml` and the operator-facing guide.

### Changed

- **`api.timeout_ms` default raised from 5 000 ms to 60 000 ms** in
  `config.example.toml`. Production `GET /v1/auth/whoami` has been measured well
  above 5 s, which exhausted the retry budget and surfaced as repeated
  `timed out` warnings during `--check`.
- **`sync.poll_status` now defaults to `false`** in `config.example.toml`.
  Waiting for ingestion to reach `READY` makes a cron run last as long as the
  platform's queue; leaving it off returns as soon as the bytes are committed,
  and the next run reconciles.
- Key naming clarified throughout the User Guide: examples now use a
  `<your-key-name>` placeholder rather than a UUID for `key_filename`, so the
  independence of `key_filename` (the file on disk, your choice) and `key_id`
  (the UUID the platform issued, sent as the JWT `kid`) is unambiguous.
- Corpus/Key ID sections reworked — §4.2 renamed "Get the corpus ID" and a new
  §4.3 "Get the Key ID" added, explaining that both IDs are read from the header
  of the corresponding page in the Verbatim AI console. Subsequent sections
  renumbered.
- README now renders the project banner (`assets/fds.webp`).
- `USER_GUIDE.md` moved from `docs/` to the repository root and its table of
  contents removed, for GitBook publishing.

### Fixed

- Corrected the RSA key documentation link to
  `https://verbatim-ai.gitbook.io/docs/api-keys`.

[Unreleased]: https://github.com/verbatim-ai/file-directory-sync/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/verbatim-ai/file-directory-sync/releases/tag/v0.1.0
