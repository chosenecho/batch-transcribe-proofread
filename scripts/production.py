"""Load a restricted deployment env file, then invoke the installed CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path


def load_environment(path: Path) -> dict[str, str]:
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("environment file must not be accessible to group or other users")
    values = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"invalid environment assignment on line {number}")
        if value.startswith(("'", '"')):
            try:
                tokens = shlex.split(value)
            except ValueError:
                raise ValueError(f"invalid quoting on line {number}") from None
            if len(tokens) != 1:
                raise ValueError(f"invalid quoted value on line {number}")
            value = tokens[0]
        values[key] = value
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--release-manifest", type=Path,
                        help="require this complete wheel release before starting the installed CLI")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a bili-asr command is required after --")
    try:
        overrides = load_environment(args.env_file)
    except (OSError, ValueError):
        print("production: cannot load environment file; check syntax and permissions", file=sys.stderr)
        return 2
    environment = {**os.environ, **overrides}
    if args.release_manifest is not None:
        try:
            release = json.loads(args.release_manifest.read_text(encoding="utf-8"))
            wheel_name = release["wheel"]
            if (not isinstance(wheel_name, str) or Path(wheel_name).name != wheel_name
                    or not wheel_name.endswith(".whl") or "/" in wheel_name or "\\" in wheel_name
                    or not re.fullmatch(r"[0-9a-f]{64}", release["build_id"])):
                raise ValueError("invalid release")
            wheel = args.release_manifest.parent / wheel_name
            if wheel.is_symlink() or hashlib.sha256(wheel.read_bytes()).hexdigest() != release["wheel_sha256"]:
                raise ValueError("invalid wheel")
            environment["BILI_ASR_EXPECTED_BUILD_ID"] = release["build_id"]
            # The selected interpreter must verify its own installed package.
            # The normal CLI repeats this guard and every worker rechecks disk
            # drift; validating this separate process alone is insufficient.
            check = subprocess.run([sys.executable, "-c",
                "from bili_asr.release_identity import require_expected_build; require_expected_build()"],
                env=environment, capture_output=True, timeout=60)
            if check.returncode:
                raise ValueError("installed release mismatch")
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            print("production: release verification failed; command was not started", file=sys.stderr)
            return 2
    return subprocess.call([sys.executable, "-m", "bili_asr", *command], env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
