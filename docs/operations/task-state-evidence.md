# Task state evidence contract

`archive-task-state-backup/v1` is an independent full state evidence point. Its
stored ZIP includes `archive.db`, original transcript, document and publication
files, a typed control inventory, and `task-state.json`. It preserves original
primary keys, rowids, attempt order, frozen JSON whitespace, failure reasons,
job status and stored leases. It copies no audio. Required audio dependencies
have a size and SHA256 identity, so the package is complete state evidence but
is explicitly not a media self-contained execution environment.

The separately registered `archive-task-state-backup/v2` adds the explicit
`verified-references/v1` media policy. V1 still requires audio to exist locally
at capture. V2 can instead freeze an externally retained audio reference after
verifying the exact bound copy. The reference records the original SHA256 and
size, replica ID, target ID, relative package and member keys, catalog
generation, verification/presence observations, package SHA256 and size,
manifest SHA256, backend version, and exact SSH instance when applicable.
These facts must match the frozen database catalog; the package and its
original member must pass stream verification before capture completes.
Target markers, package file generations and SSH instances are checked again
before publishing the state point. Catalog scans fail closed above 4096
replicas for one audio object rather than silently truncating alternatives.

V2 state checks compare reference facts against the embedded immutable
database. They require no target binding or network, extract only state
evidence and never materialize audio. Reports distinguish
`media_restored:false`, `media_restore_required`,
`external_audio_references`, and `external_media_verified_now:false`.
The last field avoids presenting historical capture evidence as a live target
probe. Restoring media requires a separate explicit artifact restore with
fresh target verification. Reading this state package does not perform that
operation or activate its saved jobs.

The package is inactive. Verification and the reader never acquire a WRITE
session, start a worker, recover a lease, release a hold, refund a charged
retry, change a budget, requeue a job or delete a hot-library record. A running
job or an interrupted backup receipt is a historical observation. Import into
an active worker environment requires a separate explicit recovery decision.

The SQLite observation is a read transaction followed by SQLite's backup API;
it includes committed WAL records even while an idle long-lived WRITE session
remains open. A staged database is verified against its registered native
schema, foreign keys, table fingerprints and runtime marker. This does not
relax the exclusive writer refusal of full media snapshot maintenance.

External controller locks are acquired in canonical sorted order before
shared archive access and the database transaction, matching the retry
controller's lock order. Every declared controller must identify its actual
stable lock. Captured external bytes and immutable evidence are checked again
after capture. The manifest binds the database SHA256, observed component
versions, UTC start/end interval and source schema. It explicitly declares
`atomic_cross_components:false`: these components do not share a distributed
transaction. Cooperating controllers are excluded by their real locks;
uncoordinated changes during the observation window refuse publication.

Capture has a bounded timeout, with a default of 30 seconds and a maximum of
300 seconds. A held controller lock fails promptly. Space preflight includes
twice the payload size plus a 32 MiB margin. The final ZIP is verified and
fsynced before atomic publication; failure leaves no successful output.

Supported external component declarations are exact objects with `kind`,
canonical absolute `path` and distinct canonical absolute `lock_path`.
`retry-control/v1` has exactly these fields:

```json
{"format":"retry-control/v1","schema_version":1,
 "retry_holds":{"original-job":"original hold reason"},
 "retry_ledger":{"original-job":{"retries":2,"last_retry_at":1791840000,
 "next_retry_at":1791840300,"max_retries":2,"last_error_code":"timeout",
 "reserved_attempt_id":"original-reservation","dispatch_outcome":"reserved"}}}
```

Hold values may be the original string or object. Ledger entries require
`retries` and `last_retry_at`; remaining shown fields are optional. Budget and
timestamp integers are nonnegative. Dispatch outcomes are `reserved`,
`requeued` or `stale`; a reserved physical attempt stays charged. Unknown
fields, versions and layouts fail closed. Existing untyped operational JSON
requires an explicitly registered adapter instead of guessing from field
names. An archive without external controls must explicitly declare that fact.

`retry-control/v2` is also a registered external component. It is the portable
reference-backup retry envelope with original-source/projection/content hashes,
immutable archive job scope and the explicit `unix-seconds-utc/v1` clock.
Task-state capture preserves its exact bytes and the reader checks its scope
against the captured SQLite database. Unknown nested fields or version/digest
mismatches refuse the package; this does not rewrite historical v1 bytes or
start a controller. See `remote-reference-backups.md` for projection and handoff.

`backup-job-state/v1` includes exactly `format`, `policy`, `current_run`,
`prior_runs` and `state`. Policy and run contracts and invocation identities
are checked. Completed prior runs require their receipt digest; an interrupted
running observation requires its original invocation identity and is preserved
without claiming completion. This evidence does not authorize scheduling.

The reader supports `bilibili-v1` and `universal-v2`, frozen content versions
1–2, workflow payload version 1 and ASR evidence version 1. Unknown registered
versions, altered schemas, external paths, undeclared control members,
encrypted members, hash damage and inconsistent observation identities fail
before any records are returned. Raw provider diagnostic JSON remains original
evidence; it is not heuristically migrated.

For automatic policy and retention examples see
[task-state-automation.md](task-state-automation.md). For offline filtering,
table pagination and original evidence retrieval see
[task-state-reader.md](task-state-reader.md).

| Issue #369 owner requirement | Concrete API and regression evidence |
|---|---|
| Lightweight state backup, default stored, media references | `save_task_state`; idle WRITE/WAL test verifies audio is absent, original media hashes and running rows present |
| Whole historical evidence with IDs/order/versions unchanged | Frozen fixture roundtrip; reader original rows, rowid and byte-preserved frozen JSON tests |
| Typed holds, budgets and backup-job state | `validate_components`, `_control`; real control lock, reserved charge, invalid budget/version and interrupted scheduler history tests |
| Consistent observation without starving idle workers | `_database`, controller-lock-first capture; live writer fingerprint baseline equals extracted database ledger |
| Inactive state, no implicit restart or cleanup | `restore_policy`, immutable reader connections; running/cancelled/queued/failed and no active session/model calls regressions |
| Independent offline reader and bounded filtering | `task-state read/records/evidence`; registered relationship filters, pagination, evidence bytes, future version and scratch failure tests |
| Automatic time/event/manual, alerts and retention | Shared scheduler task-state policy; real repeated runs, exact receipt adoption, event dedupe, corrupt package alert and per-policy verified retention tests |
| Corrupt/unknown members refused | Rehashed manifest traversal, absolute component, altered runtime, database/table/observation identity and undeclared media regressions |

Production-sized capture performance has not been benchmarked. Encrypted
payloads, automatic untyped control conversion and executable restoration are
outside this registered evidence contract. Windows atomic replacement of an
existing business archive still fails closed under the separate import API.
