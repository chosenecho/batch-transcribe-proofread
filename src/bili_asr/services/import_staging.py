"""Owned import staging outside the physical archive artifact inventory."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
from uuid import uuid4

from bili_asr.file_ops import (
    atomic_replace_file, checked_path, exclusive_lock as _exclusive_lock,
    fsync_directory as _fsync_directory, json_bytes,
)


def _recover(root: Path, journal: Path) -> None:
    if not journal.exists():
        return
    checked_path(journal)
    record = json.loads(journal.read_bytes())
    if (not isinstance(record, dict) or set(record) != {"version", "directory"} or record["version"] != 1
            or not isinstance(record["directory"], str) or not re.fullmatch(r"\.import-stage-[0-9a-f]{32}", record["directory"])):
        raise ValueError("import-staging: unmanaged recovery journal")
    directory = checked_path(root / record["directory"])
    if directory.exists():
        if not directory.is_dir():
            raise ValueError("import-staging: recovery target is not a directory")
        children = list(directory.iterdir())
        for path in children:
            checked_path(path)
            if not path.is_file() or not path.name.startswith(".manuscript-"):
                raise ValueError("import-staging: unknown recovery file; preserving staging")
        for path in children:
            path.unlink()
        directory.rmdir()
    journal.unlink()
    _fsync_directory(root)


@contextmanager
def import_staging(write_root: Path):
    root = checked_path(write_root)
    if not root.is_dir():
        raise ValueError("import-staging: write root must exist")
    journal = checked_path(root / ".preserved-body-import-journal.json")
    with _exclusive_lock(root / "preserved-body-import"):
        _recover(root, journal)
        # Publish ownership before creating the directory or any staged bytes.
        name = f".import-stage-{uuid4().hex}"
        atomic_replace_file(journal, json_bytes({"version": 1, "directory": name}),
                            prefix=".import-journal-")
        directory = root / name
        directory.mkdir()
        try:
            yield directory
        finally:
            _recover(root, journal)
