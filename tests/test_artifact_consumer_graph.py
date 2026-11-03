"""Final release checks include every dependent job and terminate on cycles."""
from __future__ import annotations

import sqlite3

from bili_asr.services.artifact_consumer_graph import (
    consumer_job_ids,
    consumer_part_rows,
    has_live_consumer,
    latest_completion,
)
from bili_asr.services.artifact_inventory_service import (
    ArtifactSelection,
    inventory_artifacts,
)
from tests.support.artifact_online import archive as online_archive
from tests.test_artifact_inventory_service import job


def test_partless_transitive_consumer_protects_its_original_audio(tmp_path):
    archive = online_archive.__wrapped__(tmp_path)
    with sqlite3.connect(archive.root / "archive.db") as connection:
        job(connection, "asr", kind="asr", status="succeeded")
        job(connection, "draft", kind="proofread", status="succeeded")
        job(connection, "repair", kind="proofread", status="queued")
        connection.execute("UPDATE workflow_jobs SET video_part_id=NULL WHERE job_id IN ('draft','repair')")
        connection.execute("INSERT INTO workflow_job_dependencies VALUES ('draft','asr')")
        connection.execute("INSERT INTO workflow_job_dependencies VALUES ('repair','draft')")
        connection.execute("UPDATE workflow_jobs SET updated_at=700 WHERE job_id='repair'")
        assert set(consumer_job_ids(connection, 1)) == {"asr", "draft", "repair"}
        assert has_live_consumer(connection, 1)
        assert latest_completion(connection, archive.digest) == 700
    inventory = inventory_artifacts(archive.root / "archive.db", archive.roots, deep=True,
        selection=ArtifactSelection(kinds=("audio",)), external_holds={})
    assert not any(copy["candidate"] for copy in inventory["copies"])
    assert any("consumer:repair:" in reason for obj in inventory["objects"] for reason in obj["retention_reasons"])


def test_consumer_projection_is_bounded_even_when_dependency_cycle_is_retained(tmp_path):
    archive = online_archive.__wrapped__(tmp_path)
    with sqlite3.connect(archive.root / "archive.db") as connection:
        job(connection, "left", kind="proofread", status="succeeded")
        job(connection, "right", kind="proofread", status="succeeded")
        connection.execute("UPDATE workflow_jobs SET video_part_id=NULL WHERE job_id='right'")
        connection.execute("INSERT INTO workflow_job_dependencies VALUES ('left','right')")
        connection.execute("INSERT INTO workflow_job_dependencies VALUES ('right','left')")
        assert len(list(consumer_part_rows(connection))) == 2
        assert set(consumer_job_ids(connection, 1)) == {"left", "right"}
        assert has_live_consumer(connection, 1) is False
