"""Schema comparison primitives, independent of archive business extensions."""
from __future__ import annotations
from functools import lru_cache
from importlib import resources
import re
import sqlite3

class SchemaContractError(RuntimeError):
    """An existing archive does not carry its claimed immutable schema."""

def schema_resource(name: str) -> str:
    return resources.files("bili_asr.storage").joinpath(name).read_text(encoding="utf-8")

def _strip_sql_comments(statement: str) -> str:
    """Remove SQL comments, respecting every quoted region.

    Both a whole-line comment and a trailing one are removed, because SQLite
    stores whatever was written: a comment left in either form would make the
    shipped and stored texts differ forever.

    The scan understands all four of SQLite's quoting forms — ``'...'`` strings
    (with ``''`` escaping), ``"..."`` and ``[...]`` identifiers, and MySQL-style
    ``` `...` ``` identifiers — plus ``/* ... */`` block comments.  It has to:
    ``--`` inside *any* quoted region is data, and a scan that only knew about
    single quotes would truncate the statement at that marker, which makes two
    views selecting *different* columns normalize equal.  That is a false
    negative — a genuinely stale body judged current — i.e. exactly the defect
    this refresh exists to close.
    """

    out: list[str] = []
    index = 0
    length = len(statement)
    # (closing character, doubled-character escape) for each quoted form.
    quotes: dict[str, tuple[str, bool]] = {
        "'": ("'", True),
        '"': ('"', True),
        "`": ("`", True),
        "[": ("]", False),
    }
    while index < length:
        char = statement[index]
        if char in quotes:
            closing, doubled = quotes[char]
            out.append(char)
            index += 1
            while index < length:
                current = statement[index]
                out.append(current)
                if current == closing:
                    if doubled and index + 1 < length and statement[index + 1] == closing:
                        out.append(current)
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if char == "/" and index + 1 < length and statement[index + 1] == "*":
            closing_block = statement.find("*/", index + 2)
            if closing_block < 0:
                break
            index = closing_block + 2
            continue
        if char == "-" and index + 1 < length and statement[index + 1] == "-":
            # Skip to the end of the line, keeping the newline itself so two
            # tokens on either side of a removed comment do not fuse together.
            newline = statement.find("\n", index)
            if newline < 0:
                break
            index = newline
            continue
        out.append(char)
        index += 1
    return "".join(out)


# Cache only immutable SQL text. Callers still read sqlite_master on every
# validation, so schema changes on either connection remain visible.
@lru_cache(maxsize=2048)
def _normalize_view_sql(statement: str) -> str:
    """Collapse a view body to the shape SQLite stores, for comparison only.

    SQLite keeps the original text of a view's ``SELECT`` but drops the trailing
    semicolon and rewrites the header, so the shipped statement and the stored
    one are never byte-equal even when the view is current.  Whitespace and case
    are collapsed as well, which makes the comparison a statement about the body
    rather than about formatting.

    Two properties are load-bearing and were each measured:

    * comments are stripped from **both** sides.  SQLite *preserves* a comment
      that sits inside a view statement, and every shipped view carries several
      (4 to 24 lines each), so stripping only the shipped body would make every
      view read as stale on every open.
    * whitespace and case are collapsed **outside string literals only**.  A body
      differing from the shipped one only inside a literal — ``'no-subtitle'``
      against ``'NO-SUBTITLE'`` — must still read as different, or a genuinely
      stale view would never refresh.
    """

    text = _strip_sql_comments(statement).strip().rstrip(";").strip()
    # SQLite also drops the IF NOT EXISTS clause when it stores a view, so the
    # shipped form has to lose it too before the two can be compared.
    text = re.sub(r"(?i)^create\s+(view|table)\s+if\s+not\s+exists\s+", r"CREATE \1 ", text)

    out: list[str] = []
    plain: list[str] = []
    index = 0
    length = len(text)

    def flush_plain() -> None:
        if plain:
            out.append(" ".join("".join(plain).casefold().split()))
            plain.clear()

    while index < length:
        char = text[index]
        if char == "'":
            flush_plain()
            literal = ["'"]
            index += 1
            while index < length:
                current = text[index]
                literal.append(current)
                if current == "'":
                    if index + 1 < length and text[index + 1] == "'":
                        literal.append("'")
                        index += 2
                        continue
                    break
                index += 1
            out.append("".join(literal))
            index += 1
            continue
        plain.append(char)
        index += 1
    flush_plain()
    return " ".join(piece for piece in out if piece)


@lru_cache(maxsize=2048)
def normalize_schema_sql(sql: str) -> str:
    sql = re.sub(r"(?i)\bIF\s+NOT\s+EXISTS\s+", "", sql)
    return _normalize_view_sql(sql)


def _validate_connection(connection: sqlite3.Connection) -> None:
    """Require the connection state every repository in this module is built on.

    The connection must come with ``row_factory = sqlite3.Row`` and
    ``PRAGMA foreign_keys`` enabled — exactly the state :func:`open_database`
    establishes.
    """
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection")
    if connection.row_factory is not sqlite3.Row:
        raise TypeError("connection must use the sqlite3.Row row_factory")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ValueError("connection must have PRAGMA foreign_keys enabled")
