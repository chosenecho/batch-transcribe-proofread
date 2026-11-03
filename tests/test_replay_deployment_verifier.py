from contextlib import closing

from bili_asr.storage.database import open_database
from bili_asr.storage.editorial_replay import install_indexes
from scripts.verify_replay_deployment import verify


def test_verifier_reports_actual_expression_plans_without_installing_or_claiming_worker_version(tmp_path):
    with closing(open_database(tmp_path)) as connection:
        before = (tmp_path / "archive.db").read_bytes()
        report = verify(tmp_path / "archive.db")
        assert not report["indexes_complete"]
        assert not report["ready_for_worker_acceptance"]
        assert (tmp_path / "archive.db").read_bytes() == before
        install_indexes(connection)
        before = (tmp_path / "archive.db").read_bytes()
        report = verify(tmp_path / "archive.db")
        assert report["ready_for_worker_acceptance"]
        assert not report["worker_loaded_version_verified"]
        assert report["paid_requests_sent"] == 0
        assert (tmp_path / "archive.db").read_bytes() == before
