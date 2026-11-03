"""Reconcile the audio store with what the archive actually holds.

The two tables ``audio_objects`` / ``part_audio_objects`` are written as audio is
acquired (``MediaQueueRepository.mark_audio_acquired``); nothing could *read*
them back as an inventory.  :func:`reconcile_audio_inventory` is that read.

The four counters are **product rulings**, not naming choices — they are defined
in ``specs/audio-retention-contract.md`` §3.1, and the plan that owns this module
says to implement them as written rather than redefining one locally.  They are
reproduced here so a reader of the code does not have to hold the spec open:

``recorded``
    A manifest candidate that had **no** ``audio_objects`` row and now has one.
    Never invented when the file was absent.
``already``
    A candidate whose object row was already present **and matched**.  A re-run
    converges; it does not accumulate, and never writes a second row for one
    ``sha256``.
``missing``
    A manifest row that **names** an ``audio_path`` and the file is not there —
    the shipped reclaim path and a manual delete both produce it.  Never deleted,
    never re-created, never silently skipped.
``unlinked``
    An observed audio object that **no** ``part_audio_objects`` row attributes to
    a part.  The store can see the object but cannot tie it to a work.

**The command is additive and read-only over the audio tree.**  No counter
authorises a write to the filesystem: this module reads bytes to digest a file
and it never unlinks, re-creates or moves one.

**This module does not print.**  It is a service: it returns an outcome and names
the rows it could not read in that outcome, and ``cli.py`` decides what an
operator sees.  (It was the only module under ``services/`` that printed, which
put an operator-facing message in a layer that cannot know the command's name.)

Digesting is **not** optional.  ``audio_objects.sha256`` is ``NOT NULL UNIQUE``
(``schema.sql:94``), so a row without one is a row the store cannot deduplicate,
and ``mark_audio_acquired`` refuses a bare hash outright.  The single streamed
pass below is therefore the same read the size probe already pays for.  ``--deep``
is not a "hash or don't" switch; it *re-verifies* the digest of an object whose
row is already present.

**"Matched" is checked by size, not by reading.**  §3.1 says ``already`` counts a
row that was "already present **and matched**", so a presence-only check would
assert something it did not check.  Comparing digests would mean reading every
known file on every run and would break the published cost model, so the check is
``os.stat().st_size`` against the stored ``byte_size`` — a stat, not a read, so
"zero file reads for a known row" stays true.  ``--deep`` is what escalates that
to a digest comparison.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..artifact_root import ArtifactRoots, iter_audio_paths, resolve_audio_path
from ..audio_reclaim import _candidate_paths
from ..page_identity import parse_work_id

#: The route name recorded in ``part_audio_objects.acquisition_source``.  A
#: bounded scalar, per contract §3 — free-form prose is not allowed there.
ACQUISITION_SOURCE = "derive-audio-inventory"

#: Chunk size for the streamed digest read.  1 MiB keeps the documented
#: "one streamed read per newly recorded file" true without holding a whole
#: 200 MB audio file in memory.
_READ_CHUNK_BYTES = 1 << 20


@dataclass(frozen=True)
class AudioInventoryOutcome:
    """One reconciliation's counters (``specs/…contract.md`` §3.1) and its misses.

    ``unreadable`` is not a fifth counter: a present-but-unreadable file is
    neither absent (so not ``missing``) nor recordable (no digest, so not
    ``recorded``).  Naming those rows separately is what lets the command report
    them without redefining a counter the contract owns.
    """

    recorded: int
    already: int
    missing: int
    unlinked: int
    unreadable: tuple[str, ...] = field(default=())

    def summary_line(self) -> str:
        """§3.1's single summary line, in its fixed field order."""
        return (
            f"derive-audio-inventory: recorded={self.recorded} "
            f"already={self.already} missing={self.missing} unlinked={self.unlinked}"
        )


def _digest_and_size(path: Path) -> tuple[str, int]:
    """One streamed pass yielding the content hash and the on-disk size.

    A single read, so the digest costs no more I/O than the size probe would on
    its own.  ``sha256`` is the store's deduplication identity, so this is not
    skippable: see the module docstring.
    """
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _resolve_existing(
    roots: ArtifactRoots, entry: Mapping[str, Any]
) -> tuple[str, Path] | None:
    """The first candidate that actually holds a file, with its declared string.

    Returns ``(declared, resolved)`` — the **root-relative string that matched**
    alongside the path it resolved to — or ``None``.

    ``require_exists=True`` is the mode ``missing`` needs: it walks the ordered
    bases and hits one only when the *file* is there, so a row whose named file
    is absent at every base answers ``None`` rather than a plausible-looking path
    (``resolve_audio_path`` returns a non-existent path under the other mode).

    Returning the matched candidate as well as the path is what keeps a recorded
    ``storage_key`` naming a file that exists: a row declaring ``audio/X.p0.m4a``
    whose file is actually ``audio/X.p0.flac`` must not record the ``.m4a`` name,
    because the store's own field rule is that the two surfaces agree about
    *which file a row names* — and a name pointing at no file cannot agree with
    anything.
    """
    for declared in _candidate_paths(entry):
        resolved = resolve_audio_path(roots, declared, require_exists=True)
        if resolved is not None:
            # Manifest and storage keys are portable archive-relative names.
            # Windows may hand us a legacy backslash spelling, but persisting
            # that spelling makes the same file look new on the next run or
            # after moving the archive to a POSIX host.
            return str(declared).replace("\\", "/"), resolved

        # ``require_exists=True`` proves presence by opening the file.  That is
        # useful for ordinary readers, but it collapses a present file whose
        # open is denied (permissions, transient I/O) into the same ``None``
        # result as an absent file.  Inventory needs the distinction so the
        # caller can report ``unreadable`` rather than violating the meaning of
        # the ``missing`` counter.  The non-opening resolver still applies the
        # same confinement and no-follow checks; stat only decides whether the
        # candidate is present before the digest pass reports the read failure.
        for _base, _candidate, candidate_path in iter_audio_paths(
            roots, (declared,), require_exists=False
        ):
            try:
                info = os.stat(candidate_path, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                # A metadata refusal is itself evidence that this named path
                # cannot be read.  Keep it out of ``missing``; the digest stage
                # will either confirm the failure or surface a more specific
                # error while preserving the same classification.
                return str(declared).replace("\\", "/"), candidate_path
            if stat.S_ISREG(info.st_mode):
                return str(declared).replace("\\", "/"), candidate_path
    return None


def reconcile_audio_inventory(
    *,
    roots: ArtifactRoots,
    entries: Mapping[str, Mapping[str, Any]],
    known_objects: Mapping[str, tuple[int, str]],
    known_audio_ids: Iterable[int],
    linked_audio_ids: Iterable[int],
    part_durations: Mapping[str, int],
    record: Callable[..., int],
    moment: int,
    deep: bool = False,
) -> AudioInventoryOutcome:
    """Reconcile ``entries`` (the manifest) against the audio store and the disk.

    The **manifest is the candidate set** — this function never enumerates a
    directory to decide what should exist.  The filesystem is consulted for one
    question only: "is the file this row names actually there?".  A directory
    scan would invent candidates the archive never declared, which is precisely
    what ``missing`` is forbidden to do.

    ``known_objects`` maps ``audio_objects.storage_key`` to its stored
    ``(byte_size, sha256)`` — the size is what makes §3.1's "and matched"
    checkable without reading bytes, and the digest is what ``--deep`` compares a
    fresh read against.  ``known_audio_ids`` is the ``audio_objects.audio_id``
    set and ``linked_audio_ids`` the ``part_audio_objects.audio_id`` set.

    ``part_durations`` maps a manifest ``work_id`` to the store's own
    ``video_parts.duration_ms``.  It comes from the **store**, not from the
    manifest row: the manifest carries whole seconds (``duration_s``) because its
    writers floor and clamp them, so reconstructing milliseconds from it loses
    the exact value (``1234567 ms → 1234 s → 1234000 ms``).  A work id absent from
    the mapping records ``0``, which the schema defines as "unknown".

    ``record`` is ``MediaQueueRepository.mark_audio_acquired``, called with
    keyword arguments — this module does not open a transaction of its own, so the
    store keeps owning its writes.

    ``deep`` re-verifies the digest of a candidate whose row already exists.  It
    is not required to *obtain* a digest; see the module docstring.
    """
    objects_by_key = dict(known_objects)
    # Content identity. `sha256` is the store's own dedup key, and `audio_objects`
    # allows exactly one row per digest, so a candidate whose *content* is already
    # held is "present and matched" even when the single row currently names a
    # different path.  Without this the store oscillates: three byte-identical
    # files can only ever own one row, so each run repoints it to a different
    # path and reports them as newly `recorded` for ever (measured: the key
    # bouncing A->B->C->B with recorded=2 on every unchanged re-run).
    known_digests = {sha for _, sha in objects_by_key.values()}
    stored_ids = set(known_audio_ids)
    linked = set(linked_audio_ids)
    recorded = already = missing = 0
    unreadable: list[str] = []

    for work_id in sorted(entries):
        entry = entries[work_id]
        if not isinstance(entry, Mapping):
            continue
        declared = entry.get("audio_path")
        if not declared:
            # A row that names no audio_path is not an audio candidate at all --
            # neither recorded nor missing.  `missing` counts rows that *name* a
            # file (contract §3.1), so silence here is the definition, not a skip.
            continue

        found = _resolve_existing(roots, entry)
        if found is None:
            # named-but-absent: reported, never invented, never deleted.
            missing += 1
            continue
        storage_key, resolved = found

        stored = objects_by_key.get(storage_key)
        if stored is not None:
            stored_size, stored_sha = stored
            try:
                on_disk_size = resolved.stat().st_size
            except OSError:
                unreadable.append(storage_key)
                continue
            if on_disk_size == stored_size:
                if not deep:
                    # §"cost model": zero file reads for a known row not passed to
                    # `--deep`.  A `stat` is not a read, so the size makes §3.1's
                    # "and matched" checkable without breaking that promise.
                    already += 1
                    continue
                # `--deep` escalates the comparison to the digest: it reads the
                # file and must APPLY what it read, not discard the observation.
                try:
                    digest, _ = _digest_and_size(resolved)
                except OSError:
                    unreadable.append(storage_key)
                    continue
                if digest == stored_sha:
                    already += 1
                    continue
                # Same location and size, different content: the stored digest is
                # stale, so fall through and let the record below refresh it.

        parts = _work_id_parts(work_id)
        if parts is None:
            # A manifest key that is not a work id cannot be attributed to a
            # part, so it can be recorded as nothing.  It is still an observed
            # file: `missing` is for absence, and this is present.
            continue

        try:
            sha256, byte_size = _digest_and_size(resolved)
        except OSError:
            # A present-but-unreadable file.  It is **not** `missing` — the file
            # is there, and §3.1 defines that counter as the file being absent —
            # and it cannot be recorded, because `sha256` is `NOT NULL UNIQUE` and
            # a row without one cannot be deduplicated.  It is therefore reported
            # by name, never folded into a counter §3.1 owns.
            unreadable.append(storage_key)
            continue

        if sha256 in known_digests:
            # The content is already archived under some path.  Count it and do
            # NOT record: a second row for one `sha256` is the one thing §3.1 says
            # `already` never does, and recording would repoint the existing row
            # and make the next run disagree -- the oscillation this branch
            # exists to stop.
            already += 1
            continue

        bvid, page_index = parts
        audio_id = record(
            bvid=bvid,
            page_index=page_index,
            audio_path=storage_key,
            sha256=sha256,
            byte_size=byte_size,
            format=resolved.suffix.lstrip("."),
            duration_ms=int(part_durations.get(str(work_id), 0)),
            acquisition_source=ACQUISITION_SOURCE,
            acquired_at=moment,
        )
        objects_by_key[storage_key] = (byte_size, sha256)
        known_digests.add(sha256)
        # `recorded` is "this run wrote the object's content".  For a row that
        # existed and did NOT match, the content is rewritten here, so it counts:
        # calling it `already` would assert the match §3.1 requires, and counting
        # it as neither would hide a rewrite.  The next unchanged run then finds a
        # matching row and reports `already`, which is the convergence F8 asks
        # for.  (The §3.1 wording "had no row and now has one" covers the common
        # case but not this one -- flagged as residual A-R7 rather than settled
        # silently.)
        recorded += 1
        stored_ids.add(int(audio_id))
        # `record` inserts the part link with ON CONFLICT DO NOTHING, so the
        # object is attributed to its part from here on.
        linked.add(int(audio_id))

    # `unlinked` is a *store* observation, not a manifest one: an object row that
    # no part link attributes to a work, so an operator can see that the store
    # holds audio it cannot tie to any archived part.  It is the object set --
    # the rows that existed before this run plus the ones it recorded -- minus
    # everything a `part_audio_objects` row attributes to a part.
    unlinked = len(stored_ids - linked)

    return AudioInventoryOutcome(
        recorded=recorded,
        already=already,
        missing=missing,
        unlinked=unlinked,
        unreadable=tuple(unreadable),
    )


def _work_id_parts(work_id: object) -> tuple[str, int] | None:
    """``"BV….p2" → ("BV…", 2)``, or ``None`` when the key is not a work id."""
    try:
        return parse_work_id(str(work_id))
    except ValueError:
        return None


__all__ = [
    "ACQUISITION_SOURCE",
    "AudioInventoryOutcome",
    "reconcile_audio_inventory",
]
