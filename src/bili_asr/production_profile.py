"""Explicit portable defaults for future plans and owned worker processes."""
from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from bili_asr.asr.config import ASRConfig, default_config
from bili_asr.editorial import EditorialConfig
from bili_asr.portable_bundle import json_bytes
from bili_asr.services.retry_maintenance_io import (
    maintenance_lock,
    read_json,
    write_json,
)

FORMAT = "asr-production-profile/v1"
RUNTIME = {"poll_interval": 5.0, "drain_timeout": 60.0, "max_restarts": 8,
    "gpu_session": "persistent", "asr_prefetch": False, "asr_prefetch_bytes": 64 * 1024**2,
    "runtime_bindings": None, "storage_targets": [], "warmup_audio": None,
    "warmup_timeout_seconds": 300.0, "cache_root": None, "cache_max_bytes": 10 * 1024**3}


def default_profile(*, archive_root: str = "archive", external_controls=None) -> dict:
    return {"format": FORMAT, "archive_root": archive_root, "artifact_root": None,
            "asr": asdict(default_config()), "editorial": EditorialConfig().to_dict(),
            "workers": {"asr": 1, "cpu": 1, "editorial": 1}, "runtime": deepcopy(RUNTIME),
            # null means not yet declared, [] explicitly means there are no external controls.
            "external_controls": external_controls}


def asr_config(profile: dict) -> ASRConfig:
    values = dict(profile["asr"])
    if isinstance(values.get("hotwords"), list):
        values["hotwords"] = tuple(values["hotwords"])
    try:
        return ASRConfig(**values)
    except (TypeError, AttributeError) as exc:
        raise ValueError("invalid ASR profile fields or types") from exc


def validate_profile(value: dict) -> dict:
    fields = {"format", "archive_root", "artifact_root", "asr", "editorial", "workers", "runtime", "external_controls"}
    if not isinstance(value, dict) or set(value) != fields or value["format"] != FORMAT:
        raise ValueError("unsupported production profile fields/version; credentials belong in the environment")
    if not isinstance(value["archive_root"], str) or not value["archive_root"].strip():
        raise ValueError("archive_root must be a nonempty path")
    if value["artifact_root"] is not None and (not isinstance(value["artifact_root"], str) or not value["artifact_root"].strip()):
        raise ValueError("artifact_root must be a nonempty path or null")
    if not isinstance(value["asr"], dict) or not isinstance(value["editorial"], dict):
        raise ValueError("model settings must be objects")  # noqa: TRY004 - user-facing configuration error
    asr_config(value)
    try:
        EditorialConfig(**value["editorial"])
    except (TypeError, AttributeError) as exc:
        raise ValueError("invalid editorial profile fields or types") from exc
    workers = value["workers"]
    if (not isinstance(workers, dict) or set(workers) != {"asr", "cpu", "editorial"}
            or any(type(count) is not int or not 0 <= count <= 32 for count in workers.values()) or not sum(workers.values())):
        raise ValueError("worker counts must be 0..32 with at least one active worker")
    runtime = value["runtime"]
    if not isinstance(runtime, dict) or set(runtime) != set(RUNTIME):
        raise ValueError("unsupported runtime settings")
    for key in ("poll_interval", "drain_timeout", "warmup_timeout_seconds"):
        number = runtime[key]
        if type(number) not in {int, float} or not math.isfinite(number) or number <= 0:
            raise ValueError(key + " must be finite and positive")
    for key in ("max_restarts", "asr_prefetch_bytes", "cache_max_bytes"):
        if type(runtime[key]) is not int or runtime[key] < (0 if key == "max_restarts" else 1):
            raise ValueError("invalid " + key)
    if runtime["gpu_session"] not in {"persistent", "oneshot"} or type(runtime["asr_prefetch"]) is not bool:
        raise ValueError("invalid GPU session/prefetch settings")
    for key in ("runtime_bindings", "warmup_audio", "cache_root"):
        if runtime[key] is not None and (not isinstance(runtime[key], str) or not runtime[key].strip()):
            raise ValueError(key + " must be a nonempty path or null")
    if not isinstance(runtime["storage_targets"], list) or any(not isinstance(item, str) or "=" not in item for item in runtime["storage_targets"]):
        raise ValueError("storage_targets must be TARGET_ID=PATH bindings")
    controls = value["external_controls"]
    if controls is not None:
        if not isinstance(controls, list):
            raise ValueError("external_controls must be typed declarations, [] or null")
        for control in controls:
            if (not isinstance(control, dict) or set(control) != {"kind", "path", "lock_path"}
                    or control["kind"] not in {"retry-control/v1", "retry-control/v2", "backup-job-state/v1"}
                    or any(not isinstance(control[k], str) or not control[k].strip() for k in ("path", "lock_path"))):
                raise ValueError("invalid external control declaration")
        if sum(item["kind"].startswith("retry-control/") for item in controls) > 1:
            raise ValueError("one authoritative retry control file is supported per deployment")
    json_bytes(value)
    return value


def load_profile(path: Path) -> dict:
    if Path(path).is_symlink():
        raise ValueError("production profile must not be a symbolic link")
    return validate_profile(read_json(Path(path)))


def save_profile(path: Path, profile: dict, *, create: bool = False) -> None:
    path = Path(path).absolute()
    validate_profile(profile)
    with maintenance_lock(path.with_name("." + path.name + ".lock")):
        if path.is_symlink() or (create and path.exists()):
            raise ValueError("profile already exists or is a symbolic link")
        write_json(path, profile)


def update_profile(path: Path, assignments: list[str]) -> dict:
    path = Path(path).absolute()
    with maintenance_lock(path.with_name("." + path.name + ".lock")):
        profile = load_profile(path)
        for assignment in assignments:
            dotted, separator, encoded = assignment.partition("=")
            if not separator or not dotted:
                raise ValueError("settings require KEY=JSON_OR_STRING")
            keys, current = dotted.split("."), profile
            if keys[0] == "format":
                raise ValueError("profile format cannot be changed")
            for key in keys[:-1]:
                if not isinstance(current, dict) or key not in current or not isinstance(current[key], dict):
                    raise ValueError("unknown setting: " + dotted)
                current = current[key]
            if keys[-1] not in current:
                raise ValueError("unknown setting: " + dotted + "; do not store credentials in profiles")
            try:
                replacement = json.loads(encoded)
            except json.JSONDecodeError:
                replacement = encoded
            current[keys[-1]] = replacement
        validate_profile(profile)
        write_json(path, profile)
        return profile


def resolve_path(profile_path: Path, value: str | None) -> str | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    return str((path if path.is_absolute() else Path(profile_path).absolute().parent / path).resolve())


def resolved_profile(path: Path, profile: dict | None = None) -> dict:
    value = deepcopy(load_profile(path) if profile is None else validate_profile(profile))
    for key in ("archive_root", "artifact_root"):
        value[key] = resolve_path(path, value[key])
    for key in ("model_name", "aligner_name"):
        # Hub IDs retain their meaning; explicit ./, ../ or absolute paths relocate.
        model = value["asr"][key]
        if model.startswith(("./", "../", ".\\", "..\\")) or Path(model).is_absolute():
            value["asr"][key] = resolve_path(path, model)
    for key in ("runtime_bindings", "warmup_audio", "cache_root"):
        value["runtime"][key] = resolve_path(path, value["runtime"][key])
    value["runtime"]["storage_targets"] = [name + "=" + resolve_path(path, target)
        for binding in value["runtime"]["storage_targets"] for name, _, target in [binding.partition("=")]]
    for component in value["external_controls"] or []:
        for key in ("path", "lock_path"):
            component[key] = resolve_path(path, component[key])
    return value


def runtime_directory(path: Path) -> Path:
    directory = Path(path).absolute().with_name(Path(path).name + ".runtime")
    if any(parent.is_symlink() for parent in (directory, *directory.parents)):
        raise ValueError("production runtime directory must not use symbolic links")
    return directory
