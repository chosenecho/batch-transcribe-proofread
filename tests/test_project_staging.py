from __future__ import annotations

from pathlib import Path

import pytest

from scripts.project_staging import stage_project


def _make_project(root: Path) -> Path:
    (root / "src" / "demo").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / "src" / "demo" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "notes.txt").write_text("development-only\n", encoding="utf-8")
    return root


def test_stage_project_copies_only_packaging_inputs(tmp_path: Path) -> None:
    source = _make_project(tmp_path / "source")

    staged = stage_project(source, tmp_path / "stage")

    assert (staged / "pyproject.toml").is_file()
    assert (staged / "README.md").is_file()
    assert (staged / "src" / "demo" / "module.py").is_file()
    assert not (staged / "notes.txt").exists()


def test_stage_project_rejects_destination_inside_source(tmp_path: Path) -> None:
    source = _make_project(tmp_path / "source")

    with pytest.raises(ValueError, match="outside the source tree"):
        stage_project(source, source / "nested-stage")
