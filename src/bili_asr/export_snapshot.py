"""Locked, recoverable replacement of a complete managed export directory."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from typing import Mapping
import uuid

from bili_asr.file_ops import (
    FileOperationError as ExportSnapshotError, checked_path, json_bytes,
    exclusive_lock as _exclusive_lock, fsync_directory as _fsync_directory,
    write_new_file as _write_file,
)
from bili_asr.contracts.json_schema import validate_json
from bili_asr.contracts.registry import catalog_contract, manifest_contract

_REVIEW_STATUSES = frozenset({"pending-review", "in-review", "changes-requested", "approved", "rejected"})
_REVIEW_FILES = frozenset({"ai-draft.md", "review.md", "edition.md", "edition.json", "review.json", "differences/ai.patch", "differences/parent.patch"})
_IMPORT_REVIEW_FILES = frozenset({"import-origin.json", "preserved-body.md", "differences/preserved.patch"})


def _unique_object(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ExportSnapshotError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_bytes().decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ExportSnapshotError(f"invalid export JSON: {path.name}") from exc


def guard_output(connection: sqlite3.Connection, output: Path, artifact_roots: tuple[Path, ...]) -> Path:
    output = checked_path(output)
    if output.parent == output or not output.name:
        raise ExportSnapshotError("export output cannot be a filesystem root")
    sources = [checked_path(root) for root in artifact_roots]
    sources.extend(checked_path(Path(row[2]).parent) for row in connection.execute("PRAGMA database_list") if row[2])
    for source in sources:
        if output == source or output.is_relative_to(source) or source.is_relative_to(output):
            raise ExportSnapshotError(f"export output overlaps a source archive or artifact root: {source}")
    if output.exists() and not output.is_dir():
        raise ExportSnapshotError("export output must be a directory")
    return output


def _allowed_file(name: str, kind: str, profile: str | None = None) -> bool:
    if name == "provenance.json":
        return kind == "editorial-export" and profile == "private-provenance-v1"
    if name == "origins.json":
        return profile in {"universal-origin-v1", "universal-origin-v2"} and kind in {"publication-export", "publication-draft-export"}
    if kind == "publication-export":
        return (name in {"catalog.json", "content-rights.json", "series.json", "publication-products.json"}
                or re.fullmatch(r"articles/part-[1-9][0-9]*/(?:publish|review)\.md", name) is not None
                or re.fullmatch(r"transcripts/part-[1-9][0-9]*/bundle\.(?:srt|vtt|txt|md|raw\.json)|transcripts/part-[1-9][0-9]*/source-metadata\.json", name) is not None)
    if kind == "publication-draft-export":
        return name in {"catalog.json", "content-rights.json", "series.json"} or re.fullmatch(r"drafts/edition-[0-9a-f]{32}/(?:preview|review)\.md", name) is not None
    return name in _REVIEW_FILES | _IMPORT_REVIEW_FILES


def _contract_json(identity: str, value: object) -> None:
    try:
        validate_json(identity, value)
    except ValueError as exc:
        raise ExportSnapshotError(str(exc)) from exc


def _validate_article(article: object, *, draft: bool = False, catalog_version: int = 2) -> dict:
    _contract_json(catalog_contract(catalog_version, draft=draft), {
        "schemaVersion": catalog_version, "manuscriptType": "publication-draft" if draft else "publication",
        "articles": [article],
    })
    return _validate_article_semantics(article, draft=draft, catalog_version=catalog_version)


def _validate_article_semantics(article: dict, *, draft: bool, catalog_version: int) -> dict:
    """Check domain identities and file relations after structural validation."""
    universal = catalog_version in {3, 4} and isinstance(article, dict) and article.get("contentVersion") == 2
    # JSON Schema patterns use search semantics: `$` can match before a final
    # newline. Preserve the original exact identity checks at this boundary.
    hashes = ("aiRevisionId", "contentSha256", "artifactSha256", "reviewArtifactSha256")
    for key in hashes if draft else ("releaseId", *hashes):
        if re.fullmatch(r"[0-9a-f]{64}", article[key]) is None:
            raise ExportSnapshotError(f"invalid public article hash: {key}")
    if re.fullmatch(r"[0-9a-f]{32}", article["editionId"]) is None:
        raise ExportSnapshotError("invalid public edition ID")
    for key, minimum in (("videoPartId", 1), ("partIndex" if universal else "pageIndex", 0), ("createdAt" if draft else "publishedAt", 0)):
        if type(article[key]) is not int or article[key] < minimum:
            raise ExportSnapshotError(f"invalid public article integer: {key}")
    if not article["title"].strip() or not article["attribution"].strip():
        raise ExportSnapshotError("invalid public article title, attribution, or source")
    if universal:
        from bili_asr.platform_identity import ContentRef
        from bili_asr.source_identity import source_url
        from bili_asr.source_metadata import SourceMetadataSnapshot
        try:
            ref = ContentRef(article["platform"], article["externalVideoId"], article["partIndex"])
            metadata = SourceMetadataSnapshot.from_dict(article["sourceMetadata"])
            if (type(article["contentVersion"]) is not int or metadata.ref != ref
                    or article["sourcePublishedAt"] != metadata.to_dict()["sourcePublishedAt"]
                    or article["pubdateUnix"] != metadata.pubdate):
                raise ValueError("source snapshot mismatch")
            expected_url = source_url(ref)
        except (ValueError, TypeError, KeyError) as exc:
            raise ExportSnapshotError("invalid universal public source") from exc
    else:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", article["bvid"]):
            raise ExportSnapshotError("invalid public article source")
        expected_url = f"https://www.bilibili.com/video/{article['bvid']}/?p={article['pageIndex'] + 1}"
    tags = article["tags"]
    if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags) or len(set(tags)) != len(tags):
        raise ExportSnapshotError("invalid public article tags")
    slug = f"edition-{article['editionId']}" if draft else f"part-{article['videoPartId']}"
    manuscript_type = "publication-draft" if draft else "publication"
    filename = f"drafts/{slug}/preview.md" if draft else f"articles/{slug}/publish.md"
    review_filename = f"drafts/{slug}/review.md" if draft else f"articles/{slug}/review.md"
    if draft:
        if not isinstance(article["reviewStatus"], str) or article["reviewStatus"] not in _REVIEW_STATUSES:
            raise ExportSnapshotError("invalid draft review status")
    elif article["templateVersion"] != ("publish-v2" if universal else "publish-v1"):
        raise ExportSnapshotError("invalid public article template")
    if (article["manuscriptType"] != manuscript_type
            or article["slug"] != slug or article["file"] != filename or article["reviewFile"] != review_filename
            or article["sourceUrl"] != expected_url):
        raise ExportSnapshotError("public article identity or source URL mismatch")
    return article


def _file_parts(name: str, kind: str, profile: str | None = None) -> tuple[str, ...]:
    parts = PurePosixPath(name).parts
    if not _allowed_file(name, kind, profile) or PurePosixPath(name).as_posix() != name or any(part in {".", ".."} for part in parts):
        raise ExportSnapshotError(f"unmanaged export file: {name}")
    return parts


def _snapshot_id(files: list[dict[str, str]], kind: str | None = None, profile: str | None = None) -> str:
    value = files if profile is None else {"schemaVersion": 3 if profile == "universal-origin-v2" else 2, "manuscriptType": kind, "contractProfile": profile, "files": files}
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _scan(directory: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    checked_path(directory)
    if not directory.is_dir():
        raise ExportSnapshotError("export snapshot is not a directory")
    for current, children, names in os.walk(directory, followlinks=False):
        for name in (*children, *names):
            path = Path(current) / name
            checked_path(path)
            info = path.lstat()
            relative = path.relative_to(directory).as_posix()
            if stat.S_ISDIR(info.st_mode):
                directories.add(relative)
            elif stat.S_ISREG(info.st_mode):
                files.add(relative)
            else:
                raise ExportSnapshotError(f"export contains a nonregular file: {relative}")
    return files, directories


def _validate_snapshot(directory: Path, kind: str, *, allow_empty: bool = False) -> str | None:
    actual_files, actual_directories = _scan(directory)
    if not actual_files and not actual_directories and allow_empty:
        return None
    manifest_name = f"{kind}-manifest.json"
    if manifest_name not in actual_files:
        raise ExportSnapshotError(f"output is not a managed {kind} snapshot; legacy exports and manual files are refused")
    manifest = read_json(directory / manifest_name)
    profile = manifest.get("contractProfile") if isinstance(manifest, dict) else None
    imported = bool(actual_files & _IMPORT_REVIEW_FILES) and kind == "editorial-export"
    try:
        identity = manifest_contract(kind, profile, imported=imported)
    except ValueError as exc:
        raise ExportSnapshotError(str(exc)) from exc
    _contract_json(identity, manifest)
    if type(manifest["schemaVersion"]) is not int or manifest["manuscriptType"] != kind:
        raise ExportSnapshotError("unsupported export manifest contract")
    records = manifest["files"]
    if not isinstance(records, list) or not records:
        raise ExportSnapshotError("invalid export manifest file list")
    expected_files = {manifest_name}
    expected_directories: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            raise ExportSnapshotError("invalid export manifest file record")
        name, digest = record["path"], record["sha256"]
        if not isinstance(name, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ExportSnapshotError("invalid export file identity")
        _file_parts(name, kind, profile)
        if name in expected_files:
            raise ExportSnapshotError("duplicate export manifest file")
        expected_files.add(name)
        expected_directories.update(parent.as_posix() for parent in PurePosixPath(name).parents if parent != PurePosixPath("."))
        path = directory / name
        if name not in actual_files or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ExportSnapshotError(f"export file is missing or modified: {name}")
    if records != sorted(records, key=lambda item: item["path"]) or manifest["snapshotId"] != _snapshot_id(records, kind, profile):
        raise ExportSnapshotError("export snapshot identity mismatch")
    if actual_files != expected_files or actual_directories != expected_directories:
        raise ExportSnapshotError("export contains unmanaged files or directories")
    if kind in {"publication-export", "publication-draft-export"}:
        if "catalog.json" not in expected_files:
            raise ExportSnapshotError("public snapshot has no catalog")
        catalog = read_json(directory / "catalog.json")
        draft = kind == "publication-draft-export"
        manuscript_type = "publication-draft" if draft else "publication"
        if not isinstance(catalog, dict):
            raise ExportSnapshotError("unsupported public catalog contract")
        try:
            identity = catalog_contract(catalog.get("schemaVersion"), draft=draft)
        except ValueError as exc:
            raise ExportSnapshotError("unsupported public catalog contract") from exc
        _contract_json(identity, catalog)
        article_files: set[str] = set()
        for article in catalog["articles"]:
            article = _validate_article_semantics(article, draft=draft, catalog_version=catalog["schemaVersion"])
            for file_field, hash_field in (("file", "artifactSha256"), ("reviewFile", "reviewArtifactSha256")):
                if article[file_field] in article_files:
                    raise ExportSnapshotError("invalid public catalog article file")
                article_files.add(article[file_field])
                record = next((record for record in records if record["path"] == article[file_field]), None)
                if record is None or record["sha256"] != article[hash_field]:
                    raise ExportSnapshotError("public article and manifest hashes differ")
        if "content-rights.json" in expected_files:
            rights_document = read_json(directory / "content-rights.json")
            try:
                _contract_json("content-rights/v1", rights_document)
            except ExportSnapshotError as exc:
                raise ExportSnapshotError("invalid rights notice") from exc
            required_notice_fields = {
                "editionId", "videoPartId", "contentSha256", "license", "licenseUrl",
                "rightsHolder", "sourceCreator", "attribution", "sourceUrl", "scope", "expiresAt",
            }
            try:
                from bili_asr.content_rights import LICENSE_URLS
                notices = rights_document["notices"]
                if (rights_document["schemaVersion"] != 1
                        or set(rights_document) != {"schemaVersion", "notices"}
                        or not isinstance(notices, list)
                        or len(notices) != len(catalog["articles"])):
                    raise ValueError
                articles_by_edition = {article["editionId"]: article for article in catalog["articles"]}
                if len(articles_by_edition) != len(catalog["articles"]):
                    raise ValueError
                for notice in notices:
                    if set(notice) != required_notice_fields:
                        raise ValueError
                    article = articles_by_edition[notice["editionId"]]
                    if (type(notice["videoPartId"]) is not int
                            or notice["videoPartId"] != article["videoPartId"]
                            or notice["contentSha256"] != article["contentSha256"]
                            or notice["license"] not in LICENSE_URLS
                            or notice["licenseUrl"] != LICENSE_URLS[notice["license"]]
                            or not all(isinstance(notice[field], str) and notice[field].strip()
                                       for field in ("rightsHolder", "sourceCreator", "attribution", "sourceUrl"))
                            or notice["sourceUrl"] != article["sourceUrl"]
                            or notice["scope"] != ["reader-document", "full-source-transcript"]
                            or (notice["expiresAt"] is not None
                                and (type(notice["expiresAt"]) is not int or notice["expiresAt"] <= 0))):
                        raise ValueError
            except (KeyError, TypeError, ValueError):
                raise ExportSnapshotError("invalid rights notice")
        if profile is not None:
            from bili_asr.publication_origin_contract import validate_origins
            if catalog["schemaVersion"] not in {3, 4} or "origins.json" not in expected_files:
                raise ExportSnapshotError("origin profile requires catalog v3/v4 and origins.json")
            origins = read_json(directory / "origins.json")
            _contract_json(profile, origins)
            if origins["contractProfile"] != profile:
                raise ExportSnapshotError("origins and manifest profiles differ")
            validate_origins(origins, catalog["articles"], manuscript_type)
        product_files = set()
        if not draft:
            products = {"products": []}
            if catalog["articles"] and "publication-products.json" not in expected_files and any(
                    name.startswith("transcripts/") for name in expected_files):
                raise ExportSnapshotError("public snapshot has no publication product manifest")
            if "publication-products.json" in expected_files:
                products = read_json(directory / "publication-products.json")
            if not isinstance(products, dict) or products.get("schema", "publication-products/v1") != "publication-products/v1" or not isinstance(products.get("products"), list):
                raise ExportSnapshotError("invalid publication product manifest")
            for product in products["products"]:
                for artifact in product.get("artifacts", []):
                    for item in artifact.get("files", []):
                        path = item.get("path")
                        if not isinstance(path, str) or path in product_files:
                            raise ExportSnapshotError("invalid publication product file")
                        record = next((record for record in records if record["path"] == path), None)
                        if record is None or record["sha256"] != item.get("sha256"):
                            raise ExportSnapshotError("publication product and manifest hashes differ")
                        product_files.add(path)
        if article_files | product_files != expected_files - {manifest_name, "catalog.json", "content-rights.json", "series.json", "origins.json", "publication-products.json"}:
            raise ExportSnapshotError("catalog and manifest file sets differ")
        if "series.json" in expected_files:
            from bili_asr.publication_series import validate_public_series
            series = read_json(directory / "series.json")
            _contract_json("publication-series/v1", series)
            validate_public_series(series, catalog["articles"], manuscript_type)
    elif expected_files - ({"provenance.json"} if profile == "private-provenance-v1" else set()) not in (
            _REVIEW_FILES | {manifest_name}, _REVIEW_FILES | _IMPORT_REVIEW_FILES | {manifest_name}):
        raise ExportSnapshotError("private editorial snapshot is incomplete")
    else:
        review = read_json(directory / "review.json")
        universal = isinstance(review, dict) and isinstance(review.get("ai"), dict) and review["ai"].get("templateVersion") == "ai-draft-v2"
        _contract_json("editorial-review-universal/v1" if universal else "editorial-review/v1", review)
        if profile == "private-provenance-v1":
            from bili_asr.provenance_contract import read_graph
            graph = read_graph(directory / "provenance.json")
            editions = [node["record"]["edition"] for node in graph["nodes"] if node["kind"] == "edition"]
            selected = next((edition for edition in editions if edition["edition_id"] == review["editionId"]), None)
            if (graph["selector"] != {"kind": "edition", "value": review["editionId"]}
                    or selected is None or selected["content_sha256"] != review["contentSha256"]
                    or selected["revision_id"] != review["revisionId"] or selected["video_part_id"] != review["videoPartId"]
                    or selected["review_status"] != review["reviewStatus"]):
                raise ExportSnapshotError("private provenance differs from editorial review")
    return str(manifest["snapshotId"])


def _remove_private(directory: Path) -> None:
    """Only used for privately generated directories after an ownership check."""
    if not directory.exists():
        return
    files, directories = _scan(directory)
    for name in sorted(files):
        (directory / name).unlink()
    for name in sorted(directories, key=lambda value: value.count("/"), reverse=True):
        (directory / name).rmdir()
    directory.rmdir()


def _journal_path(output: Path) -> Path:
    return output.parent / f".{output.name}.export-recovery.json"


def _private_paths(output: Path, token: str) -> tuple[Path, Path]:
    prefix = f".{output.name}.export-{token}"
    return output.parent / f"{prefix}.stage", output.parent / f"{prefix}.backup"


def _recover(output: Path, kind: str) -> None:
    journal_path = checked_path(_journal_path(output))
    if not journal_path.exists():
        return
    journal = read_json(journal_path)
    if not isinstance(journal, dict) or set(journal) != {"schemaVersion", "manuscriptType", "token", "snapshotId", "previousSnapshotId", "hadOutput"} or type(journal["schemaVersion"]) is not int or journal["schemaVersion"] != 1 or journal["manuscriptType"] != kind or not isinstance(journal["token"], str) or not re.fullmatch(r"[0-9a-f]{32}", journal["token"]) or type(journal["hadOutput"]) is not bool:
        raise ExportSnapshotError("invalid export recovery journal")
    for key in ("snapshotId", "previousSnapshotId"):
        if journal[key] is None and key == "previousSnapshotId":
            continue
        if not isinstance(journal[key], str) or not re.fullmatch(r"[0-9a-f]{64}", journal[key]):
            raise ExportSnapshotError("invalid export recovery identity")
    stage, backup = _private_paths(output, journal["token"])
    checked_path(stage)
    checked_path(backup)
    if stage.exists() and _validate_snapshot(stage, kind) != journal["snapshotId"]:
        raise ExportSnapshotError("staged export identity mismatch")
    if backup.exists() and (not journal["hadOutput"] or _validate_snapshot(backup, kind, allow_empty=True) != journal["previousSnapshotId"]):
        raise ExportSnapshotError("previous export identity mismatch")
    if output.exists():
        current = _validate_snapshot(output, kind, allow_empty=True)
        if stage.exists() and current == journal["previousSnapshotId"] and not backup.exists() and journal["hadOutput"]:
            _remove_private(stage)
        elif not stage.exists() and current == journal["snapshotId"]:
            if journal["hadOutput"] and not backup.exists():
                # A crash may occur after backup cleanup and before journal removal.
                pass
            # The installed snapshot is already complete. Finalize recovery
            # before cleanup so an interrupted cleanup cannot block retries.
            journal_path.unlink()
            _fsync_directory(output.parent)
            _remove_private(backup)
            return
        else:
            raise ExportSnapshotError("ambiguous export recovery state; preserving all files")
    elif backup.exists():
        os.replace(backup, output)
        _remove_private(stage)
    elif stage.exists() and not journal["hadOutput"]:
        _remove_private(stage)
    else:
        raise ExportSnapshotError("incomplete export recovery state; preserving all files")
    journal_path.unlink()
    _fsync_directory(output.parent)


def replace_snapshot(output: Path, *, kind: str, files: Mapping[str, bytes],
                     profile: str | None = None, before_install=None) -> str:
    """Install one exact snapshot, or restore the previous directory on failure.

    Recovery artifacts are siblings of the public directory. OS advisory locks
    are released on process exit, so an interrupted exporter can be retried.
    """
    if kind not in {"publication-export", "publication-draft-export", "editorial-export"} or not files:
        raise ExportSnapshotError("unsupported or empty export snapshot")
    if profile is not None and not (
            (profile in {"universal-origin-v1", "universal-origin-v2"} and kind != "editorial-export")
            or (profile == "private-provenance-v1" and kind == "editorial-export")):
        raise ExportSnapshotError("unsupported export origin profile")
    output = checked_path(output)
    for name, content in files.items():
        _file_parts(name, kind, profile)
        if not isinstance(content, bytes):
            raise ExportSnapshotError("export content must be bytes")
    records = [{"path": name, "sha256": hashlib.sha256(files[name]).hexdigest()} for name in sorted(files)]
    snapshot_id = _snapshot_id(records, kind, profile)
    manifest = {"schemaVersion": 3 if profile == "universal-origin-v2" else 2 if profile is not None else 1,
                "manuscriptType": kind, "snapshotId": snapshot_id, "files": records}
    if profile is not None:
        manifest["contractProfile"] = profile
    output.parent.mkdir(parents=True, exist_ok=True)
    checked_path(output.parent)
    with _exclusive_lock(output):
        _recover(output, kind)
        had_output = output.exists()
        previous_id = _validate_snapshot(output, kind, allow_empty=True) if had_output else None
        token = uuid.uuid4().hex
        stage, backup = _private_paths(output, token)
        stage.mkdir(mode=0o700)
        journal_path = _journal_path(output)
        installed = moved_old = journal_written = False
        try:
            for name, content in files.items():
                target = stage.joinpath(*_file_parts(name, kind, profile))
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_file(target, content)
            _write_file(stage / f"{kind}-manifest.json", json_bytes(manifest))
            _validate_snapshot(stage, kind)
            if before_install is not None:
                before_install()
            for current, _, _ in os.walk(stage, topdown=False):
                _fsync_directory(Path(current))
            journal = {"schemaVersion": 1, "manuscriptType": kind, "token": token, "snapshotId": snapshot_id, "previousSnapshotId": previous_id, "hadOutput": had_output}
            journal_temporary = stage / ".recovery-journal.tmp"
            _write_file(journal_temporary, json_bytes(journal))
            os.replace(journal_temporary, journal_path)
            journal_written = True
            _fsync_directory(output.parent)
            if had_output:
                if _validate_snapshot(output, kind, allow_empty=True) != previous_id:
                    raise ExportSnapshotError("output changed while the export was prepared")
                os.replace(output, backup)
                moved_old = True
            os.replace(stage, output)
            installed = True
            _fsync_directory(output.parent)
            _validate_snapshot(output, kind)
        except BaseException:
            # Crash-like interruptions leave the journal for the next run.
            import sys

            # An interruption immediately after atomic journal installation
            # must keep the complete stage available for recovery.
            journal_written = journal_written or journal_path.exists()
            if not isinstance(sys.exc_info()[1], Exception) and journal_written:
                raise
            if installed:
                os.replace(output, stage)
            if moved_old:
                os.replace(backup, output)
            _remove_private(stage)
            if journal_written:
                journal_path.unlink()
            _fsync_directory(output.parent)
            raise
        if backup.exists():
            if _validate_snapshot(backup, kind, allow_empty=True) != previous_id:
                raise ExportSnapshotError("previous snapshot changed; preserving recovery files")
        journal_path.unlink()
        _fsync_directory(output.parent)
        # Backups are outside the served output. Once commit is durable, a
        # cleanup interruption may leave a private orphan, never a mixed export.
        _remove_private(backup)
    return snapshot_id
