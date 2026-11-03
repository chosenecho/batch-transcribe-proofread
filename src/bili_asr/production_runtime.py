"""Daily process operations using the existing owned-slot supervisor and queue."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from bili_asr.canonical_json import digest
from bili_asr.production_profile import (
    load_profile,
    resolved_profile,
    runtime_directory,
)
from bili_asr.services.retry_maintenance_io import read_json, write_json
from bili_asr.workflow_supervisor import WorkerSupervisor, _slot_lock


def supervisor_locked(root: Path) -> bool:
    if not Path(root).is_dir():
        return False
    try:
        with _slot_lock(Path(root)):
            return False
    except ValueError as exc:
        if str(exc) != "a workflow supervisor already owns this archive":
            raise
        return True


def _worker_digest(profile: dict) -> str:
    return digest({key: profile[key] for key in ("archive_root", "artifact_root", "workers", "runtime")})


def runtime_status(path: Path) -> dict:
    profile = resolved_profile(path)
    directory = runtime_directory(path)
    status_file = directory / "status.json"
    observation = read_json(status_file) if status_file.is_file() else None
    locked = supervisor_locked(Path(profile["archive_root"]))
    recent = observation is not None and 0 <= time.time() - observation.get("observed_at", 0) < 10
    active = bool(locked and recent and observation.get("state") in {"running", "draining"})
    return {"supervisor_lock_held": locked, "managed_observation_recent": bool(recent),
        "state": observation["state"] if active else "unconfirmed" if locked else "stopped",
        "observed": observation, "stop_requested": (directory / "drain").exists(),
        "restart_required": bool(active and observation.get("worker_settings_sha256") != _worker_digest(profile)),
        "next_plan_settings_sha256": digest({key: profile[key] for key in ("asr", "editorial")}),
        "config": str(Path(path).absolute()), "runtime_directory": str(directory)}


def run_production(path: Path, *, run_id: str | None = None) -> dict:
    original = load_profile(path)
    profile = resolved_profile(path, original)
    directory = runtime_directory(path)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "drain").exists():
        raise ValueError("stop request is retained; use production resume before starting")
    run_id = run_id or uuid4().hex
    settings_sha256 = _worker_digest(profile)

    def observe(state):
        write_json(directory / "status.json", {**state, "run_id": run_id,
            "profile_sha256": digest(original), "worker_settings_sha256": settings_sha256})

    supervisor = WorkerSupervisor(archive_root=profile["archive_root"],
        artifact_root=profile["artifact_root"] or profile["archive_root"],
        slots=profile["workers"], drain_file=str(directory / "drain"), observer=observe,
        **profile["runtime"])
    supervisor.run()
    return {"state": "stopped", "run_id": run_id}


def start_production(path: Path, *, startup_timeout: float = 10) -> dict:
    path = Path(path).absolute()
    status = runtime_status(path)
    if status["supervisor_lock_held"]:
        raise ValueError("archive already has a supervisor; inspect status or stop it before restart")
    if status["stop_requested"]:
        raise ValueError("stop request is retained; use production resume before starting")
    directory = runtime_directory(path)
    directory.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex
    log_path = directory / (run_id + ".log")
    with log_path.open("xb") as log:
        kwargs = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen([sys.executable, "-m", "bili_asr.cli", "production", "run",
            "--config", str(path), "--_run-id", run_id], stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, cwd=str(path.parent), **kwargs)
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise ValueError(f"supervisor exited during startup ({code}); inspect {log_path}")
        observed = directory / "status.json"
        if observed.is_file():
            state = read_json(observed)
            if state.get("run_id") == run_id and state.get("state") == "running":
                return {"state": "running", "run_id": run_id, "log": str(log_path)}
        time.sleep(0.1)
    # A slow contract check is not represented as confirmed startup or failure.
    return {"state": "starting", "run_id": run_id, "log": str(log_path)}


def stop_production(path: Path, *, wait: bool = True, timeout: float | None = None) -> dict:
    profile = resolved_profile(path)
    directory = runtime_directory(path)
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "drain", {"requested_at": time.time(), "action": "drain"})
    if wait:
        deadline = time.monotonic() + (profile["runtime"]["drain_timeout"] + 10 if timeout is None else timeout)
        while supervisor_locked(Path(profile["archive_root"])):
            if time.monotonic() >= deadline:
                raise ValueError("drain is still pending; inspect status (no unrelated process was killed)")
            time.sleep(0.1)
    return {"state": "stopped" if wait else "stop-requested", "drain_file": str(directory / "drain")}


def resume_production(path: Path) -> dict:
    profile = resolved_profile(path)
    with _slot_lock(Path(profile["archive_root"])):
        drain = runtime_directory(path) / "drain"
        if drain.is_symlink():
            raise ValueError("drain file must not be a symbolic link")
        drain.unlink(missing_ok=True)
    return {"state": "ready", "workers_started": False}
