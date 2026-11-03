"""Read the contract catalog or check/publish its documentation mirrors."""
from __future__ import annotations

import argparse
from importlib import resources
import json
from pathlib import Path

from bili_asr.contracts import CONTRACTS, catalog
from bili_asr.contracts.json_schema import schema_registry


def documentation_files() -> dict[str, bytes]:
    package = resources.files("bili_asr.contracts")
    result = {entry.schema: package.joinpath("schemas", entry.schema).read_bytes()
              for entry in CONTRACTS.values() if entry.schema}
    result["registry.json"] = (json.dumps(catalog(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check-docs", type=Path)
    group.add_argument("--write-docs", type=Path)
    args = parser.parse_args()
    schema_registry()  # Validate every published schema, including optional profiles.
    if args.write_docs:
        args.write_docs.mkdir(parents=True, exist_ok=True)
        for name, body in documentation_files().items():
            (args.write_docs / name).write_bytes(body)
        return 0
    if args.check_docs:
        expected = documentation_files()
        failures = [name for name, body in expected.items()
                    if not (args.check_docs / name).is_file() or (args.check_docs / name).read_bytes() != body]
        failures.extend(path.name for path in args.check_docs.glob("*.schema.json") if path.name not in expected)
        if failures:
            parser.exit(1, "contract documentation drift: " + ", ".join(sorted(failures)) + "\n")
        print(f"Contract documentation verified: {len(CONTRACTS)} contracts, {len(expected) - 1} schemas")
        return 0
    print(json.dumps(catalog(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
