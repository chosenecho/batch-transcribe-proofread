"""Frozen reader content, exact-version approval and explicit publication."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Iterable
from urllib.parse import urlparse

from bili_asr.canonical_json import digest
from bili_asr.manuscript_templates import PUBLISH_RENDERERS, renderer_for
from bili_asr.manuscript_artifacts import get_ai_artifacts
from bili_asr.manuscript_files import atomic_write_artifact, read_artifact
from bili_asr.storage.publication import (
    PublicationConflictError, PublicationRepository, read_edition as get_edition,
    read_revision as _revision, verify_release_identity as _verify_release_identity,
)
from bili_asr.publication_content import (
    EDITABLE_METADATA as _EDITABLE_METADATA, PUBLISH_TEMPLATE_VERSION,
    normalize_actor as _actor, normalize_text as _text, normalize_content,
    content_from_ai, render_publication,
)
from bili_asr.publication_tags import source_tags
from bili_asr.storage.archive_contracts import frozen_version
from bili_asr.publication_models_v2 import content_from_ai_v2, normalize_content_v2


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _read_bundle_manifest(connection: sqlite3.Connection, *, part_id: int,
                           transcript_id: int, artifact_roots: tuple[Path, ...]) -> dict:
    from bili_asr.archive import archive_bundle_complete

    row = connection.execute(
        "SELECT artifact_json FROM workflow_publications WHERE video_part_id=? AND transcript_id=?",
        (part_id, transcript_id),
    ).fetchone()
    if row is None:
        raise ValueError("publication-package: ASR transcript bundle is not published")
    paths = json.loads(row["artifact_json"])
    if not isinstance(paths, dict) or set(paths) != {"srt_path", "vtt_path", "txt_path", "md_path", "raw_path"}:
        raise ValueError("publication-package: ASR bundle paths are incomplete")
    marker_path = Path(paths["srt_path"]).parent / ".bundle-ready"
    for root in artifact_roots:
        if not archive_bundle_complete(root, paths):
            continue
        candidate = Path(root) / marker_path
        if not candidate.is_file():
            continue
        marker = json.loads(candidate.read_text(encoding="ascii"))
        artifacts = marker.get("artifacts")
        if not isinstance(artifacts, dict):
            continue
        entries = {name: {"path": item["path"], "sha256": item["sha256"]}
                   for name, item in artifacts.items()
                   if isinstance(item, dict) and isinstance(item.get("path"), str)
                   and isinstance(item.get("sha256"), str)}
        if set(entries) not in ({"srt_path", "vtt_path", "txt_path", "md_path", "raw_path"},
                                {"srt_path", "vtt_path", "txt_path", "md_path", "raw_path", "source_metadata_path"}):
            continue
        return {"kind": "asr-transcript", "transcript_id": transcript_id, "artifacts": entries}
    raise ValueError("publication-package: ASR transcript bundle is unavailable")


def _write_publication_package(connection: sqlite3.Connection, *, edition: dict,
                               revision_id: str, ai_artifacts: dict[str, bytes],
                               publish_path: str, publish_bytes: bytes, ai_template: str,
                               artifact_roots: tuple[Path, ...], write_root: Path) -> tuple[str, str]:
    revision, prepared = _revision(connection, revision_id)
    base_transcript_id = int(prepared["snapshot"]["base"]["transcript_id"])
    if revision is None:
        raise ValueError("publication-package: revision has no ASR input")
    bundle = _read_bundle_manifest(connection, part_id=int(edition["video_part_id"]),
                                   transcript_id=base_transcript_id,
                                   artifact_roots=artifact_roots)
    prefix = f"documents/part-{edition['video_part_id']}/{revision_id}/"
    metadata = prepared["snapshot"]["metadata"]
    comparison = {"path": prefix + f"{ai_template}/review.md",
                  "sha256": hashlib.sha256(ai_artifacts["review.md"]).hexdigest()}
    result = {"path": publish_path, "sha256": hashlib.sha256(publish_bytes).hexdigest()}
    package = {
        "schema": "publication-package/v1",
        "video_part_id": int(edition["video_part_id"]),
        "edition_id": edition["edition_id"],
        "revision_id": revision_id,
        "metadata": metadata,
        "artifacts": {
            "asr_transcript": {**bundle, "metadata": metadata},
            "ai_comparison": {**comparison, "metadata": metadata},
            "ai_result": {**result, "metadata": metadata},
        },
    }
    data = (json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    manifest_path = str(Path(publish_path).with_name("publication-package.json")).replace("\\", "/")
    atomic_write_artifact(Path(write_root), manifest_path, data)
    return manifest_path, hashlib.sha256(data).hexdigest()


def _roots(artifact_roots: Iterable[Path]) -> tuple[Path, ...]:
    # CLI callers normally pass read_bases(); accepting ArtifactRoots keeps the
    # filesystem policy in one place without making it a service dependency.
    if hasattr(artifact_roots, "read_bases"):
        artifact_roots = artifact_roots.read_bases()
    roots = tuple(Path(root) for root in artifact_roots)
    if not roots:
        raise ValueError("manuscript-path: at least one artifact root is required")
    return roots


def create_edition(connection: sqlite3.Connection, *, revision_id: str,
                   artifact_roots: Iterable[Path], actor: str, note: str = "",
                   expected_edition_id: str | None = None) -> dict:
    actor, note = _actor(actor), _text(note, "note")
    repository = PublicationRepository(connection)
    artifacts = get_ai_artifacts(connection, revision_id, artifact_roots)
    with repository.transaction():
        revision, prepared = _revision(connection, revision_id)
        version = frozen_version(connection, "input", prepared["input_id"])
        if version == 2:
            content = content_from_ai_v2(prepared, artifacts["ai-draft.md"].decode("utf-8"))
            content["tags"] = list(content["source"]["metadata"]["tags"])
            content = normalize_content_v2(content)
        else:
            content = content_from_ai(prepared, artifacts["ai-draft.md"].decode("utf-8"))
            content["tags"] = source_tags(connection, content["source"]["bvid"])
            content = normalize_content(content)
        return repository.insert_edition(
            part_id=revision["video_part_id"], revision_id=revision_id, content=content,
            parent_edition_id=expected_edition_id, actor=actor, note=note, event_type="created", content_version=version,
        )


def edit_edition(connection: sqlite3.Connection, *, edition_id: str, markdown_text: str,
                 metadata: dict | None = None, actor: str, note: str) -> dict:
    actor, note = _actor(actor), _text(note, "note", nonempty=True)
    if metadata is not None and (not isinstance(metadata, dict) or set(metadata) - _EDITABLE_METADATA):
        raise ValueError("publication-content: metadata contains unsupported or immutable fields")
    repository = PublicationRepository(connection)
    with repository.transaction():
        parent = get_edition(connection, edition_id)
        content = deepcopy(parent["content"])
        content["markdown"] = markdown_text
        content.update(metadata or {})
        version = parent["content_version"]
        normalize = normalize_content_v2 if version == 2 else normalize_content
        return repository.insert_edition(
            part_id=parent["video_part_id"], revision_id=parent["revision_id"],
            content=normalize(content), parent_edition_id=edition_id,
            actor=actor, note=note, event_type="edited", content_version=version,
        )


def review_edition(connection: sqlite3.Connection, *, edition_id: str, status: str,
                   content_sha256: str, expected_status: str, actor: str,
                   note: str = "", issue_url: str | None = None) -> dict:
    actor, note = _actor(actor), _text(note, "note")
    if not isinstance(content_sha256, str) or not _SHA256.fullmatch(content_sha256):
        raise ValueError("publication-approval: invalid reviewed content hash")
    if issue_url is not None:
        issue_url = _text(issue_url, "issue URL", nonempty=True, multiline=False)
        parsed = urlparse(issue_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("publication-review: issue URL must be an absolute HTTPS URL")
    repository = PublicationRepository(connection)
    with repository.transaction():
        get_edition(connection, edition_id)
        return repository.set_review(
            edition_id=edition_id, status=status, content_sha256=content_sha256,
            expected_status=expected_status, actor=actor, note=note, issue_url=issue_url,
        )


def verify_release(connection: sqlite3.Connection, release_id: str,
                   artifact_roots: Iterable[Path]) -> tuple[dict, dict, bytes]:
    repository = PublicationRepository(connection)
    roots = _roots(artifact_roots)
    release = repository.release(release_id)
    edition = _verify_release_identity(connection, release)
    from bili_asr.storage.import_origins import import_origin
    if import_origin(connection, edition["edition_id"]) is not None:
        from bili_asr.preserved_body_evidence import check_preserved_body_import
        check_preserved_body_import(connection, edition_id=edition["edition_id"], artifact_roots=artifact_roots)
    from bili_asr.services.artifact_readers import read_retained_text
    data = read_retained_text(connection, release["relative_path"], release["artifact_sha256"], roots)
    from bili_asr.storage.publication_packages import package_record
    package = package_record(connection, release_id)
    if package is None:
        return release, edition, data
    package = connection.execute(
        "SELECT relative_path,package_sha256 FROM publication_packages WHERE release_id=?",
        (release_id,),
    ).fetchone()
    if package is None:
        raise ValueError("publication-integrity: release package is missing")
    package_bytes = read_artifact(package["relative_path"], package["package_sha256"], roots)
    try:
        manifest = json.loads(package_bytes)
        products = manifest["artifacts"]
        if (manifest["schema"] != "publication-package/v1"
                or manifest["video_part_id"] != release["video_part_id"]
                or manifest["edition_id"] != edition["edition_id"]
                or manifest["revision_id"] != edition["revision_id"]
                or set(products) != {"asr_transcript", "ai_comparison", "ai_result"}
                or products["ai_result"].get("path") != release["relative_path"]
                or products["ai_result"].get("sha256") != release["artifact_sha256"]):
            raise ValueError("publication-integrity: release package identities differ")
        comparison = products["ai_comparison"]
        ai_pair = get_ai_artifacts(connection, edition["revision_id"], roots)
        expected_comparison = hashlib.sha256(ai_pair["review.md"]).hexdigest()
        if (comparison["sha256"] != expected_comparison
                or comparison["path"] != f"documents/part-{edition['video_part_id']}/{edition['revision_id']}/"
                                           f"{'ai-draft-v2' if edition['content_version'] == 2 else 'ai-draft-v1'}/review.md"):
            raise ValueError("publication-integrity: comparison artifact identity differs")
        read_artifact(comparison["path"], comparison["sha256"], roots)
        _revision_row, prepared = _revision(connection, edition["revision_id"])
        expected_bundle = _read_bundle_manifest(
            connection, part_id=int(edition["video_part_id"]),
            transcript_id=int(prepared["snapshot"]["base"]["transcript_id"]), artifact_roots=roots)
        if products["asr_transcript"] != {**expected_bundle, "metadata": manifest["metadata"]}:
            raise ValueError("publication-integrity: ASR transcript identity differs")
        bundle = products["asr_transcript"]["artifacts"]
        for item in bundle.values():
            read_artifact(item["path"], item["sha256"], roots)
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("publication-integrity: release package is malformed") from error
    return release, edition, data


def publish_edition(connection: sqlite3.Connection, *, edition_id: str,
                    artifact_roots: Iterable[Path], write_root: Path, actor: str,
                    expected_release_id: str | None = None) -> dict:
    actor = _actor(actor)
    from bili_asr.content_rights import require_publication_enabled, require_public_rights
    require_publication_enabled()
    roots = _roots(artifact_roots)
    repository = PublicationRepository(connection)
    existing = repository.release_for_edition(edition_id)
    if existing:
        release, edition, _ = verify_release(connection, existing["release_id"], roots)
        require_public_rights(connection, edition, get_ai_artifacts(connection, edition['revision_id'], roots)['review.md'])
        return {**release, "idempotent": True}
    edition = get_edition(connection, edition_id)
    from bili_asr.storage.import_origins import import_origin
    if import_origin(connection, edition_id) is not None:
        from bili_asr.preserved_body_evidence import check_preserved_body_import
        check_preserved_body_import(connection, edition_id=edition_id, artifact_roots=roots)
    if edition["review_status"] != "approved":
        raise ValueError("publication-approval: edition has not been approved")
    require_public_rights(connection, edition, get_ai_artifacts(connection, edition['revision_id'], roots)['review.md'])
    if edition["current_release_id"] != expected_release_id:
        raise PublicationConflictError("publication-conflict: effective release differs from expected release")
    if expected_release_id:
        verify_release(connection, expected_release_id, roots)
    template = "publish-v2" if edition["content_version"] == 2 else PUBLISH_TEMPLATE_VERSION
    data = renderer_for(PUBLISH_RENDERERS, template)(edition["content"])
    release_id = digest({"edition_id": edition_id, "content_sha256": edition["content_sha256"],
                         "template_version": template})
    path = f"publications/part-{edition['video_part_id']}/{release_id}/{template}/publish.md"
    atomic_write_artifact(Path(write_root), path, data)
    from bili_asr.storage.publication_packages import require_packages
    packaged = require_packages(connection)
    package_path = package_sha256 = None
    if packaged:
        package_path, package_sha256 = _write_publication_package(
        connection, edition=edition, revision_id=edition["revision_id"],
        ai_artifacts=get_ai_artifacts(connection, edition["revision_id"], roots),
        publish_path=path, publish_bytes=data, ai_template="ai-draft-v2" if edition["content_version"] == 2 else "ai-draft-v1",
        artifact_roots=roots, write_root=Path(write_root),
    )
    with repository.transaction():
        existing = repository.release_for_edition(edition_id)
        if existing:
            release, current, _ = verify_release(connection, existing["release_id"], roots)
            require_public_rights(connection, current, get_ai_artifacts(connection, current['revision_id'], roots)['review.md'])
            return {**release, "idempotent": True}
        current = get_edition(connection, edition_id)
        require_public_rights(connection, current, get_ai_artifacts(connection, current['revision_id'], roots)['review.md'])
        if current["content_sha256"] != edition["content_sha256"] or current["review_status"] != "approved":
            raise ValueError("publication-approval: approval or content changed before publication")
        # Verify installed bytes immediately before registering the database head.
        read_artifact(path, hashlib.sha256(data).hexdigest(), (Path(write_root),))
        release = {
            "release_id": release_id, "video_part_id": edition["video_part_id"],
            "edition_id": edition_id, "review_id": edition["review_id"],
            "content_sha256": edition["content_sha256"], "template_version": template,
            "relative_path": path, "artifact_sha256": hashlib.sha256(data).hexdigest(),
            "published_at": int(time.time()), "published_by": actor, "status": "published",
        }
        registered = repository.register_release(edition=current, release=release,
                                                 expected_release_id=expected_release_id, actor=actor)
        if packaged:
            connection.execute(
                "INSERT INTO publication_packages(release_id,relative_path,package_sha256) VALUES (?,?,?)",
                (registered["release_id"], package_path, package_sha256),
            )
        return {**registered, "package_path": package_path, "package_sha256": package_sha256, "idempotent": False}


def withdraw_release(connection: sqlite3.Connection, *, release_id: str, actor: str, note: str) -> dict:
    actor, note = _actor(actor), _text(note, "note", nonempty=True)
    repository = PublicationRepository(connection)
    with repository.transaction():
        release = repository.release(release_id)
        _verify_release_identity(connection, release)
        return repository.withdraw(release_id=release_id, actor=actor, note=note)


__all__ = ["PublicationConflictError", "create_edition", "edit_edition", "review_edition",
           "publish_edition", "withdraw_release", "get_edition", "verify_release",
           "get_ai_artifacts", "normalize_content", "content_from_ai", "render_publication", "PUBLISH_TEMPLATE_VERSION"]
