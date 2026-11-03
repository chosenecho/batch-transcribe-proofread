"""Run one deterministic shard of the repository's pytest node ids.

The CI workflow uses this small wrapper instead of adding a parallel-test plugin
to the product environment. Collection happens in the same checkout and the
files stay together to share module fixtures. Recent duration hints balance the
files; new tests are always collected and assigned even without a timing hint.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


DEFAULT_DURATIONS = Path(__file__).with_name("pytest-shard-durations.json")


def _shards(node_ids: list[str], count: int, durations_file: Path) -> tuple[list[list[str]], list[float]]:
    """Assign every collected node once, keeping each file on a single runner."""
    hints = {}
    if durations_file.exists():
        data = json.loads(durations_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("seconds_per_test"), dict):
            raise ValueError("invalid shard duration hints")
        hints = data["seconds_per_test"]
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or value <= 0 for value in hints.values()):
            raise ValueError("shard duration hints must be finite positive seconds")
    files: dict[str, list[str]] = {}
    for node in sorted(node_ids):
        files.setdefault(node.split("::", 1)[0], []).append(node)
    weights = {name: len(nodes) * hints.get(name, 1.0) for name, nodes in files.items()}
    shards: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for name in sorted(files, key=lambda name: (-weights[name], name)):
        index = min(range(count), key=lambda index: (loads[index], index))
        shards[index].extend(files[name])
        loads[index] += weights[name]
    return [sorted(nodes) for nodes in shards], loads


def _node_ids() -> list[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    node_ids = sorted(
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith("tests/") and "::" in line
    )
    if not node_ids:
        raise SystemExit("pytest collection produced no test node ids")
    return node_ids


def _parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--durations-file", type=Path, default=DEFAULT_DURATIONS)
    parser.add_argument("--plan-output", type=Path, help="Save shard ownership and estimates for diagnostics")
    parser.add_argument("--contract-baseline-ref", default=os.environ.get("BILI_ASR_CONTRACT_BASELINE_REF"),
                        help="Compare contracts with this exact Git commit in addition to the frozen baseline")
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="run the selected tests with coverage's parallel data mode",
    )
    args, pytest_args = parser.parse_known_args(argv)
    if args.shard_count < 1:
        parser.error("--shard-count must be at least 1")
    if not 0 <= args.shard_index < args.shard_count:
        parser.error("--shard-index must be within --shard-count")
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    return args, pytest_args


def _previous_catalog(ref: str | None) -> dict | None:
    # An initial push has no parent. Every normal CI push/PR uses its real base
    # commit; missing objects/catalogs fail instead of skipping that comparison.
    if not ref or ref == "0" * 40:
        return None
    if re.fullmatch(r"[0-9a-f]{40,64}", ref) is None:
        raise ValueError("contract baseline must be a full Git commit SHA")
    result = subprocess.run(["git", "show", ref + ":docs/contracts/registry.json"],
                            check=False, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise ValueError("cannot read the required contract baseline commit")
    return json.loads(result.stdout)


def _write_node_ids(node_ids: list[str], directory: str | None = None) -> Path:
    """Write one pytest node id per line for an argv-length-safe invocation."""
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", prefix="bili-asr-pytest-", suffix=".args",
        delete=False, dir=directory,
    )
    try:
        with handle:
            handle.write("\n".join(node_ids))
            handle.write("\n")
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return Path(handle.name)


def main(argv: list[str] | None = None) -> int:
    args, pytest_args = _parse_args(argv)
    node_ids = _node_ids()
    from bili_asr.contracts.release_gate import validate_release_collection
    try:
        release = validate_release_collection(Path.cwd(), node_ids,
            previous_catalog=_previous_catalog(args.contract_baseline_ref))
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"Contract release gate failed: {error}", file=sys.stderr)
        return 1
    print(f"Contract release gate: {release['registered_edges']} upgrade edges; "
          f"{release['frozen_files']} fixed evidence files; {release['baseline_count']} baseline(s)")
    try:
        shards, estimates = _shards(node_ids, args.shard_count, args.durations_file)
    except (ValueError, TypeError, OSError) as error:
        print(f"Shard planning failed: {error}", file=sys.stderr)
        return 1
    selected = shards[args.shard_index]
    if args.plan_output:
        args.plan_output.parent.mkdir(parents=True, exist_ok=True)
        args.plan_output.write_text(json.dumps({
            "collected": len(node_ids), "selected_shard": args.shard_index,
            "estimated_seconds": estimates, "shards": shards,
        }, indent=2) + "\n", encoding="utf-8")
    print(
        f"pytest shard {args.shard_index + 1}/{args.shard_count}: "
        f"{len(selected)} of {len(node_ids)} test(s); "
        f"estimated {estimates[args.shard_index]:.1f}s",
        flush=True,
    )
    if not selected:
        print("No tests assigned to this shard; nothing to execute")
        return 0
    command = [sys.executable]
    if args.coverage:
        command.extend(["-m", "coverage", "run", "--branch", "--parallel-mode", "-m"])
    else:
        command.extend(["-m"])
    # pytest >=8.2 reads one argument per line. Keep thousands of parameterized
    # nodes out of the OS command line and preserve exact collected selections.
    with tempfile.TemporaryDirectory(prefix="bili-asr-pytest-shard-") as directory:
        selection = _write_node_ids(selected, directory)
        command.extend(["pytest", *pytest_args, f"@{selection}"])
        return subprocess.run(command, check=False).returncode



if __name__ == "__main__":
    raise SystemExit(main())
