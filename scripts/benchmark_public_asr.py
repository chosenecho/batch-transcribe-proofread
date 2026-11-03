#!/usr/bin/env python3
"""Fetch a pinned FLEURS subset and measure the real archive ASR path in WSL.

Reference text is used only after inference, never as decoder or hotword input.
Audio stays in the operator cache; the manifest and result JSON are shareable.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import time
import unicodedata


DATASET = "FluidInference/fleurs-full"
LANGUAGE = "cmn_hans_cn"
NORMALIZATION = "NFKC, lowercase, remove Unicode punctuation and whitespace; preserve numerals and symbols"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".pending")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def normalize(text: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKC", text).lower()
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def character_errors(reference: str, hypothesis: str) -> dict:
    """Levenshtein CER with deterministic diagonal/deletion/insertion ties."""
    ref, hyp = normalize(reference), normalize(hypothesis)
    costs = [list(range(len(hyp) + 1))]
    for i, expected in enumerate(ref, 1):
        row = [i]
        for j, observed in enumerate(hyp, 1):
            row.append(min(costs[i - 1][j - 1] + (expected != observed),
                           costs[i - 1][j] + 1, row[j - 1] + 1))
        costs.append(row)
    i, j = len(ref), len(hyp)
    substitutions = deletions = insertions = 0
    while i or j:
        if i and j and costs[i][j] == costs[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            substitutions += ref[i - 1] != hyp[j - 1]
            i, j = i - 1, j - 1
        elif i and costs[i][j] == costs[i - 1][j] + 1:
            deletions += 1
            i -= 1
        else:
            insertions += 1
            j -= 1
    errors = substitutions + deletions + insertions
    return {"reference_chars": len(ref), "hypothesis_chars": len(hyp),
            "substitutions": substitutions, "deletions": deletions, "insertions": insertions,
            "errors": errors, "cer": errors / len(ref) if ref else None,
            "normalized_reference": ref, "normalized_hypothesis": hyp}


def download(session, url: str, path: Path, max_bytes: int) -> None:
    temporary = path.with_name(path.name + ".download")
    try:
        with session.get(url, timeout=(15, 60), stream=True) as response:
            response.raise_for_status()
            size = 0
            with temporary.open("wb") as stream:
                for block in response.iter_content(1024 * 1024):
                    size += len(block)
                    if size > max_bytes:
                        raise ValueError(f"download exceeded {max_bytes} bytes")
                    stream.write(block)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def fetch(args) -> None:
    import requests
    import soundfile as sf
    from requests.adapters import HTTPAdapter

    root = args.cache_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    endpoint = args.endpoint.rstrip("/")
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=2))
    metadata = session.get(f"{endpoint}/api/datasets/{DATASET}", timeout=30)
    metadata.raise_for_status()
    revision = args.revision or metadata.json()["sha"]
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("dataset revision must be a full commit hash")
    prefix = f"{endpoint}/datasets/{DATASET}/resolve/{revision}/{LANGUAGE}"
    transcript = root / f"{LANGUAGE}.trans.txt"
    download(session, f"{prefix}/{transcript.name}", transcript, 4 * 1024 * 1024)
    rows = []
    for line in transcript.read_text(encoding="utf-8").splitlines():
        identifier, reference = line.split(maxsplit=1)
        if not re.fullmatch(r"cmn_hans_cn_[0-9]{4}", identifier):
            raise ValueError("unexpected sample identifier")
        rows.append((identifier, reference))
    rows.sort()
    if not 2 <= args.count <= len(rows):
        raise ValueError("sample count must be between 2 and the corpus size")
    indices = [round(i * (len(rows) - 1) / (args.count - 1)) for i in range(args.count)]
    samples = []
    for index in indices:
        identifier, reference = rows[index]
        path = root / f"{identifier}.wav"
        download(session, f"{prefix}/{path.name}", path, 20 * 1024 * 1024)
        info = sf.info(path)
        if info.samplerate != 16000 or info.channels != 1:
            raise ValueError("expected the publisher's 16 kHz mono WAV format")
        sample = {"id": identifier, "filename": path.name, "reference": reference,
                  "duration_s": info.duration, "sample_rate": info.samplerate,
                  "sha256": sha256(path), "source_row_index": index,
                  "source_url": f"https://huggingface.co/datasets/{DATASET}/resolve/{revision}/{LANGUAGE}/{path.name}"}
        samples.append(sample)
        print(json.dumps({"downloaded": identifier, "duration_s": info.duration}), flush=True)
    manifest = {"schema_version": 1, "dataset": DATASET, "revision": revision,
                "upstream": "https://huggingface.co/datasets/google/fleurs",
                "publisher": f"https://huggingface.co/datasets/{DATASET}",
                "license": "CC-BY-4.0", "attribution": "Google FLEURS; WAV extraction by FluidInference",
                "split": "test (according to the WAV publisher)", "language": LANGUAGE,
                "selection": "uniformly spaced indices over sorted publisher IDs; no output-based selection",
                "corpus_rows": len(rows), "download_endpoint": endpoint,
                "reference_sha256": sha256(transcript), "samples": samples}
    save_json(root / "manifest.json", manifest)


def verify_sample(root: Path, sample: dict) -> Path:
    path = (root / sample["filename"]).resolve()
    if path.parent != root.resolve():
        raise ValueError("sample escaped cache root")
    if sha256(path) != sample["sha256"]:
        raise ValueError(f"audio hash mismatch: {sample['id']}")
    return path


def compose(args) -> None:
    """Make labelled synthetic chunk-boundary and silence controls."""
    import numpy as np
    import soundfile as sf

    source = args.source.resolve()
    root = args.cache_root.resolve()
    if root == source:
        raise ValueError("derived cache must differ from the downloaded corpus")
    if not math.isfinite(args.gap_seconds) or args.gap_seconds < 0:
        raise ValueError("gap must be finite and nonnegative")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    root.mkdir(parents=True, exist_ok=True)
    parts, components = [], []
    offset = 0
    for index, sample in enumerate(manifest["samples"]):
        path = verify_sample(source, sample)
        audio, rate = sf.read(path, dtype="float32")
        if rate != 16000 or audio.ndim != 1:
            raise ValueError("composition requires 16 kHz mono source audio")
        if index:
            gap = np.zeros(round(args.gap_seconds * rate), dtype=np.float32)
            parts.append(gap)
            offset += len(gap)
        components.append({"id": sample["id"], "sha256": sample["sha256"],
                           "start_s": offset / rate, "end_s": (offset + len(audio)) / rate})
        parts.append(audio)
        offset += len(audio)
    derived = [
        ("concatenated", np.concatenate(parts), " ".join(sample["reference"] for sample in manifest["samples"])),
        ("silence", np.zeros(8 * 16000, dtype=np.float32), ""),
    ]
    samples = []
    for identifier, audio, reference in derived:
        path = root / f"{identifier}.wav"
        # Float WAV preserves the original decoded amplitude without another PCM quantization.
        sf.write(path, audio, 16000, subtype="FLOAT")
        samples.append({"id": identifier, "filename": path.name, "reference": reference,
                        "duration_s": len(audio) / 16000, "sha256": sha256(path), "sample_rate": 16000})
    manifest.update(parent_manifest_sha256=sha256(source / "manifest.json"),
                    selection="all selected public clips concatenated in order, plus digital silence",
                    synthetic=True, gap_seconds=args.gap_seconds, components=components, samples=samples,
                    split="synthetic controls derived from published test samples")
    save_json(root / "manifest.json", manifest)
    print(json.dumps({"derived_duration_s": samples[0]["duration_s"], "silence_s": 8}), flush=True)


def checkpoint_fingerprint(root: Path) -> dict:
    paths = sorted(set(root.glob("*.json")) | set(root.glob("*.safetensors")))
    if not any(path.suffix == ".safetensors" for path in paths):
        raise ValueError("benchmark requires local safetensors checkpoints")
    files = [{"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)} for path in paths]
    return {"path": str(root), "files": files,
            "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}


def aggregate(rows: list[dict]) -> dict:
    total = {key: sum(row["score"][key] for row in rows)
             for key in ("reference_chars", "substitutions", "deletions", "insertions", "errors")}
    total["cer"] = total["errors"] / total["reference_chars"] if total["reference_chars"] else None
    total["samples"] = len(rows)
    total["failed_samples"] = sum(row["status"] != "ok" for row in rows)
    total["audio_duration_s"] = sum(row["duration_s"] for row in rows)
    total["elapsed_s"] = sum(row["elapsed_s"] for row in rows)
    total["rtf"] = total["elapsed_s"] / total["audio_duration_s"] if total["audio_duration_s"] else None
    return total


def run(args) -> None:
    from bili_asr.asr import ASRConfig, ASRRunner, two_pass_transcribe

    root = args.cache_root.resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    samples = manifest["samples"]
    paths = [verify_sample(root, sample) for sample in samples]
    config = ASRConfig(model_name=str(args.model.resolve()), aligner_name=str(args.aligner.resolve()),
                       device="cpu", language="Chinese", chunk_seconds=args.chunk_seconds, offline=True)
    revision = args.source_revision or subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("source revision must be a full commit hash")
    report = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "source_revision": revision, "manifest": manifest, "normalization": NORMALIZATION,
              "reference_used_as_model_input": False, "inference": "production two_pass_transcribe; reusable CPU runner",
              "checkpoint_fingerprints": {"asr": checkpoint_fingerprint(args.model.resolve()),
                                           "aligner": checkpoint_fingerprint(args.aligner.resolve())},
              "arms": [], "completed": False}
    runner = ASRRunner(config)
    try:
        for language in args.languages:
            runner.config = replace(config, language=None if language == "auto" else language)
            arm = {"language": language, "config": asdict(runner.config), "rows": []}
            report["arms"].append(arm)
            for sample, path in zip(samples, paths, strict=True):
                clock = time.perf_counter()
                row = {"id": sample["id"], "duration_s": sample["duration_s"], "status": "ok"}
                try:
                    segments = two_pass_transcribe(runner, str(path), paired_subtitle_text=None)
                    diagnostic = runner.diagnostics()
                    hypothesis = "".join(segment["text"] for segment in segments)
                    raw = "".join(chunk["text"] for chunk in diagnostic["passes"][-1]["chunks"])
                    row.update(hypothesis=hypothesis, decoder_hypothesis=raw, segments=segments,
                               diagnostics=diagnostic, provenance=runner.provenance())
                    invalid = sum(not (math.isfinite(segment["start"]) and math.isfinite(segment["end"])
                                      and 0 <= segment["start"] <= segment["end"] <= sample["duration_s"] + 0.05)
                                  for segment in segments)
                    row["segment_bounds_violations"] = invalid
                    row["segment_order_violations"] = sum(a["start"] > b["start"] for a, b in zip(segments, segments[1:]))
                except Exception as exc:
                    hypothesis = raw = ""
                    row.update(status="failed", error_type=type(exc).__name__, error=str(exc)[:512])
                row["elapsed_s"] = time.perf_counter() - clock
                row["score"] = character_errors(sample["reference"], hypothesis)
                row["decoder_score"] = character_errors(sample["reference"], raw)
                arm["rows"].append(row)
                arm["aggregate"] = aggregate(arm["rows"])
                save_json(args.output, report)
                print(json.dumps({"language": language, "id": sample["id"], "status": row["status"],
                                  "cer": row["score"]["cer"], "elapsed_s": row["elapsed_s"]}), flush=True)
        report["completed"] = True
        save_json(args.output, report)
    finally:
        runner.release()
    if any(arm["aggregate"]["failed_samples"] for arm in report["arms"]):
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch_parser = subparsers.add_parser("fetch")
    fetch_parser.add_argument("--cache-root", type=Path, required=True)
    fetch_parser.add_argument("--endpoint", default="https://huggingface.co")
    fetch_parser.add_argument("--revision", default=None)
    fetch_parser.add_argument("--count", type=int, default=12)
    compose_parser = subparsers.add_parser("compose")
    compose_parser.add_argument("--source", type=Path, required=True)
    compose_parser.add_argument("--cache-root", type=Path, required=True)
    compose_parser.add_argument("--gap-seconds", type=float, default=1)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--cache-root", type=Path, required=True)
    run_parser.add_argument("--model", type=Path, required=True)
    run_parser.add_argument("--aligner", type=Path, required=True)
    run_parser.add_argument("--languages", nargs="+", choices=("Chinese", "auto"), default=["Chinese"])
    run_parser.add_argument("--chunk-seconds", type=float, default=180)
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--source-revision", help="Host Git commit for Windows worktrees whose gitdir WSL cannot resolve")
    args = parser.parse_args()
    {"fetch": fetch, "compose": compose, "run": run}[args.command](args)


if __name__ == "__main__":
    main()
