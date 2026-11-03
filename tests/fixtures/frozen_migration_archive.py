"""Materialize the checked-in pre-change archive without invoking any writer."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

_DATA = Path(__file__).with_name("data")


def frozen_archive(root: Path) -> dict:
    expected = json.loads((_DATA / "bilibili-v1-frozen.json").read_text(encoding="utf-8"))
    archive = _DATA / "bilibili-v1-frozen.zip"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected["package_sha256"]
    root.mkdir()
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            from bili_asr.artifact_inventory import portable_artifact_parts
            path = root.joinpath(*portable_artifact_parts(member.filename))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(package.read(member))
    return expected
