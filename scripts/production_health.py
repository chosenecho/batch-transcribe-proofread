"""Read local worker heartbeats without contacting providers or a database."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from bili_asr.release_identity import build_status
from bili_asr.services.worker_health import inspect_workers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.archive_root.absolute()
    status = build_status()
    print(json.dumps({"interpreter": status, "workers": inspect_workers(
        root.parent / f".{root.name}.worker-health",
        expected_build_id=status["build_id"] if status["state"] == "verified" else None)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
