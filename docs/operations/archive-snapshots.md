# Portable Archive Snapshots

Database schema fingerprints are now pinned independently of the current DDL in
`src/bili_asr/contracts/database-fingerprints.json`. A change to a registered
schema fails validation rather than redefining an existing snapshot contract.
Runtime, snapshot and fixed migration-source policies remain distinct; see
[contract governance](../contract-governance.md).


Snapshot format version 1 now supports both validated `bilibili-v1` and
`universal-v2` database contracts. Restore retains the saved contract; the
explicit archive migration command performs contract conversion. Read-only
`snapshot inspect`, `snapshot plan`, and `snapshot doctor` distinguish data
completeness from runtime readiness, including verified model relocation.
`snapshot restore --report PATH` writes a separate recovery audit. The commands and installation/audit failure semantics are described below;
contract conversion uses the [explicit upgrade guide](archive-upgrades.md).

`snapshot save`, `snapshot check`, and `snapshot restore` transfer one complete
archive between devices. SQLite remains the source of metadata, transcripts,
workflow progress, and editorial history. The snapshot inventory describes
files for transfer and verification; it does not schedule work.

## Commands

```powershell
bili-asr snapshot save --archive-root C:\BiliArchive --out D:\Backups\bili.zip
bili-asr snapshot check --file D:\Backups\bili.zip
bili-asr snapshot restore --file D:\Backups\bili.zip --archive-root E:\BiliArchive
bili-asr status --archive-root E:\BiliArchive
bili-asr workflow run --archive-root E:\BiliArchive
```

Each snapshot command prints a JSON report on success and exits 0. Invalid or
incomplete snapshots, unsupported database contracts, busy archives, and
nonempty restore targets produce a diagnostic on stderr and exit 1.

The output parent directory is created when needed. Output must be outside both
source roots and must not already exist. Restore accepts a nonexistent or empty target
directory. Neither operation replaces an existing nonempty archive.

On Linux/WSL, the output filesystem must support hard links for atomic publication
without overwriting an existing ZIP. For filesystems such as FAT/exFAT, save on
a local filesystem first, then copy the completed ZIP to the transfer device.
Windows saves use a native rename that refuses an existing destination.

### Separate product roots

```powershell
bili-asr snapshot save --archive-root C:\BiliState --artifact-root D:\BiliProducts --out E:\Backups\bili.zip
```

Save follows the same product lookup order as artifact readers: the configured
product root first, then the archive root. `BILI_ARTIFACT_ROOT` is also honored
for saving. Check and restore operate entirely on the ZIP and do not use that
environment variable. Restored products are placed directly under the new
archive root. Clear any old `BILI_ARTIFACT_ROOT` setting before reading the new
archive to avoid selecting products from the former device configuration.

## Read-only recovery planning and runtime bindings

```text
bili-asr snapshot inspect --file BACKUP.zip
bili-asr snapshot plan --file BACKUP.zip --archive-root NEW
bili-asr snapshot restore --file BACKUP.zip --archive-root NEW --report recovery.ndjson
bili-asr snapshot doctor --archive-root NEW
```

Inspect and plan validate in a private temporary copy. Plan does not create the
target; it checks package identity, empty target/link rules, space and host
requirements. Doctor uses READ/query_only and neither changes profiles nor
requeues tasks. `data_complete` and `runtime_ready` are separate: a compatible
restored archive can be complete while models, credentials or GPU support remain
unready. Reports list credential variable names, never values, and do not claim
online credential verification or model-loading validation. Proofreading readiness resolves the exact
frozen input (or the frozen automatic ASR-chain configuration): direct DeepSeek
requires `DEEPSEEK_API_KEY`, OpenCode Go requires `OPENCODE_API_KEY`. Client
execution and doctor share the endpoint policy. A missing/corrupt input,
endpoint or configuration reports `editorial:invalid-frozen-config`; current
global defaults never substitute for historical evidence. Environment probes
record only whether the named variables contain nonblank values.

`--runtime-bindings FILE` on inspect/plan/doctor and workers relocates model and
aligner paths while preserving their original model IDs, exact revisions and
frozen profile. Binding schema version 1 requires original/path/model_id/revision/
manifest_sha256; each checkpoint model-manifest.json lists schema_version,
model_id, revision and relative file SHA-256 values. Config and weights must be
present, files match the manifest and links are refused. Missing historical
identity rejects a binding; device, precision or inference changes need a new
profile. See [ASR workers](../asr/asr-workers.md).

Recovery NDJSON records entity/identity/before/after/snapshot/reason for affected
rows and separately records recovery transaction and directory installation.
An audit or parent-directory fsync failure after installation reports
`installed: true` with a warning; it must not be treated as permission to
overwrite an apparently absent target. Restore does not start workers.

## Contents and Format

The ZIP defaults to stored entries so existing compressed audio is copied without
expensive recompression. `snapshot save --compression deflate` selects lossless
Deflate level 1 when the destination has limited space. This changes only container
encoding; the same manifest records original sizes and SHA-256 hashes, and check
and restore validate all original bytes. Compression may save little for existing
compressed audio and consumes additional CPU. File contents are streamed, allowing large audio
collections without loading them into memory. The package contains:

```text
snapshot.json
archive.db
audio/...
transcripts/...
documents/...
publications/...
subtitles/...
```

All present regular files in those supported product directories are included,
including reusable audio not yet linked in SQLite. Database references are
checked as well: a referenced missing file or conflicting known hash refuses
save. Temporary publication/download files and symlinks cause a diagnostic;
finish or inspect that interrupted operation before saving.

The private archive snapshot preserves the AI draft/reference pair and all
registered publication release files, including replaced and withdrawn history.
It preserves complete editions, exact reviews, both heads, and publication events
inside the database. A missing historical release file or mismatched registered
hash refuses the snapshot. This complete archive is private; use
`publication export` to prepare the separate release-only public reading snapshot.

`snapshot.json` identifies `bili-asr-snapshot` format version 1 and records a
snapshot UUID, creation time, producer version, the database contract, and a
file inventory. Each inventory entry has a portable relative POSIX path, byte
size, and SHA-256 hash. The database itself is included in the inventory.

Restore keeps database row IDs and relative file paths, so a different drive
letter or directory name does not require rewriting source identities. Runtime
lock files, legacy manifests, logs, caches, model weights, installed packages,
and credentials are not copied. Search tables already inside `archive.db`
remain available.

## Consistency and Coordination

Stop archive writers and wait for active tasks to finish before saving. Current
CLI writers hold shared archive access for their full invocation; save and
restore hold exclusive access. Multiple normal workflow workers can still run
concurrently. Conflicting maintenance returns `archive_busy` immediately.
The stable coordination file lives beside the archive root as
`.<root-name>.archive-maintenance.lock`. Keep this file in place; replacing or
deleting it during an invocation would split coordination between processes.

Status and runs use read-only archive sessions without schema initialization;
ordinary writers also participate in shared coordination.
Programmatic callers performing writes must use
`bili_asr.archive_maintenance.archive_access(root)` for their whole operation.
Processes from older versions and direct SQLite/file modifications do not
participate. Stop those processes before saving as well. Stop writers in both
Windows and WSL when they access the same archive across operating systems.

Inside exclusive access, save creates a read-only SQLite backup, inventories
the products, checks referenced file hashes, and publishes the complete ZIP.
An unexpired running workflow lease is treated as active and refuses save.
Expired leases can be retained in a snapshot for later recovery.

Check streams every listed file, rejects undeclared or duplicate members,
verifies hashes, checks database integrity and foreign keys, validates the
supported schema contract, and checks database-to-file references. Relative
paths must be portable to Windows and Linux; traversal, absolute paths,
backslashes, reserved device names, and case collisions are rejected.
Published transcript completion markers must match the bundle paths and hashes.
New bundles include SRT, WebVTT, plain text, Markdown, raw JSON and
source-metadata.json, covered by the six-member v3 completion marker. Existing
five-member v2 bundles remain valid under their saved contract. Obsolete four-file bundles must be republished before saving.
Check stages only the database in the system temporary directory; audio and
other products are hashed without creating full temporary copies. Save needs
space for the output ZIP and a database copy, and restore needs space for the
full extracted archive beside its target. The ZIP file must remain outside the
restore target.

Restore repeats full verification in a temporary directory beside the target,
normalizes interrupted execution records in that private copy, and publishes
the directory only after verification succeeds. A damaged or invalid package
does not create a partially usable target archive.

## Recovery Behavior

| Saved state | Restored behavior |
| --- | --- |
| Metadata cursor | Preserved; `fetch-meta` continues from its recorded page. |
| Successful job | Preserved with its result and attempt history. |
| Queued job | Preserved with its dependencies. |
| Failed or cancelled job | Preserved; retry remains an explicit operation. |
| Interrupted running job | Old attempt ends as failed with a recovery code, lease ownership is cleared, and the job is queued immediately. |
| Unfinished acquisition/ingestion record | Closed as interrupted without advancing a collection cursor. |
| Unfinished editorial model call | Closed with an interruption error; completed chunks and revisions are preserved. |

Resume is at the task boundary. Incomplete inference or downloads may restart;
already completed audio acquisition and stored transcript results are reused.
The snapshot file itself and the source archive are unchanged by restoration.

Credentials and the destination GPU/model environment are configured separately.
Existing immutable ASR profiles are preserved rather than edited to fit a new
host. Inspect pending profiles before running on a device with different GPU
support; use the workflow planner to create a new profile when needed.

This format supports both declared database contracts, including the same
normalized core table definitions required by ordinary application opens.
It does not upgrade
older contracts, merge independently modified archives, provide bidirectional
sync, or checkpoint live GPU inference. A structural contract check allows
supported auxiliary objects such as FTS while refusing incompatible core schema.

## Modules

| Module | Responsibility |
| --- | --- |
| `cli.snapshot` | Arguments, product-root resolution, reports, exit status. |
| `services.archive_snapshot` | ZIP inventory, streamed transfer, validation, staged restore. |
| `storage.snapshots` | Read-only database validation/backup, file references, interrupted-record recovery. |
| `archive_maintenance` | Shared writer and exclusive maintenance coordination on Windows and Linux. |

No new queue, mutable manifest state, or media blob tables are introduced.
