"""Stable schema SQL fingerprints preserving frozen literal semantics."""
from __future__ import annotations

import hashlib
import re


def normalize_schema_sql(sql: str) -> str:
    """Normalize unquoted SQL only; literals and quoted identifiers stay exact."""
    tokens: list[str] = []
    pattern = re.compile(
        r"--[^\n]*(?:\n|$)|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\""
        r"|`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|\s+|.",
        re.DOTALL,
    )
    for match in pattern.finditer(sql):
        token = match.group()
        if token.isspace() or token.startswith(("--", "/*")):
            continue
        tokens.append(token if token[0] in "'\"`[" else token.casefold())
    # SQLite removes this clause and trailing terminators from stored DDL.
    text = " ".join(tokens).rstrip(" ;")
    return re.sub(
        r"^(create (?:unique |virtual )?(?:table|view|index|trigger)) if not exists ",
        r"\1 ", text,
    )


def schema_sql_sha256(sql: str) -> str:
    return hashlib.sha256(normalize_schema_sql(sql).encode("utf-8")).hexdigest()


