"""Artifact references in the fixed 9b28957 Bilibili source contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
import re
import sqlite3

from bili_asr.manuscript_templates import render_ai_v1, render_publish_v1


LEGACY_MARKER = ".bundle-ready"
LEGACY_BUNDLE_SCHEMA = "archive-bundle-v2"
LEGACY_BUNDLE_NAMES = {
    "srt_path": "bundle.srt", "vtt_path": "bundle.vtt", "txt_path": "bundle.txt",
    "md_path": "bundle.md", "raw_path": "bundle.raw.json",
}
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class MigrationArtifactError(ValueError):
    """Legacy references are incomplete or internally inconsistent."""


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise MigrationArtifactError("duplicate field in legacy artifact JSON")
        result[key] = value
    return result


def legacy_object(raw: str | bytes) -> dict:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, TypeError, RecursionError) as exc:
        raise MigrationArtifactError("invalid legacy artifact JSON") from exc
    if not isinstance(value, dict):
        raise MigrationArtifactError("legacy artifact JSON must be an object")
    return value


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _legacy_review_history(connection: sqlite3.Connection) -> None:
    transitions = {
        "pending-review": {"in-review"},
        "in-review": {"approved", "changes-requested", "rejected"},
        "changes-requested": {"in-review"}, "approved": set(), "rejected": set(),
    }
    for edition, part, sha256, creator, edition_note, parent, review, status, actor, note, issue in connection.execute(
        "SELECT e.edition_id,e.video_part_id,e.content_sha256,e.created_by,e.note,e.parent_edition_id,"
        "r.review_id,r.status,r.actor,r.note,r.issue_url FROM publication_editions e "
        "JOIN publication_edition_reviews r ON r.edition_id=e.edition_id"
    ):
        previous, last = None, None
        for event_part, event_review, event_sha, kind, old, new, event_actor, event_note, event_issue, release in connection.execute(
            "SELECT video_part_id,review_id,content_sha256,event_type,from_status,to_status,actor,note,issue_url,release_id "
            "FROM publication_events WHERE edition_id=? AND event_type IN "
            "('created','edited','in-review','changes-requested','approved','rejected') ORDER BY event_id", (edition,),
        ):
            if ((event_part, event_review, event_sha) != (part, review, sha256) or release is not None
                    or old != previous):
                raise MigrationArtifactError("legacy publication review audit identity or transition is inconsistent")
            if previous is None:
                if (kind not in {"created", "edited"} or new != "pending-review"
                        or (kind == "edited" and parent is None)
                        or (event_actor, event_note, event_issue) != (creator, edition_note, None)):
                    raise MigrationArtifactError("legacy publication review audit history has an invalid origin")
            elif kind not in transitions[previous] or new != kind:
                raise MigrationArtifactError("legacy publication review audit identity or transition is inconsistent")
            previous = new
            # Creation records the edit note; the newly created review starts empty.
            last = (event_actor, "" if kind in {"created", "edited"} else event_note, event_issue)
        if previous != status or last != (actor, note, issue):
            raise MigrationArtifactError("legacy publication review disagrees with audit history")


def _legacy_publication_checks(connection: sqlite3.Connection) -> None:
    """Validate frozen edition/review/release bindings without runtime repositories."""
    if connection.execute(
        "SELECT 1 FROM publication_editions e LEFT JOIN publication_edition_reviews r ON r.edition_id=e.edition_id "
        "LEFT JOIN editorial_revisions v ON v.revision_id=e.revision_id "
        "LEFT JOIN editorial_inputs i ON i.input_id=v.input_id "
        "LEFT JOIN publication_editions parent ON parent.edition_id=e.parent_edition_id "
        "LEFT JOIN publication_heads h ON h.video_part_id=e.video_part_id "
        "WHERE r.edition_id IS NULL OR r.content_sha256!=e.content_sha256 OR h.video_part_id IS NULL "
        "OR i.video_part_id!=e.video_part_id OR (parent.edition_id IS NOT NULL AND parent.video_part_id!=e.video_part_id) LIMIT 1"
    ).fetchone() or connection.execute(
        "SELECT 1 FROM publication_heads h JOIN publication_editions e ON e.edition_id=h.current_edition_id "
        "LEFT JOIN publication_releases r ON r.release_id=h.current_release_id "
        "WHERE e.video_part_id!=h.video_part_id OR "
        "(h.current_release_id IS NOT NULL AND (r.video_part_id!=h.video_part_id OR r.status!='published')) LIMIT 1"
    ).fetchone():
        raise MigrationArtifactError("legacy publication review or head identity is inconsistent")
    for raw, sha256, part, bvid, page in connection.execute(
        "SELECT e.content_json,e.content_sha256,e.video_part_id,p.bvid,p.page_index "
        "FROM publication_editions e JOIN video_parts p ON p.video_part_id=e.video_part_id"
    ):
        content = legacy_object(raw)
        expected = {"bvid": bvid, "pageIndex": page, "videoPartId": part,
                    "url": f"https://www.bilibili.com/video/{bvid}/?p={page + 1}"}
        if content.get("source") != expected or _canonical(content) != raw or _digest(content) != sha256:
            raise MigrationArtifactError("legacy publication frozen content identity is inconsistent")
    _legacy_review_history(connection)
    for row in connection.execute(
        "SELECT l.release_id,l.video_part_id,l.edition_id,l.review_id,l.content_sha256,l.template_version,"
        "l.relative_path,l.artifact_sha256,l.status,l.published_by,e.video_part_id,e.content_sha256,e.content_json,"
        "r.review_id,r.content_sha256,r.status,h.current_release_id "
        "FROM publication_releases l JOIN publication_editions e ON e.edition_id=l.edition_id "
        "JOIN publication_edition_reviews r ON r.edition_id=e.edition_id "
        "LEFT JOIN publication_heads h ON h.video_part_id=l.video_part_id"
    ):
        rid, part, edition, review, sha256, version, path, artifact, status, publisher, epart, esha, raw, rreview, rsha, rstatus, head = row
        expected_id = _digest({"edition_id": edition, "content_sha256": sha256, "template_version": "publish-v1"})
        if (version != "publish-v1" or rid != expected_id or part != epart or review != rreview
                or sha256 != esha or sha256 != rsha or rstatus != "approved"
                or (status == "published") != (head == rid)
                or path != f"publications/part-{part}/{rid}/publish-v1/publish.md"
                or hashlib.sha256(render_publish_v1(legacy_object(raw))).hexdigest() != artifact):
            raise MigrationArtifactError("legacy release is not bound to its exact approved edition")
        previous = None
        transitions = {None: {"published"}, "published": {"superseded", "withdrawn"},
                       "superseded": {"withdrawn"}, "withdrawn": set()}
        for event_part, event_edition, event_review, event_sha, kind, old, new, actor in connection.execute(
            "SELECT video_part_id,edition_id,review_id,content_sha256,event_type,from_status,to_status,actor "
            "FROM publication_events WHERE release_id=? ORDER BY event_id", (rid,),
        ):
            if (kind not in transitions.get(previous, set()) or old != previous or new != kind
                    or (event_part, event_edition, event_review, event_sha) != (part, edition, review, sha256)
                    or (kind == "published" and actor != publisher)):
                raise MigrationArtifactError("legacy release audit identity or transition is inconsistent")
            previous = kind
        if previous != status:
            raise MigrationArtifactError("legacy release state disagrees with audit history")


def _legacy_editorial_checks(connection: sqlite3.Connection) -> None:
    for input_id, prepared_raw, part, base, reference, bvid, page in connection.execute(
        "SELECT i.input_id,i.prepared_json,i.video_part_id,i.base_transcript_id,i.reference_transcript_id,"
        "p.bvid,p.page_index FROM editorial_inputs i JOIN video_parts p ON p.video_part_id=i.video_part_id"
    ):
        prepared = legacy_object(prepared_raw)
        snapshot = prepared["snapshot"]
        metadata = snapshot["metadata"]
        if (prepared["input_id"] != input_id or _digest(snapshot) != input_id
                or snapshot["video_part_id"] != part or snapshot["base"]["transcript_id"] != base
                or (snapshot["reference"]["transcript_id"] if snapshot["reference"] else None) != reference
                or (metadata["bvid"], metadata["page_index"]) != (bvid, page)):
            raise MigrationArtifactError("legacy AI frozen input identity is inconsistent")
        for transcript in (base, reference):
            if transcript is not None and connection.execute(
                "SELECT video_part_id FROM transcripts WHERE transcript_id=?", (transcript,),
            ).fetchone() != (part,):
                raise MigrationArtifactError("legacy AI transcript belongs to another part")
    if connection.execute(
        "SELECT 1 FROM editorial_job_inputs b JOIN editorial_inputs i ON i.input_id=b.input_id "
        "JOIN workflow_jobs j ON j.job_id=b.job_id WHERE j.video_part_id!=i.video_part_id "
        "OR j.kind!='proofread' LIMIT 1"
    ).fetchone():
        raise MigrationArtifactError("legacy AI frozen job input binding is inconsistent")
    for revision, input_id, blocks_raw, prepared_raw, job_part, part, kind, bound_input in connection.execute(
        "SELECT r.revision_id,r.input_id,r.blocks_json,i.prepared_json,j.video_part_id,i.video_part_id,j.kind,b.input_id "
        "FROM editorial_revisions r JOIN editorial_inputs i ON i.input_id=r.input_id "
        "JOIN workflow_jobs j ON j.job_id=r.job_id LEFT JOIN editorial_job_inputs b ON b.job_id=r.job_id"
    ):
        prepared = legacy_object(prepared_raw)
        blocks = json.loads(blocks_raw, object_pairs_hook=_pairs)
        if (not isinstance(blocks, list) or _digest({"input_id": input_id, "blocks": blocks}) != revision
                or part != job_part or kind != "proofread" or bound_input != input_id):
            raise MigrationArtifactError("legacy AI frozen revision identity or job binding is inconsistent")
        # A committed revision can survive rendering failure without any registrations.
        if not connection.execute("SELECT 1 FROM document_artifacts WHERE revision_id=?", (revision,)).fetchone():
            continue
        documents = render_ai_v1(prepared["snapshot"]["metadata"], prepared, blocks, revision)
        for name, sha256 in connection.execute(
            "SELECT artifact_name,content_sha256 FROM document_artifacts WHERE revision_id=?", (revision,),
        ):
            if hashlib.sha256(documents[name].encode("utf-8")).hexdigest() != sha256:
                raise MigrationArtifactError("legacy AI document hash does not match frozen revision")


def _legacy_artifact_references(connection: sqlite3.Connection) -> dict[str, dict]:
    """Enumerate every retained generation, independently of current readers."""
    references: dict[str, dict] = {}

    def add(path, sha256=None, size=None):
        if not isinstance(path, str) or not path:
            raise MigrationArtifactError("legacy artifact reference has no path")
        if sha256 is not None and (not isinstance(sha256, str) or not _HASH.fullmatch(sha256)):
            raise MigrationArtifactError("legacy artifact reference has invalid SHA-256")
        previous = references.setdefault(path, {"sha256": None, "size": None})
        for key, value in (("sha256", sha256), ("size", size)):
            if value is not None:
                if previous[key] is not None and previous[key] != value:
                    raise MigrationArtifactError("legacy artifact references disagree")
                previous[key] = value

    for path, sha256, size in connection.execute("SELECT storage_key, sha256, byte_size FROM audio_objects"):
        add(path, sha256, size)
    for (raw,) in connection.execute(
        "SELECT a.result_json FROM workflow_attempts a JOIN workflow_jobs j ON j.job_id=a.job_id "
        "WHERE a.outcome='succeeded' AND j.kind='audio'"
    ):
        value = legacy_object(raw)
        if value.get("sha256") is None:
            raise MigrationArtifactError("successful legacy audio attempt has no SHA-256")
        add(value.get("storage_key"), value.get("sha256"))
    for (raw,) in connection.execute("SELECT artifact_json FROM workflow_publications"):
        value = legacy_object(raw)
        if set(value) != set(LEGACY_BUNDLE_NAMES):
            raise MigrationArtifactError("legacy workflow publication needs a complete five-file bundle")
        directory = PurePosixPath(str(value["srt_path"])).parent
        if len(directory.parts) != 2 or directory.parts[0] != "transcripts":
            raise MigrationArtifactError("legacy workflow bundle has invalid directory")
        for key, basename in LEGACY_BUNDLE_NAMES.items():
            if value[key] != f"{directory}/{basename}":
                raise MigrationArtifactError("legacy workflow bundle paths disagree")
            add(value[key])
        add(f"{directory}/{LEGACY_MARKER}")
    pairs: dict[str, set[str]] = {}
    for revision, part, version, name, role, path, sha256 in connection.execute(
        "SELECT d.revision_id,i.video_part_id,d.template_version,d.artifact_name,"
        "d.manuscript_role,d.relative_path,d.content_sha256 FROM document_artifacts d "
        "JOIN editorial_revisions r ON r.revision_id=d.revision_id "
        "JOIN editorial_inputs i ON i.input_id=r.input_id"
    ):
        expected_role = {"ai-draft.md": "ai-draft", "review.md": "review-reference"}.get(name)
        if (version != "ai-draft-v1" or role != expected_role
                or path != f"documents/part-{part}/{revision}/ai-draft-v1/{name}"):
            raise MigrationArtifactError("legacy AI document identity is invalid")
        pairs.setdefault(revision, set()).add(name)
        add(path, sha256)
    if any(names != {"ai-draft.md", "review.md"} for names in pairs.values()):
        raise MigrationArtifactError("legacy AI documents require a complete paired draft and review")
    used = {row[0] for row in connection.execute("SELECT DISTINCT revision_id FROM publication_editions")}
    if used - pairs.keys():
        raise MigrationArtifactError("legacy edition requires a complete paired AI draft and review")
    _legacy_editorial_checks(connection)
    _legacy_publication_checks(connection)
    for path, sha256 in connection.execute("SELECT relative_path,artifact_sha256 FROM publication_releases"):
        add(path, sha256)
    return references


def legacy_artifact_references(connection: sqlite3.Connection) -> dict[str, dict]:
    """Bound malformed legacy JSON to diagnostics that reveal no content values."""
    try:
        return _legacy_artifact_references(connection)
    except MigrationArtifactError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as exc:
        raise MigrationArtifactError("legacy frozen content is malformed") from exc
