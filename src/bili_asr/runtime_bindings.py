"""Explicit model relocation with pinned identity and verified local bytes."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import stat
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bili_asr.asr.config import ASRConfig

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40,64}\Z")
_LIMIT = 1024 * 1024


class ModelBindingError(ValueError):
    error_code = "model_identity_unverified"


def _regular(path: Path) -> None:
    for component in (path, *path.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ModelBindingError("runtime binding cannot use linked paths")
    if not path.is_file():
        raise ModelBindingError("runtime binding requires a regular file")


def _digest(path: Path) -> str:
    _regular(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(_LIMIT), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    _regular(path)
    if path.stat().st_size > _LIMIT:
        raise ModelBindingError("runtime binding document exceeds 1 MiB")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ModelBindingError("duplicate runtime binding field")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text("utf-8"), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ModelBindingError("invalid runtime binding JSON") from exc
    if not isinstance(value, dict):
        raise ModelBindingError("runtime binding must be an object")
    return value


def _name(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or "://" in value:
        raise ModelBindingError("invalid declared model identity")
    return value


class ModelBindings:
    """Bindings are operator-supplied provenance, never changes to stored profiles.

    A binding requires a previously pinned model revision. The independent
    manifest digest pins its local byte inventory; missing legacy identity is
    refused rather than inferred from a directory name.
    """

    def __init__(self, document: dict, *, base: Path):
        if set(document) != {"schema_version", "bindings"} or type(document["schema_version"]) is not int or document["schema_version"] != 1:
            raise ModelBindingError("unsupported runtime binding contract")
        if not isinstance(document["bindings"], list) or len(document["bindings"]) > 256:
            raise ModelBindingError("runtime bindings must be a bounded list")
        self.base = base
        self.entries = {}
        self._cache: dict[str, tuple[dict, tuple]] = {}
        for entry in document["bindings"]:
            if not isinstance(entry, dict) or set(entry) != {"model", "aligner"}:
                raise ModelBindingError("invalid runtime binding entry")
            original = _name(entry["model"].get("original") if isinstance(entry["model"], dict) else None)
            if original in self.entries:
                raise ModelBindingError("duplicate original model binding")
            self.entries[original] = entry

    def _checkpoint(self, entry: dict, original: str, logical_id: str, revision: str | None) -> tuple[str, dict]:
        if set(entry) != {"original", "path", "model_id", "revision", "manifest_sha256"}:
            raise ModelBindingError("invalid checkpoint binding fields")
        if entry["original"] != original or _name(entry["model_id"]) != logical_id:
            raise ModelBindingError("binding differs from frozen model identity")
        if not revision or not _REVISION.fullmatch(revision) or entry["revision"] != revision:
            raise ModelBindingError("model relocation requires the frozen exact revision")
        expected = entry["manifest_sha256"]
        if not isinstance(expected, str) or not _HASH.fullmatch(expected):
            raise ModelBindingError("checkpoint manifest digest is required")
        path = Path(_name(entry["path"]))
        path = path if path.is_absolute() else self.base / path
        manifest_path = path / "model-manifest.json"
        if _digest(manifest_path) != expected:
            raise ModelBindingError("checkpoint manifest hash mismatch")
        manifest = _json(manifest_path)
        if set(manifest) != {"schema_version", "model_id", "revision", "files"} or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
            raise ModelBindingError("unsupported checkpoint manifest")
        if manifest["model_id"] != logical_id or manifest["revision"] != revision:
            raise ModelBindingError("checkpoint manifest identity mismatch")
        files = manifest["files"]
        if not isinstance(files, dict) or not 1 <= len(files) <= 512 or "config.json" not in files:
            raise ModelBindingError("checkpoint manifest must include config and model files")
        if not any(name.endswith((".safetensors", ".bin")) for name in files):
            raise ModelBindingError("checkpoint manifest has no model weights")
        from bili_asr.artifact_inventory import portable_artifact_parts

        signatures = []
        members = []
        for name, digest in sorted(files.items()):
            parts = portable_artifact_parts("documents/" + name)[1:]
            if not isinstance(digest, str) or not _HASH.fullmatch(digest):
                raise ModelBindingError("invalid checkpoint member hash")
            member = path.joinpath(*parts)
            _regular(member)
            info = member.stat()
            signatures.append((name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
            members.append((member, digest))
        actual_names = set()
        for member in path.rglob("*"):
            info = member.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ModelBindingError("linked checkpoint member is not supported")
            if member.is_file() and member != manifest_path:
                actual_names.add(member.relative_to(path).as_posix())
        if actual_names != set(files):
            raise ModelBindingError("checkpoint inventory differs from manifest")
        key = str(path.absolute()) + ":" + expected
        signature = tuple(signatures)
        cached = self._cache.get(key)
        if cached is None or cached[1] != signature:
            for member, digest in members:
                if _digest(member) != digest:
                    raise ModelBindingError("checkpoint member hash mismatch")
            for member, _ in members:
                info = member.stat()
                before = next(row for row in signatures if row[0] == member.relative_to(path).as_posix())
                if before[1:] != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns):
                    raise ModelBindingError("checkpoint changed while validating")
            identity = {"model_id": logical_id, "revision": revision, "manifest_sha256": expected}
            self._cache[key] = (identity, signature)
        return str(path.absolute()), dict(self._cache[key][0])

    def resolve(self, config: ASRConfig) -> tuple[ASRConfig, dict[str, Any]]:
        entry = self.entries.get(config.model_name)
        if entry is None:
            return config, {}
        logical = config.model_id or config.model_name
        if config.model_id is None and (Path(config.model_name).is_absolute()
                or config.model_name.startswith(("models/", "./", "../")) or "\\" in config.model_name):
            raise ModelBindingError("legacy local model lacks a pinned logical model identity")
        aligner_logical = config.aligner_name
        if Path(aligner_logical).is_absolute() or aligner_logical.startswith(("models/", "./", "../")) or "\\" in aligner_logical:
            raise ModelBindingError("legacy local aligner lacks a pinned logical model identity")
        try:
            model, model_identity = self._checkpoint(entry["model"], config.model_name, logical, config.model_revision)
            aligner, aligner_identity = self._checkpoint(entry["aligner"], config.aligner_name, aligner_logical, config.aligner_revision)
        except (OSError, TypeError, KeyError, ValueError) as exc:
            if isinstance(exc, ModelBindingError):
                raise
            raise ModelBindingError("checkpoint cannot be verified") from exc
        identity = {"schema_version": 1, "model": model_identity, "aligner": aligner_identity}
        identity["binding_sha256"] = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return replace(config, model_name=model, aligner_name=aligner), identity


def load_runtime_bindings(path: str | Path | None) -> ModelBindings | None:
    if path is None:
        return None
    source = Path(path).absolute()
    try:
        return ModelBindings(_json(source), base=source.parent)
    except OSError as exc:
        raise ModelBindingError("runtime bindings are missing or unreadable") from exc
