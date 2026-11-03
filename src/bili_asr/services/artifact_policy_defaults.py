"""Shared artifact policy defaults independent of maintenance execution."""
from dataclasses import asdict

from bili_asr.services.artifact_inventory_service import ArtifactSelection

DEFAULT_POLICY = {
    "version": 1, "mode": "off", "target_id": None,
    "selection": asdict(ArtifactSelection()), "minimum_age_seconds": 86400,
    "trigger_free_bytes": 10 * 1024**3, "stop_free_bytes": 20 * 1024**3,
    "minimum_free_bytes": 1024**3, "batch_bytes": 1024**3, "batch_objects": 100,
    "max_concurrency": 1, "bytes_per_second": 16 * 1024**2,
    "scan_interval_seconds": 60, "backoff_seconds": 300,
    "priority": "oldest", "download_bytes_per_second": 256000,
    "download_workspace_multiplier": 3,
}
