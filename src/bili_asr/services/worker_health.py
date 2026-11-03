"""Private per-process release evidence; drift requests existing worker drain."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import threading
import uuid
import re

from bili_asr.file_ops import atomic_replace_file, checked_path, json_bytes
from bili_asr.release_identity import build_status, require_expected_build


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkerHealth:
    """A health record is evidence of a heartbeat, never proof of GPU acceptance."""

    def __init__(self, root: Path, *, role: str, stop: threading.Event, interval: float = 5):
        self.directory = checked_path(root.parent / f".{root.name}.worker-health")
        self.instance_id = uuid.uuid4().hex
        self.path = self.directory / f"{os.getpid()}-{self.instance_id}.json"
        self.role, self.stop, self.interval = role, stop, interval
        self.started_at = _now()
        self.phase = "ready"
        self.reason = None
        self.profile_digests = set()
        self.dependencies = {}
        for name in ("bili-asr", "bilibili-api-python", "curl_cffi", "transformers", "torch"):
            try:
                self.dependencies[name] = version(name)
            except PackageNotFoundError:
                self.dependencies[name] = None
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    def _write(self):
        status = build_status()
        if status["state"] not in {"verified", "unattested"}:
            self.reason = status["state"]
            self.stop.set()
        with self._lock:
            phase = "draining" if self.stop.is_set() and self.phase != "stopped" else self.phase
            value = {"schema_version": 1, "instance_id": self.instance_id, "pid": os.getpid(),
                     "role": self.role, "started_at": self.started_at, "heartbeat_at": _now(),
                     "phase": phase, "reason": self.reason, "build": status,
                     "dependencies": self.dependencies, "profile_digests": sorted(self.profile_digests),
                     "gpu_acceptance": "not_recorded"}
            checked_path(self.path)
            atomic_replace_file(self.path, json_bytes(value))

    def _watch(self):
        while not self._closed.wait(self.interval):
            if not self._observe():
                return

    def _observe(self):
        try:
            self._write()
            return True
        except (OSError, ValueError):
            # Observability failure requests drain without changing a business
            # attempt's committed outcome or replacing its original exception.
            self.reason = "health_write_failed"
            self.stop.set()
            return False

    def __enter__(self):
        require_expected_build()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        checked_path(self.directory)
        self._write()
        self._thread = threading.Thread(target=self._watch, name="worker-build-health", daemon=True)
        self._thread.start()
        return self

    @contextmanager
    def attempt(self):
        self.phase = "running"
        self._observe()
        try:
            yield
        finally:
            self.phase = "ready"
            self._observe()

    def profile(self, digest: str):
        with self._lock:
            # Bounded record size for a long-running mixed-profile worker.
            if len(self.profile_digests) < 64:
                self.profile_digests.add(digest)

    def __exit__(self, *_exc):
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.phase = "stopped"
        self._observe()


def inspect_workers(directory: Path, *, stale_after_seconds: float = 30,
                    expected_build_id: str | None = None) -> list[dict]:
    """Expose stale/stopped separately; a recycled PID cannot revive a record."""
    import json
    now = datetime.now(timezone.utc)
    expected = expected_build_id or os.environ.get("BILI_ASR_EXPECTED_BUILD_ID")
    records = []
    for path in sorted(checked_path(directory).glob("*.json")):
        try:
            checked_path(path)
            if path.stat().st_size > 64 * 1024:
                raise ValueError("record too large")
            value = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or value.get("schema_version") != 1
                    or value.get("phase") not in {"ready", "running", "draining", "stopped"}
                    or value.get("role") not in {"asr", "acquisition", "cpu", "editorial", "mixed"}
                    or not isinstance(value.get("instance_id"), str)
                    or not re.fullmatch(r"[0-9a-f]{32}", value["instance_id"])
                    or type(value.get("pid")) is not int or value["pid"] <= 0
                    or not isinstance(value.get("build"), dict)):
                raise ValueError("invalid worker health")
            build = value["build"]
            build_state = build.get("state")
            build_id = build.get("build_id")
            if build_state not in {"verified", "unattested", "invalid_manifest", "unexpected_build",
                                   "disk_drift", "disk_unreadable"}:
                raise ValueError("invalid worker build state")
            if build_state == "verified" and (not isinstance(build_id, str) or not re.fullmatch(r"[0-9a-f]{64}", build_id)):
                raise ValueError("invalid worker build identity")
            age = (now - datetime.fromisoformat(value["heartbeat_at"])).total_seconds()
            value["observed_state"] = ("stopped" if value["phase"] == "stopped" else
                "stale" if age < 0 or age > stale_after_seconds else
                "unexpected_build" if expected is not None and build_id != expected else
                build_state if build_state != "verified" else value["phase"])
            records.append(value)
        except (OSError, ValueError, KeyError, TypeError):
            records.append({"observed_state": "invalid_record"})
    return records
