"""Validate durable job descriptions before a worker begins side effects.

Unversioned rows are the existing v1 contract. Validation does not rewrite
their JSON, dedupe keys or frozen profile/input hashes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from bili_asr.workflow_models import JobKind, WorkflowJob
from bili_asr.contracts.registry import WORKFLOW_PAYLOAD_VERSION


@dataclass(frozen=True)
class ValidatedPayload:
    kind: JobKind
    schema_version: int
    values: Mapping[str, Any]


def _positive_id(value: Any, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"workflow payload: {name} must be a positive integer")
    return value


def validate_payload(kind: JobKind, payload: Mapping[str, Any], *,
                     part_id: int | None, profile_id: int | None = None) -> ValidatedPayload:
    if not isinstance(payload, Mapping):
        raise ValueError("workflow payload must be a JSON object")
    version = payload.get("schema_version", WORKFLOW_PAYLOAD_VERSION)
    if type(version) is not int or version != WORKFLOW_PAYLOAD_VERSION:
        raise ValueError("unsupported workflow payload schema version")
    if kind in {JobKind.SUBTITLE, JobKind.AUDIO, JobKind.ASR, JobKind.PUBLISH}:
        if _positive_id(payload.get("video_part_id"), "video_part_id") != part_id:
            raise ValueError("workflow payload belongs to another video part")
    if kind is JobKind.ASR:
        if _positive_id(payload.get("profile_id"), "profile_id") != profile_id:
            raise ValueError("workflow payload belongs to another ASR profile")
        reference = payload.get("reference_transcript_id")
        if reference is not None:
            _positive_id(reference, "reference_transcript_id")
        if "repair_of_job_id" in payload:
            _text(payload["repair_of_job_id"], "repair_of_job_id")
        if "repair_of_transcript_id" in payload:
            _positive_id(payload["repair_of_transcript_id"], "repair_of_transcript_id")
    elif kind is JobKind.PUBLISH:
        _positive_id(payload.get("transcript_id"), "transcript_id")
        if "preserve_publication" in payload:
            expected = payload["preserve_publication"]
            if not isinstance(expected, Mapping) or set(expected) != {
                "publication_id", "published_at", "transcript_sha256", "segments_sha256"
            }:
                raise ValueError("workflow payload: invalid publication refresh fence")
            _positive_id(expected["publication_id"], "publication_id")
            if type(expected["published_at"]) is not int or expected["published_at"] < 0:
                raise ValueError("workflow payload: invalid publication timestamp")
            for name in ("transcript_sha256", "segments_sha256"):
                digest = expected[name]
                if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    raise ValueError("workflow payload: invalid transcript digest")
        if "publication_request_id" in payload:
            _text(payload["publication_request_id"], "publication_request_id")
    elif kind is JobKind.PROOFREAD:
        _one_reference(payload, "input_id", "asr_job_id")
        if "repair_of_job_id" in payload:
            _text(payload["repair_of_job_id"], "repair_of_job_id")
            if "input_id" not in payload:
                raise ValueError("workflow repair requires an explicit frozen input")
        if "asr_job_id" in payload and not isinstance(payload.get("editorial_config"), Mapping):
            raise ValueError("workflow payload: automatic proofreading requires editorial_config")
    elif kind is JobKind.RENDER_DOCUMENT:
        _one_reference(payload, "revision_id", "proofread_job_id")
        _text(payload.get("template_version"), "template_version")
        if payload["template_version"] == "auto" and "proofread_job_id" not in payload:
            raise ValueError("workflow payload: automatic template requires proofreading dependency")
    return ValidatedPayload(kind, version, MappingProxyType(dict(payload)))


def _text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"workflow payload: {name} must be non-empty text")


def _one_reference(payload: Mapping[str, Any], first: str, second: str) -> None:
    if (first in payload) == (second in payload):
        raise ValueError(f"workflow payload requires exactly one of {first} and {second}")
    name = first if first in payload else second
    _text(payload[name], name)


def decode_job_payload(job: WorkflowJob) -> ValidatedPayload:
    return validate_payload(job.kind, job.payload, part_id=job.video_part_id, profile_id=job.profile_id)
