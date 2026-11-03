"""Freeze bounded connected artifact groups before maintenance execution."""
from __future__ import annotations

import time

from bili_asr.services.artifact_inventory_service import plan_artifact_copy, plan_artifact_offload


def bounded_plan(inventory, config, *, purpose="offload", preferred_objects=()):
    # Freeze complete connected groups; sharing one object joins their members.
    candidates = {copy["copy_id"]: copy for copy in inventory["copies"] if copy["candidate"]}
    membership = {}
    for obj in inventory["objects"]:
        groups = {ref["group_id"] for ref in obj["references"]}
        for copy_id in obj["copies"]:
            if copy_id in candidates:
                membership.setdefault(copy_id, set()).update(groups)
    components = []
    remaining = set(candidates)
    while remaining:
        current = {remaining.pop()}
        groups = set(membership.get(next(iter(current)), ()))
        while True:
            adjacent = {key for key in remaining if membership.get(key, set()) & groups}
            if not adjacent:
                break
            current.update(adjacent)
            remaining.difference_update(adjacent)
            for key in adjacent:
                groups.update(membership.get(key, ()))
        copies = [candidates[key] for key in current]
        if any(copy["fingerprint"][3] / 1e9 > time.time() - config["minimum_age_seconds"] for copy in copies):
            continue
        components.append(copies)
    preferred = set(preferred_objects)
    components.sort(key=lambda copies: (
        not any(copy["sha256"] in preferred for copy in copies),
        -sum(copy["size"] for copy in copies) if config["priority"] == "largest"
        else min(copy["fingerprint"][3] for copy in copies)))
    selected, total, objects = set(), 0, set()
    for copies in components:
        incoming = {copy["sha256"]: copy["size"] for copy in copies}
        added = sum(size for identity, size in incoming.items() if identity not in objects)
        if total + added > config["batch_bytes"] or len(objects | incoming.keys()) > config["batch_objects"]:
            continue
        selected.update(copy["copy_id"] for copy in copies)
        total += added
        objects.update(incoming)
    # Preserve the selected but guarded rows in the preview. Batch limits
    # exclude otherwise eligible rows; they must not hide release blockers.
    filtered = {**inventory, "copies": [{**copy, "selected": copy["copy_id"] in selected
        or (copy["selected"] and bool(copy["blockers"]))} for copy in inventory["copies"]]}
    physical = {copy["physical_id"]: copy["size"] for copy in candidates.values() if copy["copy_id"] in selected}
    filtered["summary"] = {**inventory["summary"], "reclaimable_content_bytes": sum(physical.values()),
        "target_payload_bytes": total, "restore_workspace_bytes": max((copy["size"] for copy in candidates.values() if copy["copy_id"] in selected), default=0)}
    planner = {"copy": plan_artifact_copy, "offload": plan_artifact_offload}[purpose]
    return planner(filtered, target_id=config["target_id"])
