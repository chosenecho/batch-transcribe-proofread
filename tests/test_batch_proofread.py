from types import SimpleNamespace

import pytest

from bili_asr.editorial import EditorialConfig
from bili_asr.services.workflow_application import WorkflowApplication
from tests.test_ai_editorial import database, insert_record, record


def test_batch_proofread_is_atomic_and_idempotent(database):
    with database:
        database.execute("INSERT INTO video_parts(video_part_id,bvid,page_index,cid,title,duration_ms,"
                         "processing_status,created_at,updated_at) "
                         "VALUES (2,'BVtest',1,2,'part 2',1000,'metadata_collected',1,1)")
    insert_record(database, record(transcript_id=2, part=2))
    application = WorkflowApplication(SimpleNamespace(connection=database))
    first = application.proofread_many([1, 2, 1], config=EditorialConfig())
    assert [item["part_id"] for item in first] == [1, 2]
    assert application.proofread_many([1, 2], config=EditorialConfig()) == first
    assert database.execute("SELECT COUNT(*) FROM workflow_jobs").fetchone()[0] == 4


def test_invalid_batch_rolls_back_prepared_inputs_and_jobs(database):
    application = WorkflowApplication(SimpleNamespace(connection=database))
    with pytest.raises(ValueError, match="no stored ASR"):
        application.proofread_many([1, 999], config=EditorialConfig())
    assert database.execute("SELECT COUNT(*) FROM workflow_jobs").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM editorial_inputs").fetchone()[0] == 0
