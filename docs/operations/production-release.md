# Complete release and process acceptance (#453)

Do not update individual `site-packages` files or install over an environment
with running workers. A build, an installation, a process which loaded that
installation, and successful GPU acceptance are separate facts. The server
was powered off during this repair: no production state is attested here.

## Build and stage

Commit the candidate first. From a trusted build environment with setuptools
and wheel already installed, run:

```text
python scripts/build_release.py --ref HEAD --output /releases/bili-asr/CANDIDATE
```

The output directory must not exist. The builder archives exactly the selected
commit and records every regular package file ending in `.py`, `.json` or
`.sql`, excluding only the package-root `_build_identity.json` manifest.
Source packages and wheels containing any `.pyc` are rejected. At runtime,
interpreter-generated `.pyc` files under `__pycache__` are accepted only when
their interpreter magic, cache header, optimization level and marshaled code
object match a fresh compilation of the paired source. Invalid, stale or
source-mismatched caches trigger `disk_drift`. Symlinks, non-regular entries
and other package file types fail closed. The wheel check accepts only
that package inventory plus normal top-level `.dist-info/` metadata; extra
wheel-root members and unregistered package members fail closed. The builder
builds offline without resolving dependencies and verifies the resulting
wheel against the inventory. `release.json` records commit, lock digest, build
ID and wheel SHA-256. Local uncommitted code, `.env` and the source checkout's
old `build/` directory do not enter the candidate. A partial/failed output is
not a release; choose a new output directory after investigating it. Keep the
previous wheel and environment available for rollback.

Provision a **new** environment. Install the host-specific Torch build first,
then the frozen dependencies using the approved domestic mirror or an offline
wheelhouse, and finally the candidate wheel. The application wheel build itself
does not contact a mirror. Do not replace the lock or GPU runtime simply to
make resolution succeed. Run `pip check`, installed CLI imports and the
existing ASR deployment checks; dependency metadata alone does not prove model
class compatibility or working inference (#439).

For every production command require the release explicitly:

```text
python scripts/production.py --env-file /private/production.env --release-manifest /releases/bili-asr/CANDIDATE/release.json -- workflow status --archive-root /data/archive
```

Use the script from the reviewed candidate checkout. It checks the wheel
checksum and starts a fresh installed-package verifier using the selected
interpreter. The actual CLI and worker repeat the build guard. All supervisor
children inherit `BILI_ASR_EXPECTED_BUILD_ID`. Omitting the release flag remains
supported for development, but an editable/unattested process is **not** an
accepted production release. Credentials are loaded as literal environment
values and never included in manifests or worker health records.

## Before switching

1. Inventory all running roles: acquisition, ASR, CPU, editorial and any external
   scheduler/backup helper. A controller's `ai_enabled=false` does not prove
   there are no editorial workers. Hold every scheduler's new claims.
2. Request existing graceful drain. Wait for active attempts, leases and artifact
   reader pins to converge; record any unfinished attempt rather than erasing
   it. Preserve retry holds, frozen inputs and raw inference evidence.
3. Back up the database and verify receipts for copied artifacts. Copy and
   release are separate operations. Validate the local backup destination and
   its hashes before deleting any source. Save the current environment, wheel,
   deployment settings and private operational state for rollback.
4. Open the candidate against the archive **read-only** with `ArchiveSession`
   and the normal runtime contract. A schema change requires its own reviewed
   migration/restore plan. This branch adds no SQLite schema migration.
5. Exercise the installed entrypoint with offline fixtures. Run real model-class
   import, actual GPU short-speech ASR + alignment + admission + publication,
   and a representative long sample with the new explicit deadline policy.
   The original invalid alignment sample must be checked from retained private
   audio; rejecting its bad interval is not proof it now transcribes correctly.
6. Use the recovery CLI's dry-run and recorded plan ID to assess replacement
   dependencies. Applying the reviewed plan only queues work; do not run paid
   editorial requests as part of deployment smoke tests.

## Loaded workers and drift

Workers write private, atomic records to the sibling directory
`.<archive-name>.worker-health/`. Each record has a random instance ID, PID,
startup/heartbeat timestamps, role, dependency versions, frozen profile
digests, build ID and phase. No cookie, API response, model text or `.env`
content belongs there. Profile digests identify the exact frozen configuration
and model revisions in the workflow database; they do not export local model
paths. The packaged DB-contract fingerprint file is included in the build's
file inventory. The archive's actual contract is checked by the existing
`ArchiveSession` before the worker runs.

```text
python scripts/production_health.py --archive-root /data/archive
```

The command reports the invoking interpreter and each worker independently.
Old/stopped records remain evidence; `stale` and `stopped` must not be counted
as ready, even if the OS reused a PID. `ready` means the worker can dispatch its
role, not that GPU acceptance has succeeded. `gpu_acceptance=not_recorded` and
`production_acceptance=pending` are deliberate until host evidence is stored.

## Versioned production acceptance record

After the candidate has been installed and the checks below have actually been
performed on the production host, write a separate acceptance record. Prepare a
version-1 operator input JSON from
`src/bili_asr/contracts/schemas/production-acceptance-input-v1.schema.json`. It records
a non-sensitive environment ID and platform, operator alias, the complete
expected worker-role inventory and its evidence pointer, all five bounded
external actor categories (`scheduler`, `backup_helper`, `worker_supervisor`,
`gpu_runner`, `other`), and every required check exactly once. Each check is
`pass`, `fail`, or `not_run`; a pass/fail needs an evidence ID and SHA-256, while
unrun checks stay `not_run`. Keep evidence itself outside the record. Never put
hostnames, credentials, .env values, local model paths, or audio, subtitle, and
transcript content in the input.

The operator scope declaration is a manual inventory attestation: list the
worker roles expected on this host and identify evidence for the complete worker
inventory. For each external actor category, choose `none` only after confirming
none is in scope, `included` when its relevant evidence is covered by this
acceptance, or `not_run` when its scope has not been checked. A `not_run` entry
keeps production acceptance pending. The observed current worker roles must
match the expected set exactly; all observed workers must be fresh, ready or
running, and report the release build ID.

Record on the production host with the interpreter from the environment where
the candidate wheel is installed; do not run this command from a source checkout
or another environment. Before writing, the command independently checks the
running package's loaded and on-disk build identity against the wheel beside the
release manifest, even when `BILI_ASR_EXPECTED_BUILD_ID` is unset. A mismatch
fails before creating a record. Use a new, private output path for each record;
the command refuses to replace a file. On POSIX the record file is mode `0600`.
On Windows the file inherits the output directory's ACL; verify that directory
ACL grants access only to the intended operators before recording.
Only pass `--confirm-production-verified` after an operator has reviewed the
complete production evidence and every required check is pass:

```text
python -m bili_asr production-acceptance record --input /secure/acceptance-input.json --release-manifest /releases/bili-asr/CANDIDATE/release.json --archive-root /data/archive --worker-health-dir /data/.archive.worker-health --output /secure/acceptance-2026-10-27.json --confirm-production-verified
python -m bili_asr production-acceptance verify /secure/acceptance-2026-10-27.json
python -m bili_asr production-acceptance show /secure/acceptance-2026-10-27.json
```

Omit the confirmation flag when evidence is incomplete; unrun checks remain
pending. The record binds the release commit/build/wheel/lock, read-only archive
contract identity, worker PIDs/instance IDs/roles/freshness/builds, and profile
digests. It does not write acceptance state into `release.json` or worker health.
`verify` recomputes schema and internal status consistency only and reports
`self_consistent_only`; it cannot prove that the recorded production
observations or evidence are authentic. Source delivery, installation, loaded
workers, and production verification remain separate facts. Local hashes,
offline tests, and a healthy heartbeat alone do not constitute production
acceptance.

The startup build is captured once when the package is imported. Re-reading a
new manifest cannot make an old PID report the new identity. A changed,
missing, extra or unsupported package file triggers `disk_drift` and the
existing graceful drain path. Verified runtime `.pyc` files are not hashed
into the release identity because their code has been checked against the
inventoried source; unverified bytecode triggers drift. The worker stops
taking jobs. The
watchdog checks every five seconds and the attempt boundary checks again. It
neither hot reloads code nor claims to prove every Python object in memory.
Health-write failure also requests drain. A forced interruption still follows
existing attempt/lease fencing.

Switch only after the candidate checks pass. Start controlled role counts,
confirm fresh instance IDs and expected build IDs, then run the host acceptance
matrix in the repair plan. Watch FD trend, source-sync progress, deadlines,
admission rejects, replica receipts and release blockers before widening load.

## Rollback and tracking

Stop new claims and drain the candidate. Switch services back to the retained
environment and matching release manifest, then check fresh process identities.
Do not copy old source files over a running candidate. Do not restore an older
database over newly committed attempts without a separate recovery decision.
No issue is production-verified solely because local tests or `pip check` pass.
Record source-delivered, installed, loaded and production-verified separately
for #410, #366/#367, #406, #350 and #449–#458. Keep external schedulers and helper
scripts in that inventory, including their policy/config versions.
