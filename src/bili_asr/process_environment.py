"""Pin disposable Python workers to the package selected by their caller."""
from __future__ import annotations

import os
from pathlib import Path


def worker_environment() -> dict[str, str]:
    # sys.path changes from source callers do not reach an exec child.
    environment = os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[1])
    previous = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = package_root + (os.pathsep + previous if previous else "")
    return environment
