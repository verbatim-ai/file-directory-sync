# User Guide

This guide covers everything needed to install, configure and run the job that
mirrors a local directory tree into a [Verbatim AI](https://www.verbatim-ai.com)
corpus.

- [1. What the job does](#1-what-the-job-does)
- [2. Requirements](#2-requirements)
- [3. Installation](#3-installation)
- [4. Before the first run](#4-before-the-first-run)
- [5. Preparing the local database](#5-preparing-the-local-database)
- [6. Running the job](#6-running-the-job)
- [7. Command reference](#7-command-reference)
- [8. Configuration reference](#8-configuration-reference)
- [9. Running from cron](#9-running-from-cron)
- [10. Monitoring](#10-monitoring)
- [11. Recovering the local database](#11-recovering-the-local-database)
- [12. Troubleshooting](#12-troubleshooting)

---

## 1. What the job does

Point it at a directory and a corpus. On each run it walks the tree, decides
what has changed since last time, and brings the corpus into line:

- files it has never seen are **uploaded**
- files whose content changed are **replaced**, keeping the same document ID
- files that no longer exist on disk have their document **deleted**

A local SQLite database records which local file maps to which document UID in
the corpus. That database is what makes runs cheap: without it, every run would
have to re-upload everything.

The job is one-way. It never modifies your local files, and it never treats the
corpus as the source of truth — except during `--rebuild-db` (§11).

---

## 2. Requirements

| | |
|---|---|
| Python | 3.11 or newer |
| [uv](https://docs.astral.sh/uv/) | for dependency management and running |
| A Verbatim AI account | with an organisation ID and a corpus |
| An RSA key pair | registered in the backoffice (§4.1) |
| Network access | to `api.verbatim-ai.com` (or the staging host) over HTTPS |

Install `uv` if you do not have it:

```shell
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Disk: the local database is small — roughly a kilobyte per tracked file. Logs
rotate and are capped by `logging.rotate_max_bytes` × `logging.backup_count`.

---

## 3. Installation

```shell
git clone https://github.com/verbatim-ai/file-directory-sync.git /opt/file-directory-sync
cd /opt/file-directory-sync
uv sync
```

`uv sync` creates `.venv/` and installs the pinned dependencies from
`uv.lock`, so a cron host gets exactly the versions you tested with.

Check it works:

```shell
uv run verbatim-sync --version
```

---

## 4. Before the first run

Three things must exist before the job can do anything: a key pair, a corpus,
and a configuration file.

### 4.1 Create and register an RSA key pair

Authentication uses a short-lived RS512 JWT signed with your own RSA private
key. Generate the pair with the platform's `build_keys.py`
([documentation](https://verbatim-ai.gitbook.io/docs/integration/rsa-keys)):

```shell
mkdir -p /etc/verbatim/keys
python build_keys.py --gen-keys \
    --key-id "$(uuidgen | tr 'A-Z' 'a-z')" \
    --keys-dir /etc/verbatim/keys
```

That writes two files:

```
/etc/verbatim/keys/<uuid>       private key, mode 600 — never leaves this server
/etc/verbatim/keys/<uuid>.pub   public key
```

Open <https://app.verbatim-ai.com> → **Keys**, create a new key, and paste in
the contents of the `.pub` file. The backoffice shows the key's **UUID** and
your **organisation UUID** — you need both for the configuration.

> **Security.** The private key is a credential equivalent to a password for
> your organisation's corpora. Keep it outside the repository, owned by the
> user the cron job runs as, mode `0600`. The job logs a warning if it is
> readable by anyone else, and refuses to print key material in errors.
> `.gitignore` covers `keys/`, `key.json` and `*.pem` as a backstop.

You can generate the key pair by hand instead, if you prefer:

```shell
openssl genrsa -out /etc/verbatim/keys/prod 4096
openssl rsa -in /etc/verbatim/keys/prod -pubout -out /etc/verbatim/keys/prod.pub
chmod 600 /etc/verbatim/keys/prod
```

The filename is yours to choose — see `key_filename` vs `key_id` in §8.4.

### 4.2 Get the corpus UID

Create or open the target corpus in the backoffice and copy its UUID. The job
never creates a corpus; it only pushes documents into one that already exists.

### 4.3 Write the configuration file

Start from the shipped example:

```shell
cp config.example.toml /etc/verbatim/sync.toml
$EDITOR /etc/verbatim/sync.toml
```

The minimum viable configuration:

```toml
[source]
root_dir = "/data/documents"

[corpus]
id = "550e8400-e29b-41d4-a716-446655440001"

[api]
organization_id = "66666666-7777-8888-9999-000000000000"
keys_dir        = "/etc/verbatim/keys"
key_filename    = "20c5ff08-c1f3-464b-be32-cf87be5da7ef"
key_id          = "20c5ff08-c1f3-464b-be32-cf87be5da7ef"
```

Every relative path in the file is resolved **against the configuration file's
own directory**, never the working directory — cron does not control the
latter. §8 documents every option.

### 4.4 Verify it

```shell
uv run verbatim-sync --config /etc/verbatim/sync.toml --check
```

This validates the configuration, signs a token, verifies it against your local
`.pub`, then calls the API to confirm the credentials are accepted and to list
the content types the platform will ingest. It changes nothing. Fix anything it
reports before continuing.

---

## 5. Preparing the local database

The job creates and migrates the database automatically on every run, so this
step is optional — but doing it explicitly is a good way to confirm the path in
`database.path` is writable by the cron user:

```shell
uv run verbatim-sync --config /etc/verbatim/sync.toml --init-db
```

This creates the parent directory if needed, applies any outstanding schema
migrations, and exits. It is safe to run repeatedly.

Inspect the result:

```shell
sqlite3 /var/lib/verbatim/sync.db ".schema"
sqlite3 /var/lib/verbatim/sync.db "PRAGMA user_version;"
```

The database holds three tables:

| Table | Contents |
|---|---|
| `file` | the local-file ↔ document-UID mapping, one row per tracked file |
| `sync_run` | one row per invocation: mode, status, counters |
| `event` | append-only audit trail, outliving log rotation |

> **Back it up.** Losing the database does not lose your documents, but the next
> run would re-upload the whole tree. `--rebuild-db` (§11) recovers it from the
> corpus, which is the safety net — but a periodic file copy is cheaper.

---

## 6. Running the job

### 6.1 See what would happen

Always start here. `--dry-run` performs the full analysis — walking, filtering,
hashing, comparing — then prints what it would do and stops. No document is
touched and no file state is recorded; it runs exactly the code a real sync
runs, and stops before acting on the result:

```shell
uv run verbatim-sync --config /etc/verbatim/sync.toml --dry-run
```

```
Would UPLOAD   annual-report-2025.pdf (not in the local database, 84213 bytes)
Would REPLACE  minutes/2026-01.pdf [doc-0042] (content changed since last sync, 12004 bytes)
Would DELETE   minutes/2025-12.pdf [doc-0031] (no longer on disk)
SKIP     notes.txt (content type is not in filters.content_types: text/plain)
SKIP     scans/huge.pdf (file is larger than filters.max_file_size: 91203344 bytes > 52428800 bytes)
Plan: 214 scanned, 1 new, 1 updated, 1 removed, 0 resumed, 209 unchanged, 2 skipped, 0 unreadable
Dry run: 3 change(s) identified, nothing was sent to the backend
```

The report always reaches your console, even when `logging.console = false`.

### 6.2 Run the sync

```shell
uv run verbatim-sync --config /etc/verbatim/sync.toml
```

Each file goes through three API calls — initialise, push the bytes to storage,
commit — and the job then polls until the platform reports the document `READY`
or `FAILED`.

One file failing does not abort the run: the rest are processed, the failure is
logged against that file, and the command exits `1` so cron notices.

### 6.3 Check the state

```shell
uv run verbatim-sync --config /etc/verbatim/sync.toml --stats
```

---

## 7. Command reference

```
verbatim-sync --config PATH [mode] [options]
```

`--config PATH` (or `-c`) is always required.

### Modes

Exactly one mode may be given. With none, the job performs a full sync.

| Mode | Effect | Contacts the API? |
|---|---|---|
| *(none)* | Full synchronisation | yes |
| `--dry-run` | Report what a sync would change, then stop | no |
| `--stats` | Print statistics from the local database | no |
| `--check` | Validate config and credentials, probe the API | yes |
| `--init-db` | Create or migrate the local database | no |
| `--rebuild-db` | Rebuild the local database from the corpus | yes |
| `--scan-only` | Walk, filter and record file state; no planning or sync | no |

`--scan-only` is a diagnostic: it populates the `file` table from the tree so
you can inspect what the filters accepted, without any corpus interaction. For
"what will the sync do?", use `--dry-run`.

### Options

| Option | Effect |
|---|---|
| `--verbose`, `-v` | Force `DEBUG` logging regardless of `logging.level` |
| `--log-file PATH` | Override `logging.file` for this run |
| `--version` | Print the version and exit |
| `--help`, `-h` | Print usage and exit |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Runtime failure — including a run where any individual file failed |
| `2` | Configuration error (the file is missing, malformed or invalid) |

---

## 8. Configuration reference

The file is TOML. Unknown keys are a **fatal error**, not a warning: a typo that
silently disabled a filter would be the worst possible failure mode for a job
nobody is watching.

### 8.1 `[source]` — what to sync

| Key | Default | Meaning |
|---|---|---|
| `root_dir` | *required* | Directory tree to walk, recursively |
| `follow_symlinks` | `false` | Follow symlinked files and directories |
| `include` | `[]` | Glob allowlist; empty means "everything" |
| `exclude` | `[]` | Globs dropped before anything else |

Globs match the path **relative to `root_dir`**, using `/` separators. `**/`
means "at any depth", so `**/*.pdf` matches both `a.pdf` and `deep/a.pdf`. A
directory matching an `exclude` pattern is not descended into at all, so
excluding a large subtree costs nothing.

```toml
include = ["**/*.pdf"]
exclude = ["**/.*", "**/~$*", "**/*.tmp", "**/*.part", "archive"]
```

Leaving `follow_symlinks = false` is recommended: following links can walk out
of the tree entirely. When enabled, the walker detects and breaks symlink loops.

### 8.2 `[corpus]` — where it goes

| Key | Default | Meaning |
|---|---|---|
| `id` | *required* | Target corpus UUID |
| `lang` | `"en"` | ISO-639 language used by the platform for summarisation |
| `provider` | `"file-directory-sync"` | Free-form label stored on each document |

### 8.3 `[filters]` — what is in scope

| Key | Default | Meaning |
|---|---|---|
| `content_types` | `[]` | Accepted MIME types; empty accepts any recognised type |
| `max_file_size` | `"50MiB"` | Files above this are skipped |
| `min_file_size` | `"1B"` | Files below this are skipped (excludes empty files) |

Sizes accept SI suffixes (`MB` = 1,000,000), IEC suffixes (`MiB` = 1,048,576),
or a bare integer meaning bytes.

Content type is resolved from the file extension. `--check` warns if
`content_types` lists something the platform will not ingest.

> A file already in the corpus that later stops matching these filters is **not
> deleted**. It is reported and left alone — tightening `max_file_size` should
> not silently destroy documents. Only files that genuinely vanish from disk are
> removed from the corpus.

### 8.4 `[api]` — endpoint and credentials

| Key | Default | Meaning |
|---|---|---|
| `organization_id` | *required* | Your organisation UUID (`oid` claim) |
| `keys_dir` | *required* | Directory holding the RSA key pair |
| `key_filename` | *required* | Filename of the **private** key inside `keys_dir` |
| `key_id` | *required* | The key's UUID as issued by the platform (`kid` header) |
| `base_url` | `https://api.verbatim-ai.com` | Use `https://staging-api.verbatim-ai.com` for staging |
| `timeout_ms` | `5000` | Per-request timeout for API calls |
| `max_retries` | `5` | Retries for timeouts, `429` and `5xx`, with exponential backoff |
| `token_ttl_minutes` | `30` | Lifetime of each signed token (max `1440`) |

**`key_filename` and `key_id` are independent.** `build_keys.py` happens to name
the private key file after the key's UUID, but that is only its convention —
call the file `prod.pem` if you like. What goes into the JWT `kid` header is
always `key_id`. The public half is looked for beside the private key, as
`<name>.pub` or `<stem>.pub`.

`timeout_ms` applies to the JSON API calls. Pushing file bytes to storage uses a
much longer timeout automatically, since a 50 MB upload cannot share a budget
sized for a control-plane request.

### 8.5 `[database]` — local state

| Key | Default | Meaning |
|---|---|---|
| `path` | `"state/sync.db"` | SQLite file; parent directories are created |

### 8.6 `[logging]`

| Key | Default | Meaning |
|---|---|---|
| `level` | `"INFO"` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `file` | *none* | Rotating log file; omit to log only to the console |
| `console` | `true` | Also log to stderr |
| `format` | `"text"` | `"text"` for humans, `"json"` for one object per line |
| `rotate_max_bytes` | `"10MiB"` | Rotate the log file at this size |
| `backup_count` | `7` | How many rotated files to keep |

Setting both `file` and `console` to nothing is rejected — a job that logs
nowhere cannot be diagnosed.

Every line carries the run ID, so overlapping runs stay separable:

```
2026-08-05 21:04:02 INFO    [run=12] verbatim_sync.sync.engine: Synced annual.pdf
```

Presigned upload URLs, signed tokens and PEM private keys are stripped before
anything reaches a log handler.

### 8.7 `[sync]` — behaviour

| Key | Default | Meaning |
|---|---|---|
| `threads` | `5` | How many files to process at once (1–32) |
| `dry_run` | `false` | Always dry-run, as if `--dry-run` were passed |
| `delete_remote_when_missing` | `true` | Delete the document when its local file disappears |
| `poll_status` | `true` | Wait for ingestion to finish before moving on |
| `poll_timeout_seconds` | `300` | Give up waiting after this long |

Each worker takes one file through the whole init → upload → commit → poll
flow. Because that is almost entirely waiting on the network, threads help even
though Python runs one bytecode stream at a time — the GIL is released during
socket I/O and while hashing.

Raising `threads` past a few dozen rarely helps: the platform rate-limits, and
`429` responses are retried with backoff, so the extra workers end up queuing
anyway. The job never starts more workers than there are files to process, and
`threads = 1` skips the pool entirely.

Concurrency changes only the wall time, never the outcome. The plan is computed
before any worker starts, each action concerns exactly one file, and a failure
is still isolated to its own file.

Set `delete_remote_when_missing = false` for an append-only corpus. Deletions
are then reported and skipped.

With `poll_status = false` the job queues ingestion and moves on, which is
faster but means a run reports success before the platform has confirmed the
documents are usable.

---

## 9. Running from cron

```cron
17 * * * * flock -n /var/lock/verbatim-sync.lock \
    /usr/local/bin/uv run --project /opt/file-directory-sync \
    verbatim-sync --config /etc/verbatim/sync.toml >> /var/log/verbatim/cron.log 2>&1
```

Points to get right:

- **Use absolute paths.** `uv` is often not on cron's `PATH`.
- **`--project`** lets `uv` find the environment without a `cd`.
- **Run as the user that owns the private key**, not root.
- **Set a log file** in the config, and consider `logging.console = false` so
  cron does not also mail you every line.
- **Watch the exit code.** `1` means at least one file failed.
- **Do not overlap runs against the same corpus.** The database tolerates
  concurrent access (WAL plus a busy timeout, so you will not see "database is
  locked"), but two syncs planning the same tree at once can both decide to
  upload the same file. If a run regularly takes longer than the interval, widen
  the schedule or add a lock — for example `flock -n /var/lock/verbatim.lock`.

A run interrupted mid-transfer is picked up by the next one: the affected files
are recorded in flight and resumed from wherever they stopped.

---

## 10. Monitoring

### Statistics

```shell
uv run verbatim-sync --config /etc/verbatim/sync.toml --stats
```

```
Verbatim file directory sync — statistics

  Corpus     550e8400-e29b-41d4-a716-446655440001
  Tree       /data/documents
  Database   /var/lib/verbatim/sync.db

Files
  Synced                     1,247  (12.41 GB)
  Not synced yet                 2
    NEW                          1
    PENDING_UPLOAD               1
  Failed                         1
  Tracked in total           1,250

Excluded by filters             42  (as of the run on 2026-08-05 19:18)

Last sync                 2026-08-05 19:18

Recent runs
  #3     2026-08-05 19:18  sync        SUCCESS    1293 scanned  1247 new     0 upd     0 del    42 skip     0 fail
  #2     2026-08-05 18:18  sync        FAILED     1293 scanned     3 new     0 upd     0 del    42 skip     1 fail
        1 file(s) failed: scans/bad.pdf
```

- **Synced** — files the corpus holds the current content of, and their volume.
- **Not synced yet** — tracked but not yet confirmed in the corpus, broken down
  by stage when there is anything to break down.
- **Failed** — files that hit an error; see `last_error` in the database.
- **Excluded by filters** — how many files the last tree-walking run skipped.
- **Recent runs** — the last five, newest first, with the error of any failure.

`--stats` reads only the local database, so it is safe to run at any time,
including while a sync is in progress.

### Querying directly

```shell
# What is in a bad state, and why?
sqlite3 /var/lib/verbatim/sync.db \
  "SELECT rel_path, sync_state, attempts, last_error FROM file
   WHERE sync_state NOT IN ('SYNCED');"

# What did run 12 do to this file?
sqlite3 /var/lib/verbatim/sync.db \
  "SELECT ts, event_type, message FROM event WHERE run_id = 12 ORDER BY id;"
```

---

## 11. Recovering the local database

Every document this job uploads carries its full local path in the
`sync_fullpath` metadata key. That is what makes recovery possible if the
SQLite file is lost or corrupted:

```shell
uv run verbatim-sync --config /etc/verbatim/sync.toml --rebuild-db
```

It fetches every document in the corpus and, for each one:

| Situation | Result |
|---|---|
| Local file present, same size as the corpus | restored as **synced** |
| Local file present, different size | document ID restored, marked for **update** on the next run |
| No local file | reported, document left in the corpus |
| No `sync_fullpath` metadata | reported and skipped — not managed by this job |
| Path outside `root_dir` | reported and skipped |

Rebuild only reads from the corpus; it never deletes documents. Follow it with
`--dry-run` to confirm the recovered state looks right before syncing.

---

## 12. Troubleshooting

**`configuration error: api.key_filename: private key not found`**
The path is `keys_dir` + `key_filename`. Check both, and remember
`key_filename` must name the private key, not the `.pub`.

**`configuration error: api.key_id: not a valid UUID`**
`key_id` is the UUID the backoffice issued for the key, not the filename.

**`Local key self-check` warning, or `token does not verify`**
The `.pub` beside your private key does not match it. Regenerate the pair and
re-register the public half.

**`GET /v1/auth/whoami returned HTTP 403`**
The token was signed correctly but rejected. Confirm the public key is
registered and active in the backoffice, and that `organization_id` is your
organisation's UUID.

**Repeated `timed out ... retrying` warnings**
`timeout_ms` defaults to 5000, which some endpoints have been observed to
exceed. Raise it:

```toml
[api]
timeout_ms = 30000
```

**`415` or a document stuck in `FAILED`**
The platform would not ingest that content type. Run `--check` to list the
accepted types and narrow `filters.content_types` to match.

**A file keeps being re-uploaded every run**
Its content is genuinely changing, or a commit is failing. Check:

```shell
sqlite3 sync.db "SELECT rel_path, content_hash, synced_hash, last_error
                 FROM file WHERE rel_path = 'the/file.pdf';"
```

If `synced_hash` is `NULL`, no commit ever succeeded for it.

**Everything is scheduled for upload after a database loss**
Use `--rebuild-db` (§11) rather than letting it re-upload the tree.

**The sync is slower than expected**
Each file needs at least three round trips plus polling, so latency dominates.
Raise `sync.threads`. If the log fills with `429 ... retrying`, you have gone
past what the platform will accept — lower it again.

**Nothing is found at all**
Check `include` — an allowlist that matches nothing excludes everything. Run
with `-v` to see each exclusion decision:

```shell
uv run verbatim-sync --config /etc/verbatim/sync.toml --dry-run -v
```
