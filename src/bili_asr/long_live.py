"""Opt-in long-live campaign helpers for bounded `schedule` runs.

Composes the existing audio budget and duration-policy seams. Pilot's
default 45-minute short-video selection is unchanged; long-duration rows
are processed only when the operator passes ``--allow-long-live`` and a
configured (non-zero) ``--max-audio-gb``.
"""

from __future__ import annotations

from typing import Any, Mapping

from .audio_budget import (
    audio_dir_usage_bytes,
    estimate_audio_bytes,
    max_duration_exceeded,
    parse_duration_s,
    would_exceed_budget,
)

DEFAULT_SHORT_MAX_DURATION_MIN = 45


def is_long_live(
    entry: Mapping[str, Any],
    max_duration_min: int = DEFAULT_SHORT_MAX_DURATION_MIN,
) -> bool:
    """True when the row exceeds the default short-video duration cap.

    Missing, zero, or unparseable duration fail-closes as long-live.
    """
    return max_duration_exceeded(entry, max_duration_min)


def refuse_disabled_audio_cap(max_audio_gb: float) -> str | None:
    """Long-live campaigns must keep the configured audio cap enabled."""
    if max_audio_gb <= 0:
        return "--allow-long-live cannot disable the audio cap (--max-audio-gb 0)"
    return None


def apply_long_live_policy(
    rows: list[tuple[str, Mapping[str, Any]]],
    *,
    allow_long_live: bool,
    explicit_scope: bool,
) -> tuple[list[tuple[str, Mapping[str, Any]]], int, str | None]:
    """Filter or refuse long-duration rows unless the operator opted in.

    Returns ``(rows, held_count, error)``. ``pending`` / ``failed`` hold
    long rows out of the batch; an explicit work_id selector of a long
    row is a usage error without ``--allow-long-live``.
    """
    long_rows = [(key, entry) for key, entry in rows if is_long_live(entry)]
    if allow_long_live:
        return list(rows), 0, None
    if explicit_scope and long_rows:
        labels = ", ".join(str(entry.get("work_id") or key) for key, entry in long_rows)
        return list(rows), len(long_rows), (
            f"long-duration row requires --allow-long-live: {labels}"
        )
    filtered = [(key, entry) for key, entry in rows if not is_long_live(entry)]
    return filtered, len(long_rows), None


def campaign_plan(
    archive_root: str,
    entry: Mapping[str, Any],
    max_audio_bytes: int,
    *,
    usage_bytes: int | None = None,
) -> dict[str, Any]:
    """Conservative pre-download estimate against current ``audio/`` usage."""
    parsed = parse_duration_s(entry.get("duration_s"))
    estimated = estimate_audio_bytes(entry.get("duration_s"))
    usage = audio_dir_usage_bytes(archive_root) if usage_bytes is None else usage_bytes
    would_exceed = would_exceed_budget(
        archive_root, entry, max_audio_bytes, usage_bytes=usage,
    )
    if parsed is None and max_audio_bytes > 0:
        # Fail closed in the printed plan: unknown duration is not a 0-byte clip.
        estimated = max_audio_bytes + 1
    return {
        "work_id": str(entry.get("work_id") or ""),
        "duration_s": parsed if parsed is not None else "unknown",
        "estimated_bytes": estimated,
        "audio_usage_bytes": usage,
        "projected_peak_bytes": usage + estimated,
        "cap_bytes": max_audio_bytes,
        "would_exceed": would_exceed,
    }


def format_campaign_plan(plan: Mapping[str, Any]) -> str:
    would = "true" if plan.get("would_exceed") else "false"
    return (
        f"schedule: long-live work_id={plan.get('work_id')} "
        f"duration_s={plan.get('duration_s')} "
        f"estimated_bytes={plan.get('estimated_bytes')} "
        f"audio_usage_bytes={plan.get('audio_usage_bytes')} "
        f"projected_peak_bytes={plan.get('projected_peak_bytes')} "
        f"cap_bytes={plan.get('cap_bytes')} "
        f"would_exceed={would}"
    )


__all__ = [
    "DEFAULT_SHORT_MAX_DURATION_MIN",
    "apply_long_live_policy",
    "campaign_plan",
    "format_campaign_plan",
    "is_long_live",
    "refuse_disabled_audio_cap",
]
