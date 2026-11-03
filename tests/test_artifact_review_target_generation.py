"""Independent final release target-generation boundary regressions."""
from __future__ import annotations

import pytest

from bili_asr.services import artifact_transfer as service
from tests.support.artifact_online import archive as online_archive
from tests.test_artifact_transfer import plan


@pytest.fixture
def archive(tmp_path):
    return online_archive.__wrapped__(tmp_path)


@pytest.mark.parametrize("window", ["before-member-read", "after-member-read", "before-deletion-guard"])
def test_target_changed_after_member_read_never_becomes_verified_generation(archive, monkeypatch, window):
    original = service.copy_package_object

    def corrupt(package_path):
        # A concurrent change after the verified read must not become the
        # generation that the final deletion guard treats as verified.
        content = bytearray(package_path.read_bytes())
        content[content.index(archive.data)] ^= 0xFF
        package_path.write_bytes(content)

    def corrupt_around_read(package_path, *args, **kwargs):
        if window == "before-member-read":
            corrupt(package_path)
        result = original(package_path, *args, **kwargs)
        if window == "after-member-read":
            corrupt(package_path)
        return result

    def corrupt_before_deletion(*args, **kwargs):
        callback = kwargs["before_delete"]

        def checked():
            callback()
            corrupt(next((archive.target / "packages").glob("*.zip")))

        kwargs["before_delete"] = checked
        return original_release(*args, **kwargs)

    if window == "before-deletion-guard":
        original_release = service.release_copy
        monkeypatch.setattr(service, "release_copy", corrupt_before_deletion)
    else:
        monkeypatch.setattr(service, "copy_package_object", corrupt_around_read)
    with pytest.raises(ValueError):
        service.transfer_artifacts(archive.roots, plan(archive), target_root=archive.target,
                                   mode="offload", external_holds={}, _online=True)
    assert archive.data in [path.read_bytes() for path in (archive.root / "audio").iterdir()
                            if path.is_file()]
