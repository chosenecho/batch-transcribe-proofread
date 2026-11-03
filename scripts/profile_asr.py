#!/usr/bin/env python3
"""Profile isolated, bounded ASR samples; never attach to a production worker."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time
import unicodedata


def _duration(path: Path) -> float:
    child = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "json", str(path)], capture_output=True, text=True,
                           check=True, timeout=30)
    seconds = float(json.loads(child.stdout)["format"]["duration"])
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("audio duration must be positive and finite")
    return seconds


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def assert_idle_gpu(device: str) -> None:
    child = subprocess.run(["nvidia-smi", "-i", device, "--query-compute-apps=pid",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True,
                           check=True, timeout=15)
    if child.stdout.strip():
        raise RuntimeError("selected GPU has an active compute owner; drain before profiling")


def bounded_diagnostics(diagnostics: dict) -> dict:
    """Retain measurements and flags, excluding generated text and language content."""
    result = dict(diagnostics)
    result["passes"] = []
    for source in diagnostics.get("passes", []):
        item = dict(source)
        item["chunks"] = [{key: value for key, value in chunk.items() if key not in ("text", "language")}
                          for chunk in source.get("chunks", [])]
        result["passes"].append(item)
    return result


def character_error_rate(reference: str, hypothesis: str) -> dict:
    """Explicit reproducible character metric; never exports the reference text."""
    def normalize(value):
        return "".join(character for character in unicodedata.normalize("NFC", value)
                       if not character.isspace())

    expected, actual = normalize(reference), normalize(hypothesis)
    if not expected:
        raise ValueError("reference must contain non-whitespace characters")
    # Limit the quadratic metric's work independently of inference duration.
    if len(expected) * len(actual) > 4_000_000:
        return {"status": "unmeasured", "reason": "edit_distance_work_limit",
                "reference_characters": len(expected), "hypothesis_characters": len(actual)}
    previous = list(range(len(actual) + 1))
    for row, expected_character in enumerate(expected, start=1):
        current = [row]
        for column, actual_character in enumerate(actual, start=1):
            current.append(min(current[-1] + 1, previous[column] + 1,
                               previous[column - 1] + (expected_character != actual_character)))
        previous = current
    distance = previous[-1]
    return {"status": "measured", "normalization": "NFC_remove_whitespace_preserve_case_and_punctuation",
            "reference_characters": len(expected), "hypothesis_characters": len(actual),
            "edit_distance": distance, "cer": distance / len(expected),
            "semantic_quality_accepted": False}


@contextmanager
def marked_stage(torch, name: str):
    with torch.profiler.record_function(name):
        torch.cuda.nvtx.range_push(name)
        try:
            yield
        finally:
            torch.cuda.nvtx.range_pop()


def install_ranges(runner, torch) -> None:
    for owner, attribute, label in ((runner, "_prepare_decode_inputs", "asr.prepare"),
            (runner.backend, "generate", "asr.decode"), (runner.backend, "align", "asr.align")):
        original = getattr(owner, attribute)
        def wrapped(*args, _method=original, _label=label, **kwargs):
            with marked_stage(torch, _label):
                return _method(*args, **kwargs)
        setattr(owner, attribute, wrapped)


def run(args) -> dict:
    if not args.ack_exclusive_device:
        raise ValueError("explicit exclusive-device acknowledgement is required")
    if args.output.exists():
        raise ValueError("output directory must not already exist")
    audio_seconds = _duration(args.audio)
    if audio_seconds > args.max_audio_seconds:
        raise ValueError("sample exceeds configured audio duration limit")
    if args.warmup_audio is not None and _duration(args.warmup_audio) > 30:
        raise ValueError("warmup audio must be at most 30 seconds")
    # This runs before importing Torch or loading checkpoints. It is a point-in-
    # time guard, not a scheduler lock: the operator must maintain exclusivity.
    assert_idle_gpu(args.nvidia_device)
    audio_digest = _sha256(args.audio)
    reference = None
    reference_digest = None
    if args.reference_text is not None:
        if args.reference_text.stat().st_size > 64 * 1024:
            raise ValueError("reference text must be at most 64 KiB")
        reference = args.reference_text.read_text(encoding="utf-8")
        character_error_rate(reference, "")  # Validate before loading checkpoints.
        reference_digest = _sha256(args.reference_text)
    from bili_asr.asr.config import ASRConfig
    from bili_asr.asr.runner import ASRRunner, two_pass_transcribe
    import torch

    torch.set_num_threads(args.cpu_threads)
    config = ASRConfig(model_name=str(args.model.resolve()), aligner_name=str(args.aligner.resolve()),
                       device=args.device, language=args.language, chunk_seconds=args.chunk_seconds,
                       second_pass_use_cache=args.second_pass_use_cache, hotwords=tuple(args.hotword),
                       asr_batch_size=args.asr_batch_size, aligner_batch_size=args.aligner_batch_size,
                       batch_max_audio_seconds=args.batch_max_audio_seconds,
                       batch_max_input_bytes=args.batch_max_input_bytes, batch_max_tokens=args.batch_max_tokens,
                       asr_attention=args.asr_attention, aligner_attention=args.aligner_attention,
                       asr_cache_implementation=args.asr_cache_implementation,
                       asr_compile=args.asr_compile, aligner_compile=args.aligner_compile,
                       compile_max_buckets=args.compile_max_buckets,
                       compile_max_input_tokens=args.compile_max_input_tokens,
                       compile_max_output_tokens=args.compile_max_output_tokens,
                       model_dtype=args.model_dtype, aligner_dtype=args.aligner_dtype)
    runner = ASRRunner(config)
    runner.configure_prefetch(enabled=args.prefetch, max_bytes=args.prefetch_bytes)
    args.output.mkdir(parents=True)
    report = {"schema_version": 1, "mode": args.mode, "status": "incomplete",
              "performance_comparison_allowed": args.mode == "baseline",
              "audio_sha256": audio_digest, "audio_seconds": audio_seconds,
              "reference_sha256": reference_digest,
              "measurement": "synchronized_invocation_wall_not_kernel_time",
              "profiler_wall_is_not_normal_throughput": True, "runs": []}
    try:
        started = time.perf_counter()
        runner.prepare()
        torch.cuda.synchronize(args.device)
        report["model_load_wall_s"] = time.perf_counter() - started
        if args.warmup_audio is not None:
            started = time.perf_counter()
            runner.prepare(str(args.warmup_audio.resolve()))
            torch.cuda.synchronize(args.device)
            report["warmup_wall_s"] = time.perf_counter() - started
        else:
            report["warmup_wall_s"] = None
        if args.mode != "baseline":
            install_ranges(runner, torch)
        for index in range(args.repeats):
            context = (torch.profiler.profile(activities=[torch.profiler.ProfilerActivity.CPU,
                       torch.profiler.ProfilerActivity.CUDA], record_shapes=False,
                       profile_memory=False, with_stack=False) if args.mode == "timeline" else nullcontext())
            torch.cuda.synchronize(args.device)
            torch.cuda.reset_peak_memory_stats(args.device)
            started = time.perf_counter()
            with context as profiler:
                cues = two_pass_transcribe(runner, str(args.audio.resolve()), paired_subtitle_text=None)
                torch.cuda.synchronize(args.device)
            elapsed = time.perf_counter() - started
            if profiler is not None:
                profiler.export_chrome_trace(str(args.output / f"timeline-{index}.json"))
            encoded = json.dumps({"cues": cues, "characters": runner.characters()},
                                 sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
            report["runs"].append({"index": index, "wall_s": elapsed,
                "normal_audio_s_per_wall_s": audio_seconds / elapsed if args.mode == "baseline" else None,
                "output_sha256": hashlib.sha256(encoded).hexdigest(), "cue_count": len(cues),
                "character_quality": (character_error_rate(reference, "".join(cue["text"] for cue in cues))
                    if reference is not None else {"status": "unmeasured", "reason": "no_reference"}),
                "gpu_allocator_peak_allocated_bytes": torch.cuda.max_memory_allocated(args.device),
                "gpu_allocator_peak_reserved_bytes": torch.cuda.max_memory_reserved(args.device),
                "diagnostics": bounded_diagnostics(runner.diagnostics())})
            if _sha256(args.audio) != audio_digest:
                raise ValueError("sample audio changed during profiling")
            if args.reference_text is not None and _sha256(args.reference_text) != reference_digest:
                raise ValueError("reference text changed during profiling")
        report["status"] = "completed"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        raise
    finally:
        runner.release()
        (args.output / "measurement.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("baseline", "timeline", "nvtx"), default="baseline")
    for name in ("audio", "model", "aligner", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--warmup-audio", type=Path)
    parser.add_argument("--reference-text", type=Path, help="optional UTF-8 reference for explicit character error rate")
    parser.add_argument("--model-dtype", choices=("bfloat16", "float16"), default="bfloat16")
    parser.add_argument("--aligner-dtype", choices=("bfloat16", "float16"), default="bfloat16")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--nvidia-device", default="0", help="physical nvidia-smi index or UUID matching --device")
    parser.add_argument("--language", default="Chinese")
    parser.add_argument("--chunk-seconds", type=float, default=180)
    parser.add_argument("--max-audio-seconds", type=float, default=360)
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--repeats", type=int, choices=range(1, 11), default=1)
    parser.add_argument("--hotword", action="append", default=[])
    parser.add_argument("--second-pass-use-cache", action="store_true")
    parser.add_argument("--prefetch", action="store_true")
    parser.add_argument("--prefetch-bytes", type=int, default=64 * 1024**2)
    parser.add_argument("--asr-batch-size", type=int, default=1)
    parser.add_argument("--asr-attention", choices=("default", "eager", "sdpa", "flash_attention_2"), default="default")
    parser.add_argument("--aligner-attention", choices=("default", "eager", "sdpa", "flash_attention_2"), default="default")
    parser.add_argument("--asr-cache-implementation", choices=("default", "dynamic", "static"), default="default")
    parser.add_argument("--asr-compile", action="store_true")
    parser.add_argument("--aligner-compile", action="store_true")
    parser.add_argument("--compile-max-buckets", type=int, default=2)
    parser.add_argument("--compile-max-input-tokens", type=int, default=4096)
    parser.add_argument("--compile-max-output-tokens", type=int, default=2048)
    parser.add_argument("--aligner-batch-size", type=int, default=1)
    parser.add_argument("--batch-max-audio-seconds", type=float, default=360)
    parser.add_argument("--batch-max-input-bytes", type=int, default=64 * 1024**2)
    parser.add_argument("--batch-max-tokens", type=int, default=8192)
    parser.add_argument("--ack-exclusive-device", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.cpu_threads <= 64 or not math.isfinite(args.max_audio_seconds) or not 0 < args.max_audio_seconds <= 1800:
        parser.error("CPU threads must be 1..64 and maximum sample duration 0..1800 seconds")
    try:
        run(args)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps({"status": "completed", "mode": args.mode}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
