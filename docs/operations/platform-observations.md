# Platform observations and the metadata superset

`source-observations/v1` retains the metadata-domain JSON returned by the pinned
Bilibili SDK and YouTube extractor before DTO normalization. Each request has an
independent observation ID, acquisition endpoint, extractor version, scope,
timestamp, state, payload digest and envelope digest. Repeated responses have
separate receipts; appending the same receipt again is idempotent. Updates and
deletes are rejected by the optional schema.

Unknown keys, nested arrays/objects, booleans, numeric values, Unicode, nulls and
empty values survive storage and snapshot save/check/restore. The source object
is never mutated. This is JSON value fidelity, not preservation of an HTTP wire
encoding the SDK has already parsed. Transport credentials (`http_headers`,
`request_headers`, cookies, authorization, proxy/password/API tokens) are outside
this metadata domain. Excluded JSON pointers are recorded in the receipt. Raw
payloads are private; public exports continue to serialize an explicit whitelist
of source facts and must not copy the private namespace.

Create a new universal archive with `archive init --with-source-observations`.
Existing archives require an explicit offline `archive upgrade plan` and
`archive upgrade apply` into an empty destination using a registered
`source-observations-*` edge. The source stays unchanged. The optional extension
has registered fingerprints and edges for preserved/imported, supplemented,
artifact-storage and artifact-online combinations. Old schemas, frozen inputs,
editions and content hashes remain readable and unchanged. Without the extension,
the ingest/refresh service capture report and source import diagnostic say raw
coverage is unavailable; historical responses are never reconstructed from
current normalized fields.

`source metadata --archive-root PATH --youtube ID` (or `--bvid ID`) returns the
private platform namespace, common fields, field states, JSON-pointer provenance
and coverage. `--database PATH/TO/archive.db` reads a verified standalone backup
without restoring a working archive. `--mapping-version source-superset/v2`
reprojects the same observations offline; v1 stays registered. Missing and null
fields preserve previously known values; explicit empty prose and tag lists clear
them. YouTube categories stay in its namespace and never become Bilibili `tid`.

Reads have an explicit observation count limit (default 1,000, maximum 10,000)
and a 64 MiB payload/envelope budget. Exceeding either fails instead of silently
truncating history. `--observed-after UNIX` and `--observed-before UNIX` select an
inclusive historical range, reported in `coverage.selection`. The coverage report
distinguishes successful, failed, denied and unrequested endpoints. It also
records page/range/part scopes and transport exclusions. A successful extraction
only proves the requested observations succeeded; `coverage.complete` remains
false because unrequested endpoints/pages and platform-private information cannot
be asserted complete. A malformed domain payload is still retained before the
normalizer rejects it. Network failures retain bounded error codes rather than
raw exception text.

Both ingestion sinks and source upserts use the same append-only repository.
Sink receipts commit before subsequent normalization so a failed page cannot
erase the original response. Observation storage participates in full and
reference snapshots as database state; backup validation verifies both recorded
digests and the unchanged registered extension definition.

New Bilibili DTO adoption also records private application-owned
`metadata-acceptance/v1` nodes in this immutable store. They have a distinct
operation, endpoint and extractor, and link the actual adopted field value to
its provider receipt and pure normalizer. Superset projection excludes these
nodes from `platformFields`, acquisition coverage and provider-history limits.
See [Bilibili selection receipts](bilibili-metadata-acceptance.md) for atomic
write semantics, frozen bindings and offline validation.

Validation: original-response round trips on both platforms, real gateway/source
ingestion seams, all registered extension upgrade edges, old frozen evidence
preservation, missing/null/empty semantics, offline mapping revisions, scoped
bounded reads, transport exclusions, append-only enforcement and corrupt receipt
rejection. The mutation check deliberately drops an unknown provider key and must
fail the original-response round trip regression.
