"""Build one complete wheel from a committed ref, without modifying a checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile


def build(repository: Path, output: Path, ref: str = "HEAD") -> dict:
    from bili_asr.release_identity import (
        MANIFEST_NAME, create_manifest, validate_manifest, wheel_package_files,
    )
    from bili_asr.file_ops import checked_path, json_bytes, write_new_file

    output = checked_path(output)
    if output.exists():
        raise ValueError("release output must be a new directory")
    source_commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repository, text=True).strip()
    with tempfile.TemporaryDirectory(prefix="bili-release-") as temporary:
        stage = Path(temporary)
        archive = stage / "source.tar"
        subprocess.run(["git", "archive", "--format=tar", f"--output={archive}", source_commit],
                       cwd=repository, check=True)
        source = stage / "source"
        source.mkdir()
        with tarfile.open(archive) as bundle:
            if any(Path(member.name).name == ".env" for member in bundle.getmembers()):
                raise ValueError("a release cannot include a tracked environment file")
            bundle.extractall(source, filter="data")
        package = source / "src" / "bili_asr"
        manifest = create_manifest(package, source_commit,
                                   hashlib.sha256((source / "uv.lock").read_bytes()).hexdigest())
        write_new_file(package / MANIFEST_NAME, json_bytes(manifest))
        wheels = stage / "wheels"
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                        "--no-index", "--disable-pip-version-check",
                        "--wheel-dir", str(wheels), str(source)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        candidates = list(wheels.glob("*.whl"))
        if len(candidates) != 1:
            raise ValueError("release must contain exactly one application wheel")
        wheel = candidates[0]
        with zipfile.ZipFile(wheel) as built:
            embedded = validate_manifest(json.loads(built.read(f"bili_asr/{MANIFEST_NAME}")))
            actual = wheel_package_files(built)
            if embedded != manifest or actual != manifest["files"]:
                raise ValueError("wheel contents differ from the complete source inventory")
        release = {"schema_version": 1, "build_id": manifest["build_id"],
                   "source_commit": source_commit, "lock_sha256": manifest["lock_sha256"],
                   "wheel": wheel.name, "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                   "production_acceptance": "pending"}
        output.mkdir(mode=0o700, parents=True)
        with wheel.open("rb") as incoming, (output / wheel.name).open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        write_new_file(output / "release.json", json_bytes(release))
        return release


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        release = build(args.repository, args.output, args.ref)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, tarfile.TarError,
            zipfile.BadZipFile):
        print("release: build failed; no release was verified", file=sys.stderr)
        return 2
    print(json.dumps(release, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
