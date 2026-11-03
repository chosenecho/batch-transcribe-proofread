"""Where archive products live: one configured root, two ordered read bases.

Audio, transcript bundles, and harvested subtitle documents may live under a
root that is not the archive root (``--artifact-root`` /
``BILI_ARTIFACT_ROOT``); durable workflow and metadata state stays in
``archive.db`` at the archive root. This module is the
single place that resolves, validates and carries that split
(``artifact-root-contract.md`` §2–§5, decisions D7–D12).

Resolution (contract §3.2, D9)::

    --artifact-root <value>   (non-blank)  -> use it
    else BILI_ARTIFACT_ROOT   (non-blank)  -> use it
    else                                   -> the archive root (today's behaviour)

A blank or whitespace-only value at either level counts as *unset* and never blocks the
next level, which is :func:`config.resolve_proxy`'s idiom (``config.py:154-175``) and
deliberately not :func:`config.resolve_sessdata`'s (``config.py:128-141``): the
credential's blank-blocks-fallthrough rule exists because it has a security-relevant
anonymous mode, and a path has no such mode.  ``~`` is expanded **here and nowhere
else**: :func:`resolve_artifact_root` is the only expander, so :meth:`ArtifactRoots.of`
is abspath-only for both roots and a ``~``-bearing archive root keeps the treatment
defined by :func:`archive._lexical_archive_root`.  A relative value is made absolute
against the process CWD exactly as ``--archive-root`` behaves today.
The path is kept **lexical** — ``realpath`` is never applied — because
``path_policy.open_audio_directory`` (``path_policy.py:32-57``) opens the root itself
with ``O_NOFOLLOW``: resolving first would silently follow a symlinked root past that
check. A symlinked artifact root is
refused, so pass the real path.

Validation (contract §3.3, D10) happens once, in :func:`roots_for`, and only for a
**configured** root.  A value lexically equal to the archive root is the **identity
case**: one base, today's code path, not validated further, because an explicit no-op
must be a no-op.  Any other value must already be an existing, **non-symlink**
directory this process can actually open, or the command refuses with its usage/config
exit and names the path.  Artifact writers additionally create, write, sync and remove
a temporary probe here; readers do not require a writable product root.  Directory
sync is probed on POSIX, matching the descriptor-based publishers; Windows publishers
use file sync without a directory-descriptor primitive.  The symlink refusal is
explicit and comes first: ``is_dir()``
follows a link, so without it
a symlinked root would be accepted here and only fail later — every write raising a raw
``OSError`` from ``O_NOFOLLOW``, every read silently degrading to the archive root.  A
missing root is **never created**: an unmounted mount point still exists as an empty
directory, and auto-creating a missing one would publish products to the underlying
filesystem instead of the mount.  A root *inside* the archive root is accepted — a
legitimate layout, and the two bases stay distinct.

Reads (contract §5, D7/D8) never see an absolute recorded path.  The recorded strings
stay artifact-root-relative (``audio/{stem}.m4a``, ``transcripts/{stem}/bundle.srt``), so
the manifest's bytes are unchanged by this feature and no reader needs to know which
root a row was written under.  :func:`resolve_audio_path` walks
:meth:`ArtifactRoots.read_bases` in order and returns the first hit, with each
candidate validated at **its own** base by that family's existing guard — never a
cross-root path computation.  A legacy row written before the root was configured
therefore keeps resolving to its existing file (§5, D6), and nothing durable records
the root (D12): a later run with a different root neither rewrites nor invalidates
earlier rows.

That loop is a **read** rule, and it is a genuine ordered probe only in its default
mode: with ``require_exists=False`` the first base whose ``audio/`` directory opens wins
whether or not the file is there, so base 2 is reached only when base 1 has no ``audio/``
at all and the returned path may not exist.  **Writes must not use it.**  Every artifact
write, and every write-side re-confinement (``audio.download_audio``'s ``audio.py:151``),
resolves on :attr:`ArtifactRoots.write_base` alone — one base, chosen by where the write
goes rather than by which directory happens to exist.

Layering: a shared policy leaf beside ``path_policy``, importing stdlib plus
``path_policy`` only — never a manifest, storage or artifact writer — so the
cross-layer rule that only ``cli`` composes the stages is not widened.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from .path_policy import confined_audio_path

#: Environment variable carrying the optional configured artifact root.
ARTIFACT_ROOT_ENV_VAR = "BILI_ARTIFACT_ROOT"

#: Environment variable carrying the optional audio retention policy (``1`` keep,
#: ``0`` reclaim); the flag is resolved against it once at the command boundary.
KEEP_AUDIO_ENV_VAR = "BILI_KEEP_AUDIO"

#: Retention default: audio is retained unless a flag or the variable says otherwise.
KEEP_AUDIO_DEFAULT = True


class ArtifactRootError(ValueError):
    """A configured artifact root cannot be used → the command's usage/config exit (1)."""


@dataclass(frozen=True)
class ArtifactRoots:
    """The roots one invocation reads and writes under (D7, D8, D10 in one value).

    ``archive_root`` and ``artifact_root`` are both absolute and lexical; they are
    equal when no root was configured, which is the identity case the libraries
    default to.
    """

    archive_root: Path
    artifact_root: Path

    @classmethod
    def of(
        cls,
        archive_root: str | os.PathLike[str],
        artifact_root: str | os.PathLike[str] | None = None,
    ) -> "ArtifactRoots":
        """Build the value object from raw paths — abspath only, no ``isdir`` check.

        Neither root is reinterpreted: ``~`` is expanded by :func:`resolve_artifact_root`
        before the value ever reaches this constructor, and the caller that resolves no
        configuration passes an already-absolute path (contract §3.4, D10).
        """
        archive = _lexical(archive_root)
        configured = archive if artifact_root is None else _lexical(artifact_root)
        return cls(archive_root=archive, artifact_root=configured)

    @property
    def configured(self) -> bool:
        """True when a root other than the archive root was configured."""
        return self.artifact_root != self.archive_root

    @property
    def write_base(self) -> Path:
        """The base every artifact write uses — always the configured root."""
        return self.artifact_root

    def read_bases(self) -> tuple[Path, ...]:
        """The ordered bases a recorded artifact path is resolved against (D8)."""
        if self.configured:
            return (self.artifact_root, self.archive_root)
        return (self.archive_root,)


def _root_text(value: str | os.PathLike[str]) -> str:
    """Turn one raw root into text, or refuse it (contract §3.4).

    The sibling leaf this module wraps refuses a malformed input rather than letting a
    bare ``TypeError`` escape (``path_policy._audio_parts:17-20``); the same shape here
    with this module's own error, because it is a policy leaf other layers consume.
    """
    try:
        text = os.fspath(value)
    except TypeError:
        text = None
    if not isinstance(text, str):
        raise ArtifactRootError(f"artifact root is not a path ({value!r})")
    return text


def _lexical(value: str | os.PathLike[str]) -> Path:
    """Make absolute; never ``~``, never ``realpath`` (contract §3.2, D9).

    ``~`` expansion is :func:`resolve_artifact_root`'s alone, so ``--archive-root``'s
    shipped treatment of a tilde-shaped path is not reinterpreted in the no-flag case.
    """
    return Path(os.path.abspath(_root_text(value)))


def resolve_artifact_root(value: str | None, environ: Mapping[str, str]) -> str | None:
    """Return the configured artifact root: flag, else environment, else ``None``.

    The flag wins over :data:`ARTIFACT_ROOT_ENV_VAR`; a blank or whitespace-only value
    at either level counts as unset and never blocks the next level.  The value that
    resolves is stripped, ``~`` is expanded — the only place in this module that happens
    (§3.2) — and the result is made absolute against the process CWD.  ``None`` means
    unconfigured — the caller then uses the archive root.
    """
    for candidate in (value, environ.get(ARTIFACT_ROOT_ENV_VAR)):
        if candidate is None:
            continue
        stripped = _root_text(candidate).strip()
        if stripped:
            return str(_lexical(os.path.expanduser(stripped)))
    return None


def resolve_keep_audio(flag_value: bool | None, environ: Mapping[str, str]) -> bool:
    """Return the retention policy: the flag, else the variable, else the default.

    The flag wins outright.  Otherwise ``BILI_KEEP_AUDIO == "1"`` retains and ``== "0"``
    reclaims — the two documented values keep the meanings they have today — and
    anything else, including an absent or blank value, falls back to
    :data:`KEEP_AUDIO_DEFAULT` (retain).
    """
    if flag_value is not None:
        return flag_value
    value = environ.get(KEEP_AUDIO_ENV_VAR)
    if value == "1":
        return True
    if value == "0":
        return False
    return KEEP_AUDIO_DEFAULT


def _unopenable(path: Path) -> bool:
    """True when this process cannot open an existing path **as a directory** (D17).

    :meth:`Path.is_dir` is a stat, and the residue it leaves is the one D17 calls an
    unusable root: a directory the process may not open — a denied mount, a failing
    one — passes the type checks and then fails every write, while every read degrades
    *silently*, because a reader drops a base it cannot open and answers from the
    archive root instead.  So the probe is the operation the writers and readers
    actually perform, not another question about the path's shape.

    POSIX uses a directory descriptor, as ``path_policy.open_audio_directory`` does.
    Platforms without that primitive open a directory iterator instead to detect
    access errors. This fallback checks openability, not descriptor confinement.
    """
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
        try:
            with os.scandir(path):
                pass
        except OSError:
            return True
        return False
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return True
    os.close(descriptor)
    return False


def _probe_writable(path: Path) -> None:
    """Exercise the writer's write/sync operations without retaining a product.

    A readable mount can still reject writes, file sync, or directory sync.  Check
    those before a command takes its writer lock or begins processing rows.  The
    POSIX probe stays relative to a no-follow directory descriptor, as the artifact
    publishers do; platforms without that primitive use the file-sync boundary
    their publishers support.
    """
    directory_fd = None
    name = f".bili-asr-probe-{uuid.uuid4().hex}"
    try:
        if os.name == "posix" and hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(
                path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            )
        try:
            options = {"dir_fd": directory_fd} if directory_fd is not None else {}
            target = name if directory_fd is not None else path / name
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
                0o600,
                **options,
            )
            try:
                content = b"bili-asr artifact-root probe\n"
                if os.write(descriptor, content) != len(content):
                    raise OSError("artifact root probe write was incomplete")
                os.fsync(descriptor)
                if directory_fd is not None:
                    os.fsync(directory_fd)
            finally:
                try:
                    os.close(descriptor)
                finally:
                    os.unlink(target, **options)
                    if directory_fd is not None:
                        os.fsync(directory_fd)
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
    except OSError as exc:
        raise ArtifactRootError(f"artifact root cannot be written and synced ({path})") from exc


def roots_for(
    archive_root: str | os.PathLike[str],
    *,
    flag_value: str | None = None,
    environ: Mapping[str, str] | None = None,
    require_writable: bool = True,
) -> ArtifactRoots:
    """Resolve and validate the roots for one command invocation (contract §3.2/§3.3).

    The CLI's one entry point: precedence, the blank rule and the validation of a
    *configured* root live here and nowhere else.  Raises :class:`ArtifactRootError`
    when a configured root is a symlink, does not exist, is not a directory, or exists
    as a directory this process cannot open — it is never created.  The identity case is
    accepted without touching the filesystem.  Artifact writers additionally require
    a real write and sync probe; callers that only read products pass
    ``require_writable=False``.
    """
    environment: Mapping[str, str] = os.environ if environ is None else environ
    roots = ArtifactRoots.of(archive_root, resolve_artifact_root(flag_value, environment))
    if roots.configured:
        # Before `is_dir()`, which follows a link (contract §3.2/§6, D4): accepting a
        # symlinked root here would let every write fail later with a raw OSError from
        # `O_NOFOLLOW` while every read silently fell back to the archive root.
        if os.path.islink(roots.artifact_root):
            raise ArtifactRootError(f"artifact root is a symlink ({roots.artifact_root})")
        try:
            if not roots.artifact_root.is_dir():
                reason = (
                    "is not a directory" if roots.artifact_root.exists() else "does not exist"
                )
                raise ArtifactRootError(f"artifact root {reason} ({roots.artifact_root})")
        except OSError:
            # The classification is itself a stat, and `pathlib` re-raises every OSError
            # outside its ignored errnos (ENOENT/ENOTDIR/EBADF/ELOOP): a root under an
            # unsearchable ancestor (EACCES) or on a dropped mount (ENOTCONN/EIO) would
            # otherwise escape `main()`'s `except ArtifactRootError` as a traceback,
            # where the fourth line below is the one that names exactly that input
            # (D17, §9).  An absent root is unaffected — those two errnos are the ones
            # `pathlib` swallows, so it still reports "does not exist".
            raise ArtifactRootError(
                f"artifact root cannot be opened ({roots.artifact_root})"
            ) from None
        # R4: an existing directory the process cannot open is "unusable" under D17 and
        # takes the named exit-1 refusal here, rather than degrading to a base the
        # readers drop in silence.  It is a distinct condition, so it gets a distinct
        # line: the two above would each state something false about this path.
        if _unopenable(roots.artifact_root):
            raise ArtifactRootError(f"artifact root cannot be opened ({roots.artifact_root})")
        if require_writable:
            _probe_writable(roots.artifact_root)
    return roots


def resolve_audio_path(
    roots: ArtifactRoots,
    declared: str | os.PathLike[str] | None,
    *,
    require_exists: bool = True,
) -> Path | None:
    """Resolve one recorded ``audio_path`` over the ordered bases (contract §5).

    Each candidate is validated at its own base by
    ``path_policy.confined_audio_path`` — the same no-follow guard the single-root
    readers use today — and the first hit wins.  ``None`` means the value is missing or
    refused at *every* base: under ``require_exists=True`` the guard returns ``None``
    for both, so the resolver does not try to tell them apart.

    The two modes differ in what "first hit" means, and **only the default is a probe**:

    * ``require_exists=True`` — a base is a hit when it holds the *file*, so the loop
      really does walk the bases and base 2 is reached whenever base 1 does not hold it.
    * ``require_exists=False`` — a base is a hit as soon as the shape is valid **and that
      base's ``audio/`` directory opens**, so base 1 wins even when the file is absent
      there, base 2 is reached only when base 1 has no ``audio/`` at all, and the
      returned path **may not exist**.  That is the guard's own semantics
      (``path_policy.py:98-109``), kept because §5's loop is verbatim.

    **Writes and write-side re-confinement must not use this loop.**  They resolve on
    :attr:`ArtifactRoots.write_base` alone — one base, the one being written to — so the
    base is never chosen by which ``audio/`` directory happens to exist (contract §4).
    """
    for _base, _declared, confined in iter_audio_paths(
        roots, (declared,), require_exists=require_exists
    ):
        return confined
    return None


def usable_audio_path(
    roots: ArtifactRoots,
    declared_candidates: Iterable[str | os.PathLike[str] | None],
) -> tuple[Path, str, Path] | None:
    """Return the first confined nonempty audio as (base, declared, path).

    Holding-base selection includes usability: an empty configured-root stub
    cannot shadow a usable legacy file. Consumers share this predicate and
    keep the selected relative name instead of re-deriving/probing it.
    """
    from .path_policy import confined_audio_file

    for base, declared, confined in iter_audio_paths(roots, declared_candidates):
        try:
            with confined_audio_file(base, declared) as safe_audio:
                if os.stat(safe_audio).st_size > 0:
                    return base, os.fspath(declared), confined
        except OSError:
            continue
    return None


def usable_audio_for_path(
    roots: ArtifactRoots, path: str | os.PathLike[str],
) -> tuple[Path, str, Path] | None:
    """Pair one returned absolute audio path with its own confined base."""
    for base in roots.read_bases():
        try:
            declared = os.path.relpath(path, base).replace(os.sep, "/")
        except ValueError:
            continue
        # Restrict this probe to the base used to derive the relative name.
        found = usable_audio_path(ArtifactRoots.of(base), (declared,))
        if found is not None:
            return found
    return None


def iter_audio_paths(
    roots: ArtifactRoots,
    declared_candidates: Iterable[str | os.PathLike[str] | None],
    *,
    require_exists: bool = True,
) -> Iterator[tuple[Path, str | os.PathLike[str], Path]]:
    """Yield confined audio candidates in the contract's base/candidate order.

    All readers use the same ordered, per-base confinement rule.  Keeping the loop
    here prevents one caller from accidentally resolving a legacy archive copy
    against the configured root or weakening the no-follow guard.
    """
    # A caller may pass a generator assembled from manifest fields. Materialize
    # once so every read base sees the same candidate sequence.
    candidates = tuple(declared_candidates)
    for base in roots.read_bases():
        for declared in candidates:
            if declared is None:
                continue
            try:
                confined = confined_audio_path(base, declared, require_exists=require_exists)
            except OSError:
                continue
            if confined is not None:
                yield base, declared, confined


__all__ = [
    "ARTIFACT_ROOT_ENV_VAR",
    "KEEP_AUDIO_DEFAULT",
    "KEEP_AUDIO_ENV_VAR",
    "ArtifactRootError",
    "ArtifactRoots",
    "iter_audio_paths",
    "resolve_artifact_root",
    "resolve_audio_path",
    "usable_audio_path",
    "usable_audio_for_path",
    "resolve_keep_audio",
    "roots_for",
]
