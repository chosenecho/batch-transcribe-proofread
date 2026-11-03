"""Package or verify a private standalone proofreading probe without API calls."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from bili_asr.services.editorial_sample_evidence import package_sample, verify_package


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--annotations", type=Path)
    args = parser.parse_args()
    if args.annotations and not args.output:
        parser.error("--annotations requires --output")
    if args.output:
        annotations = json.loads(args.annotations.read_bytes()) if args.annotations else None
        manifest = package_sample(args.sample, args.output, annotations=annotations)
    else:
        manifest = verify_package(args.sample)
    print(json.dumps({key: manifest[key] for key in
                      ("schema", "input_id", "revision_id", "quality_status", "semantic_status")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
