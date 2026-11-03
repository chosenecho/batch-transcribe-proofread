"""Missing extras, broken lazy imports and real deployment gates are distinct."""

from __future__ import annotations

import builtins
import json
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from bili_asr.asr import deployment, device_probe, runtime_import
from bili_asr.asr.config import ASRConfig
from bili_asr.asr.errors import ASRDependencyError, ASRRuntimeCompatibilityError
from bili_asr.asr.runner import _load_qwen_models


def fail_import(monkeypatch, error):
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "transformers":
            raise error
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)


def test_missing_extra_retains_install_hint_and_original_error(monkeypatch):
    error = ModuleNotFoundError("No module named 'transformers'", name="transformers")
    fail_import(monkeypatch, error)
    with pytest.raises(ASRDependencyError) as caught:
        _load_qwen_models(model_name="unused", aligner_name="unused")
    assert not isinstance(caught.value, ASRRuntimeCompatibilityError)
    assert "not installed" in str(caught.value)
    assert caught.value.__cause__ is error


@pytest.mark.parametrize("error", [AttributeError("secret"), ImportError("secret"),
                                        OSError("secret"), ModuleNotFoundError("secret", name="missing_transitive")])
def test_installed_runtime_import_failure_is_not_a_missing_extra(monkeypatch, error):
    fail_import(monkeypatch, error)
    with pytest.raises(ASRRuntimeCompatibilityError) as caught:
        _load_qwen_models(model_name="unused", aligner_name="unused")
    assert caught.value.__cause__ is error
    assert "not installed" not in str(caught.value)
    assert "secret" not in str(caught.value)
    assert "root_error_type=" + type(error).__name__ in str(caught.value)


def test_lazy_module_not_found_keeps_actual_torch_attribute_cause(monkeypatch):
    root = AttributeError("module 'torch' has no attribute 'accelerator'")
    wrapped = ModuleNotFoundError("Could not import module", name="transformers")
    wrapped.__cause__ = root
    fail_import(monkeypatch, wrapped)
    monkeypatch.setattr(deployment, "installed_versions", lambda: {
        "torch": "2.5.1+cu121", "transformers": "5.19.0", "huggingface-hub": "2.2.0", "tokenizers": "0.23.3"})
    with pytest.raises(ASRRuntimeCompatibilityError) as caught:
        runtime_import.import_model_classes()
    assert caught.value.__cause__.__cause__ is root
    assert "root_error_type=AttributeError" in str(caught.value)
    assert "2.5.1+cu121" in str(caught.value) and "5.19.0" in str(caught.value)


def test_root_category_follows_context_without_exporting_messages_and_bounds_cycles():
    outer = RuntimeError("secret")
    inner = ValueError("secret")
    outer.__context__ = inner
    assert runtime_import.root_error_type(outer) == "ValueError"
    inner.__cause__ = outer
    assert runtime_import.root_error_type(outer) == "RuntimeError"
    assert runtime_import.root_error_type(Exception("secret")) == "Exception"


@pytest.mark.parametrize("failure", [None, ASRDependencyError("secret"), ASRRuntimeCompatibilityError("secret")])
def test_probe_distinguishes_gpu_math_from_model_class_import(monkeypatch, capsys, failure):
    monkeypatch.setattr(sys, "argv", ["probe", "--backend", "cuda"])
    monkeypatch.setattr(device_probe, "probe_gpu", lambda *args, **kwargs: {
        "precision_execution": {"bfloat16": "passed"}, "model_execution": "not_run"})

    def importing():
        if failure is not None:
            raise failure

    monkeypatch.setattr(device_probe, "import_model_classes", importing)
    assert device_probe.main() == (0 if failure is None else 1)
    report = json.loads(capsys.readouterr().out)
    assert report["precision_execution"] == {"bfloat16": "passed"}
    assert report["model_execution"] == "not_run"
    assert report["model_class_import"] == ("passed" if failure is None else "failed")
    if failure is not None:
        assert report["gpu_status"] == "passed"
        assert report["runtime_error"] == ("incompatible" if isinstance(failure, ASRRuntimeCompatibilityError) else "missing")
    assert "secret" not in json.dumps(report)


def test_probe_device_failure_never_attempts_model_import(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["probe", "--backend", "cuda"])

    def failing(*args, **kwargs):
        raise RuntimeError("secret")

    monkeypatch.setattr(device_probe, "probe_gpu", failing)
    monkeypatch.setattr(device_probe, "import_model_classes", lambda: pytest.fail("device already failed"))
    assert device_probe.main() == 1
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "error_type": "RuntimeError"}


@pytest.mark.parametrize("class_import", ["passed", "failed", None])
def test_deployment_requires_real_class_gate_and_retains_bounded_root(monkeypatch, class_import):
    monkeypatch.setattr(deployment, "installed_versions", lambda: {name: "test" for name in deployment.PACKAGES})
    payload = {"status": "passed", "model_execution": "not_run", "precision_execution": {"bfloat16": "passed"}}
    returncode = 0
    if class_import is not None:
        payload["model_class_import"] = class_import
    if class_import == "failed":
        returncode = 1
        payload.update(status="failed", gpu_status="passed", runtime_error="incompatible", root_cause_type="AttributeError")
    monkeypatch.setattr(deployment.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=returncode, stdout=json.dumps(payload), stderr="secret"))
    report = deployment.deployment_report(ASRConfig("local"), backend="cuda", check_gpu=True)
    assert report["state"] == ("unverified" if class_import == "passed" else "blocked")
    if class_import == "failed":
        assert report["gpu"]["status"] == "passed"
        assert report["model_class_import"] == {"status": "failed", "reason": "runtime_incompatible", "root_cause_type": "AttributeError"}
    assert report["model_execution"] == "not_run"
    assert "secret" not in json.dumps(report)


@pytest.mark.parametrize("root", ["secret", "AttributeError"])
def test_deployment_rejects_untrusted_import_failure_metadata(monkeypatch, root):
    payload = {"status": "failed", "gpu_status": "passed", "model_class_import": "failed",
               "runtime_error": "incompatible", "root_cause_type": root}
    if root == "AttributeError":
        payload["unexpected"] = "secret"
    monkeypatch.setattr(deployment.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout=json.dumps(payload), stderr="secret"))
    report = deployment.deployment_report(ASRConfig("local"), backend="cuda", check_gpu=True)
    assert report["state"] == "blocked"
    assert "secret" not in json.dumps(report)


def test_asr_extra_and_lock_freeze_the_gpu_accepted_dependency_pair():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    extra = project["project"]["optional-dependencies"]["asr"]
    assert "transformers==5.17.0" in extra
    assert "huggingface-hub==1.31.0" in extra
    locked = {entry["name"]: entry["version"] for entry in tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))["package"]}
    assert locked["transformers"] == "5.17.0" and locked["huggingface-hub"] == "1.31.0"
