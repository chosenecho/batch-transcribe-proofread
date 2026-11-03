"""Undo journal-owned isolation after a controlled audio maintenance failure."""
from __future__ import annotations

import json
from pathlib import Path

from bili_asr.services.artifact_coordination import object_fence
from bili_asr.services.artifact_inventory_service import validate_offload_plan
from bili_asr.services.artifact_release import release_copy
from bili_asr.services.artifact_transfer import _local_target, _quarantine_key
from bili_asr.storage.artifact_releases import ArtifactReleaseRepository


def restore_isolated_audio(connection, roots, plan, *, compression_level):
    """Restore only exact, frozen journal names; never scan or delete orphans."""
    validate_offload_plan(plan)
    operation_id = f"offload-{plan['plan_sha256']}-v2-deflate{compression_level}"
    items = {item["copy_id"]: item for item in plan["items"]}
    restored = 0
    for intent in connection.execute("SELECT * FROM artifact_release_intents WHERE transfer_id=? AND state='isolated'",
                                     (operation_id,)).fetchall():
        item = items.get(intent["copy_id"])
        if (item is None or intent["object_id"] != item["sha256"] or intent["source_key"] != item["path"]
                or intent["quarantine_key"] != _quarantine_key(item["path"], operation_id, item["copy_id"])
                or _local_target(roots, Path(item["root"])) != intent["source_target_id"]):
            raise ValueError("audio recovery journal differs from frozen plan")
        with object_fence(roots, intent["object_id"], exclusive=True):
            outcome = release_copy(Path(item["root"]), intent["source_key"], intent["quarantine_key"],
                intent["object_id"], json.loads(intent["source_generation_json"]), allow_delete=False)
            if outcome["state"] == "cancelled":
                with connection:
                    ArtifactReleaseRepository(connection).record_outcome(operation_id, intent["copy_id"], "cancelled")
                restored += 1
    return restored
