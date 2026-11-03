# Offline task-state backup reader

The `task-state` CLI reads `archive-task-state-backup/v1` and the explicit cold-reference `archive-task-state-backup/v2` packages directly. It does not import a database into an active archive, acquire a worker session, load a model, contact a provider, or restore media. Each operation verifies the stored ZIP members, database contract, typed table ledger, dependencies and external component declarations before returning records. The reader uses a temporary directory and an immutable, query-only SQLite connection. Its source package and active archive remain unchanged.

```sh
bili-asr task-state verify --snapshot /backups/task-state.zip
bili-asr task-state read --snapshot /backups/task-state.zip \
  --job original-job-id --status failed --limit 20
bili-asr task-state read --snapshot /backups/task-state.zip \
  --video BVoriginal --part 123 --stage proofread --model original-model \
  --updated-from 1791763200 --updated-to 1791849599
```

`read` selects original `workflow_jobs` rows. Exact matches are OR within repeated flags and AND across different flags. `--video` accepts the original BVID or, for a universal source without a BVID, `platform:external_id`. Stage is the persisted job kind, status is its original status, and time bounds are inclusive Unix seconds over `updated_at`. Model selects the original ASR profile model name or the model field of a recorded editorial request. Request JSON is inspected as evidence; the provider is never called.

The report includes the selected jobs, related attempts and evidence tables, global collection run summaries and model catalogs, declared evidence member hashes, original typed external control bytes, the observation boundary and media identity references. Global summaries have no implied ownership by a selected job. Frozen JSON values remain strings with their original whitespace and UTF-8 SHA256; failed reasons, IDs and attempt counts remain unchanged. `archive_rowid` and row order preserve insertion order, including retries whose timestamps or random IDs would sort differently. Binary SQLite values are losslessly encoded as typed base64 objects. The row SHA256 binds the stable JSON representation of all returned values including original rowid.

For v2, a reference with `available:false` records the exact verified external
copy at capture: original identity, package/member hashes, target and catalog
generation, and SSH instance where applicable. Offline verification compares
these facts to the embedded catalog without contacting the target, downloading
a package or extracting media. It remains possible after the original archive
and target have been removed. A successful state report includes
`media_restored:false`, `external_media_verified_now:false` and the number of
`external_audio_references`; it does not claim the retained target is currently
available. `media_restore_required:true` means media dependencies need a
separate explicit restore and current copy verification before execution.

The default job page contains at most 20 rows; `--limit` accepts 1–100. `pagination.next_offset` identifies another page. Each history table independently reports `truncated` when `--history-limit` (1–1000) is reached. Use `records` to page a specific original table beyond that limit or inspect collection state which has no workflow job:

```sh
bili-asr task-state records --snapshot /backups/task-state.zip \
  --table ingestion_runs --status failed --updated-from 1791763200
bili-asr task-state records --snapshot /backups/task-state.zip \
  --table acquisition_runs --stage subtitle --status failed
bili-asr task-state records --snapshot /backups/task-state.zip \
  --table acquisition_attempts --part 123 --status failed
bili-asr task-state records --snapshot /backups/task-state.zip \
  --table workflow_attempts --job original-job-id --limit 100 --offset 100
bili-asr task-state records --snapshot /backups/task-state.zip \
  --table editorial_model_calls --job original-job-id --model original-model
bili-asr task-state evidence --snapshot /backups/task-state.zip \
  --path control-state/component-0.json
```

`records` accepts only tables from the verified contract's typed ledger. Stage uses a table's `kind`; status uses its first declared `status`, `outcome`, `quality_status`, `processing_status` or `to_status` field. Time uses `updated_at`, otherwise `finished_at` with `started_at` fallback for running records, otherwise `started_at`, `created_at`, `published_at`, `assessed_at` or `changed_at`. Model uses a recorded model name, model ID, profile ID or recorded request model. Job, part and video relationships use explicit registered relations. A filter with no defined meaning for that table is refused rather than silently ignored. For example, `ingestion_runs` supports status and time but has no workflow job filter.

`evidence` returns a declared evidence member's exact original bytes as base64, with its recorded size and SHA256. It rejects database bytes, undeclared names and path traversal. External holds and retry ledgers remain original control evidence; reading never releases a hold or replenishes a retry budget. A saved `running` row and its lease/PID are observations, not live processes: the reader does not restart or dispatch them. A `paused` state represented by a typed external hold remains in that original control record.

Only the registered `bilibili-v1` and `universal-v2` runtime adapters and frozen editorial/publication versions 1–2 are supported. Unknown package, database, payload or external control versions, altered schemas and corrupted hashes fail closed. Future contracts require an explicit reader adapter; no heuristic migration is attempted.

Reports are stable JSON with `operation`, `valid`, `content_id` and `inactive_archive`. Query-only VM execution is bounded, one SQLite value and the serialized report are capped at 16 MiB, and pagination offsets are bounded at 1,000,000. Narrow filters or page individual tables if a report exceeds its byte budget. Verification requires temporary disk space for state evidence; `--work-directory` selects an existing ordinary scratch directory for read operations. Permission denial, full scratch storage or corruption produces exit status 1 without a partial success report. State evidence is complete according to its manifest, while `media_self_contained:false` explicitly records that audio is a verified identity reference rather than bundled execution input.
