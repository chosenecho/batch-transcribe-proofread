"""Verify the selected GPU runtime and optionally real local ASR/alignment."""

from __future__ import annotations

import argparse
import json
import time
import hashlib
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("cuda", "rocm", "hcu"), required=True)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--model-dtype", choices=("bfloat16", "float16"), default="bfloat16")
    parser.add_argument("--aligner-dtype", choices=("bfloat16", "float16"), default="bfloat16")
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--aligner", type=Path)
    parser.add_argument("--language", default="Chinese")
    args = parser.parse_args()
    if any((args.audio, args.model, args.aligner)) and not all((args.audio, args.model, args.aligner)):
        parser.error("real inference requires --audio, --model and --aligner together")
    for path in (args.audio, args.model, args.aligner):
        if path is not None and not path.is_absolute():
            parser.error("audio and checkpoint paths must be absolute")
    from bili_asr.asr.device_probe import probe_gpu
    result = probe_gpu(args.backend, device=args.device, dtypes=(args.model_dtype, args.aligner_dtype))
    result.update(bf16="bfloat16" in result["precision_execution"], real_inference="not_run")
    if args.audio:
        from bili_asr.asr import ASRConfig, ASRRunner

        runner = ASRRunner(ASRConfig(model_name=str(args.model), aligner_name=str(args.aligner),
                                     device=args.device, language=args.language, offline=True,
                                     model_dtype=args.model_dtype, aligner_dtype=args.aligner_dtype))
        started = time.monotonic()
        try:
            cues = runner.transcribe(str(args.audio))
            if not cues or not runner.characters():
                raise RuntimeError("real sample produced no transcript or character alignment")
            with args.audio.open("rb") as audio_handle:
                audio_sha256 = hashlib.file_digest(audio_handle, "sha256").hexdigest()
            result.update(real_inference="passed", segments=len(cues), seconds=round(time.monotonic() - started, 2),
                          model_execution="passed", audio_sha256=audio_sha256,
                          provenance=runner.provenance(), diagnostics=runner.diagnostics(),
                          coverage=runner.transcribed_coverage())
        finally:
            runner.release()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
