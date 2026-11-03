"""Validate the installable dependency graph, including externally supplied torch."""

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> dict:
    with (ROOT / name).open("rb") as stream:
        return tomllib.load(stream)


def test_lock_matches_declared_dependency_groups_and_versions() -> None:
    project = _read("pyproject.toml")["project"]
    packages = _read("uv.lock")["package"]
    root = next(package for package in packages if package["name"] == project["name"])
    resolved = {canonicalize_name(package["name"]): package for package in packages}
    groups = {"": project["dependencies"], **project["optional-dependencies"]}
    for extra, declarations in groups.items():
        locked = root["dependencies"] if not extra else root["optional-dependencies"][extra]
        requirements = [Requirement(value) for value in declarations]
        assert {canonicalize_name(item["name"]) for item in locked} == {
            canonicalize_name(requirement.name) for requirement in requirements
        }
        for requirement in requirements:
            assert resolved[canonicalize_name(requirement.name)]["version"] in requirement.specifier


def test_locked_closure_keeps_gpu_runtime_external_and_drops_old_engine() -> None:
    lock = _read("uv.lock")
    names = {canonicalize_name(package["name"]) for package in lock["package"]}
    assert "torch" in lock["manifest"]["excludes"]
    assert {"accelerate", "transformers", "soundfile", "soxr", "librosa"} <= names
    assert not ({"torch", "torchaudio", "triton", "funasr", "torch-complex"} & names)
    assert not any(name.startswith(("nvidia-", "cuda-")) for name in names)
    for package in lock["package"]:
        for dependency in package.get("dependencies", []):
            assert canonicalize_name(dependency["name"]) in names
