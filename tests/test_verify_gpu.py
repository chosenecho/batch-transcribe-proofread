"""GPU acceptance must distinguish a device probe from actual alignment."""

import json
import sys
from types import SimpleNamespace

import pytest

from scripts import verify_gpu


def fake_torch(monkeypatch, *, backend="cuda", bf16=True):
    class Tensor:
        def __matmul__(self, other):
            return self

        def __mul__(self, value):
            return self

        def all(self):
            return self

        def item(self):
            return True

    def ones(*args, **kwargs):
        if kwargs.get("dtype") == "bf16" and not bf16:
            raise RuntimeError("BF16 allocation unavailable")
        return Tensor()
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        __version__="test-runtime", bfloat16="bf16", float16="fp16",
        version=SimpleNamespace(hip="test" if backend == "rocm" else None,
                                cuda="test" if backend == "cuda" else None),
        cuda=SimpleNamespace(is_available=lambda: True, is_bf16_supported=lambda: bf16,
                             current_device=lambda: 0,
                             synchronize=lambda *args: None, get_device_name=lambda index: "test-device"),
        ones=ones, isfinite=lambda value: value,
        allclose=lambda left, right: True,
    ))


def test_preflight_does_not_claim_inference(monkeypatch, capsys):
    fake_torch(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["verify_gpu.py", "--backend", "cuda"])
    assert verify_gpu.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["bf16"] is True
    assert report["real_inference"] == "not_run"


def test_implicit_device_uses_current_gpu_for_identity_allocation_and_sync(monkeypatch):
    from bili_asr.asr.device_probe import probe_gpu
    fake_torch(monkeypatch)
    torch = sys.modules["torch"]
    torch.cuda.current_device = lambda: 2
    torch.cuda.get_device_name = lambda index: "current-gpu" if index == 2 else pytest.fail("wrong GPU identity")
    synchronized, allocations = [], []
    torch.cuda.synchronize = synchronized.append
    original = torch.ones
    def ones(*args, **kwargs):
        allocations.append(kwargs["device"])
        return original(*args, **kwargs)
    torch.ones = ones
    assert probe_gpu("cuda")["device"] == "current-gpu"
    assert synchronized == [2] and allocations == ["cuda:2"]


@pytest.mark.parametrize("backend,bf16", [("rocm", True), ("cuda", False)])
def test_rejects_wrong_backend_or_missing_bf16(monkeypatch, backend, bf16):
    fake_torch(monkeypatch, backend=backend, bf16=bf16)
    monkeypatch.setattr(sys, "argv", ["verify_gpu.py", "--backend", "cuda"])
    with pytest.raises(RuntimeError):
        verify_gpu.main()


def test_transcript_without_alignment_is_not_accepted(tmp_path, monkeypatch):
    fake_torch(monkeypatch)
    released = []
    from bili_asr import asr

    monkeypatch.setattr(asr, "ASRRunner", lambda config: SimpleNamespace(
        transcribe=lambda audio: [{"text": "speech"}], characters=lambda: None,
        release=lambda: released.append(True),
    ))
    monkeypatch.setattr(sys, "argv", ["verify_gpu.py", "--backend", "cuda",
                       "--audio", str(tmp_path / "sample.wav"), "--model", str(tmp_path / "asr"),
                       "--aligner", str(tmp_path / "aligner")])
    with pytest.raises(RuntimeError, match="alignment"):
        verify_gpu.main()
    assert released == [True]
