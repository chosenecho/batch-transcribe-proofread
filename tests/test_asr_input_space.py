"""Actual owner fences distinguish completed reservations from missing evidence."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from bili_asr.artifact_fences import resource_fence
from bili_asr.artifact_root import ArtifactRoots
from bili_asr.services.asr_input_space import reserve_input_space
from bili_asr.workflow_errors import JobExecutionError


@pytest.mark.parametrize("read_stage", ["stat", "read_text"])
@pytest.mark.parametrize("owner_finishes", [True, False])
def test_receipt_disappearance_requires_release_of_real_owner_fence(
    tmp_path, monkeypatch, read_stage, owner_finishes,
):
    roots = ArtifactRoots.of(tmp_path)
    monkeypatch.setattr("shutil.disk_usage", lambda _: SimpleNamespace(free=200 * 1024 * 1024))
    ready, release, released = Event(), Event(), Event()

    def own_input():
        with reserve_input_space(roots, tmp_path, 120 * 1024 * 1024, lambda: None):
            ready.set()
            assert release.wait(10)
        released.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(own_input)
        try:
            assert ready.wait(10)
            receipt, = (tmp_path / ".asr-input-reservations").glob("*.json")
            original = getattr(Path, read_stage)
            intercepted = False

            def disappear(path, *args, **kwargs):
                nonlocal intercepted
                if path == receipt and not intercepted:
                    intercepted = True
                    if owner_finishes:
                        release.set()
                        assert released.wait(10)
                    else:
                        receipt.unlink()
                return original(path, *args, **kwargs)

            monkeypatch.setattr(Path, read_stage, disappear)
            if owner_finishes:
                with reserve_input_space(roots, tmp_path, 40 * 1024 * 1024, lambda: None):
                    pass
            else:
                with (pytest.raises(JobExecutionError, match="audio_input_space_unavailable") as error,
                      reserve_input_space(roots, tmp_path, 40 * 1024 * 1024, lambda: None)):
                    pytest.fail("a live owner with missing evidence cannot admit another input")
                assert error.value.safe_details["reason"] == "missing-live-reservation"
            assert intercepted
        finally:
            release.set()
            worker.result(timeout=10)
    assert not list((tmp_path / ".asr-input-reservations").glob("*.json"))


@pytest.mark.parametrize("record", ["null", "[]", '{"byte_size":',
    '{"schema_version":1,"byte_size":-1}', '{"schema_version":true,"byte_size":1}'])
def test_malformed_live_reservation_has_a_classified_capacity_failure(tmp_path, monkeypatch, record):
    roots = ArtifactRoots.of(tmp_path)
    monkeypatch.setattr("shutil.disk_usage", lambda _: SimpleNamespace(free=200 * 1024 * 1024))
    ready, release = Event(), Event()
    receipt = tmp_path / ".asr-input-reservations" / ("a" * 32 + ".json")
    receipt.parent.mkdir()

    def malformed_owner():
        with resource_fence(roots, "asr-input:" + receipt.stem, exclusive=True):
            receipt.write_text(record, encoding="utf-8")
            ready.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(malformed_owner)
        try:
            assert ready.wait(10)
            with (pytest.raises(JobExecutionError, match="audio_input_space_unavailable") as error,
                  reserve_input_space(roots, tmp_path, 1, lambda: None)):
                pytest.fail("invalid active evidence cannot be treated as zero reserved bytes")
            assert error.value.safe_details["reason"] == "invalid-reservation"
        finally:
            release.set()
            worker.result(timeout=10)
