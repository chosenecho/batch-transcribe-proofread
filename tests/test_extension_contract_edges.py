"""Explicit extensions reject incompatible markers and unsupported selector types."""
from contextlib import closing
import sqlite3

import pytest

from bili_asr.contracts.fingerprints import database_fingerprint
from bili_asr.storage.database import open_database
from bili_asr.storage.publication_packages import install_packages, require_packages
from bili_asr.storage.schema_contract import SchemaContractError


@pytest.mark.parametrize("mode", ["unknown", 1, None])
def test_package_fingerprint_requires_explicit_registered_mode(mode):
    with pytest.raises(ValueError, match="unsupported publication package"):
        database_fingerprint("bilibili-v1", packages=mode)


def test_package_install_refuses_caller_transaction_and_changed_marker(tmp_path):
    with closing(open_database(tmp_path)) as connection:
        connection.execute("BEGIN")
        with pytest.raises(ValueError, match="own transaction"):
            install_packages(connection)
        connection.rollback()
        install_packages(connection)
        # The immutable marker refuses in-place deletion rather than making the
        # installation appear to be an old archive again.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM publication_packages_contract")
        connection.rollback()
        connection.execute("DROP TRIGGER immutable_publication_packages_contract_delete")
        with pytest.raises(SchemaContractError, match="missing or altered"):
            require_packages(connection)
