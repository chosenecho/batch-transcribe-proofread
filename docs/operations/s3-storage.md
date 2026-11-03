# Private S3-compatible packages and catalog v2

This opt-in backend extends the existing artifact catalog. SQLite remains local.
It creates no bucket, changes no bucket policy, creates no public URL and never
authorizes source deletion. Prepare a private bucket and account-scoped credentials.
The private_bucket flag is an operator attestation; an S3 endpoint does not prove
that every custom/public domain is disabled.

## Explicit upgrade and rollback

Stop old workers and preserve a complete verified archive/control backup. If
artifact-storage-v1 is absent, first use the existing artifacts upgrade into an
independent empty archive. Then install v2 under the exclusive maintenance lock:

~~~sh
bili-asr remote upgrade-catalog --archive-root /data/archive \
  --backup /independent-backups/catalog-before-s3.db
~~~

The new backup path must be outside both archive roots. The command makes,
verifies and retains a consistent SQLite backup, then runs atomic DDL and checks
foreign keys. Only the catalog marker, target constraint and new instance/part
receipt tables change. Existing object/target IDs, replicas, packages, pins,
transfers, jobs, attempts, batch pauses and frozen evidence remain authoritative.
Ordinary reads never install a schema.

Frozen v1 SQL and snapshot identities remain available; v2 has separate pinned
identities. Full snapshots and reference backups validate v2. Generic selective
merge/upgrade routes have no implicit v2 support. Old binaries reject v2. Before
new writes, rollback may restore the retained DB and matching binary. After new
transfers/attempts, preserve a full current state before choosing a complete
earlier backup. Dropping remote rows is not a downgrade procedure.

## Machine binding

Install the locked remote extra. Keep this JSON outside exported roots:

~~~json
{
  "backend": "s3-compatible/v1",
  "target_id": "private-cold",
  "endpoint": "https://S3-ENDPOINT",
  "bucket": "private-archive",
  "prefix": "bili-asr",
  "region": "auto",
  "addressing_style": "path",
  "access_key_env": "BILI_S3_ACCESS_KEY_ID",
  "secret_key_env": "BILI_S3_SECRET_ACCESS_KEY",
  "private_bucket": true,
  "part_size": 8388608,
  "timeout": 30
}
~~~

Replace endpoint/bucket/region with verified account configuration. Optional
session_token_env references an environment variable. Inline credentials, signed
URLs, URL userinfo/query strings and unknown fields are rejected. HTTPS is
required; allow_local_http permits only loopback tests. Credentials are read at
connection time. Rotation preserves identity; changing endpoint/bucket/prefix
requires a different target ID.

Only target IDs, an instance digest, relative keys, content identities and opaque
part receipts enter the catalog. Endpoint, bucket configuration, credential values
and signed URLs are not exported. Errors classify authentication, permission,
timeout, missing object and checksum failure without echoing provider messages.

~~~sh
bili-asr remote probe --binding /secure/s3.json
bili-asr remote push --archive-root /data/archive \
  --binding /secure/s3.json --package /prepared/package.zip
~~~

Use packages from the existing verified artifact-package flow. Never package a
live SQLite file: capture a consistent snapshot/reference/task-state backup first.

## Interruptions and exact restoration

The package SHA-256 enters its key. Existing bytes must pass full readback before
idempotent reuse. Multipart parts are 5–64 MiB, with a 10,000-part limit. Upload ID,
part size, local part hashes and opaque ETags persist in artifact_multipart_uploads,
linked to artifact_transfers. Retry the same push to resume verified parts.
Changed part sizes are refused. A missing upload ID is classified and cleared
for a subsequent retry. ETag is never treated as original-content SHA-256.

Failed transfers retain failure state without a present replica. Registration
requires complete client download-and-hash verification. Conditional publication
is not assumed. Restrict external writers to these keys; later damage is detected
on fresh readback. Configure incomplete-upload lifecycle cleanup for a crash
between server upload creation and local receipt commit.

~~~sh
bili-asr remote abort-upload --archive-root /data/archive \
  --binding /secure/s3.json --transfer-id EXACT-FAILED-TRANSFER
bili-asr remote restore --archive-root /data/archive \
  --binding /secure/s3.json --object-id ORIGINAL-SHA256 \
  --storage-key audio/RETAINED-FILENAME --scratch-root /bounded-scratch
~~~

Abort affects only the exact incomplete upload and retains the failed transfer.
Restore checks package, manifest and original member identity in private staging
before exact installation. Scratch needs package bytes plus 16 MiB; the final
local object needs additional space. Manual staging remains available. For an
archive with the opt-in artifact-online-v1 contract, a worker can instead bind
the S3 configuration file using its existing storage-target option:

~~~sh
bili-asr workflow run --archive-root /data/archive --role asr \
  --storage-target private-cold=/secure/s3.json
~~~

The supervisor passes the same option to each worker. Install the online
extension through the existing staged upgrade procedure before catalog v2;
generic upgrade edges after v2 installation are not registered. See
artifact-storage.md for that explicit upgrade/backup procedure.

Before claim, missing retained audio takes a consumer pin and exact-object
restore fence, reserves package/member headroom, verifies the machine binding
against the retained S3 instance, downloads a bounded package and installs only
the exact hashed member. The package cache is temporary; the verified audio
remains local for subsequent consumers. Concurrent restores use the existing
object fence. Permissions, credentials, missing/corrupt bytes or capacity
failures block the candidate without an attempt or model call; normal input
backoff prevents an immediate download loop. The pin lasts through the handler.
Existing directory bindings continue to work. S3-backed local release/GC
remains disabled.

## Database plus remote-only recovery

~~~sh
bili-asr reference save --archive-root /data/archive --out /backups/state.zip \
  --remote-binding /secure/s3.json --retry-control-file /ops/state.json \
  --retry-control-lock /ops/state.lock
bili-asr reference check --file /backups/state.zip --online \
  --remote-binding /secure/s3.json
bili-asr reference restore --file /backups/state.zip \
  --archive-root /empty/restored --source-workers-stopped
~~~

Use typed retry-control capture for holds, budget/ledger and unknown-send state.
Without external controls, reports retain their unavailable/limited status. S3
instance digests are checked against the snapshot catalog even offline. Recovery
preserves history and controls and starts neither workers nor retries. Verify
all required objects before execution handoff, using manual hydration or the
explicit worker binding for retained audio. The older maintenance-media
verified-references/v1 format remains directory/SSH only; use reference for S3.
Hydrate every required object before making a normal self-contained full snapshot.

## Capacity and acceptance

~~~sh
python scripts/estimate_object_storage.py \
  --package-bytes 11534336 --restore-downloads 2 --months 1
~~~

The calculator separates stored bytes, upload/readback/restore bandwidth,
request operations, multipart memory and cache headroom. Cost stays unknown unless
--rates supplies currency, storage_per_gib_month, egress_per_gib and a complete
per_1000_requests mapping. Retries/resumes add requests. No free tier, account
quota, tax or deletion credit is assumed. Verify actual account rules first.

SDK/HTTP fault tests live in tests/test_s3_storage.py. Separate local Moto evidence:
../evidence/s3-local-interop-2026-11-03.json. From a checkout with moto[server]==5.2.5,
the locked remote extra and pytest in an isolated environment, reproduce with:

~~~sh
python scripts/verify_s3_local.py --output /new/evidence/s3-local.json
~~~

The server binds loopback with generated synthetic credentials/content. This is
emulator evidence, not Cloudflare R2 or production acceptance. The user reported
no real environment on 2026-11-03. Real R2 and representative staged recovery
remain unverified. Local deletion/GC is unavailable for this backend.
