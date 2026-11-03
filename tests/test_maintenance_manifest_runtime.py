"""Portable manifests retain explicit runtime policies and old reader contracts."""
import json
import zipfile

import pytest

from bili_asr.services import maintenance_package_core as package
from bili_asr.services.archive_snapshot import check_snapshot, save_snapshot
from tests.test_archive_snapshot import _rewrite_snapshot, _seed_archive


def _rewrite_package(original, output, mutate):
    with zipfile.ZipFile(original) as bundle:
        files = {name: bundle.read(name) for name in bundle.namelist()}
    manifest = json.loads(files["maintenance.json"])
    mutate(manifest)
    manifest["content_id"] = package.digest({key: value for key, value in manifest.items()
                                           if key not in {"content_id", "created_at", "producer_version"}})
    files["maintenance.json"] = package.canonical(manifest)
    with zipfile.ZipFile(output, "w") as bundle:
        for name, payload in files.items():
            bundle.writestr(name, payload)
    return output


def test_old_package_source_provenance_without_runtime_policy_remains_readable(tmp_path):
    root = tmp_path / "source"
    _seed_archive(root)
    original = tmp_path / "original.zip"
    package.export_package(root, original)
    old = _rewrite_package(original, tmp_path / "old.zip", lambda doc: doc["source"].pop("runtime_policy"))
    assert package.verify_package(old)["valid"]


@pytest.mark.parametrize("change", [
    lambda doc: doc["source"].pop("runtime_policy"),
    lambda doc: doc["source"]["excluded"][0].update(path="../unrecognized"),
    lambda doc: doc["source"]["excluded"][0].update(count=True),
    lambda doc: doc["source"]["excluded"][0].pop("state"),
])
def test_runtime_package_exclusion_cannot_be_forged_with_rehashed_manifest(tmp_path, change):
    root = tmp_path / "source"
    _seed_archive(root)
    (root / ".artifact-runtime").mkdir()
    original = tmp_path / "original.zip"
    package.export_package(root, original)
    forged = _rewrite_package(original, tmp_path / "forged.zip", change)
    with pytest.raises(ValueError):
        package.verify_package(forged)


@pytest.mark.parametrize("change", [
    lambda doc: doc.pop("runtime_policy"),
    lambda doc: doc["excluded"][0].update(path="../unrecognized"),
    lambda doc: doc["excluded"][0].update(state="stopped-runtime-fence"),
    lambda doc: doc["excluded"].append(dict(doc["excluded"][0])),
])
def test_snapshot_runtime_exclusions_are_validated_by_same_policy(tmp_path, change):
    root = tmp_path / "source"
    _seed_archive(root)
    (root / ".artifact-runtime").mkdir()
    original, forged = tmp_path / "original.zip", tmp_path / "forged.zip"
    save_snapshot(root, original)
    _rewrite_snapshot(original, forged, mutate_manifest=change)
    with pytest.raises(ValueError):
        check_snapshot(forged)


@pytest.mark.parametrize("tampering", ["unpaired", "policy", "boundary", "plain-native-policy"])
def test_selected_observation_policy_has_exact_declaration_shape(tmp_path, tampering):
    root = tmp_path / "source"
    _seed_archive(root)
    original = tmp_path / "selected.zip"
    package.export_package(root, original, part_ids=[1])

    def change(document):
        selection = document["selection"]
        selection["observation_policy"] = "source-observations-closure/v1"
        if tampering != "unpaired":
            selection["observation_boundary"] = {"shared_creator_pages": [], "raw_observation_rows": 0}
        if tampering == "policy":
            selection["observation_policy"] = "future-policy/v2"
        elif tampering == "boundary":
            selection["observation_boundary"]["raw_observation_rows"] = True

    forged = _rewrite_package(original, tmp_path / "forged.zip", change)
    with pytest.raises(ValueError):
        package.verify_package(forged)


def test_managed_import_lock_exclusion_cannot_name_an_arbitrary_file(tmp_path):
    from bili_asr.file_ops import exclusive_lock
    root = tmp_path / "source"
    _seed_archive(root)
    with exclusive_lock(root / "preserved-body-import"):
        pass
    original = tmp_path / "stopped-import.zip"
    package.export_package(root, original)
    assert package.verify_package(original)["valid"]

    def change(document):
        entry = next(item for item in document["source"]["excluded"] if item["category"] == "runtime-export-lock")
        entry["path"] = "arbitrary-user-history.json"

    forged = _rewrite_package(original, tmp_path / "forged.zip", change)
    with pytest.raises(ValueError, match="runtime exclusion"):
        package.verify_package(forged)
