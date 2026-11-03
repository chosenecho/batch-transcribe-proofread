from contextlib import closing
import sqlite3

import pytest

from bili_asr.storage.archive_contracts import bootstrap_contract, require_universal_contract
from bili_asr.storage.database import SchemaContractError


@pytest.mark.parametrize("other_connection", [False, True])
def test_warm_contract_checks_reject_later_schema_changes(tmp_path, other_connection):
    database = tmp_path / "archive.db"
    with closing(sqlite3.connect(database)) as connection:
        bootstrap_contract(connection)
        require_universal_contract(connection)
        require_universal_contract(connection)
        if other_connection:
            with closing(sqlite3.connect(database)) as writer:
                writer.execute("ALTER TABLE source_videos ADD COLUMN unregistered TEXT")
        else:
            connection.execute("ALTER TABLE source_videos ADD COLUMN unregistered TEXT")
        with pytest.raises(SchemaContractError, match="missing or altered object: source_videos"):
            require_universal_contract(connection)
