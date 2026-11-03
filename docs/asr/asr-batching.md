# Bounded ASR and alignment batches

The native Hugging Face backend can process adjacent chunks from one claimed task
and one pass together. Opt in when planning a new immutable profile:

```sh
bili-asr workflow plan --part-id 123 --asr-batch-size 2 --aligner-batch-size 2 \
  --batch-max-audio-seconds 360 --batch-max-input-bytes 1610612736 --batch-max-tokens 8192
```

These are example capacity limits, not measured hardware recommendations. Batch
sizes default to 1. Default canonical profile bytes and existing profile IDs are
unchanged. Nondefault settings enter the versioned `batching` profile extension,
the session configuration key and execution evidence. The profiling script
accepts the same five options for isolated acceptance runs.

There is one CPU builder, one pending window, one running batch and no wait to
fill a batch. No additional workflow job is claimed. Window width is the larger
of the independently configured ASR and alignment limits (each at most 8).
Adjacent lengths must be within a factor of two. Total padded audio and a
conservative waveform-times-64 preparation reservation are checked before
feature extraction. Estimated output tokens are checked before ASR preparation;
the exact CPU feature-mask budgets and measured input bytes are checked before
device transfer. A batch requires identical per-item token budgets, so batching
cannot silently raise the generation limit for a shorter item.

Refused groups run as ordinary single chunks with a named diagnostic fallback.
The limits bound additional batching work, not the ordinary single-chunk path.
Oversized single chunks are preserved and processed with the existing profile;
they are neither dropped nor silently truncated. The separate prefetch thread
is disabled when batch scheduling owns preparation. Full audio decoding remains
the existing task input, and is reported separately.

Alignment is grouped independently after ASR text is available. Empty text does
not enter alignment. Each input carries the current pass's text and language;
second-pass text always gets fresh alignment. Results are restored by original
chunk identity and offset. A processor/backend result-count mismatch fails the
task, so partially returned batches cannot publish. Known timestamp and coverage
checks still evaluate raw units; batching does not sort or clamp them.

Only the current task-owned process may run these batches. Parent session
heartbeat, deadline, lease fencing and process termination retain their existing
ownership. Closing the batch generator discards pending results on failure.
Cross-task fairness and per-request cancellation in a shared engine are not
provided by this scheduler, and the backend does not advertise shared abort.

`batching` diagnostics record fallback counts, independent stage batch IDs,
original chunk IDs and observed input peaks. Batch wall timings are counted once
per stage; nested preparation/transfer/forward events are wall observations and
must not be summed as kernel time. EOS evidence excludes padding after an item's
first EOS. Original text/language remain in the ordinary chunk evidence.

Diagnostic schema 2 distinguishes fixed capacity from observed activity.
`limits` contains the one-window/one-stage/one-builder limits; `current` and
`peaks` observe scheduler windows, running decode/alignment stages and native
batch preparation. A running stage includes its preparation and postprocessing;
these counts are not concurrent GPU kernel counts. Delegated serial preparation
is outside the native builder observation. The three original top-level fields
remain capacity aliases for older readers. `current` returns to zero on normal
completion, stage failure or generator close. The suspended generator retains
at most its current window and never builds the next window until the current
one is consumed. `dispatch` states FIFO order, zero fill wait and the parent
session's existing request deadline; there is no additional batch queue deadline.

`resource_admission` records the peak requested conservative reservation, including
refused candidates, and lists excluded resources. Total RSS and total GPU VRAM
admission are explicitly `not_enforced`. This process does not coordinate other
GPU owners or allocate a global memory quota; an operator must size model slots
and batch limits together. Cross-task fairness is not applicable inside one owned
task: another task is never pulled into its local batch. Adding a shared engine
would require a separate capacity and fairness protocol, not just larger limits.

The reservation is not a hard bound on RSS or total VRAM. It excludes processor
temporaries, resident models, KV cache, allocator fragmentation, compiler/Graph
state and other processes. No increase in workflow workers or model replicas is
implied. Real Chinese quality, throughput, cold cost and combined model/batch
peak memory need the isolated GPU comparison described in `asr-profiling.md`;
offline equivalence tests cannot establish those results.

The [2026-10-11 GPU record](../evidence/asr-gpu-validation-2026-10-11.md) subsequently verifies
one fixed short sample, including actual native ASR/alignment batches and explicit
64 MiB fallback. It does not qualify long-input quality or other-process capacity.

The [Transformers Qwen3-ASR processor documentation](https://huggingface.co/docs/transformers/v5.19.0/model_doc/qwen3_asr)
defines native batched audio/transcription and forced alignment inputs. This
implementation uses those installed processor interfaces without introducing a
new inference package or changing the checkpoint format.
