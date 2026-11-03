"""Explicit public profile delegates to the canonical publication exporter."""
import json
import os
import stat
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from bili_asr.archive_session import ArchiveAccessMode, ArchiveSession
from bili_asr.publication_export import export_publications
from bili_asr.services import maintenance_package_core as package
from bili_asr.services.maintenance_package_core import absolute

NAME = "public-backup.json"


def export_public_profile(source_root: Path, output: Path, *, artifact_root: Path | None = None) -> dict:
    root, out = absolute(source_root), absolute(output)
    product = root if artifact_root is None else absolute(artifact_root)
    if out.exists() or out.is_relative_to(root) or out.is_relative_to(product):
        raise ValueError("public maintenance export needs a new output directory outside its source")
    with ArchiveSession(root, mode=ArchiveAccessMode.READ) as session:
        count = export_publications(session.connection, artifact_roots=tuple(dict.fromkeys((product, root))), output=out)
    return {"operation": "maintenance-export", "valid": True, "profile": "publication",
            "output": str(out), "published_articles": count, "database_recoverable": False,
            "contract": "publication-export; public-rights-and-review-gates-preserved"}


def save_public_backup(source_root: Path, output: Path, *, artifact_root=None) -> dict:
    out = absolute(output)
    if out.exists() or not out.parent.is_dir():
        raise ValueError("public backup output must be a new file with an existing parent")
    with tempfile.TemporaryDirectory(prefix=".public-backup-", dir=out.parent) as temporary:
        source = absolute(source_root)
        product = source if artifact_root is None else absolute(artifact_root)
        inventory, _ = package._inventory(source, product)
        root = Path(temporary) / "public"
        export_public_profile(source_root, root, artifact_root=artifact_root)
        if package._inventory(source, product)[0] != inventory:
            raise ValueError("public backup source changed during projection")
        files = [package._record(path.relative_to(root).as_posix(), path)
                 for path in sorted(root.rglob("*")) if path.is_file()]
        content = {"format": "publication-maintenance-backup/v1", "profile": "publication", "database_recoverable": False,
                   "source_fingerprint": inventory["fingerprint"],
                   "files": files, "container": {"compression": "stored", "encryption": None}}
        manifest = {**content, "content_id": package.digest(content), "created_at": datetime.now(UTC).isoformat()}
        staged = Path(temporary) / "public.zip"
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
            for item in files:
                with (root / item["path"]).open("rb") as reader, bundle.open(item["path"], "w", force_zip64=True) as writer:
                    package.stream_hash(reader, writer)
            bundle.writestr(NAME, package.canonical(manifest))
        verify_public_backup(staged)
        with staged.open("r+b") as stream:
            package._sync_file(stream)
        if os.name == "nt":
            staged.rename(out)
        else:
            os.link(staged, out)
        package._sync_directory(out.parent)
    return {"operation": "maintenance-public-backup", "valid": True, "content_id": manifest["content_id"], "files": files}


def verify_public_backup(snapshot: Path) -> dict:
    path = absolute(snapshot)
    with zipfile.ZipFile(path) as bundle:
        info = bundle.getinfo(NAME)
        if info.file_size > package._LIMIT:
            raise ValueError("oversized public backup manifest")
        manifest = json.loads(bundle.read(info), object_pairs_hook=package._manifest_object)
        if (set(manifest) != {"format", "profile", "database_recoverable", "source_fingerprint", "files", "container", "content_id", "created_at"}
                or manifest["format"] != "publication-maintenance-backup/v1" or manifest["profile"] != "publication"
                or manifest["database_recoverable"] is not False or manifest["content_id"] != package.digest({k: v for k, v in manifest.items() if k not in {"content_id", "created_at"}})):
            raise ValueError("invalid public backup identity")
        if not isinstance(manifest["source_fingerprint"], str) or not package._HASH.fullmatch(manifest["source_fingerprint"]):
            raise ValueError("invalid public backup source identity")
        if manifest["container"] != {"compression": "stored", "encryption": None}:
            raise ValueError("unsupported public backup container compression or encryption")
        files = manifest["files"]
        names = []
        for item in bundle.infolist():
            if (item.orig_filename != item.filename or item.is_dir() or item.flag_bits & 1
                    or item.compress_type != zipfile.ZIP_STORED or stat.S_IFMT(item.external_attr >> 16) not in {0, stat.S_IFREG}):
                raise ValueError("unsafe public backup member")
            names.append(item.filename)
        if len(names) != len(set(names)) or set(names) != {*(entry["path"] for entry in files), NAME}:
            raise ValueError("public backup inventory mismatch")
        from bili_asr.export_snapshot import _allowed_file
        for entry in files:
            if entry["path"] != "publication-export-manifest.json" and not _allowed_file(entry["path"], "publication-export"):
                raise ValueError("unsupported public backup projection path")
            if entry["path"] == "archive.db" or entry["path"].endswith((".db", ".sqlite")):
                raise ValueError("public backup cannot contain a business database")
            with bundle.open(entry["path"]) as stream:
                if package.stream_hash(stream) != (entry["size"], entry["sha256"]):
                    raise ValueError("public backup payload changed")
    return {"operation": "maintenance-public-backup-check", "valid": True, "manifest": manifest,
            "content_id": manifest["content_id"], "database_recoverable": False,
            "total_bytes": sum(entry["size"] for entry in files)}
