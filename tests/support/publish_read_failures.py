import errno
import os
import tempfile
from pathlib import Path
import pytest
from bili_asr import archive
from bili_asr.manifest import ManifestStore
from tests.support.cli_publish_transcripts import (
    CHAIN_BVID,
    FRESH_BVID,
    _bundle_hashes,
    _chain_archive,
    _declared,
    _publish,
    _seed_archive,
    _store_caption,
)


def _existing_bundle(root):
    _seed_archive(root)
    _store_caption(root, CHAIN_BVID, 0)
    return _chain_archive(root, CHAIN_BVID, 0, 5001)


def _fail_one_read(monkeypatch, target, error):
    """Inject at the actual read of one real file, then allow normal reads."""
    pending = [True]
    identity = target.stat()
    real_read = os.read

    def read(fd, size):
        info = os.fstat(fd)
        if pending[0] and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
            pending[0] = False
            raise error
        return real_read(fd, size)

    monkeypatch.setattr(archive.os, "read", read)
    return pending
