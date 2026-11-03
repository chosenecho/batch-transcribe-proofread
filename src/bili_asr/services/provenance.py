"""Read final manuscripts back to frozen inputs and every recorded producer."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from bili_asr.canonical_json import digest
from bili_asr.provenance_contract import (
    MAX_BYTES,
    MAX_NODES,
    EvidenceGraph,
    exact_json,
    write_graph,
)
from bili_asr.services.artifact_readers import read_retained_text
from bili_asr.storage.archive_contracts import frozen_version
from bili_asr.storage.publication import (
    read_edition,
    read_revision,
    verify_release_identity,
)


@contextmanager
def _snapshot(connection):
    owned = not connection.in_transaction
    if owned:
        connection.execute("BEGIN")
    try:
        yield
    finally:
        if owned:
            connection.rollback()


class TraceReader:
    def __init__(self, connection: sqlite3.Connection, artifact_roots: tuple[Path, ...]):
        self.connection, self.roots = connection, artifact_roots
        self.graph = EvidenceGraph()
        self.cache: dict[tuple[str, str], str] = {}

    def rows(self, query: str, parameters=()) -> list[dict]:
        rows = self.connection.execute(query, parameters).fetchmany(MAX_NODES + 1)
        if len(rows) > MAX_NODES:
            raise ValueError("provenance: evidence query exceeds node limit")
        return [dict(row) for row in rows]

    def one(self, query: str, parameters=()) -> dict:
        rows = self.rows(query, parameters)
        if len(rows) != 1:
            raise ValueError("provenance: required evidence row is missing or ambiguous")
        return rows[0]

    def remember(self, kind, identity, record, dependencies=()):
        key = self.graph.add(kind, str(identity), record, dependencies)
        self.cache[kind, str(identity)] = key
        return key

    def artifact(self, name: str, path: str, sha256: str) -> str:
        try:
            text = read_retained_text(self.connection, path, sha256, self.roots, max_bytes=MAX_BYTES).decode("utf-8")
            availability = "embedded_verified"
        except ValueError as error:
            if not any(code in str(error) for code in ("artifact is missing:", "artifact_restore_required:")):
                raise
            text, availability = None, "unavailable_local_bytes"
        return self.remember("artifact", path, {"name": name, "relative_path": path,
                             "sha256": sha256, "text": text, "availability": availability})

    def workflow_attempt(self, attempt_id: str, active=()) -> str:
        if attempt_id in active or len(active) > 64:
            raise ValueError("provenance: invalid workflow selection dependency chain")
        cached = self.cache.get(("workflow-attempt", attempt_id))
        if cached:
            return cached
        attempt = self.one("SELECT * FROM workflow_attempts WHERE attempt_id=?", (attempt_id,))
        job = self.one("SELECT * FROM workflow_jobs WHERE job_id=?", (attempt["job_id"],))
        profile = None
        if job["profile_id"] is not None:
            profile = self.one("SELECT * FROM workflow_asr_profiles WHERE profile_id=?", (job["profile_id"],))
            configs = self.rows("SELECT * FROM workflow_asr_profile_configs WHERE profile_id=?", (job["profile_id"],))
            profile["frozen_config"] = configs[0] if configs else None
        result = exact_json(attempt["result_json"]) if attempt["result_json"] is not None else {}
        selected = result.get("source_selection")
        dependencies = []
        if selected is not None:
            if not isinstance(selected, dict) or not isinstance(selected.get("attempt_id"), str):
                raise ValueError("provenance: invalid recorded source selection")
            dependencies.append(self.workflow_attempt(selected["attempt_id"], (*active, attempt_id)))
        binding = None
        if job["kind"] == "proofread":
            binding = self.one("SELECT * FROM editorial_job_inputs WHERE job_id=?", (job["job_id"],))
        return self.remember("workflow-attempt", attempt_id, {"attempt": attempt, "job": job, "profile": profile,
                             "input_binding": binding}, dependencies)

    def call(self, call_id: str, active=()) -> str:
        if call_id in active or len(active) > 64:
            raise ValueError("provenance: invalid replay dependency chain")
        cached = self.cache.get(("call", call_id))
        if cached:
            return cached
        row = self.one("SELECT * FROM editorial_model_calls WHERE call_id=?", (call_id,))
        dependencies = [self.workflow_attempt(row["attempt_id"])]
        stored = exact_json(row["request_json"])
        replay = stored.get("replay_of_call_id")
        if replay is not None:
            dependencies.append(self.call(replay, (*active, call_id)))
        return self.remember("call", call_id, {"row": row}, dependencies)

    def audio(self, identity: dict) -> str:
        cache_id = digest(identity)
        cached = self.cache.get(("audio-evidence", cache_id))
        if cached:
            return cached
        sha256 = identity.get("sha256")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ValueError("provenance: invalid recorded audio identity")
        retained = self.rows("SELECT * FROM audio_objects WHERE sha256=?", (sha256,))
        replicas = []
        if self.connection.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_replicas'").fetchone():
            replicas = self.rows("SELECT * FROM artifact_replicas WHERE object_id=? ORDER BY replica_id", (sha256,))
        byte_state = "unavailable_local_bytes"
        from bili_asr.artifact_inventory import portable_artifact_parts, stream_hash
        from bili_asr.file_ops import checked_path
        from bili_asr.path_policy import confined_audio_path
        storage_key = identity.get("storage_key")
        if isinstance(storage_key, str):
            for root in self.roots:
                checked_path(root.joinpath(*portable_artifact_parts(storage_key)))
                path = confined_audio_path(root, storage_key, require_exists=True)
                if path is None:
                    continue
                with path.open("rb") as stream:
                    size, observed = stream_hash(stream)
                if observed != sha256 or (identity.get("byte_size") is not None and size != identity["byte_size"]):
                    raise ValueError("provenance: local audio digest mismatch")
                byte_state = "local_bytes_verified_at_capture"
                break
        key = self.remember("audio", sha256, {"identity": identity, "retained": retained,
                            "replicas": replicas, "byte_state_at_capture": byte_state})
        self.cache["audio-evidence", cache_id] = key
        return key

    def producer(self, attempt: dict) -> str:
        run = self.one("SELECT * FROM acquisition_runs WHERE run_id=?", (attempt["run_id"],))
        evidences = self.rows("SELECT * FROM transcript_asr_evidence WHERE run_id=? AND video_part_id=?",
                              (attempt["run_id"], attempt["video_part_id"]))
        evidence = evidences[0] if evidences else None
        dependencies = []
        if evidence is not None:
            parsed = exact_json(evidence["evidence_json"])
            if parsed.get("audio"):
                dependencies.append(self.audio(parsed["audio"]))
        for workflow in self.rows(
                "SELECT a.* FROM workflow_attempts a JOIN workflow_jobs j USING(job_id) "
                "WHERE j.video_part_id=? AND j.kind IN ('asr','subtitle') AND a.result_json IS NOT NULL "
                "AND json_extract(a.result_json,'$.run_id')=? ORDER BY a.started_at,a.attempt_id",
                (attempt["video_part_id"], attempt["run_id"])):
            result = exact_json(workflow["result_json"])
            if result.get("run_id") == attempt["run_id"]:
                if result.get("transcript_id") != attempt["transcript_id"]:
                    raise ValueError("provenance: workflow result differs from acquisition producer")
                dependencies.append(self.workflow_attempt(workflow["attempt_id"]))
        coverage = self.rows("SELECT * FROM transcript_coverage_attestations WHERE run_id=? AND video_part_id=?",
                             (attempt["run_id"], attempt["video_part_id"]))
        return self.remember("producer", f"{attempt['run_id']}:{attempt['video_part_id']}",
                             {"attempt": attempt, "run": run, "evidence": evidence,
                              "coverage": coverage[0] if coverage else None}, dependencies)

    def transcript(self, transcript_id: int) -> str:
        cached = self.cache.get(("transcript", str(transcript_id)))
        if cached:
            return cached
        row = self.one("SELECT * FROM transcripts WHERE transcript_id=?", (transcript_id,))
        segments = self.rows("SELECT * FROM transcript_segments WHERE transcript_id=? ORDER BY ordinal", (transcript_id,))
        dependencies = [self.producer(attempt) for attempt in self.rows(
            "SELECT * FROM acquisition_attempts WHERE transcript_id=? ORDER BY started_at,run_id", (transcript_id,))]
        return self.remember("transcript", str(transcript_id), {"row": row, "segments": segments}, dependencies)

    def source(self, metadata: dict) -> str:
        frozen = metadata.get("source_metadata")
        bindings = frozen.get("observationBindings", []) if isinstance(frozen, dict) else []
        from bili_asr.metadata_acceptance import (
            CONTRACT,
            OPERATION,
            acceptance_document,
            binding_tuple,
        )
        from bili_asr.source_metadata import SourceMetadataSnapshot
        from bili_asr.storage.source_observations import ObservationRepository

        if frozen:
            SourceMetadataSnapshot.from_dict(frozen)
        repository = ObservationRepository(self.connection)
        observations = {}
        read_bytes = 0
        for binding in bindings:
            observation = repository.get(binding["observationId"])
            read_bytes += len(observation.envelope_json.encode("utf-8")) + len((observation.payload_json or "").encode("utf-8"))
            if read_bytes > MAX_BYTES:
                raise ValueError("provenance: frozen observation read exceeds byte limit")
            envelope = observation.envelope
            if (envelope["payloadSha256"] != binding["payloadSha256"]
                    or digest(envelope) != binding["envelopeSha256"]):
                raise ValueError("provenance: frozen raw observation binding mismatch")
            observations[observation.observation_id] = (observation, binding)
        dependencies, providers = [], {}
        # Install provider nodes first: application choices can only reference
        # exact originals already present in this frozen historical selection.
        for identity, (observation, binding) in observations.items():
            if observation.envelope["operation"] == OPERATION:
                continue
            key = self.remember("observation", identity, {
                "envelope_json": observation.envelope_json, "payload_json": observation.payload_json,
                "envelope_sha256": binding["envelopeSha256"], "payload_sha256": binding["payloadSha256"]})
            providers[identity] = key
            dependencies.append(key)
        for identity, (observation, binding) in observations.items():
            if observation.envelope["operation"] != OPERATION:
                continue
            document = acceptance_document(observation)
            originals = []
            for item in document["fields"]:
                selected = item["source"]
                source_id = selected["observationId"]
                if (source_id not in providers or binding_tuple(observations[source_id][1]) != binding_tuple(selected)):
                    raise ValueError("provenance: acceptance original provider is absent from frozen bindings")
                originals.append(providers[source_id])
            dependencies.append(self.remember("metadata-acceptance", identity, {
                "owner": "application", "codec": CONTRACT,
                "envelope_json": observation.envelope_json, "payload_json": observation.payload_json,
                "envelope_sha256": binding["envelopeSha256"], "payload_sha256": binding["payloadSha256"]}, originals))
        return self.remember("source", digest(metadata), {"metadata": metadata,
                             "raw_observation_binding": "verified_frozen" if bindings else "unavailable_unfrozen_history"}, dependencies)

    def revision(self, revision_id: str) -> str:
        cached = self.cache.get(("revision", revision_id))
        if cached:
            return cached
        row, prepared = read_revision(self.connection, revision_id)
        # Preserve the exact original serialized record, rather than the JOIN projection.
        revision = self.one("SELECT * FROM editorial_revisions WHERE revision_id=?", (revision_id,))
        frozen = self.one("SELECT * FROM editorial_inputs WHERE input_id=?", (row["input_id"],))
        snapshot = prepared["snapshot"]
        input_dependencies = [self.source(snapshot.get("metadata", {})), self.transcript(frozen["base_transcript_id"])]
        if frozen["reference_transcript_id"] is not None:
            input_dependencies.append(self.transcript(frozen["reference_transcript_id"]))
        input_node = self.remember("input", frozen["input_id"],
                                   {"row": frozen, "version": frozen_version(self.connection, "input", frozen["input_id"])}, input_dependencies)
        dependencies = [input_node]
        for call in self.rows("SELECT call_id FROM editorial_model_calls WHERE input_id=? ORDER BY call_id", (frozen["input_id"],)):
            dependencies.append(self.call(call["call_id"]))
        for checkpoint in self.rows("SELECT * FROM editorial_chunk_results WHERE input_id=? ORDER BY chunk_id", (frozen["input_id"],)):
            dependencies.append(self.remember("checkpoint", f"{checkpoint['input_id']}:{checkpoint['chunk_id']}",
                                             {"row": checkpoint}, [self.call(checkpoint["call_id"])]))
        for attempt in self.rows("SELECT a.attempt_id FROM workflow_attempts a JOIN editorial_job_inputs i USING(job_id) "
                                 "WHERE i.input_id=? ORDER BY a.attempt_id", (row["input_id"],)):
            dependencies.append(self.workflow_attempt(attempt["attempt_id"]))
        for artifact in self.rows("SELECT * FROM document_artifacts WHERE revision_id=? ORDER BY artifact_name", (revision_id,)):
            dependencies.append(self.artifact(artifact["artifact_name"], artifact["relative_path"], artifact["content_sha256"]))
        binding = self.one("SELECT * FROM editorial_job_inputs WHERE job_id=?", (revision["job_id"],))
        return self.remember("revision", revision_id, {"row": revision, "job_binding": binding}, dependencies)

    def edition(self, edition_id: str, active=()) -> str:
        if edition_id in active or len(active) > 64:
            raise ValueError("provenance: invalid edition ancestry")
        cached = self.cache.get(("edition", edition_id))
        if cached:
            return cached
        edition = read_edition(self.connection, edition_id)
        dependencies = [self.revision(edition["revision_id"])]
        if edition["parent_edition_id"]:
            dependencies.append(self.edition(edition["parent_edition_id"], (*active, edition_id)))
        from bili_asr.storage.import_origins import import_origin, read_baseline
        from bili_asr.storage.source_supplements import edition_supplement
        origin = import_origin(self.connection, edition_id)
        baseline, supplement = None, None
        if origin is not None:
            baseline = read_baseline(self.connection, origin["import_id"])
            for name, column, hash_field in (("preserved-body.md", "body_path", "body_sha256"),
                                              ("import-review.md", "review_path", "review_sha256")):
                dependencies.append(self.artifact(name, baseline[column], baseline[hash_field]))
            link = edition_supplement(self.connection, edition_id)
            if link is not None:
                supplement = {"link": link, "row": self.one("SELECT * FROM source_metadata_supplements WHERE supplement_id=?", (link["supplement_id"],))}
        events = self.rows("SELECT * FROM publication_events WHERE edition_id=? ORDER BY event_id", (edition_id,))
        return self.remember("edition", edition_id, {"edition": edition, "events": events,
                             "import_origin": origin, "import_baseline": baseline, "source_supplement": supplement}, dependencies)

    def release(self, release_id: str) -> str:
        release = self.one("SELECT * FROM publication_releases WHERE release_id=?", (release_id,))
        verify_release_identity(self.connection, release)
        events = self.rows("SELECT * FROM publication_events WHERE release_id=? ORDER BY event_id", (release_id,))
        return self.remember("release", release_id, {"release": release, "events": events}, [
            self.edition(release["edition_id"]), self.artifact("publish.md", release["relative_path"], release["artifact_sha256"])])


def trace(connection: sqlite3.Connection, *, artifact_roots: tuple[Path, ...], edition_id: str | None = None,
          release_id: str | None = None, revision_id: str | None = None, artifact_sha256: str | None = None) -> dict:
    selectors = [(kind, value) for kind, value in (("edition", edition_id), ("release", release_id),
                  ("revision", revision_id), ("artifact", artifact_sha256)) if value is not None]
    if len(selectors) != 1:
        raise ValueError("provenance: select exactly one artifact hash, edition, release or revision")
    kind, identity = selectors[0]
    if not isinstance(identity, str) or not identity or len(identity) > 256:
        raise ValueError("provenance: invalid selector")
    with _snapshot(connection):
        reader = TraceReader(connection, artifact_roots)
        if kind != "artifact":
            roots = [getattr(reader, kind)(identity)]
        else:
            roots = [reader.release(row["release_id"]) for row in reader.rows(
                "SELECT release_id FROM publication_releases WHERE artifact_sha256=? ORDER BY release_id", (identity,))]
            roots.extend(reader.revision(row["revision_id"]) for row in reader.rows(
                "SELECT DISTINCT revision_id FROM document_artifacts WHERE content_sha256=? ORDER BY revision_id", (identity,)))
            if not roots:
                raise ValueError("provenance: unknown final artifact hash")
        return reader.graph.seal({"kind": kind, "value": identity}, roots)


def export_trace(connection, *, output: Path, artifact_roots: tuple[Path, ...], **selector) -> dict:
    from bili_asr.export_snapshot import guard_output
    guard_output(connection, output, artifact_roots)
    return write_graph(output, trace(connection, artifact_roots=artifact_roots, **selector))

def _public_summary(connection, release_id: str, artifact_roots: tuple[Path, ...]) -> dict:
    from bili_asr.content_rights import require_public_rights
    from bili_asr.manuscript_artifacts import get_ai_artifacts
    with _snapshot(connection):
        release = dict(connection.execute("SELECT * FROM publication_releases WHERE release_id=?", (release_id,)).fetchone() or {})
        if not release or release["status"] != "published":
            raise ValueError("provenance: public summary requires a current published release")
        edition = verify_release_identity(connection, release)
        review = get_ai_artifacts(connection, edition["revision_id"], artifact_roots)["review.md"]
        require_public_rights(connection, edition, review)
        graph = trace(connection, release_id=release_id, artifact_roots=artifact_roots)
        result = {"format": "public-provenance-summary/v1", "schema_version": 1,
                  "release_id": release_id, "edition_id": release["edition_id"], "revision_id": edition["revision_id"],
                  "content_sha256": release["content_sha256"], "artifact_sha256": release["artifact_sha256"],
                  "private_graph_sha256": graph["graph_sha256"], "published_at": release["published_at"]}
        from bili_asr.contracts.json_schema import validate_json
        validate_json("public-provenance-summary/v1", result)
    require_public_rights(connection, read_edition(connection, edition["edition_id"]), review)
    return result


from bili_asr.content_rights import serialized_distribution

public_summary = serialized_distribution(_public_summary)
