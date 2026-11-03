"""Authoritative workflow job state transitions."""

from __future__ import annotations

from typing import Final


JOB_STATES: Final = frozenset({"queued", "running", "succeeded", "failed", "cancelled"})

ALLOWED_JOB_TRANSITIONS: Final = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"queued", "succeeded", "failed", "cancelled"}),
    "succeeded": frozenset({"queued"}),
    "failed": frozenset({"queued"}),
    "cancelled": frozenset(),
}


class InvalidJobTransition(ValueError):
    """Raised when a workflow job attempts an illegal state change."""


def validate_job_transition(current: str, target: str) -> None:
    if current not in JOB_STATES:
        raise InvalidJobTransition(f"unknown workflow job state: {current!r}")
    if target not in JOB_STATES:
        raise InvalidJobTransition(f"unknown workflow job state: {target!r}")
    if current == target:
        return
    if target not in ALLOWED_JOB_TRANSITIONS[current]:
        raise InvalidJobTransition(f"invalid workflow job transition: {current} -> {target}")

