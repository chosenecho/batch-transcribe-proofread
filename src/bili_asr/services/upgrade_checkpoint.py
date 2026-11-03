"""Durable, verified phase directories for one exact archive upgrade plan."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

from bili_asr.artifact_inventory import collect_artifacts, require_no_links, require_regular_file
from bili_asr.canonical_json import canonical, digest
from bili_asr.services.archive_migration import _sync_directory
from bili_asr.services.migration_preflight import _database_state, _hash_file


def inventory(root: Path) -> list[dict]:
    require_no_links(root)
    _database_state(root / "archive.db")
    files = {"archive.db": root / "archive.db", **collect_artifacts((root,))}
    result = []
    for name, path in sorted(files.items()):
        require_regular_file(path)
        if path.stat().st_nlink != 1:
            raise ValueError("upgrade stage contains a hard-linked file")
        size, sha256, _ = _hash_file(path)
        result.append({"path": name, "size": size, "sha256": sha256})
    return result


def save(path: Path, value: dict) -> None:
    require_no_links(path)
    temporary = path.with_name(path.name + ".pending")
    require_no_links(temporary)
    if temporary.exists():
        require_regular_file(temporary)
        if temporary.stat().st_nlink != 1:
            raise ValueError("upgrade checkpoint temporary file is hard-linked")
        temporary.unlink()
    with temporary.open("xb") as stream:
        stream.write((canonical(value) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    _sync_directory(path.parent)


class UpgradeCheckpoint:
    """Completed phases are immutable; interrupted conversions restart alone."""

    def __init__(self, target: Path, plan_id: str):
        self.root = target.parent / ("." + target.name + ".upgrade-" + plan_id)
        require_no_links(self.root)
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.path = self.root / "checkpoint.json"
        self.plan_id = plan_id
        pending = self.path.with_name(self.path.name + ".pending")
        if not self.path.exists() and pending.exists():
            require_regular_file(pending)
            if pending.stat().st_nlink != 1 or pending.stat().st_size > 16 * 1024**2:
                raise ValueError("invalid pending upgrade checkpoint")
            value = json.loads(pending.read_bytes())
            if (not isinstance(value, dict) or value.get("format") != "archive-upgrade-checkpoint/v1" or value.get("plan_id") != plan_id
                    or value.get("checkpoint_id") != digest({k: v for k, v in value.items() if k != "checkpoint_id"})):
                raise ValueError("pending upgrade checkpoint identity changed")
            pending.replace(self.path)
            _sync_directory(self.root)
        if self.path.exists():
            require_regular_file(self.path)
            if self.path.stat().st_size > 16 * 1024**2:
                raise ValueError("oversized upgrade checkpoint")
            self.value = json.loads(self.path.read_bytes())
            if (not isinstance(self.value, dict) or self.value.get("format") != "archive-upgrade-checkpoint/v1"
                    or self.value.get("plan_id") != plan_id
                    or self.value.get("checkpoint_id") != digest({k: v for k, v in self.value.items() if k != "checkpoint_id"})):
                raise ValueError("upgrade checkpoint identity changed")
            if not isinstance(self.value.get("phases"), list) or len(self.value["phases"]) > 64:
                raise ValueError("invalid upgrade checkpoint phases")
        else:
            if any(self.root.iterdir()):
                raise ValueError("unowned upgrade staging directory")
            self.value = {"format": "archive-upgrade-checkpoint/v1", "plan_id": plan_id, "phases": []}
            self._save()
        for index, phase in enumerate(self.value["phases"]):
            if (not isinstance(phase, dict) or set(phase) != {"index", "label", "files"}
                    or phase["index"] != index or not isinstance(phase["label"], str)
                    or not isinstance(phase["files"], list)):
                raise ValueError("invalid completed upgrade phase")
            if not self.phase(index).exists():
                pending = self.pending(index, rebuild=False)
                if inventory(pending) != phase["files"]:
                    raise ValueError("uncommitted upgrade phase changed")
                pending.rename(self.phase(index))
                _sync_directory(self.root)
            if phase.get("index") != index or inventory(self.phase(index)) != phase["files"]:
                raise ValueError("completed upgrade phase changed")
        self.reused_phases = len(self.value["phases"])

    def _save(self) -> None:
        self.value.pop("checkpoint_id", None)
        self.value["checkpoint_id"] = digest(self.value)
        save(self.path, self.value)

    def phase(self, index: int) -> Path:
        return self.root / f"phase-{index}"

    def pending(self, index: int, *, rebuild: bool) -> Path:
        pending = self.root / f"pending-{index}"
        require_no_links(pending)
        if rebuild and pending.exists():
            # This exact child is owned by the plan, never an archive target.
            for path in pending.rglob("*"):
                require_no_links(path)
            shutil.rmtree(pending)
        return pending

    def seal(self, index: int, pending: Path, label: str) -> Path:
        files = inventory(pending)
        complete = self.phase(index)
        if complete.exists():
            raise ValueError("unexpected completed upgrade phase")
        for directory, _, _ in os.walk(pending, topdown=False):
            _sync_directory(Path(directory))
        self.value["phases"].append({"index": index, "label": label, "files": files})
        self._save()
        pending.rename(complete)
        _sync_directory(self.root)
        return complete

    def copy_completed(self, source: Path, pending: Path) -> None:
        if pending.exists():
            raise ValueError("conversion stage was not reset")
        shutil.copytree(source, pending)
        for entry in inventory(source):
            path = pending / entry["path"]
            # This is an owned copied destination. Windows fsync requires a
            # writable descriptor; source bytes remain untouched.
            with path.open("r+b") as stream:
                os.fsync(stream.fileno())
        if inventory(pending) != inventory(source):
            raise ValueError("upgrade phase copy differs from completed checkpoint")
