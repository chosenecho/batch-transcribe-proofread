# Maintenance Packages and Backups

`maintenance export` creates a verifiable private-history package. It is the
recoverable database profile and may contain private prompts, responses and
review records. `maintenance export --profile publication` delegates to the
canonical rights-checked publication exporter and produces a public projection;
it intentionally contains no recoverable database and cannot be used with
private part or dataset selectors.

The selectors are composable and are persisted in the manifest: `--dataset`
(`metadata`, `tags`, `workflow`, `transcripts`, `editorial`, `publication`),
`--part-id`, `--bvid`, `--artifact-type`, `--directory`, `--since`, `--until`
and `--status`. The resolved row counts and excluded artifact paths are recorded.
If a referenced file or foreign-key dependency is excluded, export fails before
the package is published. The `database-only` profile makes this especially
explicit: referenced artifacts must be included or the operation fails.

Import plans support `new`, `replace` and `merge`. Plans are read-only previews
and include package/target fingerprints, per-file `added`/`replaced`/`skipped`
actions, row counts and integer mappings. `replace` keeps the old directory in a
rollback exchange root. `merge` maps only ordinary identity rows; a conflicting
frozen transcript, editorial input, revision, model call, publication, source or
model identity fails closed because rewriting prepared JSON, segment references,
publication paths or content hashes would change historical evidence. The plan
must be reapplied only when package and target fingerprints still match.

Completed workers may leave `.artifact-runtime` and `.asr-input-reservations`
in the archive. Package export, snapshot and task-state backup share
`archive-runtime-v1`: empty runtime directories and stopped, empty fence files
have explicit exclusion receipts. The managed `.preserved-body-import.export.lock`
is also excluded only after its marker and stopped lock state are verified.
Active fences, any reservation receipt,
malformed entries, links and unknown root files refuse capture. Backup does not
remove runtime state. Old packages without these optional declarations remain
readable.

Selective export supports the registered `source-observations/v1` extension and
publication content-rights events as part of the native dependency closure.
`source-observations-closure/v1` preserves selected raw payloads, field receipts,
acceptances and frozen editorial/publication bindings without rewriting them.
A shared creator-page response stays whole; `observation_boundary` records its
observation IDs and the raw row count. Unselected sibling videos do not become
owned histories merely because their fields occur in that raw page. Import
verification compares this boundary to the actual database, including when a
manifest digest has been recomputed. Adopted video-wide parts metadata can expand
the requested part set; `resolved_part_ids` records that closure. Other extensions
still require full export until their selection semantics are defined.

Merge suspends only the five registered Bilibili-to-source projection triggers
inside its staging transaction. Identity guards remain enabled; exact trigger
SQL is restored and the complete database is validated before commit. Repeated
merges reuse natural creator/video/part identities, while conflicting frozen
history continues to refuse the merge. Staging validation currently serializes
one database image in memory; production-size throughput remains unbenchmarked.

`maintenance delta-save` creates a stored or deflated file-level differential
against a full package, another differential, or a legacy `snapshot save` ZIP.
The chain stores its baseline container hash, content ID, changed files and
deletions. `maintenance chain-verify` recursively materializes the chain in a
private staging directory and verifies every hash, database contract, FK and
typed table ledger. `chain-materialize` turns it back into a normal package.
Deleting a baseline is refused while a selected restore point still references
it; `retention-plan` and `retention-apply` use the repository maintenance lock.

`maintenance checkpoint` takes the exclusive archive coordination lock, rejects
running jobs, leases, attempts, ingestion/acquisition runs, unfinished model
calls, journals and incomplete SQLite checkpoints, then verifies the database.
It cannot stop an old writer that ignores the project lock; that writer must be
stopped by the operator. `backup-policy` writes a policy bound to its integrity
digest. Digests identify content and do not grant authority: apply operations
independently rederive source, target, selectors and retention facts.
`run_archive_backup.py` is the real polling scheduler. Every run has a durable
receipt, immutable event idempotency, consecutive-failure alert threshold,
retention proof and a concrete verification kind. Notifications are local JSON
events for an operator or supervisor to consume; no third-party message is sent.
Full and public packages use stored ZIP containers. Deflate applies to changed
payloads of incremental/differential runs; full mode rejects deflate explicitly.

The supported boundary is explicit: ZIP payload encryption is reserved metadata
and no key is stored; selective exports of unregistered schema
extensions fail and require a full package; merge of frozen identity conflicts
fails closed; Windows has no `renameat2` atomic exchange for replace/merge; an
external artifact root must be local and readable during export; public output is
not a database restore; and differential chains do not patch SQL rows, they carry
changed SQLite bytes. Upgrade and legacy migration checkpoint receipts are
implemented in the archive upgrade service and retain the source unchanged.
Streaming and free-space checks are exercised on frozen acceptance archives.
Production-size merge and chain throughput have not been benchmarked, so the
reports do not claim a production-scale wall-time guarantee.

## Minimal commands

All examples use stopped, local archives and existing output parents. Epoch time
selectors are inclusive. Time/status filters determine explicitly requested rows;
FKs and immutable owned histories may pull rows outside those filters. The
manifest records explicit counts separately from the resolved dependency set.

```sh
# Select metadata/tags for two parts. New imports can choose a different subset.
bili-asr maintenance export --source-root /srv/archive --part-id 12 --part-id 18 \
  --dataset metadata --dataset tags --profile database-only --output /srv/packages/metadata.zip
bili-asr maintenance import-plan --package /srv/packages/metadata.zip --part-id 12 \
  --target-root /srv/metadata-copy --strategy new --output /srv/packages/import-plan.json
bili-asr maintenance import-apply --plan /srv/packages/import-plan.json
bili-asr maintenance import-check --target-root /srv/metadata-copy --plan-id PLAN_ID

# Preserve all frozen history, then merge disjoint/identical identities.
bili-asr maintenance export --source-root /srv/archive --output /srv/packages/full.zip
bili-asr maintenance import-plan --package /srv/packages/full.zip --target-root /srv/existing \
  --strategy merge --output /srv/packages/merge-plan.json
bili-asr maintenance import-apply --plan /srv/packages/merge-plan.json

# Exact replacement keeps the previous archive at rollback_root in the receipt.
bili-asr maintenance import-plan --package /srv/packages/full.zip --target-root /srv/existing \
  --strategy replace --output /srv/packages/replace-plan.json
bili-asr maintenance import-apply --plan /srv/packages/replace-plan.json

# Existing full snapshot format remains valid as a differential baseline.
bili-asr snapshot save --archive-root /srv/archive --out /srv/backups/base.zip
bili-asr snapshot save --archive-root /srv/archive --baseline /srv/backups/base.zip \
  --out /srv/backups/delta.zip --compression deflate
bili-asr snapshot check --file /srv/backups/delta.zip
bili-asr maintenance chain-materialize --package /srv/backups/delta.zip --output /srv/packages/restorable.zip
bili-asr maintenance import-plan --package /srv/packages/restorable.zip --target-root /srv/restored \
  --output /srv/packages/restore-plan.json
bili-asr maintenance import-apply --plan /srv/packages/restore-plan.json

# Preflight makes dependency preservation explicit before deleting containers.
bili-asr maintenance retention-plan --repository /srv/backups --keep-count 5 --keep-days 30 \
  --output /srv/packages/retention.json
bili-asr maintenance retention-apply --plan /srv/packages/retention.json

# The scheduler evaluates a time trigger; policy time controls whether it runs.
bili-asr maintenance backup-policy --source-root /srv/archive --repository /srv/backups \
  --interval-seconds 3600 --mode incremental --keep-count 5 --keep-days 30 --alert-after 3 \
  --output /etc/bili-asr/backup-policy.json
python scripts/deployment/run_archive_backup.py --policy /etc/bili-asr/backup-policy.json --poll-seconds 60
bili-asr maintenance backup-run --policy /etc/bili-asr/backup-policy.json --trigger event --event-id release-20261013
bili-asr maintenance backup-status --repository /srv/backups --policy-id POLICY_ID

# Public backups use the existing rights/review pipeline and full projection ZIPs.
bili-asr maintenance backup-policy --source-root /srv/archive --repository /srv/public-backups \
  --profile publication --mode full --output /etc/bili-asr/public-backup-policy.json
```

The policy file and the generated plan/receipt are private operational inputs.
Use permissions such as umask 0077. The supervisor template is
`scripts/deployment/bili-asr-archive-backup.supervisor.conf`. `backup_success`,
`backup_failure` and `backup_failure_alert` are local receipt notifications; an
external notification consumer must explicitly be configured to send them.
No receiver address, token or encryption key is saved in a package.

## Recovery and rollback

Import checkpoints live in the target's sibling
`.TARGET.maintenance-PLAN_ID/checkpoint.json`. The `converted` phase is published
atomically and hash-checked on resume; copying reuses each complete exact file.
Re-run the same `import-apply --plan` after a power loss. Changed package, target,
completed conversion, checkpoint ownership or hard-linked staging file is a
diagnostic refusal. A partly copied target is never installed. Large extraction
interrupted before conversion publication restarts that uncompleted conversion;
it does not claim byte-offset transfer resume.

For replacement, the old directory stays at the receipt's `rollback_root`. Stop
all consumers before rollback, verify that the new target has not acquired new
business writes, preserve both sides, and explicitly exchange directories using
the operating system's atomic directory exchange facility. Rollback never
deletes the new work automatically and does not authorize reverse schema
conversion. Strict `import-check` protects the installation baseline; it must not
be bypassed by treating later changes as an unchanged install.

For a multistage upgrade with legacy migration as its first edge:

```sh
bili-asr archive upgrade-plan --source-root /srv/old --target-root /srv/new \
  --target-contract universal-v2 --target-contract preserved-body-import-v1 \
  --target-contract legacy-part-title-supplement-v1 --no-external-control-state \
  --output /srv/packages/upgrade.json
bili-asr archive upgrade-apply --plan /srv/packages/upgrade.json
# Re-run the identical command after interruption; completed phases are verified.
bili-asr archive upgrade-check --target-root /srv/new --plan-id PLAN_ID
bili-asr archive upgrade-check --target-root /srv/new --plan-id PLAN_ID --audit
```

Use `--control-state` instead of `--no-external-control-state` whenever a retry
ledger/hold controller exists. The report binds it and keeps handoff required.
See `archive-upgrades.md` for historical/current-fact audit and rollback rules.

## Requirement-to-evidence matrix for issue #369

| Requirement | API / CLI | Executable evidence and exact boundary |
|---|---|---|
| 1: versioned manifest, source/target contract, time/version/hash/privacy | `export_package`, registered v1/v2 schemas, `inspect`, `verify` | `test_full_frozen_history_exact_source_bytes_and_idempotent_receipt`; v1 is byte-compatible, v2 scopes registered separately; private history declares no text redaction |
| 1: no business writes during inspect/verify; bounded diagnostics | `_extract`, `verify_package`, JSON error code `maintenance-validation-failed` | package corruption matrix rejects DB/hash/manifest/extra/path/duplicate/compression damage before target creation |
| 1: public/private/full distinction, verifiable receipts | `export_public_profile`, `verify_public_backup`, v2 import receipt | public profile tests prove no database and rights-gated current release export; import receipt binds exact plan and baseline |
| 2: profiles and selectable metadata/tags/workflow/transcript/editorial/publication | `scope`, `select_database`, `export`, `import-plan`, `backup-policy` | metadata/tag scope test; same-scope import test; status/time/part scope tests; closure preserves original typed values and rowid |
| 2: artifact type/directory/time/status and explicit vs closure | `filter_artifacts`, `predicate`, manifest `explicit_row_counts` / `resolved_part_ids` / `excluded_artifacts` | missing required artifact causes explicit failure; time-zero result is a valid empty native database; registered native, source-observations and content-rights closure; other extension selector semantics refuse |
| 2: no silent missing FK/transcript/publication dependency | selected closure + bundle/reference verification | cross-part FK and JSON identity tests; public heads included whenever publication editions selected; referenced artifact filters fail |
| 2: import dry run counts/hashes/ID mappings/new/replace/merge | v2 `plan_import`, `_build`, `merge_database`, `import-plan --strategy` | real frozen part1/part2 merge and replace tests; collision fixture refuses rewriting frozen identities; integer mappings are previewed for safe ordinary identities |
| 2: idempotency, stage resume, source and target drift | `apply_import`, `check_import`, persistent converted/copying/verified/installed checkpoint | interrupted copy resume test; repeated receipt reuse; target/package drift refusal; hardlink test preserves external inode |
| 2: credentials/models/cache/log exclusions | `_root_entries`, manifest redaction policy | original package tests; unrecognized root entries fail rather than accidentally exporting credentials |
| 3: incremental/differential chain changes/deletions/integrity | `save_delta`, `verify_chain`, `materialized_package`, `snapshot save --baseline` | three-generation add/delete roundtrip, missing/changed baseline, legacy full snapshot bridge and damaged transitions |
| 3: retention/reference-safe cleanup | `retention_plan`, `apply_retention`, repository maintenance lock | retained restore point's entire ancestor chain is protected; changed repository invalidates cleanup plan |
| 3: large data/space/progress/recoverable atomic install | streaming 1 MiB hashing, plan `required_free_bytes`, durable phase checkpoint | actual import apply never creates half target; progress is phase/file completion, no unsupported wall-time ETA promise; estimate includes candidate/full/stage plus fixed 32 MiB margin |
| 3: compression/encryption metadata | delta `container` stored/deflate + `encryption:null` | deflate delta roundtrip; payload encryption is reserved metadata only, encrypted members fail; no key material enters archive |
| 3: leases/checkpoint/external roots/portable paths | `checkpoint_archive`, package `_inventory`, portable path checks | committed WAL checkpoint keeps logical rows; active writer/lease refuses; disjoint local artifact root test; remote-only unmaterialized objects refuse |
| 3.1: automatic time/event/manual backups, alerts and retention | `run_archive_backup.py`, supervisor template, `backup-policy/run/status` | backup tests prove not-due/event reuse/permanent receipt event identity/consecutive failure alerts and DB state unchanged; deployment must prove scheduler passes separately |
| 3.1: database-only/subtitle/full/public ranges and dependency gaps | shared scope, public full projection backup | public auto-backup retention test; public has `database_recoverable:false`; database-only refuses referenced files; output excludes credentials/cache/models |
| 3.1: per-run plan/result/identity/restore verification | run receipt + `verification_kind` | private kind is detached chain materialization+schema/FK/typed row/file hash; public kind is rights-gated projection+hash, never claims database restoration |
| 3.1 owner extension: lightweight inactive task-state evidence, typed external controls, idle writer consistency, independent reader | `save_task_state`, `backup-policy --profile task-state`, `task-state read/records/evidence` | [task-state-evidence.md](task-state-evidence.md) maps every added requirement; committed WAL capture under idle WRITE, controller-lock-first typed capture, original frozen rows/bytes, media identities and inactive scheduler receipts are tested |
| 4: exact contract matrix/multistage legacy migration/resume | `upgrade_path`, `UpgradeCheckpoint`, archive `upgrade-*` | `test_archive_upgrade_resume.py` covers legacy first phase, second-edge power loss, phase tampering, source drift and checkpoint/rename window |
| 4: unchanged source/atomic install/history vs current/control diff | `check_upgrade`, `audit_upgrade`, control handoff | upgrade audit separates mutable workflow facts from immutable history and added artifacts; existing migration tests retained; no implicit worker/retry/hold recovery |
| 5: consistent CLI and reproducible examples | `maintenance`, existing `snapshot` and `archive` | examples above; package/CLI tests and existing snapshot compression/CLI plus contract mirrors pass; old full snapshot formats remain readable |
| Acceptance 1: frozen roundtrip PK/FK/history/paths/hashes, dry run, receipt reuse | original package + v2 strategy tests | original typed table ledgers and original files preserved; same-source disjoint merge exact union; frozen collision refuses |
| Acceptance 2: missing/hash/contract/space/drift/WAL/lease fail before install | common inventory/preflight/contract/reference guards | corruption/drift/active-writer tests; fixed source fingerprint in plan; no install before final verification |
| Acceptance 3: offline full/delta integrity, missing/tampered baseline and interruption | chain validation and resumed package installation | deletion/addition chain tests, package interruption checkpoint, no partly installed directory |
| Acceptance 4: multistage upgrade and legacy path, source/receipt/resume/diff | archive upgrade checkpoints and audit | parent upgrade resume suite provides actual three-contract path and source byte preservation; selective package extension unknown semantics remain explicit refusal |
| Acceptance 5: backward compatibility for existing export/snapshot/migrate/upgrade | v1 package reader, old snapshot bridge, untouched legacy commands | contract registry mirror tests, original maintenance tests, snapshot CLI/compression tests and upgrade/migration regression suites |

No row-remapping claim includes frozen evidence without a registered converter.
No automatic backup outcome authorizes restarting the workers. No retention plan
can clean unrelated archive files, and no public backup can be imported as a
private business database.
