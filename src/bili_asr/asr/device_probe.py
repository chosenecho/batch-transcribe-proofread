"""Explicit GPU execution and model-class import probe in a bounded child."""

from __future__ import annotations

import argparse
import json
import re

from .errors import ASRDependencyError, ASRRuntimeCompatibilityError
from .runtime_import import import_model_classes, root_error_type


def probe_gpu(backend: str, *, device: str = "cuda", dtypes: tuple[str, ...] = ("bfloat16",)) -> dict:
    import torch

    if backend not in ("cuda", "rocm", "hcu") or any(dtype not in ("bfloat16", "float16") for dtype in dtypes):
        raise ValueError("unsupported backend or precision")
    if re.fullmatch(r"cuda(?::[0-9]+)?", device) is None or not torch.cuda.is_available():
        raise RuntimeError("selected GPU device is unavailable")
    index = int(device.split(":", 1)[1]) if ":" in device else torch.cuda.current_device()
    device = f"cuda:{index}"
    name = torch.cuda.get_device_name(index)
    runtime = "rocm" if torch.version.hip else "cuda" if torch.version.cuda else "unknown"
    is_hcu = bool(re.search(r"hygon|海光|bw1000|k100|hcu", name, re.I))
    if (backend == "hcu" and not is_hcu) or (backend != "hcu" and (runtime != backend or is_hcu)):
        raise RuntimeError("device identity does not match selected backend")
    results = {}
    for dtype in dict.fromkeys(dtypes):
        # Capability flags are advisory; acceptance requires actual allocation
        # and arithmetic on the selected device, including vendor Torch builds.
        tensor = torch.ones((32, 32), device=device, dtype=getattr(torch, dtype))
        product = tensor @ tensor
        torch.cuda.synchronize(index)
        if not bool(torch.isfinite(product).all().item()) or not torch.allclose(product, tensor * 32):
            raise RuntimeError("selected precision matrix multiplication failed")
        results[dtype] = "passed"
    return {"backend": backend, "torch": str(torch.__version__), "cuda": torch.version.cuda,
            "hip": torch.version.hip, "device": str(name), "precision_execution": results,
            "model_execution": "not_run"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("cuda", "rocm", "hcu"), required=True)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--dtype", choices=("bfloat16", "float16"), action="append")
    args = parser.parse_args()
    try:
        report = probe_gpu(args.backend, device=args.device, dtypes=tuple(args.dtype or ["bfloat16"]))
    except Exception as exc:  # The parent receives only a bounded category, never driver credentials/paths.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__[:64]}))
        return 1
    try:
        import_model_classes()
    except ASRDependencyError as exc:
        print(json.dumps({"status": "failed", **report, "gpu_status": "passed",
                          "model_class_import": "failed", "root_cause_type": root_error_type(exc),
                          "runtime_error": "incompatible" if isinstance(exc, ASRRuntimeCompatibilityError) else "missing"}))
        return 1
    print(json.dumps({"status": "passed", **report, "model_class_import": "passed"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
