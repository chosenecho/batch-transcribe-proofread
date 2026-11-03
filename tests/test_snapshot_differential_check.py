"""The established snapshot check entry point verifies differential chains."""
from bili_asr.services.archive_snapshot import check_snapshot, save_snapshot
from bili_asr.services.maintenance_chain import save_delta
from tests.fixtures.frozen_migration_archive import frozen_archive


def test_snapshot_check_materializes_and_verifies_legacy_based_differential(tmp_path):
    source = tmp_path / "source"
    frozen_archive(source)
    baseline, delta = tmp_path / "base.zip", tmp_path / "delta.zip"
    save_snapshot(source, baseline)
    (source / "documents" / "new.txt").write_text("new preserved artifact")
    save_delta(source, delta, baseline, compression="deflate")
    result = check_snapshot(delta)
    assert result["valid"]
    assert len(result["chain"]) == 2
    assert result["chain"][1]["base"] == baseline.name
    assert result["total_bytes"] > 0
