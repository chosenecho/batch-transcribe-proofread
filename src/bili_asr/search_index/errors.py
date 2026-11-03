"""Errors implementation."""

from __future__ import annotations




class FTS5UnavailableError(RuntimeError):
    """Raised when SQLite FTS5 extension is not available in the current environment."""


class SearchIndexMissingError(RuntimeError):
    """The store-backed FTS table does not exist (backlog class, never a defect)."""


class TranscriptStoreError(RuntimeError):
    """The transcript store is unreadable or corrupt (defect class)."""
