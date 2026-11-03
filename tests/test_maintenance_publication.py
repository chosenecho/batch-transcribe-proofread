"""The public profile keeps publication rights and review gates authoritative."""
import json

import pytest

from bili_asr.services.maintenance_publication import export_public_profile
from tests.test_publication_export import (
    manuscript as manuscript,  # noqa: PLC0414 - pytest fixture registration
)
from tests.test_publication_export import publish

pytestmark = pytest.mark.usefixtures("enable_content_publication_for_tests")


def test_public_profile_exports_approved_release_without_private_database(manuscript, tmp_path):
    connection, root, _revision, edition = manuscript
    publish(connection, root, edition)
    output = tmp_path / "public"
    result = export_public_profile(root, output)
    assert result["published_articles"] == 1
    assert result["database_recoverable"] is False
    assert not list(output.rglob("*.db"))
    catalog = json.loads((output / "catalog.json").read_bytes())
    assert len(catalog["articles"]) == 1
    assert not any("prepared_json" in path.read_text(errors="ignore") for path in output.rglob("*") if path.is_file())


def test_public_automatic_backup_and_retention_never_expect_database(manuscript, tmp_path):
    from bili_asr.services.maintenance_backup import policy, run_backup
    from bili_asr.services.maintenance_chain import verify_chain
    from bili_asr.storage.maintenance_scope import scope
    connection, root, _revision, edition = manuscript
    publish(connection, root, edition)
    configured = policy(root, tmp_path / "public-backups", selection=scope(profile="publication"), mode="full", keep_count=1, keep_days=0)
    first = run_backup(configured, now=100)
    assert first["valid"] and first["database_recoverable"] is False, first.get("error")
    second = run_backup(configured, now=110)
    assert second["valid"]
    assert verify_chain(tmp_path / "public-backups" / second["snapshot"])["database_recoverable"] is False
