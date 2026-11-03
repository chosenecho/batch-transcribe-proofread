# Explicit HF inference strategies

Default profiles retain checkpoint attention, the existing first-pass cache
behavior, explicit cache disabling on the hotword second pass, and eager runner
execution. Historical canonical profile bytes and IDs remain unchanged. New
nondefault strategies use a versioned `runtime_strategies` profile extension;
changing any setting also rebuilds the task-owned inference session.

`workflow plan` and the isolated `scripts/profile_asr.py` accept:

| Option | Default | Meaning |
| --- | --- | --- |
| `--asr-attention`, `--aligner-attention` | `default` | Independent checkpoint/eager/SDPA/optional Flash Attention 2 request |
| `--asr-cache-implementation` | `default` | Checkpoint, dynamic or static cache within one generate call |
| `--asr-compile` | false | Submit HF `CompileConfig`; requires explicit static cache |
| `--aligner-compile` | false | Use independent `torch.compile` forward |
| `--compile-max-buckets` | 2 | At most this many exact external input signatures per stage, 1–8 |
| `--compile-max-input-tokens` | 4096 | Maximum input token length admitted to static/compiled execution |
| `--compile-max-output-tokens` | 2048 | Maximum generation budget admitted to static/compiled execution |

No packages are installed or upgraded when a strategy is selected. Attention is
requested through the model's setter. Missing APIs or optional kernels leave an
explicit fallback; if a rejected setter changed model state, restoring the
previous implementation must succeed. Actual model attention is observed in
execution diagnostics independently of the requested value.

Static allocation and compilation require finite observed input signatures and
token bounds. Batch limits remain independent and govern audio/CPU preparation.
No padding, shortening or silent truncation is introduced to fit a signature.
An out-of-budget input uses ordinary eager generation. Static cache with no
explicit compile request sets HF `disable_compile=True`, preventing its implicit
auto-compile behavior. CUDA Graphs are disabled in compile options. No standalone
Graph capture or graph pool is implemented or advertised.

ASR compilation is delegated to Transformers' mature static-cache generation
path, rather than replacing the autoregressive loop. Aligner compilation is
separate. Non-CUDA execution, missing APIs and known Dynamo/Inductor compilation
errors produce a named eager fallback. A failing input signature is not compiled
again for that model lifetime. Ordinary model failures and CUDA OOM propagate as
failed attempts; they are not disguised as successful optimization fallback.

Generated KV is never passed between calls, requests or passes. Current HF
creates fresh static caches per generation. The legacy model cache carrier is
also reset and removed after opt-in cache calls, including failures. The existing
second-pass cache policy takes precedence: compilation is bypassed when that
pass explicitly disables cache. Compiled code and shape metadata can remain for
the owned model lifetime; pending input tensors, prompts and generated results
are not retained by this strategy layer. Session cancellation still terminates
the owned process and discards the response before workflow publication.

Each pass's `runtime_strategies` observation distinguishes requested options, API calls,
fallback counts and admitted signatures. The first call for each signature has a
separate wall observation including compilation, execution and any fallback.
These observations are cumulative per model lifetime, so do not sum snapshots
from multiple passes. These changing counters stay outside the stable `execution_policy`;
the read-only throughput report also projects them out of older policy records.
`compile_api_calls` does not prove compiled kernel
execution; inspect an isolated profiler trace for that. The signature cap bounds
external shapes, not PyTorch's internal graph count or its allocator. Total VRAM
and cold/warm costs require actual combined ASR/aligner measurements.

The native HF model port has capability schema 1. The existing process protocol
1 keeps job, owner, attempt, profile digest, runtime binding, request ID and
engine generation in the envelope; it fences mismatched/late responses. Batch
records add pass-local chunk and batch identities without taking ownership of
SQLite jobs. Capability negotiation rejects unsupported precision/compile/cache
requests before model loading. This port runs real HF generation and alignment;
it does not implement or advertise a deployed vLLM/SGLang shared service,
cross-task batching, per-request shared abort, FP8 weights or FP8 KV.

Offline tests verify formulas, configuration identity, finite admission, eager
fallback, cache cleanup, final-pass alignment and real-process ownership. The
[2026-10-11 4090 record](../evidence/asr-gpu-validation-2026-10-11.md) adds a short full-model
sample with actual compiled Triton kernels, graph breaks, allocator peaks and
baseline timings. Human-verified quality, sustained throughput and Hygon/FP8
hardware behavior remain unverified; profiler timings do not establish speedups.

## Deferred FP8 scope (Issue #302)

Real FP8/quantization experiments remain deferred. The default is BF16; the HF
backend advertises only BF16/FP16, with ASR and aligner precision configured
independently. Unsupported precision is rejected before loading. This is a
configuration boundary, not evidence that FP8 weights run or improve quality.

A future isolated implementation belongs behind the existing inference backend
port. Its versioned profile must identify original and quantized weights,
calibration data/method, quantized modules, and the actual audio encoder,
language model, lm_head and aligner precision. Session identity, execution
evidence and published provenance must carry the effective configuration;
historical profiles and bundles are not rewritten. Any explicit BF16 fallback
must be recorded as the actual execution, not labelled FP8.

Activation requires a suitable backend/hardware, an explicit experimental
budget and human-verified Chinese quality samples including names, numbers,
weak/noisy speech, long chunks and the genuine two-pass path. Compare the full
ASR plus alignment workflow for quality, timestamps, omissions/repetition,
failure rate, memory and throughput. Offline capability tests cannot close the
real-effects acceptance. This repair does not download/convert quantized weights
or change the production BF16 path.

Sources: [Transformers Qwen3-ASR compilation](https://huggingface.co/docs/transformers/v5.19.0/model_doc/qwen3_asr),
[HF cache strategies](https://huggingface.co/docs/transformers/v5.19.0/kv_cache),
[generation configuration source](https://github.com/huggingface/transformers/blob/v5.19.0/src/transformers/generation/configuration_utils.py),
[PyTorch compile API](https://docs.pytorch.org/docs/stable/generated/torch.compile.html).
