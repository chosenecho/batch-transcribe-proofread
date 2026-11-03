"""Portable private sidecars are bound to exports and remain readable offline."""
from __future__ import annotations

import json
from contextlib import closing

import pytest

from bili_asr.provenance_contract import read_graph, write_graph
from bili_asr.provenance_report import report_graph
from bili_asr.publication import edit_edition, verify_release
from bili_asr.publication_export import export_editorial
from bili_asr.services.archive_snapshot import restore_snapshot, save_snapshot
from bili_asr.services.provenance import public_summary, trace
from tests.test_provenance_trace import manuscript

pytestmark = pytest.mark.usefixtures("enable_content_publication_for_tests")


@pytest.mark.parametrize("platform", ["bilibili", "youtube"])
def test_private_export_and_backup_keep_offline_chain_and_old_artifacts(tmp_path, platform):
    connection, root, revision, parent, release, _producers = manuscript(tmp_path, platform,
        raw_kind="details" if platform == "youtube" else None)
    with closing(connection):
        old = verify_release(connection, release["release_id"], (root,))[2]
        child = edit_edition(connection, edition_id=parent["edition_id"], markdown_text="Human corrected body.", actor="human", note="clarified")
        output = tmp_path / "private-review"
        export_editorial(connection, revision_id=revision, edition_id=child["edition_id"], artifact_roots=(root,), output=output,
                         include_provenance=True)
        assert len(report_graph(read_graph(output / "provenance.json"))["editions"]) == 2
        assert json.loads(output.joinpath("editorial-export-manifest.json").read_text(encoding="utf-8"))["contractProfile"] == "private-provenance-v1"
        graph = trace(connection, release_id=release["release_id"], artifact_roots=(root,))
        root.joinpath("documents/provenance").mkdir()
        write_graph(root / "documents/provenance" / (graph["graph_sha256"] + ".json"), graph)
        assert verify_release(connection, release["release_id"], (root,))[2] == old
    backup = tmp_path / "backup.zip"
    save_snapshot(root, backup)
    restored = tmp_path / "restored"
    restore_snapshot(backup, restored)
    portable = restored / "documents/provenance" / (graph["graph_sha256"] + ".json")
    assert report_graph(read_graph(portable)) == report_graph(graph)



def test_cli_private_export_is_offline_readable_and_existing_files_are_preserved(tmp_path, capsys):
    from bili_asr.cli import main
    connection, root, _revision, _edition, release, _producers = manuscript(tmp_path)
    connection.close()
    output = tmp_path / "private.json"
    arguments = ["provenance", "export", "--archive-root", str(root), "--release-id", release["release_id"],
                 "--out", str(output), "--format", "json"]
    assert main(arguments) == 0
    expected = json.loads(capsys.readouterr().out)["graph_sha256"]
    original = output.read_bytes()
    assert main(arguments) == 1
    assert output.read_bytes() == original
    root.joinpath("archive.db").rename(root.joinpath("offline.db"))
    assert main(["provenance", "read", "--file", str(output), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["graph_sha256"] == expected


def test_private_profile_refuses_tampered_existing_sidecar_without_replacing_files(tmp_path):
    from bili_asr.export_snapshot import ExportSnapshotError
    connection, root, revision, edition, _release, _producers = manuscript(tmp_path)
    with closing(connection):
        output = tmp_path / "private-review"
        arguments = dict(revision_id=revision, edition_id=edition["edition_id"], artifact_roots=(root,),
                         output=output, include_provenance=True)
        export_editorial(connection, **arguments)
        output.joinpath("provenance.json").write_bytes(b"tampered evidence")
        before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
        with pytest.raises(ExportSnapshotError, match="missing or modified"):
            export_editorial(connection, **arguments)
        assert {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()} == before


def test_private_profile_keeps_previous_snapshot_after_injected_install_failure(tmp_path, monkeypatch):
    from bili_asr.export_snapshot import replace_snapshot
    connection, root, revision, edition, _release, _producers = manuscript(tmp_path)
    with closing(connection):
        output = tmp_path / "private-review"
        arguments = dict(revision_id=revision, edition_id=edition["edition_id"], artifact_roots=(root,),
                         output=output, include_provenance=True)
        export_editorial(connection, **arguments)
        before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
        def failure_before_install(*args, **kwargs):
            def fail():
                raise OSError("injected provenance install failure")
            return replace_snapshot(*args, **kwargs, before_install=fail)
        monkeypatch.setattr("bili_asr.publication_export.replace_snapshot", failure_before_install)
        with pytest.raises(OSError, match="injected"):
            export_editorial(connection, **arguments)
        assert {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()} == before
        assert not list(tmp_path.glob(".private-review.export-*.stage"))


def test_public_summary_is_a_fixed_whitelist_and_keeps_rights_gate(tmp_path, monkeypatch):
    connection, root, _revision, _edition, release, _producers = manuscript(tmp_path)
    with closing(connection):
        summary = public_summary(connection, release["release_id"], (root,))
        assert set(summary) == {"format", "schema_version", "release_id", "edition_id", "revision_id", "content_sha256",
                                "artifact_sha256", "private_graph_sha256", "published_at"}
        assert "private-publisher" not in str(summary) and "request" not in str(summary)
        from bili_asr.content_rights import ContentRightsError
        def denied(*_args):
            raise ContentRightsError("content-rights: denied test permission")
        monkeypatch.setattr("bili_asr.content_rights.require_public_rights", denied)
        with pytest.raises(ContentRightsError, match="denied"):
            public_summary(connection, release["release_id"], (root,))


def test_private_sidecar_cannot_select_parent_in_place_of_exported_child(tmp_path):
    from bili_asr.export_snapshot import (
        _snapshot_id,
        _validate_snapshot,
    )
    connection, root, revision, parent, _release, _producers = manuscript(tmp_path)
    with closing(connection):
        child = edit_edition(connection, edition_id=parent["edition_id"], markdown_text="A child body.", actor="editor", note="child")
        output = tmp_path / "review"
        export_editorial(connection, revision_id=revision, edition_id=child["edition_id"], artifact_roots=(root,), output=output,
                         include_provenance=True)
        graph_path = output / "provenance.json"
        graph = read_graph(graph_path)
        graph["selector"] = {"kind": "edition", "value": parent["edition_id"]}
        from bili_asr.canonical_json import digest
        graph["graph_sha256"] = digest({key: value for key, value in graph.items() if key != "graph_sha256"})
        graph_path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
        manifest_path = output / "editorial-export-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        import hashlib
        record = next(item for item in manifest["files"] if item["path"] == "provenance.json")
        record["sha256"] = hashlib.sha256(graph_path.read_bytes()).hexdigest()
        manifest["snapshotId"] = _snapshot_id(manifest["files"], "editorial-export", "private-provenance-v1")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ValueError, match="root differs|provenance differs"):
            _validate_snapshot(output, "editorial-export")
