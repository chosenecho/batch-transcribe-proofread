"""Read-only extension identity; independent of database and repositories."""
from functools import lru_cache
import sqlite3

from bili_asr.storage.schema_contract import SchemaContractError, normalize_schema_sql, schema_resource

POLICY = "workflow-batches/v1"


@lru_cache(maxsize=1)
def objects() -> dict[str, tuple[str, str]]:
    with sqlite3.connect(":memory:") as reference:
        reference.executescript(schema_resource("schema-workflow-batches.sql"))
        return {name: (kind, sql) for name, kind, sql in reference.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL")}


def require_batches(connection: sqlite3.Connection, *, required: bool = False) -> bool:
    actual = {name: (kind, sql) for name, kind, sql in connection.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE sql IS NOT NULL")}
    expected = objects()
    if not actual.keys() & expected.keys():
        if required:
            raise SchemaContractError("workflow-batches/v1: explicitly install the batch contract first")
        return False
    for name, (kind, sql) in expected.items():
        if name not in actual or actual[name][0] != kind or normalize_schema_sql(actual[name][1]) != normalize_schema_sql(sql):
            raise SchemaContractError(f"workflow-batches/v1: missing or altered object {name}")
    if [tuple(row) for row in connection.execute("SELECT singleton,version FROM workflow_batch_contract")] != [(1, 1)]:
        raise SchemaContractError("workflow-batches/v1: altered marker")
    return True
