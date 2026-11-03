#!/usr/bin/env python3
"""Run the deterministic, offline verification baseline for bili-asr."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]
RESULT_SCHEMA = "bili-asr-verification-baseline/v3"
SNAPSHOT_SCHEMA = "bili-asr-advisory-snapshot/v1"
FIXTURE_SCHEMA = "bili-asr-offline-fixture/v1"
SUPPORTED_PYTHON = (3, 12)
RESULT_DIR = ROOT / "verification-results"
DEFAULT_RESULT = RESULT_DIR / "baseline.json"
MAX_DIAGNOSTIC_LENGTH = 500
SENSITIVE_KEY = r"(?:sessdata|authorization|proxy-authorization|cookie|set-cookie|access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|token|signature|sign|deadline)"
URL_RE = re.compile(r"(?i)https?://[^\s\"']+")
HEADER_SECRET_RE = re.compile(rf"(?im)^(\s*{SENSITIVE_KEY}\s*:\s*)[^\r\n]*")
BEARER_SECRET_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+\-/=]+")
JSON_SECRET_RE = re.compile(rf'(?i)("{SENSITIVE_KEY}"\s*:\s*)"(?:[^"\\]|\\.)*"')
ASSIGNMENT_SECRET_RE = re.compile(rf"(?i)(\b{SENSITIVE_KEY}\s*(?:=|:)\s*)[^\s,;]+")
BARE_SECRET_RE = re.compile(rf"(?i)(\b{SENSITIVE_KEY}\s+)[^\s,;]+")


class PrerequisiteError(RuntimeError):
    pass


def redact(value: str, limit: int = MAX_DIAGNOSTIC_LENGTH) -> str:
    """Redact routine credential and signed-URL formats before persistence."""
    redacted = URL_RE.sub("[redacted-url]", value)
    redacted = HEADER_SECRET_RE.sub(r"\1[redacted]", redacted)
    redacted = BEARER_SECRET_RE.sub("Bearer [redacted]", redacted)
    redacted = JSON_SECRET_RE.sub(r'\1"[redacted]"', redacted)
    redacted = ASSIGNMENT_SECRET_RE.sub(r"\1[redacted]", redacted)
    redacted = BARE_SECRET_RE.sub(r"\1[redacted]", redacted)
    return redacted[:limit]


def safe_env() -> dict[str, str]:
    removed = {
        "PYTHONPATH", "BILI_SESSDATA", "SESSDATA", "HTTP_PROXY", "HTTPS_PROXY",
        "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
    }
    env = {key: value for key, value in os.environ.items() if key not in removed}
    env.update(PYTHONNOUSERSITE="1", PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1")
    return env


def record_command(
    result: dict[str, Any], name: str, command: list[str], cwd: Path, *, env: dict[str, str] | None = None
) -> None:
    proc = subprocess.run(command, cwd=cwd, env=safe_env() if env is None else env, capture_output=True, text=True)
    output = redact((proc.stdout or "") + (proc.stderr or ""))
    result["commands"].append({"name": name, "returncode": proc.returncode})
    if proc.returncode:
        if name == "create_venv":
            raise PrerequisiteError("isolated_environment_unavailable: Python 3.12 venv/ensurepip support is required")
        if name == "bootstrap_declared_build_requirements":
            raise PrerequisiteError(
                "offline_dependency_closure_unavailable: declared build backend bootstrap failed "
                f"(exit {proc.returncode}); fixture must contain compatible declared build-system wheels"
            )
        if name == "install_declared_dev_extras":
            raise PrerequisiteError(f"offline_dependency_closure_unavailable: install failed (exit {proc.returncode}): {output}")
        raise RuntimeError(f"{name}_failed: exit {proc.returncode}: {output}")


def scripts_dir(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def script_path(venv: Path, name: str) -> Path:
    return scripts_dir(venv) / (f"{name}.exe" if os.name == "nt" else name)


def load_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise PrerequisiteError("advisory_snapshot_invalid: snapshot is unreadable") from exc
    if not isinstance(data, dict) or data.get("schema") != SNAPSHOT_SCHEMA or not isinstance(data.get("advisories"), list):
        raise PrerequisiteError(f"advisory_snapshot_invalid: expected schema {SNAPSHOT_SCHEMA} and advisories list")
    seen_ids: set[str] = set()
    validated: list[dict[str, str]] = []
    for item in data["advisories"]:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) and item[key].strip() for key in ("id", "name", "specifier")):
            raise PrerequisiteError("advisory_snapshot_invalid: entries require non-empty string id, name, and specifier")
        advisory_id = item["id"].strip()
        if advisory_id in seen_ids:
            raise PrerequisiteError(f"advisory_snapshot_invalid: duplicate advisory id {advisory_id}")
        try:
            SpecifierSet(item["specifier"])
        except InvalidSpecifier as exc:
            raise PrerequisiteError(f"advisory_snapshot_invalid: unsupported specifier for {advisory_id}") from exc
        seen_ids.add(advisory_id)
        validated.append({"id": advisory_id, "name": canonicalize_name(item["name"]), "specifier": item["specifier"]})
    snapshot = {**data, "advisories": validated}
    return snapshot, hashlib.sha256(raw).hexdigest()[:16]


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wheel_distribution_name(filename: str) -> str:
    try:
        name, _, _, _ = parse_wheel_filename(filename)
    except Exception as exc:
        raise PrerequisiteError(f"offline_fixture_invalid: unsupported wheel filename {filename}") from exc
    return canonicalize_name(name)


def validate_fixture(path: Path) -> dict[str, Any]:
    manifest_path = path / "fixture-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrerequisiteError("offline_fixture_invalid: fixture-manifest.json is required and must be valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != FIXTURE_SCHEMA or not isinstance(manifest.get("artifacts"), list):
        raise PrerequisiteError(f"offline_fixture_invalid: expected schema {FIXTURE_SCHEMA} and artifacts list")
    artifacts = manifest["artifacts"]
    if not artifacts:
        raise PrerequisiteError("offline_fixture_invalid: fixture has no declared artifacts")
    artifact_names: set[str] = set()
    artifact_distributions: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str) or not isinstance(item.get("sha256"), str):
            raise PrerequisiteError("offline_fixture_invalid: artifacts require filename and sha256")
        filename = item["filename"]
        candidate = path / filename
        if filename in artifact_names or candidate.parent != path or not candidate.is_file() or file_digest(candidate) != item["sha256"]:
            raise PrerequisiteError("offline_fixture_invalid: artifact content does not match fixture manifest")
        distribution = wheel_distribution_name(filename)
        if distribution in artifact_distributions:
            raise PrerequisiteError("offline_fixture_invalid: multiple artifacts provided for one distribution")
        artifact_names.add(filename)
        artifact_distributions.add(distribution)
    actual_wheels = {candidate.name for candidate in path.glob("*.whl") if candidate.is_file()}
    if actual_wheels != artifact_names:
        raise PrerequisiteError("offline_fixture_invalid: wheel artifacts must exactly match fixture manifest")
    required = manifest.get("required_distributions")
    if not isinstance(required, list) or not required or not all(isinstance(name, str) and name.strip() for name in required):
        raise PrerequisiteError("offline_fixture_invalid: required_distributions is missing")
    required_distributions = {canonicalize_name(name) for name in required}
    if len(required_distributions) != len(required):
        raise PrerequisiteError("offline_fixture_invalid: required_distributions contains duplicates")
    if required_distributions != artifact_distributions:
        raise PrerequisiteError("offline_fixture_invalid: required_distributions must exactly match wheel artifacts")
    return manifest


def build_requirements(project_root: Path) -> list[str]:
    try:
        import tomllib

        config = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PrerequisiteError("project_metadata_invalid: pyproject.toml is unreadable") from exc
    requirements = config.get("build-system", {}).get("requires", [])
    if not isinstance(requirements, list) or not all(isinstance(item, str) and item.strip() for item in requirements):
        raise PrerequisiteError("project_metadata_invalid: build-system requires must be a list")
    return requirements


def offline_install_commands(python: Path, fixture: Path) -> tuple[list[str], list[str]]:
    options = ["--no-index", "--find-links", str(fixture.resolve())]
    bootstrap = [str(python), "-m", "pip", "install", *options, *build_requirements(ROOT)]
    project = [str(python), "-m", "pip", "install", *options, "--no-build-isolation", ".[dev]"]
    return bootstrap, project


def installed_packages(venv_python: Path) -> dict[str, str]:
    proc = subprocess.run([str(venv_python), "-m", "pip", "list", "--format=json"], env=safe_env(), capture_output=True, text=True)
    if proc.returncode:
        raise PrerequisiteError("isolated_environment_invalid: cannot inspect installed distributions")
    try:
        distributions = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise PrerequisiteError("isolated_environment_invalid: invalid distribution metadata") from exc
    if not isinstance(distributions, list):
        raise PrerequisiteError("isolated_environment_invalid: invalid distribution metadata")
    packages: dict[str, str] = {}
    for distribution in distributions:
        if not isinstance(distribution, dict) or not isinstance(distribution.get("name"), str) or not isinstance(distribution.get("version"), str):
            raise PrerequisiteError("isolated_environment_invalid: invalid distribution metadata")
        packages[canonicalize_name(distribution["name"])] = distribution["version"]
    return packages


def advisory_applies(advisory: dict[str, str], installed_version: str) -> bool:
    try:
        return Version(installed_version) in SpecifierSet(advisory["specifier"])
    except (InvalidSpecifier, InvalidVersion) as exc:
        raise PrerequisiteError(f"advisory_snapshot_invalid: unsupported specifier for {advisory['id']}") from exc


def audit(venv_python: Path, snapshot: dict[str, Any], digest: str, result: dict[str, Any]) -> None:
    packages = installed_packages(venv_python)
    findings = [
        {"id": advisory["id"], "package": advisory["name"], "specifier": advisory["specifier"]}
        for advisory in snapshot["advisories"]
        if advisory["name"] in packages and advisory_applies(advisory, packages[advisory["name"]])
    ]
    result["audit"] = {
        "status": "findings" if findings else "passed",
        "policy": "local snapshot only; no pip-audit or network lookup",
        "snapshot": {"schema": snapshot["schema"], "sha256_16": digest, "advisory_count": len(snapshot["advisories"])},
        "finding_ids": [finding["id"] for finding in findings],
        "findings": findings,
        "installed_distribution_count": len(packages),
    }
    if findings:
        raise RuntimeError("security_audit_findings: remediation belongs in a separate plan")


def copy_checkout_docs(source_root: Path, destination: Path) -> None:
    source_docs = source_root / "docs"
    if not source_docs.is_dir():
        return

    entries: list[tuple[Path, Path]] = []
    for current, directories, files in os.walk(source_docs, followlinks=False):
        current_path = Path(current)
        for name in directories + files:
            source = current_path / name
            if source.is_symlink():
                raise PrerequisiteError(f"docs_invalid: symlink is not allowed in docs: {source.relative_to(source_root)}")
            if source.is_file():
                entries.append((source, source.relative_to(source_docs)))
            elif source.is_dir():
                continue
            else:
                raise PrerequisiteError(f"docs_invalid: unsupported entry in docs: {source.relative_to(source_root)}")

    docs_destination = destination / "docs"
    for source, relative in entries:
        target = docs_destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def staged_test_tree(destination: Path) -> Path:
    shutil.copy2(ROOT / "README.md", destination / "README.md")
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(ROOT / name, destination / name)
    copy_checkout_docs(ROOT, destination)
    # Keep the verifier module available to test_verify_baseline without importing
    # any checkout source; all product imports must still resolve from the wheel.
    staged_scripts = destination / "scripts"
    staged_scripts.mkdir()
    shutil.copy2(Path(__file__), staged_scripts / "verify_baseline.py")
    shutil.copy2(ROOT / "scripts" / "check_asr_env.py", staged_scripts / "check_asr_env.py")
    shutil.copy2(ROOT / "scripts" / "forensic_log.py", staged_scripts / "forensic_log.py")
    (staged_scripts / "__init__.py").write_text("", encoding="utf-8")
    staged = destination / "tests"
    shutil.copytree(
        ROOT / "tests",
        staged,
        ignore=lambda directory, names: {
            name
            for name in names
            if name in {"test_verify_baseline.py", "test_installed_cli.py"}
            or name == "__pycache__"
            or name.endswith(".pyc")
        },
    )
    (staged / "conftest.py").write_text(
        (staged / "conftest.py").read_text(encoding="utf-8").replace(
            'sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))\n',
            "",
        ),
        encoding="utf-8",
    )
    return staged


def install_network_deny_guard() -> None:
    """Deny connection attempts in this interpreter before they reach the OS."""
    def _deny(*args, **kwargs):
        raise RuntimeError("network access denied by verification baseline")

    socket.create_connection = _deny
    socket.socket.connect = _deny
    socket.socket.connect_ex = _deny


def network_deny_sitecustomize(destination: Path) -> None:
    (destination / "sitecustomize.py").write_text(
        "from scripts.verify_baseline import install_network_deny_guard\n"
        "install_network_deny_guard()\n",
        encoding="utf-8",
    )


def pytest_env(temporary: Path) -> dict[str, str]:
    env = safe_env()
    env["PYTHONPATH"] = str(temporary)
    return env


def result_path_from_args(args: argparse.Namespace) -> Path:
    requested = DEFAULT_RESULT if args.result is None else args.result.resolve()
    if requested != DEFAULT_RESULT.resolve():
        raise PrerequisiteError("result_path_invalid: --result must be verification-results/baseline.json")
    return DEFAULT_RESULT


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, help="reserved; only verification-results/baseline.json is allowed")
    parser.add_argument("--advisory-snapshot", type=Path, required=True)
    parser.add_argument("--offline-packages", type=Path, required=True, help="validated offline fixture directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    result: dict[str, Any] = {"schema": RESULT_SCHEMA, "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}", "project": "bili-asr", "network_policy": "process-level-deny", "commands": [], "status": "failed"}
    result_path = DEFAULT_RESULT
    try:
        try:
            args = parse_args(argv)
        except SystemExit:
            result["status"] = "prerequisite_failed"; result["error"] = "invalid_arguments: use --help for supported options"; return 2
        result_path_from_args(args)
        if sys.version_info[:2] != SUPPORTED_PYTHON:
            raise PrerequisiteError(f"python_version_unsupported: Python 3.12 required; running {sys.version_info.major}.{sys.version_info.minor}")
        if not args.offline_packages.is_dir():
            raise PrerequisiteError("offline_fixture_invalid: --offline-packages must be a fixture directory")
        fixture = validate_fixture(args.offline_packages)
        snapshot, digest = load_snapshot(args.advisory_snapshot)
        result["fixture"] = {"schema": fixture["schema"], "artifact_count": len(fixture["artifacts"]), "required_distributions": fixture["required_distributions"]}
        with tempfile.TemporaryDirectory(prefix="bili-asr-verify-") as temp:
            temporary = Path(temp)
            venv = temporary / "venv"
            record_command(result, "create_venv", [sys.executable, "-m", "venv", str(venv)], ROOT)
            python = scripts_dir(venv) / ("python.exe" if os.name == "nt" else "python")
            bootstrap, project_install = offline_install_commands(python, args.offline_packages)
            record_command(result, "bootstrap_declared_build_requirements", bootstrap, ROOT)
            record_command(result, "install_declared_dev_extras", project_install, ROOT)
            record_command(result, "installed_console_help", [str(script_path(venv, "bili-asr")), "--help"], temporary)
            staged_tests = staged_test_tree(temporary)
            network_deny_sitecustomize(temporary)
            record_command(result, "full_installed_distribution_test_suite", [str(python), "-m", "pytest", "-q", str(staged_tests)], temporary, env=pytest_env(temporary))
            audit(python, snapshot, digest, result)
        result["status"] = "passed"
        return 0
    except PrerequisiteError as exc:
        result["status"] = "prerequisite_failed"; result["error"] = redact(str(exc)); return 2
    except Exception as exc:
        result["status"] = "failed"; result["error"] = redact(str(exc)); return 1
    finally:
        DEFAULT_RESULT.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_RESULT.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
