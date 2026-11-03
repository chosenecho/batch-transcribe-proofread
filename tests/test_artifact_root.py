"""Unit tests for the artifact-root resolution core (artifact-root-contract.md §3, §5).

Pure and filesystem-touching cases alike stay local: every test uses ``tmp_path`` only,
no CLI and no monkeypatched ``os.environ`` — each environment is passed as a mapping.
The one shipped import beyond the module under test is the guard the read loop wraps
(``path_policy.open_audio_directory``), used to show what the symlink refusal prevents.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from bili_asr.artifact_root import (
    ARTIFACT_ROOT_ENV_VAR,
    KEEP_AUDIO_DEFAULT,
    KEEP_AUDIO_ENV_VAR,
    ArtifactRootError,
    ArtifactRoots,
    iter_audio_paths,
    resolve_artifact_root,
    resolve_audio_path,
    resolve_keep_audio,
    roots_for,
)
from bili_asr.path_policy import open_audio_directory

#: The value shapes §5 pins as refusable at every base.
_REFUSED_AUDIO_VALUES = (
    "audio/../escaped.m4a",
    "/tmp/x.m4a",
    "audio/wrong.wav",
    "audio/sub/deep.m4a",
    "audio/link.m4a",
)


def test_flag_wins_over_environment_and_unset_is_none(tmp_path):
    assert ARTIFACT_ROOT_ENV_VAR == "BILI_ARTIFACT_ROOT"
    environment = {ARTIFACT_ROOT_ENV_VAR: str(tmp_path / "env-root")}
    assert resolve_artifact_root(str(tmp_path / "flag-root"), environment) == str(
        tmp_path / "flag-root"
    )
    assert resolve_artifact_root(None, environment) == str(tmp_path / "env-root")
    assert resolve_artifact_root(None, {}) is None


def test_a_blank_value_at_either_level_counts_as_unset(tmp_path):
    environment_root = str(tmp_path / "env-root")
    for blank in ("", "   "):
        # A blank flag never blocks the environment level: resolve_proxy's idiom
        # (config.py:154-175), deliberately not resolve_sessdata's rule.
        assert (
            resolve_artifact_root(blank, {ARTIFACT_ROOT_ENV_VAR: environment_root})
            == environment_root
        )
        # A blank environment value is unset, never a root of its own.
        assert resolve_artifact_root(None, {ARTIFACT_ROOT_ENV_VAR: blank}) is None
        assert resolve_artifact_root(blank, {ARTIFACT_ROOT_ENV_VAR: blank}) is None


def test_the_value_is_stripped_and_tilde_is_expanded(tmp_path):
    assert resolve_artifact_root("  /tmp/artifact-root  ", {}) == os.path.abspath(
        "/tmp/artifact-root"
    )
    resolved = resolve_artifact_root("  ~/artifact-root  ", {})
    assert resolved == os.path.abspath(os.path.expanduser("~/artifact-root"))
    assert resolved == resolved.strip()
    assert "~" not in Path(resolved).parts


def test_of_is_abspath_only_so_a_tilde_archive_root_stays_literal():
    # `~` is expanded by `resolve_artifact_root` alone (spec §3.2); §3.4 comments `of` as
    # "abspath only". A `~`-bearing archive root therefore keeps today's treatment — the
    # literal component `archive._lexical_archive_root` (`archive.py:264-265`) and
    # `ManifestStore.root` (`manifest.py:101-107`) both keep — so the no-flag identity
    # case stays a no-op instead of silently resolving to another directory.
    literal = Path(os.path.abspath("~/a2"))
    assert ArtifactRoots.of("~/a2").archive_root == literal
    assert "~" in ArtifactRoots.of("~/a2").archive_root.parts
    # Neither root is reinterpreted at this level, the configured one included.
    assert ArtifactRoots.of("/archive", "~/a2").artifact_root == literal
    assert ArtifactRoots.of("/archive", "~/a2").write_base == literal
    # Through the CLI entry point the no-flag case is still the identity case: the
    # literal archive root is neither reinterpreted nor validated (D10), and this one
    # does not exist on disk.
    identity = roots_for("~/a2", environ={})
    assert identity.configured is False
    assert identity.read_bases() == (literal,)


def test_a_relative_value_resolves_against_the_current_directory(tmp_path):
    assert resolve_artifact_root("relative-artifact-root", {}) == os.path.join(
        os.getcwd(), "relative-artifact-root"
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    relative = os.path.relpath(archive, os.getcwd())
    roots = roots_for(archive, flag_value=relative, environ={})
    assert roots.artifact_root == archive
    assert roots.configured is False


def test_the_path_is_kept_lexical_and_never_realpath_resolved(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    real_root = tmp_path / "real-mount"
    real_root.mkdir()
    linked_root = tmp_path / "linked-mount"
    try:
        linked_root.symlink_to(real_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    # `of` never calls `realpath`: the configured value stays lexical, which is what lets
    # `roots_for` see the link and refuse it (§3.2, §6).
    unvalidated = ArtifactRoots.of(archive, str(linked_root))
    assert unvalidated.configured is True
    assert unvalidated.write_base == linked_root
    assert unvalidated.artifact_root != real_root

    linked_archive = tmp_path / "linked-archive"
    linked_archive.symlink_to(archive, target_is_directory=True)
    assert ArtifactRoots.of(linked_archive).archive_root == linked_archive


def test_a_symlinked_configured_root_is_refused(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    real_root = tmp_path / "real-mount"
    (real_root / "audio").mkdir(parents=True)
    linked_root = tmp_path / "linked-mount"
    try:
        linked_root.symlink_to(real_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    # §3.2/§6 and D4: the final component is a symlink, so the root is refused before
    # `is_dir()` can follow it — at either level of the precedence.
    with pytest.raises(ArtifactRootError) as flag_error:
        roots_for(archive, flag_value=str(linked_root), environ={})
    assert str(flag_error.value) == f"artifact root is a symlink ({linked_root})"
    assert isinstance(flag_error.value, ValueError)

    with pytest.raises(ArtifactRootError) as env_error:
        roots_for(archive, environ={ARTIFACT_ROOT_ENV_VAR: str(linked_root)})
    assert str(env_error.value) == f"artifact root is a symlink ({linked_root})"

    # And the refusal is what keeps *both* directions off the link. `of` is unvalidated,
    # so it still builds the lexical value; through it a write fails with a raw `OSError`
    # from `O_NOFOLLOW` on the root (`path_policy.py:38-40`) instead of the named refusal,
    # while the read loop silently returns the archive root's copy — the two failure modes
    # `roots_for` is the only entry point able to stop (a symlinked *archive* root keeps
    # today's later-refusal behaviour, §3.3).
    unvalidated = ArtifactRoots.of(archive, str(linked_root))
    (archive / "audio").mkdir()
    (archive / "audio" / "legacy.m4a").write_bytes(b"legacy copy")
    assert resolve_audio_path(unvalidated, "audio/legacy.m4a") == (
        archive / "audio" / "legacy.m4a"
    )
    with pytest.raises(OSError) as write_error:
        open_audio_directory(unvalidated.write_base, create=True)
    # `O_NOFOLLOW` on the root: Linux reports ENOTDIR once `O_DIRECTORY` is also set.
    assert write_error.value.errno in (errno.ENOTDIR, errno.ELOOP)


def test_an_explicit_value_equal_to_the_archive_root_is_the_identity_case(tmp_path):
    # Deliberately absent: the identity case is never isdir-checked (§3.3).
    archive = tmp_path / "absent-archive-root"
    roots = roots_for(archive, flag_value=str(archive), environ={})
    assert roots.configured is False
    assert roots.read_bases() == (archive,)
    assert roots.write_base == archive

    from_environment = roots_for(archive, environ={ARTIFACT_ROOT_ENV_VAR: str(archive)})
    assert from_environment == roots
    assert from_environment.configured is False


def test_a_missing_or_non_directory_root_is_a_refusal(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()

    missing = tmp_path / "missing-root"
    with pytest.raises(ArtifactRootError) as missing_error:
        roots_for(archive, flag_value=str(missing), environ={})
    # The exact tail §9's CLI line and the T2/T3 handoff depend on (§3.3).
    assert str(missing_error.value) == f"artifact root does not exist ({missing})"
    # A configured root is a usage/config error: the CLI's existing exit-1 path.
    assert isinstance(missing_error.value, ValueError)
    assert not missing.exists()

    # The value that resolved from the environment is validated identically, not only a
    # flag-supplied one: precedence does not change the verdict.
    with pytest.raises(ArtifactRootError) as env_error:
        roots_for(archive, environ={ARTIFACT_ROOT_ENV_VAR: str(missing)})
    assert str(env_error.value) == f"artifact root does not exist ({missing})"

    not_a_directory = tmp_path / "not-a-directory"
    not_a_directory.write_text("x", encoding="utf-8")
    with pytest.raises(ArtifactRootError) as file_error:
        roots_for(archive, flag_value=str(not_a_directory), environ={})
    assert str(file_error.value) == f"artifact root is not a directory ({not_a_directory})"


def test_a_non_path_root_is_refused_with_the_module_error(tmp_path):
    # The sibling leaf this module wraps guards the same input and never lets a bare
    # `TypeError` escape (`path_policy._audio_parts:17-20`); this module's consumers get
    # its own error instead. Not CLI-reachable today — a policy leaf others will consume.
    for bad in (b"/tmp/bytes-root", 5, object()):
        with pytest.raises(ArtifactRootError):
            ArtifactRoots.of(bad)
        with pytest.raises(ArtifactRootError):
            ArtifactRoots.of("/archive", bad)
        with pytest.raises(ArtifactRootError):
            resolve_artifact_root(bad, {})

    with pytest.raises(ArtifactRootError) as bytes_error:
        resolve_artifact_root(b"/tmp/bytes-root", {})
    assert str(bytes_error.value) == "artifact root is not a path (b'/tmp/bytes-root')"
    assert isinstance(bytes_error.value, ValueError)

    archive = tmp_path / "archive"
    archive.mkdir()
    with pytest.raises(ArtifactRootError):
        roots_for(5, environ={})
    with pytest.raises(ArtifactRootError):
        roots_for(archive, flag_value=b"/tmp/bytes-root", environ={})
    with pytest.raises(ArtifactRootError):
        roots_for(archive, environ={ARTIFACT_ROOT_ENV_VAR: b"/tmp/bytes-root"})


def test_read_bases_are_ordered_and_deduplicated(tmp_path):
    archive = tmp_path / "archive"
    artifact = tmp_path / "mount"
    artifact.mkdir(parents=True)

    roots = roots_for(archive, flag_value=str(artifact), environ={})
    assert roots.configured is True
    assert roots.write_base == artifact
    assert roots.read_bases() == (artifact, archive)
    assert len(set(roots.read_bases())) == len(roots.read_bases())

    identity = ArtifactRoots.of(archive)
    assert identity.configured is False
    assert identity.read_bases() == (archive,)
    assert len(set(identity.read_bases())) == 1

    inside = archive / "artifacts"
    inside.mkdir(parents=True)
    inside_roots = roots_for(archive, flag_value=str(inside), environ={})
    assert inside_roots.read_bases() == (inside, archive)
    assert len(set(inside_roots.read_bases())) == 2


def test_resolve_audio_path_prefers_the_artifact_root_then_the_archive_root(tmp_path):
    archive = tmp_path / "archive"
    artifact = tmp_path / "mount"
    for base in (archive, artifact):
        (base / "audio").mkdir(parents=True)
    (archive / "audio" / "both.m4a").write_bytes(b"archive copy")
    (artifact / "audio" / "both.m4a").write_bytes(b"artifact copy")
    (archive / "audio" / "legacy.m4a").write_bytes(b"legacy only")
    (artifact / "audio" / "fresh.m4a").write_bytes(b"configured only")

    roots = roots_for(archive, flag_value=str(artifact), environ={})
    # Present at both bases: the configured root wins (the disclosed shadowing, §5).
    assert resolve_audio_path(roots, "audio/both.m4a") == artifact / "audio" / "both.m4a"
    # Present only at the archive root: the legacy row keeps resolving (§5, D6).
    assert (
        resolve_audio_path(roots, "audio/legacy.m4a") == archive / "audio" / "legacy.m4a"
    )
    # Present only at the configured root.
    assert resolve_audio_path(roots, "audio/fresh.m4a") == artifact / "audio" / "fresh.m4a"
    # Present at neither base.
    assert resolve_audio_path(roots, "audio/absent.m4a") is None
    # Ordered, not existence-dependent: the first base's candidate wins.
    assert resolve_audio_path(roots, "audio/absent.m4a", require_exists=False) == (
        artifact / "audio" / "absent.m4a"
    )

    identity = ArtifactRoots.of(archive)
    assert resolve_audio_path(identity, "audio/both.m4a") == archive / "audio" / "both.m4a"
    assert resolve_audio_path(identity, "audio/fresh.m4a") is None


def test_resolve_audio_path_rejects_an_escaping_or_wrong_shaped_value_at_every_base(tmp_path):
    archive = tmp_path / "archive"
    artifact = tmp_path / "mount"
    for base in (archive, artifact):
        (base / "audio" / "sub").mkdir(parents=True)
        # Decoys: every refused value below would resolve if the guard were absent.
        (base / "escaped.m4a").write_bytes(b"escaped")
        (base / "audio" / "wrong.wav").write_bytes(b"wrong extension")
        (base / "audio" / "sub" / "deep.m4a").write_bytes(b"too deep")
        try:
            (base / "audio" / "link.m4a").symlink_to(base / "escaped.m4a")
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")

    roots = roots_for(archive, flag_value=str(artifact), environ={})
    identity = ArtifactRoots.of(archive)
    for declared in _REFUSED_AUDIO_VALUES:
        assert resolve_audio_path(roots, declared) is None
        assert resolve_audio_path(roots, declared, require_exists=False) is None
        assert resolve_audio_path(identity, declared) is None


def test_iter_audio_paths_preserves_base_then_candidate_order(tmp_path):
    archive = tmp_path / "archive"
    artifact = tmp_path / "mount"
    for base in (archive, artifact):
        (base / "audio").mkdir(parents=True)
    (artifact / "audio" / "second.m4a").write_bytes(b"artifact")
    (archive / "audio" / "first.m4a").write_bytes(b"archive")
    roots = roots_for(archive, flag_value=str(artifact), environ={})

    hits = list(iter_audio_paths(roots, ["audio/first.m4a", None, "audio/second.m4a"]))
    assert [(base, declared) for base, declared, _path in hits] == [
        (artifact, "audio/second.m4a"),
        (archive, "audio/first.m4a"),
    ]


def test_iter_audio_paths_reuses_generator_candidates_for_each_base(tmp_path):
    archive = tmp_path / "archive"
    artifact = tmp_path / "mount"
    for base in (archive, artifact):
        (base / "audio").mkdir(parents=True)
    (archive / "audio" / "fallback.m4a").write_bytes(b"archive")
    roots = roots_for(archive, flag_value=str(artifact), environ={})

    candidates = (value for value in ("audio/missing.m4a", "audio/fallback.m4a"))
    hits = list(iter_audio_paths(roots, candidates))
    assert [(base, declared) for base, declared, _path in hits] == [
        (archive, "audio/fallback.m4a"),
    ]


def test_keep_audio_precedence_table():
    assert KEEP_AUDIO_ENV_VAR == "BILI_KEEP_AUDIO"
    assert KEEP_AUDIO_DEFAULT is True
    cases = (
        (True, {KEEP_AUDIO_ENV_VAR: "0"}, True),
        (False, {KEEP_AUDIO_ENV_VAR: "1"}, False),
        (None, {KEEP_AUDIO_ENV_VAR: "1"}, True),
        (None, {KEEP_AUDIO_ENV_VAR: "0"}, False),
        (None, {KEEP_AUDIO_ENV_VAR: "yes"}, KEEP_AUDIO_DEFAULT),
        (None, {KEEP_AUDIO_ENV_VAR: ""}, KEEP_AUDIO_DEFAULT),
        (None, {}, KEEP_AUDIO_DEFAULT),
    )
    for flag_value, environ, expected in cases:
        assert resolve_keep_audio(flag_value, environ) is expected
