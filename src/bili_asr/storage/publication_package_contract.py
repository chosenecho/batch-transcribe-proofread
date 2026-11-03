"""Read-only extension identity; independent of database and repositories."""
from functools import lru_cache
import sqlite3

from bili_asr.storage.schema_contract import SchemaContractError, normalize_schema_sql, schema_resource

POLICY = "publication-packages/v1"


@lru_cache(maxsize=1)
def objects() -> dict[str, tuple[str, str]]:
    with sqlite3.connect(":memory:") as reference:
        reference.executescript(schema_resource("schema-publication-packages.sql"))
        return {name: (kind, sql) for name, kind, sql in reference.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL")}


def require_packages(connection: sqlite3.Connection) -> bool | str:
    expected = objects()
    actual = {name: (kind, sql) for name, kind, sql in connection.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL")}
    if not expected.keys() & actual.keys():
        return False
    embedded = not any("publication_package_legacy" in name or "publication_packages_contract" in name for name in actual)
    if embedded:
        expected = {name: item for name,item in expected.items()
                    if "publication_package_legacy" not in name and "publication_packages_contract" not in name}
    for name, (kind, sql) in expected.items():
        current = actual.get(name)
        if current is None or current[0] != kind or normalize_schema_sql(current[1]) != normalize_schema_sql(sql):
            raise SchemaContractError(f"{POLICY}: missing or altered object {name}")
    if not embedded and [tuple(row) for row in connection.execute(
            "SELECT singleton,version FROM publication_packages_contract")] != [(1, 1)]:
        raise SchemaContractError(f"{POLICY}: altered contract marker")
    return "embedded" if embedded else True
