from __future__ import annotations

import json
import sqlite3

import pytest

from bili_asr.archive_modes import ArchiveAccessMode
from bili_asr.asr import bailian
from bili_asr.cli import transcribe as command
from bili_asr.cli.main import main
from bili_asr.cli.parser import build_parser
from bili_asr.cli.registry import COMMANDS
from bili_asr.services import bailian_batch
from tests.test_import_order import _run_fresh


def test_cloud_module_does_not_load_gpu_runtime():
    _run_fresh("""
import sys
from bili_asr.asr import bailian
assert bailian.MODEL == "qwen-audio-3.1-asr-flash"
assert "torch" not in sys.modules and "transformers" not in sys.modules
""")


def arguments(tmp_path, *extra):
    return [
        "transcribe",
        "file",
        "--audio",
        str(tmp_path / "input.mp3"),
        "--output-root",
        str(tmp_path / "out"),
        *extra,
    ]


def test_database_access_is_explicit_and_file_needs_no_archive(tmp_path):
    parser = build_parser()
    local = parser.parse_args(arguments(tmp_path))
    youtube = parser.parse_args(
        [
            "transcribe",
            "youtube",
            "--archive-root",
            "archive",
            "--input",
            "selection.csv",
            "--output-root",
            "out",
        ]
    )
    assert COMMANDS["transcribe"].database_for(local) is None
    assert COMMANDS["transcribe"].database_for(youtube) is ArchiveAccessMode.READ


def test_missing_key_refuses_before_reading_audio(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(
        bailian, "sha256", lambda *a: pytest.fail("must validate credentials first")
    )
    assert main(arguments(tmp_path)) == 1
    assert "DASHSCOPE_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_private_key_file_precedes_environment_and_accepts_bom(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "ENV_KEY")
    key = tmp_path / "private-key.txt"
    key.write_text("  FILE_KEY  " + chr(10), encoding="utf-8-sig")
    assert command._read_api_key(key) == "FILE_KEY"
    assert command._read_api_key(None) == "ENV_KEY"
    with pytest.raises(ValueError, match="api_key_file_unavailable"):
        command._read_api_key(tmp_path / "missing")


@pytest.mark.parametrize("value", ["", "two keys", "key" + chr(10) + "other"])
def test_empty_or_multiline_key_is_refused(value, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", value)
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        command._read_api_key(None)


def test_invalid_hash_refuses_before_cloud(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "PRIVATE_KEY")
    monkeypatch.setattr(
        bailian, "transcribe", lambda *a, **k: pytest.fail("must not request")
    )
    assert main(arguments(tmp_path, "--expected-sha256", "not-a-hash")) == 1
    assert "64_hex" in capsys.readouterr().err


@pytest.mark.parametrize("review,expected_exit", [(False, 0), (True, 1)])
def test_file_entrypoint_routes_credentials_hash_and_review(
    tmp_path, monkeypatch, capsys, review, expected_exit
):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "PRIVATE_KEY")
    audio = tmp_path / "input.mp3"
    audio.write_bytes(b"fixture")
    calls = []

    def transcribe(audio_path, output, **kwargs):
        calls.append((audio_path, output, kwargs))
        return {"requires_review": review, "chunks": 1, "segments": 2}

    monkeypatch.setattr(bailian, "transcribe", transcribe)
    assert main(arguments(tmp_path, "--retry-failed")) == expected_exit
    assert calls == [
        (
            audio,
            tmp_path / "out",
            {
                "api_key": "PRIVATE_KEY",
                "expected_sha256": bailian.sha256(audio),
                "retry_failed": True,
            },
        )
    ]
    printed = capsys.readouterr()
    assert "PRIVATE_KEY" not in printed.out + printed.err
    assert json.loads(printed.out)["source"] == "asr-cloud"


def test_exception_output_cannot_echo_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "PRIVATE_KEY")
    monkeypatch.setattr(bailian, "sha256", lambda p: "a" * 64)

    def fail(*args, **kwargs):
        raise bailian.CloudError("bad_PRIVATE_KEY")

    monkeypatch.setattr(bailian, "transcribe", fail)
    assert main(arguments(tmp_path)) == 1
    assert "PRIVATE_KEY" not in capsys.readouterr().err


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    with sqlite3.connect(root / "archive.db") as db:
        db.executescript("""
            CREATE TABLE source_videos(source_video_id INTEGER, platform TEXT, external_id TEXT, title TEXT);
            CREATE TABLE video_parts(video_part_id INTEGER, source_video_id INTEGER, page_index INTEGER);
            CREATE TABLE part_audio_objects(video_part_id INTEGER, audio_id INTEGER);
            CREATE TABLE audio_objects(audio_id INTEGER, storage_key TEXT, sha256 TEXT);
        """)
        for index, video in enumerate(["ABCDEFGHIJK", "LMNOPQRSTUV"], 1):
            audio = root / (video + ".opus")
            audio.write_bytes(video.encode())
            db.execute(
                "INSERT INTO source_videos VALUES (?, ?, ?, ?)",
                (index, "youtube", video, video),
            )
            db.execute("INSERT INTO video_parts VALUES (?, ?, 0)", (index, index))
            db.execute("INSERT INTO part_audio_objects VALUES (?, ?)", (index, index))
            db.execute(
                "INSERT INTO audio_objects VALUES (?, ?, ?)",
                (index, audio.name, bailian.sha256(audio)),
            )
    return root


def run_batch(tmp_path, archive, monkeypatch, selected):
    targets = tmp_path / "targets.txt"
    targets.write_text(
        chr(10).join("https://www.youtube.com/watch?v=" + item for item in selected),
        encoding="utf-8",
    )
    return bailian_batch.run_youtube_batch(
        archive_root=archive,
        input_path=targets,
        output_root=tmp_path / "out",
        api_key="PRIVATE_KEY",
    )


def test_batch_only_selected_audio_and_database_stays_unchanged(
    tmp_path, archive, monkeypatch
):
    before = bailian.sha256(archive / "archive.db")
    calls = []

    def transcribe(audio, output, **kwargs):
        calls.append((audio, kwargs))
        return {"requires_review": False}

    monkeypatch.setattr(bailian_batch, "transcribe", transcribe)
    assert (
        run_batch(tmp_path, archive, monkeypatch, ["ABCDEFGHIJK", "ZZZZZZZZZZZ"]) == 1
    )
    assert len(calls) == 1 and calls[0][0].name == "ABCDEFGHIJK.opus"
    assert calls[0][1]["expected_sha256"] == bailian.sha256(calls[0][0])
    report = json.loads((tmp_path / "out/batch-status.json").read_text("utf-8"))
    assert [r["state"] for r in report["results"]] == ["succeeded", "audio_pending"]
    assert before == bailian.sha256(archive / "archive.db")


def test_batch_stops_after_auth_failure_and_preserves_report(
    tmp_path, archive, monkeypatch
):
    def fail(*args, **kwargs):
        raise bailian.CloudError("InvalidApiKey")

    monkeypatch.setattr(bailian_batch, "transcribe", fail)
    assert (
        run_batch(tmp_path, archive, monkeypatch, ["ABCDEFGHIJK", "LMNOPQRSTUV"]) == 1
    )
    report = json.loads((tmp_path / "out/batch-status.json").read_text("utf-8"))
    assert report["selected"] == 2 and len(report["results"]) == 1
    assert report["results"][0]["error_code"] == "InvalidApiKey"
    assert "PRIVATE_KEY" not in json.dumps(report)


def test_batch_refuses_audio_outside_archive(tmp_path, archive, monkeypatch):
    with sqlite3.connect(archive / "archive.db") as db:
        db.execute("UPDATE audio_objects SET storage_key=?", ("../private.mp3",))
    monkeypatch.setattr(
        bailian_batch, "transcribe", lambda *a, **k: pytest.fail("must not request")
    )
    with pytest.raises(ValueError, match="audio_path_outside_archive"):
        run_batch(tmp_path, archive, monkeypatch, ["ABCDEFGHIJK"])


@pytest.mark.parametrize(
    "error,code",
    [
        (sqlite3.OperationalError("PRIVATE_KEY"), "archive_database_unavailable"),
        (OSError("PRIVATE_KEY"), "input_or_output_unavailable"),
    ],
)
def test_cli_operational_errors_are_safe(tmp_path, monkeypatch, capsys, error, code):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "PRIVATE_KEY")
    monkeypatch.setattr(bailian, "sha256", lambda p: "a" * 64)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(bailian, "transcribe", fail)
    assert main(arguments(tmp_path)) == 1
    diagnostic = capsys.readouterr().err
    assert code in diagnostic and "PRIVATE_KEY" not in diagnostic


def test_youtube_handler_routes_only_read_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "PRIVATE_KEY")
    args = build_parser().parse_args(
        [
            "transcribe",
            "youtube",
            "--archive-root",
            str(tmp_path / "archive"),
            "--input",
            str(tmp_path / "selection.csv"),
            "--output-root",
            str(tmp_path / "out"),
        ]
    )
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(bailian_batch, "run_youtube_batch", run)
    assert command._cmd_transcribe(args) == 0
    assert calls == [
        {
            "archive_root": tmp_path / "archive",
            "input_path": tmp_path / "selection.csv",
            "output_root": tmp_path / "out",
            "api_key": "PRIVATE_KEY",
            "retry_failed": False,
        }
    ]


def test_invalid_batch_rows_are_a_bounded_diagnostic(tmp_path, monkeypatch):
    path = tmp_path / "targets.json"
    path.write_text("[17]", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid_youtube_target_list"):
        bailian_batch.run_youtube_batch(
            archive_root=tmp_path,
            input_path=path,
            output_root=tmp_path / "out",
            api_key="PRIVATE_KEY",
        )
