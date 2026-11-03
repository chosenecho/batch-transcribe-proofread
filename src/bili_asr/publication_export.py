"""Reader release/draft snapshots and explicitly selected private review material."""

from __future__ import annotations


from contextlib import contextmanager
import difflib
import hashlib
import json
import re
from pathlib import Path
import sqlite3
from typing import Iterator

from bili_asr.export_snapshot import ExportSnapshotError, checked_path, guard_output, json_bytes, replace_snapshot
from bili_asr.publication import content_from_ai, get_ai_artifacts, get_edition, render_publication, verify_release
from bili_asr.storage.database import require_manuscript_schema
from bili_asr.publication_models_v2 import content_from_ai_v2
from bili_asr.manuscript_renderers_v2 import render_publish_v2
from bili_asr.publication_origins import PROFILE, public_origin
from bili_asr.storage.import_origins import import_origin, read_baseline
from bili_asr.content_rights import (
    ContentRightsError, public_notice, require_public_rights, distribution_status, serialized_distribution,
)


def _catalog_version(articles, contract_profile):
    if any(article.get("sourceMetadata", {}).get("version") == "source-metadata-v2" for article in articles):
        return 4
    return 3 if contract_profile or any(article.get("contentVersion") == 2 for article in articles) else 2


def _source_fields(edition: dict) -> dict:
    source = edition["content"]["source"]
    if edition["content_version"] == 1:
        return {"bvid": source["bvid"], "pageIndex": source["pageIndex"], "sourceUrl": source["url"]}
    return {"platform": source["platform"], "externalVideoId": source["externalVideoId"],
            "partIndex": source["partIndex"], "sourceUrl": source["url"],
            "sourceMetadata": source["metadata"], "sourcePublishedAt": source["metadata"]["sourcePublishedAt"],
            "pubdateUnix": source["metadata"]["pubdateUnix"], "contentVersion": 2}


def _render_edition(edition: dict) -> bytes:
    return (render_publish_v2 if edition["content_version"] == 2 else render_publication)(edition["content"])


def _require_current_rights(connection: sqlite3.Connection, checks: list[tuple[str, bytes]]) -> None:
    for edition_id, review_document in checks:
        edition = get_edition(connection, edition_id)
        require_public_rights(connection, edition, review_document)


def _install_public_snapshot(
    connection: sqlite3.Connection,
    output: Path,
    *,
    manuscript_type: str,
    articles: list[dict],
    notices: list[dict],
    rights_checks: list[tuple[str, bytes]],
    files: dict[str, bytes],
    series: dict | None,
    origins: list[dict],
    contract_profile: str | None,
    products: list[dict] | None = None,
) -> int:
    """Drop permissions that expire during staging, then atomically install the result."""
    while True:
        denied = {
            edition_id for edition_id, _ in rights_checks
            if not distribution_status(connection, get_edition(connection, edition_id))["public"]
        }
        if denied:
            for article in articles:
                if article["editionId"] in denied:
                    files.pop(article["file"], None)
                    files.pop(article["reviewFile"], None)
            articles = [article for article in articles if article["editionId"] not in denied]
            notices = [notice for notice in notices if notice["editionId"] not in denied]
            rights_checks = [check for check in rights_checks if check[0] not in denied]
            origins = [origin for origin in origins if origin.get("editionId") not in denied]
            files["catalog.json"] = json_bytes({
                "schemaVersion": _catalog_version(articles, contract_profile),
                "manuscriptType": "publication-draft" if manuscript_type == "publication-draft-export" else "publication",
                "articles": articles,
            })
            files.pop("content-rights.json", None)
            if notices:
                files["content-rights.json"] = json_bytes({"schemaVersion": 1, "notices": notices})
            files.pop("series.json", None)
            _add_series(files, series, articles,
                        "publication-draft" if manuscript_type == "publication-draft-export" else "publication")
            files.pop("origins.json", None)
            _add_origins(files, origins,
                         "publication-draft" if manuscript_type == "publication-draft-export" else "publication",
                         contract_profile)
            if products is not None:
                products = [item for item in products if item["editionId"] not in denied]
                if products:
                    files["publication-products.json"] = json_bytes({"schema": "publication-products/v1",
                                                                      "products": products})
                else:
                    files.pop("publication-products.json", None)
        try:
            replace_snapshot(
                output,
                kind=manuscript_type,
                files=files,
                profile=contract_profile,
                before_install=lambda: _require_current_rights(connection, rights_checks),
            )
            return len(articles)
        except ContentRightsError:
            if not any(
                distribution_status(connection, get_edition(connection, edition_id))["status"] == "expired"
                for edition_id, _ in rights_checks
            ):
                raise


@contextmanager
def _read_snapshot(connection: sqlite3.Connection) -> Iterator[None]:
    owned = not connection.in_transaction
    if owned:
        connection.execute("BEGIN")
    try:
        yield
    finally:
        if owned:
            connection.rollback()


def _public_entry(release: dict, edition: dict, review_document: bytes) -> dict:
    content = edition["content"]
    source = content["source"]
    part_id = edition["video_part_id"]
    if source["videoPartId"] != part_id or release["video_part_id"] != part_id:
        raise ExportSnapshotError("publication source identity differs from its release")
    return {
        "manuscriptType": "publication",
        "slug": f"part-{part_id}",
        "title": content["title"],
        "summary": content["summary"],
        "tags": content["tags"],
        "attribution": content["attribution"],
        "editorNote": content["editorNote"],
        "releaseId": release["release_id"],
        "editionId": edition["edition_id"],
        "aiRevisionId": edition["revision_id"],
        "videoPartId": part_id,
        **_source_fields(edition),
        "contentSha256": edition["content_sha256"],
        "artifactSha256": release["artifact_sha256"],
        "templateVersion": release["template_version"],
        "publishedAt": release["published_at"],
        "file": f"articles/part-{part_id}/publish.md",
        "reviewFile": f"articles/part-{part_id}/review.md",
        "reviewArtifactSha256": hashlib.sha256(review_document).hexdigest(),
    }


def _select_heads(heads: list, identities: list[str] | None, key: str) -> list:
    if identities is None:
        return heads
    size = 32 if key == "current_edition_id" else 64
    if (not isinstance(identities, list) or any(not isinstance(identity, str) or not re.fullmatch(rf"[0-9a-f]{{{size}}}", identity) for identity in identities)
            or len(set(identities)) != len(identities)):
        raise ExportSnapshotError("export-scope: select unique, valid current version IDs")
    requested = set(identities)
    available = {head[key] for head in heads}
    if requested - available:
        raise ExportSnapshotError("export-scope: selected version is unknown or no longer current")
    return [head for head in heads if head[key] in requested]


@serialized_distribution
def export_publications(
    connection: sqlite3.Connection,
    *,
    artifact_roots: tuple[Path, ...],
    output: Path,
    series_file: Path | None = None,
    contract_profile: str | None = None,
    release_ids: list[str] | None = None,
) -> int:
    """Export only valid current releases, failing the whole export on corruption."""
    require_manuscript_schema(connection)
    output = guard_output(connection, output, artifact_roots)
    series = _read_series_source(series_file, output)
    files: dict[str, bytes] = {}
    articles = []
    origins = []
    notices = []
    rights_checks = []
    products = []
    with _read_snapshot(connection):
        broken_release = connection.execute(
            "SELECT r.release_id FROM publication_releases r "
            "LEFT JOIN publication_heads h ON h.video_part_id = r.video_part_id "
            "WHERE r.status = 'published' AND h.current_release_id IS NOT r.release_id LIMIT 1"
        ).fetchone()
        if broken_release is not None:
            raise ExportSnapshotError("published release has no matching current publication head")
        # Read heads directly: a broken release relationship must not disappear
        # through an inner join and become an apparently successful empty export.
        heads = connection.execute(
            "SELECT video_part_id, current_release_id FROM publication_heads "
            "WHERE current_release_id IS NOT NULL ORDER BY video_part_id"
        ).fetchall()
        heads = _select_heads(heads, release_ids, "current_release_id")
        for head in heads:
            candidate = connection.execute(
                "SELECT edition_id FROM publication_releases WHERE release_id = ?",
                (head["current_release_id"],),
            ).fetchone()
            if candidate is None:
                raise ExportSnapshotError("publication head points to an unknown release")
            if not distribution_status(connection, get_edition(connection, candidate["edition_id"]))["public"]:
                continue
            release, edition, document = verify_release(connection, str(head["current_release_id"]), artifact_roots)
            if release["status"] != "published" or release["video_part_id"] != head["video_part_id"]:
                raise ExportSnapshotError("publication head does not identify a valid current release")
            if not distribution_status(connection, edition)["public"]:
                continue
            artifacts = get_ai_artifacts(connection, edition["revision_id"], artifact_roots)
            _collect_origin(connection, edition, artifact_roots, contract_profile, origins)
            review_document = artifacts["review.md"]
            review_document.decode("utf-8")
            permission = distribution_status(connection, edition, review_document)
            if not permission["public"]:
                continue
            if permission["status"] == "granted":
                notices.append(public_notice(edition, permission))
                rights_checks.append((edition["edition_id"], review_document))
            entry = _public_entry(release, edition, review_document)
            if entry["file"] in files:
                raise ExportSnapshotError("duplicate public article identity")
            if hashlib.sha256(document).hexdigest() != entry["artifactSha256"]:
                raise ExportSnapshotError("release artifact hash mismatch")
            document.decode("utf-8")
            files[entry["file"]] = document
            files[entry["reviewFile"]] = review_document
            package_row = connection.execute(
                "SELECT relative_path,package_sha256 FROM publication_packages WHERE release_id=?",
                (release["release_id"],),
            ).fetchone()
            if package_row is None:
                raise ExportSnapshotError("release package is missing")
            from bili_asr.services.artifact_readers import read_retained_text
            package = json.loads(read_retained_text(connection, package_row["relative_path"],
                                                    package_row["package_sha256"], artifact_roots))
            if package.get("schema") != "publication-package/v1" or package.get("edition_id") != edition["edition_id"]:
                raise ExportSnapshotError("release package identity is invalid")
            asr_files = []
            for role, item in package["artifacts"]["asr_transcript"]["artifacts"].items():
                relative = item["path"]
                output_path = f"transcripts/part-{edition['video_part_id']}/{Path(relative).name}"
                data = read_retained_text(connection, relative, item["sha256"], artifact_roots)
                files[output_path] = data
                asr_files.append({"role": role, "path": output_path, "sha256": item["sha256"]})
            products.append({
                "releaseId": release["release_id"], "editionId": edition["edition_id"],
                "metadata": package["metadata"],
                "artifacts": [
                    {"type": "asr-transcript", "files": asr_files},
                    {"type": "ai-comparison", "files": [{"path": entry["reviewFile"],
                        "sha256": hashlib.sha256(review_document).hexdigest()}]},
                    {"type": "ai-result", "files": [{"path": entry["file"],
                        "sha256": hashlib.sha256(document).hexdigest()}]},
                ],
            })
            articles.append(entry)
    articles.sort(key=lambda entry: (-entry["publishedAt"], entry["videoPartId"]))
    version = _catalog_version(articles, contract_profile)
    files["catalog.json"] = json_bytes({"schemaVersion": version, "manuscriptType": "publication", "articles": articles})
    if products:
        files["publication-products.json"] = json_bytes({"schema": "publication-products/v1", "products": products})
    _add_series(files, series, articles, "publication")
    _add_origins(files, origins, "publication", contract_profile)
    if notices:
        files["content-rights.json"] = json_bytes({"schemaVersion": 1, "notices": notices})
    return _install_public_snapshot(
        connection, output, manuscript_type="publication-export", articles=articles,
        notices=notices, rights_checks=rights_checks, files=files, series=series,
        origins=origins, contract_profile=contract_profile, products=products,
    )


@serialized_distribution
def export_publication_drafts(
    connection: sqlite3.Connection,
    *,
    artifact_roots: tuple[Path, ...],
    output: Path,
    series_file: Path | None = None,
    contract_profile: str | None = None,
    edition_ids: list[str] | None = None,
) -> int:
    """Export current reader drafts that have never had a release of any status."""
    require_manuscript_schema(connection)
    output = guard_output(connection, output, artifact_roots)
    series = _read_series_source(series_file, output)
    files: dict[str, bytes] = {}
    articles = []
    origins = []
    notices = []
    rights_checks = []
    with _read_snapshot(connection):
        missing_head = connection.execute(
            "SELECT e.video_part_id FROM publication_editions e "
            "LEFT JOIN publication_heads h ON h.video_part_id = e.video_part_id "
            "WHERE h.video_part_id IS NULL LIMIT 1"
        ).fetchone()
        if missing_head is not None:
            raise ExportSnapshotError("edition part has no current draft head")
        broken_release = connection.execute(
            "SELECT r.release_id FROM publication_releases r "
            "LEFT JOIN publication_heads h ON h.video_part_id = r.video_part_id "
            "WHERE r.status = 'published' AND h.current_release_id IS NOT r.release_id LIMIT 1"
        ).fetchone()
        if broken_release is not None:
            raise ExportSnapshotError("published release has no matching current publication head")
        # Read heads before checking eligibility. An invalid pointer must fail
        # rather than vanish through a join or become an empty draft snapshot.
        heads = connection.execute(
            "SELECT video_part_id, current_edition_id FROM publication_heads ORDER BY video_part_id"
        ).fetchall()
        heads = _select_heads(heads, edition_ids, "current_edition_id")
        for head in heads:
            edition = get_edition(connection, head["current_edition_id"])
            if (edition["video_part_id"] != head["video_part_id"]
                    or edition["current_edition_id"] != edition["edition_id"]):
                raise ExportSnapshotError("draft head does not identify a current edition of the same part")
            released = connection.execute(
                "SELECT 1 FROM publication_releases WHERE edition_id = ? LIMIT 1",
                (edition["edition_id"],),
            ).fetchone()
            if released is not None:
                if edition_ids is not None:
                    raise ExportSnapshotError("export-scope: selected draft edition has already had a release")
                continue
            if not distribution_status(connection, edition)["public"]:
                continue
            # Verify both immutable AI artifacts before exporting the original
            # review reference. Model-call audits remain outside the snapshot.
            artifacts = get_ai_artifacts(connection, edition["revision_id"], artifact_roots)
            _collect_origin(connection, edition, artifact_roots, contract_profile, origins)
            review_document = artifacts["review.md"]
            review_document.decode("utf-8")
            permission = distribution_status(connection, edition, review_document)
            if not permission["public"]:
                continue
            if permission["status"] == "granted":
                notices.append(public_notice(edition, permission))
                rights_checks.append((edition["edition_id"], review_document))
            content = edition["content"]
            document = _render_edition(edition)
            slug = f"edition-{edition['edition_id']}"
            entry = {
                "manuscriptType": "publication-draft", "slug": slug,
                "title": content["title"], "summary": content["summary"], "tags": content["tags"],
                "attribution": content["attribution"], "editorNote": content["editorNote"],
                "editionId": edition["edition_id"], "aiRevisionId": edition["revision_id"],
                "videoPartId": edition["video_part_id"], **_source_fields(edition),
                "contentSha256": edition["content_sha256"],
                "artifactSha256": hashlib.sha256(document).hexdigest(),
                "reviewStatus": edition["review_status"], "createdAt": edition["created_at"],
                "file": f"drafts/{slug}/preview.md",
                "reviewFile": f"drafts/{slug}/review.md",
                "reviewArtifactSha256": hashlib.sha256(review_document).hexdigest(),
            }
            if entry["file"] in files:
                raise ExportSnapshotError("duplicate reader draft identity")
            files[entry["file"]] = document
            files[entry["reviewFile"]] = review_document
            articles.append(entry)
    articles.sort(key=lambda entry: (-entry["createdAt"], entry["videoPartId"], entry["editionId"]))
    version = _catalog_version(articles, contract_profile)
    files["catalog.json"] = json_bytes({"schemaVersion": version, "manuscriptType": "publication-draft", "articles": articles})
    _add_series(files, series, articles, "publication-draft")
    _add_origins(files, origins, "publication-draft", contract_profile)
    if notices:
        files["content-rights.json"] = json_bytes({"schemaVersion": 1, "notices": notices})
    return _install_public_snapshot(
        connection, output, manuscript_type="publication-draft-export", articles=articles,
        notices=notices, rights_checks=rights_checks, files=files, series=series,
        origins=origins, contract_profile=contract_profile,
    )


def _collect_origin(connection, edition, roots, profile, origins) -> None:
    from bili_asr.contracts.registry import ORIGIN_PROFILE_V2
    from bili_asr.source_supplements import DESCRIPTION_POLICY
    if profile is not None and profile not in {PROFILE, ORIGIN_PROFILE_V2}:
        raise ExportSnapshotError("unsupported export origin profile")
    if profile:
        origin = public_origin(connection, edition, roots)
        if origin.get("policyVersion") == DESCRIPTION_POLICY and profile != ORIGIN_PROFILE_V2:
            raise ExportSnapshotError("description-supplemented editions require universal-origin-v2 export profile")
        origins.append(origin)
    elif import_origin(connection, edition["edition_id"]) is not None:
        raise ExportSnapshotError("imported editions require universal-origin-v1 export profile")


def _add_origins(files, origins, manuscript_type, profile) -> None:
    from bili_asr.contracts.registry import ORIGIN_PROFILE_V2
    if profile is not None:
        if profile not in {PROFILE, ORIGIN_PROFILE_V2}:
            raise ExportSnapshotError("unsupported export origin profile")
        files["origins.json"] = json_bytes({"schemaVersion": 2 if profile == ORIGIN_PROFILE_V2 else 1, "manuscriptType": manuscript_type,
            "contractProfile": profile, "entries": sorted(origins, key=lambda entry: entry["editionId"])})


def _read_series_source(series_file: Path | None, output: Path) -> dict | None:
    if series_file is None:
        return None
    from bili_asr.publication_series import read_series
    path = checked_path(series_file)
    if path == output or path.is_relative_to(output):
        raise ExportSnapshotError("series editorial source cannot be inside the exported snapshot")
    return read_series(path)


def _add_series(files: dict[str, bytes], series: dict | None, articles: list, manuscript_type: str) -> None:
    if series is not None:
        from bili_asr.publication_series import project_series
        files["series.json"] = json_bytes(project_series(series, articles, manuscript_type))


def _difference(before: str, after: str, before_name: str, after_name: str) -> bytes:
    # Preserve missing-newline information in a conventional patch rather than
    # silently treating final-line differences as identical text.
    result = []
    for line in difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), fromfile=before_name, tofile=after_name):
        result.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(result).encode("utf-8")


def export_editorial(
    connection: sqlite3.Connection,
    *,
    revision_id: str,
    edition_id: str,
    artifact_roots: tuple[Path, ...],
    output: Path,
    include_provenance: bool = False,
) -> dict:
    """Export an exact edition and its immutable AI baseline for private review."""
    require_manuscript_schema(connection)
    output = guard_output(connection, output, artifact_roots)
    with _read_snapshot(connection):
        edition = get_edition(connection, edition_id)
        if edition["revision_id"] != revision_id:
            raise ExportSnapshotError("selected edition does not belong to the selected AI revision")
        artifacts = get_ai_artifacts(connection, revision_id, artifact_roots)
        baseline = artifacts["ai-draft.md"].decode("utf-8")
        artifacts["review.md"].decode("utf-8")
        content = edition["content"]
        parent_id = edition["parent_edition_id"]
        parent = get_edition(connection, parent_id) if parent_id else None
        if parent is not None and parent["video_part_id"] != edition["video_part_id"]:
            raise ExportSnapshotError("edition parent belongs to another video part")
        parent_content = parent["content"] if parent else None
        reviews = [dict(row) for row in connection.execute(
            "SELECT * FROM publication_edition_reviews WHERE edition_id = ? ORDER BY updated_at, review_id",
            (edition_id,),
        )]
        events = [dict(row) for row in connection.execute(
            "SELECT * FROM publication_events WHERE edition_id = ? ORDER BY event_id", (edition_id,),
        )]
        row = connection.execute(
            "SELECT r.quality_status, r.created_at, r.blocks_json, i.prepared_json FROM editorial_revisions r "
            "JOIN editorial_inputs i ON i.input_id = r.input_id WHERE r.revision_id = ?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise ExportSnapshotError("selected AI revision has no fixed input")
        prepared = json.loads(row["prepared_json"])
        from bili_asr.editorial_lineage import source_lineage
        lineage = source_lineage(prepared, json.loads(row["blocks_json"]), revision_id=revision_id)
        from bili_asr.storage.archive_contracts import frozen_version
        version = frozen_version(connection, "input", prepared["input_id"])
        ai_content = (content_from_ai_v2 if version == 2 else content_from_ai)(prepared, baseline)
        ai_metadata = {
            "revisionId": revision_id,
            "templateVersion": "ai-draft-v2" if version == 2 else "ai-draft-v1",
            "qualityStatus": row["quality_status"],
            "createdAt": row["created_at"],
            "config": prepared["snapshot"]["config"],
            "promptSha256": prepared["snapshot"]["prompt_sha256"],
            "artifactSha256": {name: hashlib.sha256(value).hexdigest() for name, value in artifacts.items()},
            "sourceLineage": lineage,
        }
        review_document = {
            "schemaVersion": 1,
            "manuscriptType": "editorial-review",
            "revisionId": revision_id,
            "editionId": edition_id,
            "videoPartId": edition["video_part_id"],
            "contentSha256": edition["content_sha256"],
            "reviewStatus": edition["review_status"],
            "parentEditionId": parent_id,
            "parentContentSha256": parent["content_sha256"] if parent else None,
            "reviews": reviews,
            "events": events,
            "ai": ai_metadata,
        }
        # Full-object parent patches also show reader-visible metadata changes.
        files = {
            **artifacts,
            "edition.md": _render_edition(edition),
            "edition.json": json_bytes(content),
            "review.json": json_bytes(review_document),
            "differences/ai.patch": _difference(json_bytes(ai_content).decode("utf-8"), json_bytes(content).decode("utf-8"),
                                                 "ai-baseline.json", "edition.json"),
            "differences/parent.patch": _difference(
                json_bytes(parent_content).decode("utf-8"), json_bytes(content).decode("utf-8"),
                f"edition-{parent_id}.json", "edition.json",
            ) if parent else _difference(json_bytes(ai_content).decode("utf-8"), json_bytes(content).decode("utf-8"),
                                         "ai-baseline.json", "edition.json"),
        }
        origin = import_origin(connection, edition_id)
        if origin is not None:
            from bili_asr.services.artifact_readers import read_retained_text
            evidence = read_baseline(connection, origin["import_id"])
            public = public_origin(connection, edition, artifact_roots)
            preserved = read_retained_text(connection, evidence["body_path"], evidence["body_sha256"], artifact_roots)
            files.update({"import-origin.json": json_bytes({"origin": public, "baseline": evidence["baseline"]}),
                "preserved-body.md": preserved,
                "differences/preserved.patch": _difference(preserved.decode("utf-8"), content["markdown"], "preserved-body.md", "edition-body.md")})
        if include_provenance:
            from bili_asr.services.provenance import trace
            files["provenance.json"] = json_bytes(trace(connection, edition_id=edition_id, artifact_roots=artifact_roots))
    snapshot_id = replace_snapshot(output, kind="editorial-export", files=files,
                                   profile="private-provenance-v1" if include_provenance else None)
    return {"revision_id": revision_id, "edition_id": edition_id, "content_sha256": edition["content_sha256"], "snapshot_id": snapshot_id, "output": str(output)}
