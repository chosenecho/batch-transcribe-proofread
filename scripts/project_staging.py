"""Copy the bounded inputs of a Python package into an isolated build tree."""

from __future__ import annotations

from pathlib import Path
import shutil


def stage_project(source: Path, destination: Path) -> Path:
    """Stage packaging inputs without traversing environments or runtime output.

    A destination within the source is refused before any directory is created.
    This also handles symlinked parents, so changing TEMP cannot cause a copy
    operation to discover its own output recursively.
    """
    source = source.resolve()
    destination = destination.resolve()
    if destination.is_relative_to(source):
        raise ValueError("project staging destination must be outside the source tree")
    required = ("pyproject.toml", "README.md", "src")
    for name in required:
        if not (source / name).exists():
            raise FileNotFoundError(f"missing packaging input: {name}")
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(
        source / "src", destination / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    for name in required[:2]:
        shutil.copy2(source / name, destination / name)
    return destination
