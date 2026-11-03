"""CLI for the database-backed, independent-producer workflow."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from uuid import uuid4

from bili_asr.archive_modes import ArchiveAccessMode
from bili_asr.artifact_root import roots_for
from bili_asr.asr import default_config
from bili_asr.config import SESSDATA_ENV_VAR, resolve_sessdata
from bili_asr.diagnostics import write_stderr
from bili_asr.editorial import TEMPLATE_VERSION, EditorialConfig
from bili_asr.workflow_models import AsrPolicy, AsrProfile, JobKind
from bili_asr.workflow_roles import WORKER_ROLES, PROCESSING_KINDS


def add_workflow_parser(subparsers: argparse._SubParsersAction, *, archive_root: str) -> None:
    parser = subparsers.add_parser(
        "workflow",
        help="Plan and run ASR and editorial jobs from imported local inputs",
    )
    actions = parser.add_subparsers(dest="workflow_action", required=True)
    batch = actions.add_parser("batch", help="Manage persistent explicit job batches")
    batch.add_argument("--archive-root", default=archive_root)
    batch_actions = batch.add_subparsers(dest="batch_action", required=True)
    create = batch_actions.add_parser("create")
    create.add_argument("--job-id", action="append", required=True)
    create.add_argument("--workers", type=int, default=10)
    create.add_argument("--actor", required=True)
    create.add_argument("--reason", required=True)
    extract = batch_actions.add_parser("extract")
    extract.add_argument("--batch-id", required=True)
    extract.add_argument("--job-id", action="append", required=True)
    extract.add_argument("--workers", type=int, default=1)
    extract.add_argument("--actor", required=True)
    extract.add_argument("--reason", required=True)
    for action in ("status", "pause", "resume"):
        item = batch_actions.add_parser(action)
        item.add_argument("--batch-id", required=True)
        if action != "status":
            item.add_argument("--actor", required=True)
            item.add_argument("--reason", required=True)
            item.add_argument("--workers", type=int)
            item.add_argument("--dry-run", action="store_true", help="Preview state and concurrency without changing retry controls")
    plan = actions.add_parser(
        "plan", help="Create idempotent jobs for stored BVIDs, video-part IDs or every stored part")
    plan.add_argument("--archive-root", default=archive_root)
    selection = plan.add_mutually_exclusive_group(required=True)
    selection.add_argument("--part-id", type=int, action="append", help="Exact stored video_part_id; repeat to select more")
    selection.add_argument("--bvid", action="append", help="Stored BVID; repeat to select more videos")
    selection.add_argument("--all-parts", dest="all_parts", action="store_true",
                           help="Select every stored non-gone part; plan ASR from registered local audio")
    plan.add_argument("--page-index", type=_nonnegative_page_index, default=None,
                      help="Stored zero-based index (0 is P1), applied to each --bvid")
    plan.add_argument("--asr-policy", choices=[item.value for item in AsrPolicy], default="all")
    plan.add_argument("--profile-key", default="qwen3-default")
    plan.add_argument("--model", default=None)
    plan.add_argument("--model-revision", default=None)
    plan.add_argument("--aligner", default=None)
    plan.add_argument("--aligner-revision", default=None)
    plan.add_argument("--model-dtype", choices=("bfloat16", "float16"), default=None)
    plan.add_argument("--aligner-dtype", choices=("bfloat16", "float16"), default=None)
    plan.add_argument("--alignment-decoder", choices=("default", "processor", "monotone-max-score", "monotone-max-score/v1"), default=None,
                      help="Freeze an explicit timestamp decoder; existing profiles stay unchanged")
    plan.add_argument("--coverage-policy", choices=("full-span", "quiet-edges"), default=None,
                      help="Opt in to measured nearly-silent edge evidence; this is not speech VAD")
    plan.add_argument("--device", default=None)
    plan.add_argument("--language", default=None)
    plan.add_argument("--chunk-seconds", type=float, default=None)
    plan.add_argument("--inference-timeout", type=float, default=None)
    plan.add_argument("--inference-timeout-policy", choices=("fixed", "audio-scaled"), default=None)
    plan.add_argument("--inference-max-rtf", type=float, default=None)
    plan.add_argument("--inference-startup-seconds", type=float, default=None)
    plan.add_argument("--inference-hard-cap-seconds", type=float, default=None)
    plan.add_argument("--inference-no-progress-seconds", type=float, default=None)
    plan.add_argument("--hotword", action="append", default=None)
    plan.add_argument("--model-id", default=None)
    plan.add_argument("--offline", action=argparse.BooleanOptionalAction, default=None)
    plan.add_argument("--tokens-per-second", type=float, default=None)
    plan.add_argument("--min-new-tokens", type=int, default=None)
    plan.add_argument("--second-pass-cache", action=argparse.BooleanOptionalAction, default=None)
    plan.add_argument("--asr-batch-size", type=int, default=None)
    plan.add_argument("--asr-attention", choices=("default", "eager", "sdpa", "flash_attention_2"), default=None)
    plan.add_argument("--aligner-attention", choices=("default", "eager", "sdpa", "flash_attention_2"), default=None)
    plan.add_argument("--asr-cache-implementation", choices=("default", "dynamic", "static"), default=None)
    plan.add_argument("--asr-compile", action=argparse.BooleanOptionalAction, default=None)
    plan.add_argument("--aligner-compile", action=argparse.BooleanOptionalAction, default=None)
    plan.add_argument("--compile-max-buckets", type=int, default=None)
    plan.add_argument("--compile-max-input-tokens", type=int, default=None)
    plan.add_argument("--compile-max-output-tokens", type=int, default=None)
    plan.add_argument("--aligner-batch-size", type=int, default=None)
    plan.add_argument("--batch-max-audio-seconds", type=float, default=None)
    plan.add_argument("--batch-max-input-bytes", type=int, default=None)
    plan.add_argument("--batch-max-tokens", type=int, default=None)
    plan.add_argument("--proofread", action="store_true", help="Queue AI proofreading after ASR and then render Markdown")
    _add_editorial_arguments(plan)

    proofread = actions.add_parser("proofread", help="Freeze stored transcript versions and queue AI proofreading")
    proofread.add_argument("--archive-root", default=archive_root)
    selection = proofread.add_mutually_exclusive_group(required=True)
    selection.add_argument("--part-id", type=int, help="Use the newest stored ASR version at planning time")
    selection.add_argument("--base-transcript-id", type=int, help="Use this explicit base transcript version")
    reference = proofread.add_mutually_exclusive_group()
    reference.add_argument("--reference-transcript-id", type=int, default=None)
    reference.add_argument("--no-reference", action="store_true")
    proofread.add_argument("--refresh-input", action="store_true",
        help="Explicitly freeze current prompt/metadata instead of reusing matching stored work; may call AI again")
    _add_editorial_arguments(proofread)

    batch = actions.add_parser("proofread-many", help="Freeze and queue AI proofreading for a batch of stored parts")
    batch.add_argument("--archive-root", default=archive_root)
    batch.add_argument("--part-id", type=int, action="append", required=True)
    batch.add_argument("--no-reference", action="store_true")
    batch.add_argument("--refresh-input", action="store_true")
    _add_editorial_arguments(batch)

    repair_ai = actions.add_parser("repair-proofread",
        help="Queue a replacement for a failed AI job using current prompt; preserve original evidence")
    repair_ai.add_argument("--archive-root", default=archive_root)
    repair_ai.add_argument("--job-id", required=True)
    repair_ai.add_argument("--base-transcript-id", type=int, default=None,
        help="Explicit corrected transcript version for the same part; otherwise retain the old source")
    _add_editorial_arguments(repair_ai)
    repair_ai.set_defaults(reasoning_effort="low", max_chunk_segments=128, max_chunk_chars=10_000)

    recover = actions.add_parser("recover-editorial",
        help="Inspect or queue a new editorial chain for an exact successful ASR replacement; never execute AI")
    recover.set_defaults(database_policy=ArchiveAccessMode.READ)
    recover.add_argument("--archive-root", default=archive_root)
    recover.add_argument("--proofread-job-id", required=True)
    recover.add_argument("--replacement-asr-job-id", required=True)
    recover.add_argument("--apply", action=_RecoveryApply, nargs=0, default=False,
                         help="Queue the exact inspected replacement and retire only untouched queued consumers")
    recover.add_argument("--expected-plan-id", default=None)
    _add_control_arguments(recover)

    render = actions.add_parser("render", help="Queue deterministic Markdown rendering without calling AI")
    render.add_argument("--archive-root", default=archive_root)
    render.add_argument("--revision-id", required=True)
    render.add_argument("--template-version", choices=[TEMPLATE_VERSION, "ai-draft-v2"],
                        help="Override the template resolved from the revision's frozen input")
    render.add_argument("--artifact-root", default=None, help="Product root used by subsequent workflow run invocations")

    run = actions.add_parser("run", help="Claim and execute ready SQLite jobs")
    run.add_argument("--archive-root", default=archive_root)
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--worker-id", default=f"cli-{os.getpid()}-{uuid4().hex[:8]}")
    run.add_argument("--sessdata", default=None)
    run.add_argument("--only-editorial", action="store_true", help="Execute only proofreading/rendering jobs")
    worker_selection = run.add_mutually_exclusive_group()
    worker_selection.add_argument("--kind", choices=[kind.value for kind in PROCESSING_KINDS], action="append",
                                  help="Claim only this job kind; repeat to select more")
    worker_selection.add_argument("--role", choices=tuple(WORKER_ROLES),
                                  help="Select the fixed ASR/editorial/CPU handler set")
    run.add_argument("--drain-file", default=None, help="Stop claiming when this file exists")
    run.add_argument("--drain-timeout", type=float, default=60,
                     help="Grace period after drain (default: 60s); repeated signal interrupts the current task")
    run.add_argument("--gpu-session", choices=("persistent", "oneshot"), default="persistent",
                     help="Reuse one killable model session or restart for each GPU task")
    run.add_argument("--asr-prefetch", action="store_true",
                     help="Experimental depth-one CPU preparation using a separate processor")
    run.add_argument("--asr-prefetch-bytes", type=int, default=64 * 1024 * 1024,
                     help="Conservative reservation budget for the prepared next chunk")
    run.add_argument("--runtime-bindings", default=None,
                     help="Verified checkpoint relocation file; frozen profiles remain unchanged")
    run.add_argument("--storage-target", action="append", help="Restore retained inputs before claim from TARGET_ID=DIRECTORY or TARGET_ID=S3_BINDING_JSON")
    run.add_argument("--warmup-audio", default=None,
                     help="Explicit absolute sentinel audio; decode and align before claiming GPU work")
    run.add_argument("--warmup-timeout", type=float, default=300,
                     help="Independent deadline for model preparation before a business attempt")
    run.add_argument("--cache-root", default=None, help="Explicit persistent cache root; does not enable compilation")
    run.add_argument("--cache-max-bytes", type=int, default=10 * 1024**3,
                     help="Cache startup admission limit; use a filesystem quota for runtime writes")
    run.add_argument("--poll-interval", type=float, default=0,
                     help="Keep this worker alive and poll idle queues; zero runs until idle once")

    supervise = actions.add_parser("supervise", help="Own fixed worker slots, restart with backoff and drain on exit")
    supervise.set_defaults(database_policy=None)
    supervise.add_argument("--archive-root", default=archive_root)
    supervise.add_argument("--artifact-root", default=None)
    supervise.add_argument("--asr-slots", type=int, default=1)
    supervise.add_argument("--cpu-slots", type=int, default=1)
    supervise.add_argument("--editorial-slots", type=int, default=1)
    supervise.add_argument("--poll-interval", type=float, default=5)
    supervise.add_argument("--drain-file", default=None)
    supervise.add_argument("--drain-timeout", type=float, default=60)
    supervise.add_argument("--max-restarts", type=int, default=8)
    supervise.add_argument("--gpu-session", choices=("persistent", "oneshot"), default="persistent")
    supervise.add_argument("--asr-prefetch", action="store_true")
    supervise.add_argument("--asr-prefetch-bytes", type=int, default=64 * 1024 * 1024)
    supervise.add_argument("--runtime-bindings", default=None)
    supervise.add_argument("--storage-target", action="append", help="Pass TARGET_ID=DIRECTORY or TARGET_ID=S3_BINDING_JSON to each worker")
    supervise.add_argument("--warmup-audio", default=None)
    supervise.add_argument("--warmup-timeout", type=float, default=300)
    supervise.add_argument("--cache-root", default=None)
    supervise.add_argument("--cache-max-bytes", type=int, default=10 * 1024**3)
    run.add_argument("--artifact-root", default=None, help="Write products here; falls back to BILI_ARTIFACT_ROOT")

    status = actions.add_parser("status", help="Print workflow job counts from SQLite")
    status.set_defaults(database_policy=ArchiveAccessMode.READ)
    status.add_argument("--archive-root", default=archive_root)
    status.add_argument("--jobs", action="store_true", help="List job IDs, stored part identity, and cancellation blockers")
    status.add_argument("--json", action="store_true", help="Print the same status report as JSON")
    status.add_argument("--failed", action="store_true", help="Include a page of current failed jobs with safe diagnostics")
    status.add_argument("--since", type=int, default=None, help="Inclusive failed-attempt finished_at UTC epoch second")
    status.add_argument("--until", type=int, default=None, help="Exclusive failed-attempt finished_at UTC epoch second")
    status.add_argument("--limit", type=int, default=100, help="Job page size, 1-1000 (default: 100)")
    status.add_argument("--offset", type=int, default=0)
    _add_control_arguments(status)

    cancel = actions.add_parser("cancel", help="Cancel selected queued/running jobs at safe commit boundaries")
    cancel.add_argument("--archive-root", default=archive_root)
    cancel.add_argument("--job-id", action="append", required=True, help="Exact workflow job ID; repeat to select more")

    publish = actions.add_parser("publish", help="Rebuild archive bundles from the preferred stored transcript")
    publish.add_argument("--archive-root", default=archive_root)
    publish.add_argument("--part-id", type=int, action="append", required=True)
    publish.add_argument("--artifact-root", default=None)
    publish.add_argument("--preserve-transcript", action="store_true",
                         help="Refresh metadata on the latest published transcript; preserve its old bundle via the artifact catalog")
    publish.add_argument("--dry-run", action="store_true", help="Inspect refresh targets without enqueueing jobs")
    status.add_argument("--details", action="store_true", help="Explain each job and its unsatisfied dependencies")
    explain = actions.add_parser("explain", help="Inspect a job, its dependency blockers and latest attempt")
    explain.set_defaults(database_policy=ArchiveAccessMode.READ)
    explain.add_argument("--archive-root", default=archive_root)
    explain.add_argument("--job-id", required=True)
    _add_control_arguments(explain)

    evidence = actions.add_parser("asr-evidence", help="Print persisted ASR diagnostics for one acquisition run")
    evidence.set_defaults(database_policy=ArchiveAccessMode.READ)
    evidence.add_argument("--archive-root", default=archive_root)
    evidence.add_argument("--run-id", required=True)
    evidence.add_argument("--part-id", type=int, required=True)

    quality = actions.add_parser("asr-quality", help="Read ASR admission without changing historical evidence")
    quality.set_defaults(database_policy=ArchiveAccessMode.READ)
    quality.add_argument("--archive-root", default=archive_root)
    identity = quality.add_mutually_exclusive_group(required=True)
    identity.add_argument("--transcript-id", type=int)
    identity.add_argument("--run-id")
    quality.add_argument("--part-id", type=int)
    quality.add_argument("--include-evidence", action="store_true")

    repair_asr = actions.add_parser("repair-asr", help="Explicitly replace unusable or unknown ASR without rewriting history")
    repair_asr.add_argument("--archive-root", default=archive_root)
    repair_origin = repair_asr.add_mutually_exclusive_group(required=True)
    repair_origin.add_argument("--job-id")
    repair_origin.add_argument("--transcript-id", type=int, help="Re-evaluate historical ASR by fresh inference")
    repair_asr.add_argument("--profile-config", required=True, help="JSON AsrProfile fields including profile_key")
    repair_asr.add_argument("--reason", required=True, help="Inspected cause and original audio/alignment repair")

    performance = actions.add_parser("asr-performance", help="Read ASR throughput and retry costs in a fixed UTC window")
    performance.set_defaults(database_policy=ArchiveAccessMode.READ)
    performance.add_argument("--archive-root", default=archive_root)
    performance.add_argument("--start", type=int, required=True, help="Inclusive UTC epoch second")
    performance.add_argument("--end", type=int, required=True, help="Exclusive UTC epoch second")
    performance.add_argument("--max-attempts", type=int, default=10_000)

    retry = actions.add_parser("retry", help="Requeue failed jobs while keeping attempt evidence")
    retry.add_argument("--archive-root", default=archive_root)
    retry.add_argument("--part-id", type=int, action="append", default=None)
    retry.add_argument("--job-id", action="append", default=None)
    retry.add_argument("--kind", choices=[kind.value for kind in PROCESSING_KINDS], action="append", default=None)
    retry_mode = retry.add_mutually_exclusive_group()
    retry_mode.add_argument("--automatic", action="store_true", help="Apply bounded automatic policy, budget, backoff and controls")
    retry_mode.add_argument("--manual", action="store_true", help="Explicit manual retry using frozen inputs (default)")
    retry.add_argument("--manual-override", action="store_true", help="Override repair/unknown policy for exactly one job; holds still apply")
    retry.add_argument("--override-reason", default=None, help="Required audit reason for --manual-override")
    retry.add_argument("--dry-run", action=_ReadOnlyDryRun, nargs=0, default=False,
                       help="Inspect decisions using read-only database access")
    retry.add_argument("--json", action="store_true", help="Print decisions and requeue result as JSON")
    _add_control_arguments(retry)

    repair = actions.add_parser("repair-dependencies", help="Plan or apply a fenced repair of legacy producer edges")
    repair.set_defaults(database_policy=ArchiveAccessMode.READ)
    repair.add_argument("--archive-root", default=archive_root)
    repair.add_argument("--part-id", type=int, action="append", required=True)
    repair.add_argument("--apply", action="store_true", help="Apply the exact inspected plan")
    repair.add_argument("--expected-plan-id", default=None, help="Required inspected plan identity for --apply")
    repair.add_argument("--retry-failed", action="store_true", help="Requeue failed audio and ASR producers, preserving attempts")


class _ReadOnlyDryRun(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, True)
        namespace.database_policy = ArchiveAccessMode.READ


class _RecoveryApply(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, True)
        namespace.database_policy = ArchiveAccessMode.WRITE


def _add_control_arguments(parser: argparse.ArgumentParser) -> None:
    control = parser.add_mutually_exclusive_group()
    control.add_argument("--control-state", default=None, help="Explicit external holds/retry-ledger JSON snapshot")
    control.add_argument("--no-control-state", action="store_true",
                         help="Explicitly declare this deployment has no external retry controls")


def _nonnegative_page_index(value: str) -> int:
    try:
        index = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("page index must be an integer") from exc
    if index < 0:
        raise argparse.ArgumentTypeError("page index must be non-negative (0 is P1)")
    return index


def _add_editorial_arguments(parser: argparse.ArgumentParser) -> None:
    defaults = EditorialConfig()
    parser.add_argument("--editorial-model", default=defaults.model)
    parser.add_argument("--editorial-base-url", default=defaults.base_url,
                        help="OpenAI-compatible editorial endpoint (DeepSeek or OpenCode Go)")
    parser.add_argument("--context-tokens", type=int, default=defaults.context_tokens)
    parser.add_argument("--max-input-tokens", type=int, default=defaults.max_input_tokens)
    parser.add_argument("--max-output-tokens", type=int, default=defaults.max_output_tokens)
    parser.add_argument("--context-segments", type=int, default=defaults.context_segments)
    parser.add_argument("--api-timeout", type=int, default=defaults.timeout_seconds)
    parser.add_argument("--thinking-mode", choices=("enabled", "disabled"), default=defaults.thinking_mode)
    parser.add_argument("--reasoning-effort", choices=("low", "high", "max"), default=defaults.reasoning_effort)
    parser.add_argument("--top-p", type=float, default=defaults.top_p)
    parser.add_argument("--temperature", type=float, default=None,
                        help="Optional sampling temperature; requires --thinking-mode disabled")
    parser.add_argument("--max-chunk-regenerations", type=int, default=defaults.max_chunk_regenerations,
                        help="Persisted extra physical call budget per chunk (0..4); only allowlisted reference failures regenerate")
    parser.add_argument("--chunk-preset", choices=("baseline-v1", "large-v1"), default=defaults.chunk_preset)
    parser.add_argument("--max-chunk-segments", type=int, default=None)
    parser.add_argument("--max-chunk-chars", type=int, default=None)


def _editorial_config(args: argparse.Namespace) -> EditorialConfig:
    guards = EditorialConfig.preset(args.chunk_preset)
    return EditorialConfig(model=args.editorial_model, base_url=args.editorial_base_url,
                           context_tokens=args.context_tokens,
                           max_input_tokens=args.max_input_tokens, max_output_tokens=args.max_output_tokens,
                           context_segments=args.context_segments, timeout_seconds=args.api_timeout,
                           thinking_mode=args.thinking_mode,
                           reasoning_effort=args.reasoning_effort, top_p=args.top_p,
                           temperature=args.temperature, max_chunk_regenerations=args.max_chunk_regenerations,
                           chunk_preset=args.chunk_preset,
                           max_chunk_segments=args.max_chunk_segments if args.max_chunk_segments is not None else guards.max_chunk_segments,
                           max_chunk_chars=args.max_chunk_chars if args.max_chunk_chars is not None else guards.max_chunk_chars)


def _cmd_workflow(args: argparse.Namespace) -> int:
    from bili_asr.storage.database import SchemaContractError

    try:
        return _execute_workflow(args)
    except (SchemaContractError, ValueError, OSError) as exc:
        write_stderr(f"workflow {args.workflow_action}: {exc}")
        return 1


def _execute_workflow(args: argparse.Namespace) -> int:
    from bili_asr.archive_session import ArchiveSession
    from bili_asr.asr.runner import ASRInferenceTimeoutError
    from bili_asr.asr.session import InferenceSessionError
    from bili_asr.services.workflow_application import WorkflowApplication

    if args.workflow_action == "supervise":
        from bili_asr.workflow_supervisor import WorkerSupervisor
        supervisor = WorkerSupervisor(archive_root=args.archive_root, artifact_root=args.artifact_root,
            slots={"asr": args.asr_slots, "cpu": args.cpu_slots, "editorial": args.editorial_slots},
            poll_interval=args.poll_interval, drain_file=args.drain_file, drain_timeout=args.drain_timeout,
            max_restarts=args.max_restarts, gpu_session=args.gpu_session,
            asr_prefetch=args.asr_prefetch, asr_prefetch_bytes=args.asr_prefetch_bytes,
            runtime_bindings=args.runtime_bindings, storage_targets=args.storage_target, warmup_audio=args.warmup_audio,
            warmup_timeout_seconds=args.warmup_timeout, cache_root=args.cache_root,
            cache_max_bytes=args.cache_max_bytes)
        supervisor.run()
        return 0
    artifact_roots = None
    if args.workflow_action in {"run", "render", "publish"}:
        artifact_roots = roots_for(args.archive_root, flag_value=args.artifact_root,
                                   require_writable=args.workflow_action == "run")
    readonly = args.workflow_action in {"status", "explain", "asr-evidence", "asr-quality", "asr-performance"} or (
        args.workflow_action == "batch" and (args.batch_action == "status" or getattr(args, "dry_run", False))) or (
        args.workflow_action in {"repair-dependencies", "recover-editorial"} and not args.apply) or (
        args.workflow_action in {"publish", "retry"} and args.dry_run)
    mode = ArchiveAccessMode.READ if readonly else ArchiveAccessMode.WRITE
    session = ArchiveSession(args.archive_root, mode=mode, artifact_roots=artifact_roots).open()
    connection = session.connection
    try:
        application = WorkflowApplication(session)
        repository = application.repository
        if args.workflow_action != "status":
            repository.require_cancellation_contract()
        if args.workflow_action == "asr-performance":
            from bili_asr.services.asr_performance import asr_performance_report
            report = asr_performance_report(connection, start=args.start, end=args.end,
                                            max_attempts=args.max_attempts)
            print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        if args.workflow_action == "repair-dependencies":
            if args.apply and args.expected_plan_id is None:
                raise ValueError("--apply requires --expected-plan-id from the inspected plan")
            from bili_asr.storage.workflow_dependency_repair import (
                repair_producer_dependencies,
            )
            report = repair_producer_dependencies(connection, args.part_id, apply=args.apply,
                expected_plan_id=args.expected_plan_id, retry_failed=args.retry_failed)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
            return 0
        if args.workflow_action == "asr-evidence":
            from bili_asr.storage import TranscriptRepository

            evidence = TranscriptRepository(connection).read_asr_evidence(args.run_id, args.part_id)
            if evidence is None:
                write_stderr("workflow asr-evidence: no evidence for this run and part")
                return 1
            print(json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        if args.workflow_action == "asr-quality":
            from bili_asr.storage.asr_quality import assess_transcript, read_assessment
            if args.transcript_id is not None:
                if args.include_evidence:
                    raise ValueError("--include-evidence requires an exact --run-id and --part-id")
                report = assess_transcript(connection, args.transcript_id)
            else:
                if args.part_id is None:
                    raise ValueError("--run-id requires --part-id")
                report = read_assessment(connection, run_id=args.run_id, video_part_id=args.part_id,
                                         include_evidence=args.include_evidence)
                if report is None:
                    raise ValueError("no admission evidence for this run and part")
            print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        if args.workflow_action == "repair-asr":
            from pathlib import Path

            from bili_asr.services.asr_repair import repair_asr
            values = json.loads(Path(args.profile_config).read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                raise ValueError("ASR repair profile must be a JSON object")
            if any(not isinstance(values.get(name), str) or not values[name].strip()
                   for name in ("profile_key", "model_name")):
                raise ValueError("ASR repair profile_key and model_name must be nonempty strings")
            if "hotwords" in values:
                if not isinstance(values["hotwords"], list):
                    raise ValueError("ASR repair hotwords must be a JSON array")
                values["hotwords"] = tuple(values["hotwords"])
            try:
                profile = AsrProfile(**values)
            except TypeError:
                raise ValueError("ASR repair profile has missing or unsupported fields") from None
            report = repair_asr(repository, job_id=args.job_id, transcript_id=args.transcript_id,
                                profile=profile, reason=args.reason)
            print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        if args.workflow_action == "batch":
            from bili_asr.storage.workflow_batches import BatchRepository
            batches = BatchRepository(connection)
            if args.batch_action == "create":
                report = batches.create(args.job_id, desired_workers=args.workers, actor=args.actor, reason=args.reason)
            elif args.batch_action == "extract":
                report = batches.extract(args.batch_id, args.job_id, desired_workers=args.workers,
                                         actor=args.actor, reason=args.reason)
            elif args.batch_action == "status":
                report = batches.status(args.batch_id)
            else:
                report = batches.set_state(args.batch_id, paused=args.batch_action == "pause",
                    actor=args.actor, reason=args.reason, desired_workers=args.workers, dry_run=args.dry_run)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0
        if args.workflow_action == "recover-editorial":
            from bili_asr.services.editorial_chain_recovery import recover_editorial_chain
            from bili_asr.workflow_errors import JobExecutionError
            try:
                report = recover_editorial_chain(repository, proofread_job_id=args.proofread_job_id,
                    replacement_asr_job_id=args.replacement_asr_job_id, apply=args.apply,
                    expected_plan_id=args.expected_plan_id, control_state_path=args.control_state,
                    no_control_state=args.no_control_state)
            except JobExecutionError as exc:
                write_stderr(json.dumps({"error_code": exc.error_code, "diagnostic": exc.safe_details}))
                return 1
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
            return 0
        if args.workflow_action == "repair-proofread":
            from bili_asr.services.editorial_repair import repair_proofread
            from bili_asr.workflow_errors import JobExecutionError
            try:
                report = repair_proofread(repository, job_id=args.job_id, config=_editorial_config(args),
                                         base_transcript_id=args.base_transcript_id)
            except JobExecutionError as exc:
                write_stderr(json.dumps({"error_code": exc.error_code, "diagnostic": exc.safe_details}))
                return 1
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0
        if args.workflow_action == "cancel":
            results = repository.cancel(job_ids=args.job_id)
            print(f"workflow cancel: changed={sum(item.changed for item in results)} "
                  f"noop={sum(not item.changed for item in results)}")
            for item in results:
                print(f"  job_id={item.job_id} previous={item.previous_status} "
                      f"status={item.status} changed={int(item.changed)}")
            return 0
        if args.workflow_action == "publish":
            for item in application.publish(args.part_id, preserve_transcript=args.preserve_transcript,
                                            dry_run=args.dry_run):
                print("workflow publish: " + " ".join(f"{name}={value}" for name, value in item.items()))
            return 0
        if args.workflow_action == "explain":
            from bili_asr.services.workflow_status import explain_workflow_job
            try:
                print(json.dumps(explain_workflow_job(connection, args.job_id,
                    control_state_path=args.control_state, no_control_state=args.no_control_state),
                    ensure_ascii=False, sort_keys=True, allow_nan=False))
            except ValueError as exc:
                write_stderr(f"workflow explain: {exc}")
                return 1
            return 0
        if args.workflow_action == "proofread-many":
            results = application.proofread_many(args.part_id, config=_editorial_config(args),
                no_reference=args.no_reference, refresh_input=args.refresh_input)
            print(json.dumps({"queued": len(results), "items": results}, ensure_ascii=False))
            return 0
        if args.workflow_action == "proofread":
            item = application.proofread(part_id=args.part_id, base_transcript_id=args.base_transcript_id,
                reference_transcript_id=args.reference_transcript_id, no_reference=args.no_reference,
                config=_editorial_config(args), refresh_input=args.refresh_input)
            print("workflow proofread: " + " ".join(f"{name}={value}" for name, value in item.items()))
            return 0
        if args.workflow_action == "render":
            job_id = application.render(args.revision_id, args.template_version)
            print(f"workflow render: job_id={job_id}")
            return 0
        if args.workflow_action == "status":
            from bili_asr.services.workflow_status import (
                render_status_text,
                workflow_status_report,
            )
            report = workflow_status_report(connection, since=args.since, until=args.until,
                limit=args.limit, offset=args.offset, failed_only=args.failed,
                include_jobs=args.jobs or args.details, control_state_path=args.control_state,
                no_control_state=args.no_control_state)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
            else:
                print(render_status_text(report, jobs=args.jobs, details=args.details or args.failed))
            return 0
        if args.workflow_action == "retry":
            from bili_asr.services.workflow_retry import guarded_retry_failed
            if args.manual_override and (args.automatic or len(args.job_id or []) != 1
                    or args.part_id is not None or args.kind is not None):
                raise ValueError('--manual-override requires exactly one --job-id and no other selector or --automatic')
            if args.override_reason is not None and not args.manual_override:
                raise ValueError('--override-reason requires --manual-override')
            report = guarded_retry_failed(repository, part_ids=args.part_id, job_ids=args.job_id,
                kinds=None if args.kind is None else [JobKind(k) for k in args.kind],
                control_state_path=args.control_state, no_control_state=args.no_control_state,
                automatic=args.automatic, manual_override=args.manual_override,
                override_reason=args.override_reason, dry_run=args.dry_run)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
            else:
                print(f"workflow retry: requeued={report['requeued']} dry_run={int(args.dry_run)}")
                for decision in report['decisions']:
                    print('  retry_decision: ' + json.dumps(decision, ensure_ascii=False, sort_keys=True))
            return 0
        if args.workflow_action == "plan":
            try:
                policy = AsrPolicy(args.asr_policy)
                editorial_config = _editorial_config(args).to_dict() if args.proofread else None
                names = {
                    "model": "model_name", "aligner": "aligner_name",
                    "inference_timeout": "inference_timeout_seconds",
                    "second_pass_cache": "second_pass_use_cache",
                    "hotword": "hotwords",
                }
                overrides = {}
                for name in ("model", "model_revision", "aligner", "aligner_revision",
                             "device", "language", "chunk_seconds", "inference_timeout",
                             "inference_timeout_policy", "inference_max_rtf", "inference_startup_seconds",
                             "inference_hard_cap_seconds", "inference_no_progress_seconds",
                             "hotword", "offline", "model_id", "tokens_per_second",
                             "min_new_tokens", "second_pass_cache", "model_dtype", "aligner_dtype",
                             "alignment_decoder", "coverage_policy",
                             "asr_batch_size", "aligner_batch_size", "batch_max_audio_seconds",
                             "batch_max_input_bytes", "batch_max_tokens", "asr_attention", "aligner_attention",
                             "asr_cache_implementation", "asr_compile", "aligner_compile", "compile_max_buckets",
                             "compile_max_input_tokens", "compile_max_output_tokens"):
                    value = getattr(args, name)
                    if value is not None:
                        overrides[names.get(name, name)] = tuple(value) if name == "hotword" else value
                values = asdict(default_config(**overrides))
                values.pop("local_source")
                values["model_revision"] = values["model_revision"] or ""
                profile = AsrProfile(profile_key=args.profile_key, **values)
                planned = application.plan(part_ids=args.part_id, bvids=args.bvid, page_index=args.page_index,
                    all_parts=args.all_parts,
                    policy=policy, profile=profile,
                    editorial_config=editorial_config)
                profile_id, plan, selection = planned.profile_id, planned.jobs, planned.selection
            except ValueError as exc:
                write_stderr(f"workflow plan: {exc}")
                return 1
            print(
                f"workflow plan: subtitle={plan.subtitle_jobs} audio={plan.audio_jobs} "
                f"asr={plan.asr_jobs} profile_id={profile_id}"
                f" proofread={plan.proofread_jobs} documents={plan.document_jobs}"
            )
            for target in selection.targets:
                print(f"  target: bvid={target.bvid} page_index={target.page_index} "
                      f"video_part_id={target.video_part_id} work_id={target.work_id}")
            for target in selection.excluded_gone:
                print(f"  excluded gone: bvid={target.bvid} page_index={target.page_index} "
                      f"video_part_id={target.video_part_id} work_id={target.work_id}")
            return 0
        sessdata = resolve_sessdata(args.sessdata, os.environ.get(SESSDATA_ENV_VAR))
        config_resolver = None
        if args.runtime_bindings is not None:
            from bili_asr.runtime_bindings import load_runtime_bindings
            config_resolver = load_runtime_bindings(args.runtime_bindings).resolve
        from bili_asr.services.artifact_access import parse_target_bindings
        summary = application.run(worker_id=args.worker_id, sessdata=sessdata,
            only_editorial=args.only_editorial, limit=args.limit,
            kinds=None if args.kind is None else tuple(JobKind(kind) for kind in args.kind), role=args.role,
            drain_file=args.drain_file, drain_timeout_seconds=args.drain_timeout,
            gpu_session=args.gpu_session, config_resolver=config_resolver,
              asr_prefetch=args.asr_prefetch, asr_prefetch_bytes=args.asr_prefetch_bytes,
              warmup_audio=args.warmup_audio, warmup_timeout_seconds=args.warmup_timeout,
              cache_root=args.cache_root, cache_max_bytes=args.cache_max_bytes,
              poll_interval_seconds=args.poll_interval, storage_targets=parse_target_bindings(args.storage_target))
        print(f"workflow run: succeeded={summary.succeeded} failed={summary.failed} "
              f"cancelled={summary.cancelled} idle={int(summary.idle)} blocked={summary.blocked}")
        return 1 if summary.failed else 0
    except (ValueError, InferenceSessionError, ASRInferenceTimeoutError) as exc:
        write_stderr(f"workflow {args.workflow_action}: {exc}")
        return 1
    finally:
        session.close()
