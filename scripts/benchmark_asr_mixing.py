"""CPU-only stereo mixing benchmark with output bit checks; no model loading."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bili_asr.asr.audio import _mono_float32


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, nargs="+", default=[100_000, 1_000_000, 8_000_000])
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1 or any(frames < 1 for frames in args.frames):
        parser.error("frames and repeats must be positive")
    rng = np.random.default_rng(1011)
    results = []
    for frames in args.frames:
        # Generate float32 directly so fixture creation avoids a float64 copy.
        samples = rng.random((frames, 2), dtype=np.float32)
        samples -= np.float32(0.5)
        samples.flags.writeable = False
        expected = samples.mean(-1).astype(np.float32)
        runs = []
        for repeat in range(args.repeats):
            order = ["mean", "fast"] if repeat % 2 == 0 else ["fast", "mean"]
            for mode in order:
                started = time.perf_counter()
                actual = samples.mean(-1).astype(np.float32) if mode == "mean" else _mono_float32(samples)
                elapsed = time.perf_counter() - started
                assert np.array_equal(actual.view(np.uint32), expected.view(np.uint32)), "waveform bits differ"
                runs.append({"mode": mode, "elapsed_s": elapsed})
                del actual
        mean_s = statistics.median(run["elapsed_s"] for run in runs if run["mode"] == "mean")
        fast_s = statistics.median(run["elapsed_s"] for run in runs if run["mode"] == "fast")
        results.append({"frames": frames, "input_bytes": samples.nbytes, "mean_median_s": mean_s,
                        "fast_median_s": fast_s, "mix_speedup": mean_s / fast_s,
                        "bits_equal": True, "runs": runs})
    print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
                      "platform": platform.system(), "numpy": np.__version__, "repeats": args.repeats,
                      "audio_module_sha256": hashlib.sha256(
                          (Path(__file__).resolve().parents[1] / "src/bili_asr/asr/audio.py").read_bytes()).hexdigest(),
                      "results": results, "limits": ["Synthetic CPU microbenchmark, not ASR task throughput.",
                      "Input creation and bit comparisons excluded from timed mixing."]}, indent=2))


if __name__ == "__main__":
    main()
