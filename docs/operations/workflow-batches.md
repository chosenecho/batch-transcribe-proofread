# Persistent workflow batches (opt-in)

`workflow-batches/v1` uses the existing workflow jobs, attempts, dependencies,
leases and fencing. It adds membership and operator intent, not another queue.
Ordinary archive access never installs it. First preserve a verified consistent
backup, drain writers, and install explicitly under the maintenance lock:

```sh
bili-asr archive install-workflow-batches --archive-root /data/archive
bili-asr workflow batch --archive-root /data/archive create \
  --job-id EXACT-JOB-ID --job-id ANOTHER-JOB-ID \
  --workers 10 --actor operator --reason 'reviewed initial scope'
```

All members must already exist and must not be running. A job belongs to one
batch. Include the exact dependency closure you intend to run, including render
jobs if document completion is the boundary. Creation freezes payload hashes;
changed payloads fail before a new attempt can execute. Batch creation does not
plan replacements, requeue failed jobs or call ASR/editorial APIs.

```sh
bili-asr workflow batch --archive-root /data/archive pause \
  --batch-id BATCH --actor operator --reason 'drain current claims'
bili-asr workflow batch --archive-root /data/archive status --batch-id BATCH
```

Pause immediately prevents **new claims** in that batch. Already claimed jobs
finish under their existing leases; the pause event records their IDs. This is
a graceful drain of claimed jobs, not a promise to claim downstream renders.
No process is killed and no job is cancelled. The batch can be paused while
queued work remains. Other batches and jobs without membership remain eligible
under the existing scheduler rules. Workers must run the batch-aware release;
old worker processes do not gain this gate through a CLI change alone.

For one video, first resolve every intended job ID for that video from the
existing workflow status/explain output. Extract only those stopped members:

```sh
bili-asr workflow batch --archive-root /data/archive extract \
  --batch-id BATCH --job-id EXACT-JOB-ID --workers 1 \
  --actor operator --reason 'isolated supplement'
bili-asr workflow batch --archive-root /data/archive resume \
  --batch-id BATCH --workers 20 --dry-run \
  --actor operator --reason 'reviewed resume'
bili-asr workflow batch --archive-root /data/archive resume \
  --batch-id BATCH --workers 20 \
  --actor operator --reason 'reviewed resume'
```

Extraction requires a paused source and never recreates jobs or attempts. A new
independent batch runs the extracted members; the source stays paused. A single
job is not necessarily a complete BVID pipeline. Dependencies outside the
selection continue to block normally. Desired concurrency is a ceiling on live
member claims; launch actual worker processes separately.

Status distinguishes desired workers, live workers/leases, queued, paused,
finite retry delay, failed, cancelled and succeeded. Completion means all
remaining explicitly selected members succeeded. `retry_held: null` means the
external retry-control inventory is not known to this command; it is never a
claim that there are zero holds. Existing runtime exclusions, budget checks,
unknown-send protection and retry maintenance remain authoritative. Batch
commands never clear them or rewrite `available_at`.

## Historical pause imports and recovery

Save the external manifest and retry state first. Compare its exact job IDs with
the database, create a batch for the reviewed non-running set, and pause it.
Review old far-future timestamps separately. Neither creation nor resume
guesses which `2100-01-01` timestamps were manual pauses, and neither moves them
to now. Restore those only through the existing reviewed retry-control process,
preserving attempt and ledger evidence. Missing external hold state blocks such
an import; do not interpret it as an empty hold set.

Batch tables, events and frozen payload hashes are included in the registered
database snapshot fingerprints and native consistent backups. Prefer a full
self-contained snapshot for recovery; selective merge and generic upgrade
edges for these new extensions are not registered. Export first if another
schema migration is required. Older binaries reject the extension; rollback
uses the retained pre-install backup and matching binary, after preserving any
new attempts in a full current snapshot. Do not drop batch tables as a rollback.

Synthetic acceptance is in `tests/test_workflow_batches.py`: 10 claims, pause
and drain, isolated supplement, backup/restart, 20 claims on resume, competing
connections, preserved retries, changed-payload refusal and CLI preview.
