"""Cross-boundary contract ownership, offline schemas and historical identity."""
from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
from copy import deepcopy
from dataclasses import FrozenInstanceError
from importlib import resources
from pathlib import Path

import pytest

from bili_asr.contracts import CONTRACTS, contract
from bili_asr.contracts.__main__ import documentation_files
from bili_asr.contracts.fingerprints import database_fingerprint
from bili_asr.contracts.json_schema import (
    ContractValidationError,
    _validator,
    validate_json,
)
from bili_asr.contracts.registry import catalog_contract, manifest_contract
from bili_asr.storage import snapshots
from bili_asr.storage.database import open_database, require_archive_schema
from bili_asr.storage.migration_source import (
    MigrationSourceError,
    inspect_migration_source,
)
from tests.fixtures.migration_archive import build_migration_archive

ROOT = Path(__file__).resolve().parents[1]


def test_every_contract_has_existing_authority_and_registered_dependencies():
    for entry in CONTRACTS.values():
        assert entry.owner and entry.consumers and entry.capabilities
        assert all((ROOT / path).is_file() for path in entry.authority), entry.identity
        assert set(entry.dependencies) <= CONTRACTS.keys(), entry.identity
    with pytest.raises(FrozenInstanceError):
        contract("bilibili-v1").owner = "other"
    with pytest.raises(ValueError, match="does not support"):
        contract("bilibili-v1", capability="migration-target")
    with pytest.raises(ValueError, match="unsupported"):
        contract("universal-v999")


def test_documentation_is_a_complete_byte_identical_package_mirror():
    expected = documentation_files()
    assert {path.name for path in (ROOT / "docs/contracts").glob("*.schema.json")} == set(expected) - {"registry.json"}
    for name, body in expected.items():
        assert (ROOT / "docs/contracts" / name).read_bytes() == body, name


def test_catalog_cross_schema_references_validate_without_network(monkeypatch):
    def refuse_network(*args, **kwargs):
        raise AssertionError("contract validation must not retrieve network resources")
    monkeypatch.setattr(socket, "create_connection", refuse_network)
    document = json.loads((ROOT / "docs/contracts/examples/publication-catalog.json").read_text(encoding="utf-8"))
    validate_json("publication-catalog/v2", document)
    document["schemaVersion"] = 3
    validate_json("publication-catalog/v3", document)
    document["articles"][0]["privateNote"] = "secret body must not appear in an error"
    with pytest.raises(ContractValidationError) as error:
        validate_json("publication-catalog/v3", document)
    assert "secret body" not in str(error.value)


@pytest.mark.parametrize(
    ("identity", "expected_id"),
    [
        (
            "artifact-inventory-v1",
            "https://github.com/SuperCatQR/bilibili-asr-archive/docs/contracts/artifact-inventory-v1.schema.json",
        ),
        (
            "content-rights/v1",
            "https://github.com/SuperCatQR/bilibili-asr-archive/docs/contracts/content-rights.schema.json",
        ),
    ],
)
def test_validator_handles_schemas_with_and_without_published_ids(identity, expected_id):
    validator = _validator(identity)

    assert validator.schema["$id"] == expected_id


@pytest.mark.parametrize("version", [1, 5, True, 2.0, None])
def test_unknown_catalog_versions_have_no_default_fallback(version):
    with pytest.raises(ValueError):
        catalog_contract(version)


@pytest.mark.parametrize("field", ["releaseId", "editionId", "aiRevisionId", "contentSha256",
                                  "artifactSha256", "reviewArtifactSha256"])
def test_public_article_identity_cannot_end_with_a_newline(field):
    from bili_asr.export_snapshot import ExportSnapshotError, _validate_article
    document = json.loads((ROOT / "docs/contracts/examples/publication-catalog.json").read_text(encoding="utf-8"))
    article = document["articles"][0]
    _validate_article(article)
    article[field] += "\n"
    with pytest.raises(ExportSnapshotError):
        _validate_article(article)


@pytest.mark.parametrize("imported", [False, True])
@pytest.mark.parametrize("damage", ["missing", "duplicate", "duplicate-different-hash", "extra"])
def test_private_manifest_layouts_have_explicit_structural_contracts(imported, damage):
    names = ["ai-draft.md", "review.md", "edition.md", "edition.json", "review.json",
             "differences/ai.patch", "differences/parent.patch"]
    if imported:
        names += ["import-origin.json", "preserved-body.md", "differences/preserved.patch"]
    value = {"schemaVersion": 1, "manuscriptType": "editorial-export", "snapshotId": "a" * 64,
             "files": [{"path": name, "sha256": "b" * 64} for name in names]}
    identity = manifest_contract("editorial-export", None, imported=imported)
    validate_json(identity, value)
    if damage == "missing":
        value["files"].pop()
    elif damage.startswith("duplicate"):
        # Preserve the count while replacing a required path, including the
        # import-origin evidence in the ten-file layout. Object uniqueness
        # alone would still permit duplicate paths with different hashes.
        index = names.index("import-origin.json") if imported else -1
        value["files"][index] = dict(value["files"][0])
        if damage == "duplicate-different-hash":
            value["files"][index]["sha256"] = "c" * 64
    else:
        value["files"][-1]["path"] = "private-note.md"
    with pytest.raises(ContractValidationError):
        validate_json(identity, value)


def test_historical_materializer_is_independent_of_current_writers(monkeypatch, tmp_path):
    def refuse_writer(*args, **kwargs):
        raise AssertionError("a historical sample must not invoke a current writer")
    monkeypatch.setattr("bili_asr.storage.MetadataRepository.record_page", refuse_writer)
    monkeypatch.setattr("bili_asr.publication.create_edition", refuse_writer)
    monkeypatch.setattr("bili_asr.manuscript_templates.render_ai_v1", refuse_writer)
    sample = build_migration_archive(tmp_path / "old", tmp_path / "media")
    expected = json.loads((ROOT / "tests/fixtures/data/bilibili-v1-frozen.json").read_text(encoding="utf-8"))
    assert sample.ids == expected["ids"]
    assert {name: hashlib.sha256(body).hexdigest() for name, body in sample.files.items()} == expected["files"]
    for name, body in sample.files.items():
        root, other = ("old", "media") if name.startswith("subtitles/") else ("media", "old")
        assert (tmp_path / root / name).read_bytes() == body
        assert not (tmp_path / other / name).exists()
    assert any((tmp_path / "old" / name).is_file() for name in sample.files)
    assert any((tmp_path / "media" / name).is_file() for name in sample.files)


@pytest.mark.parametrize("kind", ["bilibili-v1", "universal-v2"])
@pytest.mark.parametrize("imports", [False, True])
def test_snapshot_identity_is_pinned_independently_of_the_current_writer(kind, imports):
    assert snapshots._current_contract(kind, imports)[1] == database_fingerprint(kind, imports)


@pytest.mark.parametrize("kind", ["bilibili-v1", "universal-v2"])
def test_source_supplement_extension_has_a_distinct_pinned_identity(kind):
    identity = snapshots._current_contract(kind, True, True)[1]
    assert identity == database_fingerprint(kind, True, True)
    assert identity != database_fingerprint(kind, True)
    with pytest.raises(ValueError, match="unsupported"):
        database_fingerprint(kind, False, True)


def test_shipped_ddl_drift_cannot_silently_redefine_an_existing_snapshot(monkeypatch):
    original = snapshots._schema
    def altered(connection):
        shape = deepcopy(original(connection))
        shape.pop("video_tag_observations")
        return shape
    snapshots._current_contract.cache_clear()
    monkeypatch.setattr(snapshots, "_schema", altered)
    try:
        with pytest.raises(snapshots.SnapshotDatabaseError, match="has drifted"):
            snapshots._current_contract()
    finally:
        snapshots._current_contract.cache_clear()


def test_runtime_snapshot_and_fixed_source_keep_distinct_readonly_policies(tmp_path):
    path = tmp_path / "archive.db"
    connection = open_database(path)
    connection.execute("DROP TABLE video_tag_observations")
    connection.commit()
    require_archive_schema(connection)
    connection.close()
    before = path.read_bytes()
    with pytest.raises(snapshots.SnapshotDatabaseError, match="video_tag_observations"):
        snapshots.validate_snapshot_database(path)
    with pytest.raises(MigrationSourceError, match="video_tag_observations"):
        inspect_migration_source(path)
    assert path.read_bytes() == before


def test_contract_cli_publishes_checks_and_rejects_documentation_drift(tmp_path):
    base = [sys.executable, "-m", "bili_asr.contracts"]
    written = subprocess.run([*base, "--write-docs", str(tmp_path)], capture_output=True, text=True)
    assert written.returncode == 0, written.stderr
    checked = subprocess.run([*base, "--check-docs", str(tmp_path)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    (tmp_path / "publication-catalog.schema.json").write_bytes(b"{}")
    rejected = subprocess.run([*base, "--check-docs", str(tmp_path)], capture_output=True, text=True)
    assert rejected.returncode == 1
    assert "publication-catalog.schema.json" in rejected.stderr


def test_all_registered_schemas_are_packaged():
    bundled = resources.files("bili_asr.contracts").joinpath("schemas")
    assert {item.name for item in bundled.iterdir() if item.name.endswith(".json")} == {
        entry.schema for entry in CONTRACTS.values() if entry.schema}


@pytest.mark.parametrize("value", [True, "1", None, -1])
def test_source_and_storage_scalar_rules_reject_the_same_invalid_identity(value):
    from bili_asr.sources.models import _integer as source_integer
    from bili_asr.storage.models import _integer as storage_integer
    failures = []
    for validate in (source_integer, storage_integer):
        with pytest.raises((TypeError, ValueError)) as error:
            validate(value, "identity", minimum=0)
        failures.append((type(error.value), str(error.value)))
    assert failures[0] == failures[1]


def test_caption_rules_keep_multiline_text_without_applying_writer_normalization():
    from bili_asr.sources.models import _caption_text as source_caption
    from bili_asr.storage.models import _caption_text as storage_caption
    original = "  第一行\n第二行  "
    assert source_caption(original, "text") == storage_caption(original, "text") == original
