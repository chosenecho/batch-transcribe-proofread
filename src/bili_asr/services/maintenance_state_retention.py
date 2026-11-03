"""Prune only verified state packages owned by one explicitly enabled policy."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from bili_asr.services import maintenance_package_core as package


def retain_state_packages(repository: Path, policy: dict, runs: list[dict], *, now: int) -> dict:
    from bili_asr.services.maintenance_task_state import verify_task_state

    points = []
    for run in runs:
        if run["status"] != "success":
            continue
        name = run["snapshot"]
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".zip"):
            raise ValueError("state backup receipt has an unsafe snapshot name")
        expected = (datetime.fromtimestamp(run["started_at"], UTC).strftime("%Y%m%dT%H%M%SZ")
                    + "-" + run["run_id"][:16] + ".zip")
        if name != expected or run["policy_id"] != policy["policy_id"]:
            raise ValueError("state backup receipt does not own its snapshot")
        path = repository / name
        if not path.exists():
            # A retained audit receipt outlives its safely pruned package.
            continue
        package.require_regular_file(path)
        verified = verify_task_state(path)
        job = verified["manifest"]["backup_job_state"]
        if (not isinstance(job, dict) or job.get("policy") != policy
                or job.get("current_run", {}).get("run_id") != run["run_id"]):
            raise ValueError("state retention refuses a foreign policy package")
        points.append((run["started_at"], name, package._record("snapshot", path)))
    points.sort(reverse=True)
    keep = {name for _, name, _ in points[:policy["keep_count"]]}
    cutoff = now - int(timedelta(days=policy["keep_days"]).total_seconds())
    keep.update(name for timestamp, name, _ in points if timestamp >= cutoff)
    deleted = []
    for _, name, record in points:
        if name in keep:
            continue
        path = repository / name
        if package._record("snapshot", path) != record:
            raise ValueError("state backup changed during retention")
        path.unlink()
        deleted.append(name)
    package._sync_directory(repository)
    return {"operation": "task-state-retention", "valid": True, "policy_id": policy["policy_id"],
            "retained": sorted(keep), "deleted": deleted, "business_rows_changed": False,
            "media_files_deleted": False, "audit_receipts_preserved": True}
