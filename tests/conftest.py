import itertools
import os
import shutil
import sys
import tempfile
import time
import traceback
import types
import uuid

import pytest

# These suites exercise product paths removed when SQLite workflow became the
# only execution control plane. Keep their history in the repository, but do
# not collect tests whose imports and assertions target those retired modules.
collect_ignore = [
    "test_archive_md.py",
    "test_archive_producer_health.py",
    "test_artifact_root_readers.py",
    "test_artifact_root_writes.py",
    "test_asr_coverage_attestation.py",
    "test_asr_supervisor.py",
    "test_audio.py",
    "test_audio_budget.py",
    "test_audio_inventory.py",
    "test_audio_pipeline_reliability.py",
    "test_audio_usage_tracking.py",
    "test_batch_hotwords_record.py",
    "test_batch_run_records.py",
    "test_campaign.py",
    "test_cli_artifact_root.py",
    "test_cli_asr.py",
    "test_cli_asr_writeback_refusal.py",
    "test_cli_derive_manifest.py",
    "test_cli_exit_contract.py",
    "test_cli_pilot.py",
    "test_cli_publish_transcripts.py",
    "test_cli_queue_source.py",
    "test_cli_registry.py",
    "test_concurrency_gate.py",
    "test_coordinator.py",
    "test_coverage_manifest_projection.py",
    "test_coverage_report.py",
    "test_derived_queue_chain.py",
    "test_export.py",
    "test_integrity.py",
    "test_integrity_finding_classes.py",
    "test_integrity_journal_bootstrap.py",
    "test_long_live.py",
    "test_manifest.py",
    "test_manifest_derivation.py",
    "test_mixed_outcome_contract.py",
    "test_page_pipeline.py",
    "test_persistence_scale.py",
    "test_pilot_select.py",
    "test_pipeline_recovery_quality.py",
    "test_pipeline_writeback_safety.py",
    "test_publication_supervisor.py",
    "test_publish_audio_only.py",
    "test_publish_bad_candidates.py",
    "test_publish_candidate_limits.py",
    "test_publish_pending.py",
    "test_publish_read_failures.py",
    "test_published_projection_readers.py",
    "test_quality_path_probe_cost.py",
    "test_quality_raw_confinement.py",
    "test_raw_sidecar_readers.py",
    "test_run_ledger.py",
    "test_run_ledger_diagnostics.py",
    "test_runtime_burndown.py",
    "test_scheduler.py",
    "test_search_index.py",
    "test_stem_contract.py",
    "test_storage_queue_gaps.py",
    "test_storage_queue_writes.py",
    "test_subtitles.py",
    "test_transcript_adoption.py",
    "test_cli_help.py",
    "test_live_subtitle_cli_smoke.py",
    "test_proofread_routes.py",
    "test_subtitle_cli.py",
    "test_subtitle_e2e.py",
]

# --- sys.path preamble -------------------------------------------------------
# The test interpreter is intentionally NOT the pip-installed artifact (see
# tests/installed_cli.py for the console-script lane): the suite loads the
# source tree via sys.path. These are the only two inserts in the suite — one
# per path, declared once here; do not re-add ad-hoc ``sys.path.insert`` calls
# in individual test modules.
#
# The package sources live in ``src/``:
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
# ``scripts/`` (verify_baseline, check_asr_env, ...) is an importable namespace
# package rooted at the repo; without the repo root on ``sys.path`` the two test
# modules that import it fail collection with ``ModuleNotFoundError: No module
# named 'scripts'``. The repo root also makes the ``tests/`` directory itself
# importable, so sibling test modules can be imported by name.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.forensic_log import open_forensic_log


@pytest.fixture(scope="session")
def historical_archive_template(tmp_path_factory):
    from tests.fixtures.migrated_archive import build_historical_archive
    return build_historical_archive(tmp_path_factory)


@pytest.fixture(scope="session")
def preserved_archive_template(tmp_path_factory, historical_archive_template):
    from tests.fixtures.migrated_archive import build_preserved_archive
    return build_preserved_archive(tmp_path_factory, historical_archive_template)

_TEST_TMP_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test-tmp"))
_counter = itertools.count()

# Error-report forensics. The full suite has been observed red in some runs and
# green in others on an unchanged tree (recorded 261 / 0 / 196 / 0 errors), and
# the red runs left nothing behind: pytest's default report goes to the
# terminal, so the cause died with the scrollback. These constants pin the
# evidence to a stable, gitignored path (.tb/ is already an ignored convention)
# so the *next* red is diagnosable instead of merely counted.
#
# The phases the hook below *cannot* see: a **collection** error arrives as
# `pytest_collectreport(report)`, and that report carries no `item`, so it can
# never reach a `pytest_runtest_makereport` hook no matter how the guard reads.
# A collection red therefore still writes nothing here; catching one needs a
# collector-report hook, not a wider phase guard.
_TRACEBACK_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".tb"))
_TRACEBACK_PATH = os.path.join(_TRACEBACK_DIR, "errors.log")


def _environment_facts() -> str:
    """One block of the facts that discriminate environment from repo causes."""
    facts = [
        f"cwd={os.getcwd()}",
        f"sys.executable={sys.executable}",
        f"tmpdir={tempfile.gettempdir()}",
    ]
    try:
        import bili_asr

        # Resolving outside this worktree is the known invocation trap: a red
        # that comes from the wrong tree is a false lead.
        facts.append(f"bili_asr={bili_asr.__file__}")
    except Exception as exc:  # pragma: no cover - diagnostic path only
        facts.append(f"bili_asr=<unimportable: {type(exc).__name__}: {exc}>")
    for label, path in (("tmp", tempfile.gettempdir()), ("cwd", os.getcwd())):
        try:
            usage = shutil.disk_usage(path)
            facts.append(
                f"disk[{label}]={usage.free} free / {usage.total} total bytes"
            )
        except OSError as exc:  # pragma: no cover - diagnostic path only
            facts.append(f"disk[{label}]=<unavailable: {exc}>")
    return "\n".join(facts)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Persist the traceback of any setup or teardown error to a stable path.

    Only the ``setup`` and ``teardown`` phases are recorded — the phases whose
    reports count into pytest's ``errors`` total. Setup is the phase the
    recorded cross-file, large-and-varying-count red hit, and teardown errors
    are indistinguishable from "the fixture never ran" for the same reason.
    A **collection** error carries no ``item`` and is therefore invisible to
    this hook; see the module-level note above. Reporting must never itself
    fail the run, so every step is guarded.
    """
    outcome = yield
    if call.when not in ("setup", "teardown"):
        return
    report = outcome.get_result()
    if not report.failed:
        return
    try:
        # Parent directories and the final file are opened without following
        # links. A refusal leaves the original test failure in charge.
        with open_forensic_log(_TRACEBACK_PATH) as handle:
            handle.write(f"{'=' * 78}\n")
            handle.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {call.when.upper()} ERROR\n"
            )
            handle.write(f"nodeid: {item.nodeid}\n")
            handle.write(f"fixture info: {getattr(item, 'fixturenames', None)}\n")
            handle.write(f"location: {item.location}\n")
            handle.write(f"{_environment_facts()}\n")
            if report.longrepr is not None:
                handle.write(f"{report.longrepr}\n")
            else:  # pragma: no cover - pytest always populates longrepr for failures
                handle.write(f"{traceback.format_exc()}\n")
    except OSError as exc:  # pragma: no cover - diagnostic path only
        try:
            print(f"conftest: {call.when} error not persisted: {exc}",
                  file=sys.stderr)
        except Exception:
            pass
    except Exception:  # pragma: no cover - never mask the original failure
        pass


@pytest.fixture
def tmp_root():
    """Temp dir for ManifestStore tests.

    Notes on this environment: the sandbox denies directory creation under
    %TEMP% from Python and breaks pytest's tmpdir plugin cleanup, so we use a
    self-managed workspace-local temp dir created and removed in-process.

    The name ends in a UUID so a leftover dir from a killed run cannot collide
    with the next one: with only PID+counter in the name, PID reuse reproduced
    an intermittent FileExistsError setup red on an unchanged tree.
    """
    os.makedirs(_TEST_TMP_BASE, exist_ok=True)
    path = os.path.join(
        _TEST_TMP_BASE,
        f"manifest-test-{os.getpid()}-{next(_counter)}-{uuid.uuid4().hex}",
    )
    os.makedirs(path)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class _NoOpContext:
    """A context manager that does nothing, standing in for a torch grad mode."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc_info: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def mock_torch(monkeypatch):
    """A torch stand-in that reports CUDA is available and no-ops the grad modes.

    Every test that drives the ASR path with mocked models needs torch to be importable but does
    not need it to be real.  The boundary runs its decodes and its alignment inside
    ``torch.inference_mode()``, so the stand-in has to carry that context manager as well as
    ``cuda.is_available`` — without it every ASR row fails at the first decode with an
    ``AttributeError`` that the coordinator records as a per-row failure.
    """

    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: True),
        inference_mode=lambda *args, **kwargs: _NoOpContext(),
        no_grad=lambda *args, **kwargs: _NoOpContext(),
        # A ``Tensor`` class so third-party probes answer False instead of raising.  The boundary
        # never reads it; ``scipy`` does — its array-API compatibility layer tests a candidate with
        # ``issubclass(candidate, torch.Tensor)`` and reaches ``getattr(torch, "Tensor")`` on
        # import, so a stand-in without that attribute turns the probe into an ``AttributeError``
        # that fails a test for a reason unrelated to the code under test.  This was originally
        # attributed to ``librosa``'s resampler; scipy runs the same probe on its own, verified
        # 2026-09-27 in this venv with no librosa involved.
        Tensor=type("Tensor", (), {}),
        # The dtypes the loader passes to ``from_pretrained`` (``asr.py``).  They are not read by any
        # test today — every test stubs the model factory — but a stand-in missing them turns the
        # next test that touches the real loader into an ``AttributeError`` that reads like a
        # product defect.  Cheap to carry, and the failure it prevents is expensive to diagnose.
        bfloat16="bfloat16",
        float16="float16",
        float32="float32",
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)


@pytest.fixture
def enable_content_publication_for_tests(monkeypatch):
    """Exercise the optional compliance policy and its publication opt-in."""
    monkeypatch.setenv("BILI_COMPLIANCE_ENABLED", "1")
    monkeypatch.setenv("BILI_PUBLICATION_ENABLED", "1")


@pytest.fixture(autouse=True)
def enable_content_publication_for_legacy_flows(monkeypatch, request):
    """Keep legacy compliance tests strict without changing live probes.

    Live smoke tests are an explicit operator mode.  Their process
    environment is the source of truth for access policy (including a real
    operator reference), so this legacy compatibility fixture must not inject
    strict-mode variables into them.  All other historical tests retain the
    strict policy they were written to exercise.
    """
    if request.node.get_closest_marker("live_smoke") is not None:
        return
    monkeypatch.setenv("BILI_COMPLIANCE_ENABLED", "1")
    monkeypatch.setenv("BILI_PUBLICATION_ENABLED", "1")


# --- opt-in gates (plan 008-pytest-markers) ----------------------------------
# One central env-var -> marker mapping for the whole suite. Each opt-in test
# takes the ``opt_in_gate`` fixture and supplies its existing skip reason in
# the marker's ``skip_reason`` keyword; the default run's skip text stays
# byte-identical to the per-file gates this replaces, while ``-m live_smoke`` / ``-m scale`` can now
# select and CI-gate the opted-in tests.
OPT_IN_ENV_VARS = {"live_smoke": "BILI_LIVE_SMOKE", "scale": "BILI_SCALE"}


@pytest.fixture
def opt_in_gate(request):
    """Central opt-in gate for live/scale tests: skip unless the env knob is ``1``.

    The fixture reads the calling test's ``live_smoke``/``scale`` marker, maps
    it through :data:`OPT_IN_ENV_VARS`, and skips with the reason the test
    supplies in the marker's ``skip_reason`` keyword. A new caller without a
    custom reason receives a safe default. Semantics stay per-file: only
    the documented ``1`` opts in, and the knob names do not change.
    """

    for marker_name, env_var in OPT_IN_ENV_VARS.items():
        if request.node.get_closest_marker(marker_name) is not None:
            break
    else:  # pragma: no cover - contract misuse fails loudly instead of passing silently
        raise RuntimeError(
            "opt_in_gate requires a live_smoke or scale marker on the test"
        )
    marker = request.node.get_closest_marker(marker_name)
    if os.environ.get(env_var) != "1":
        reason = marker.kwargs.get(
            "skip_reason", f"{marker_name} test is opt-in: set {env_var}=1 to run it"
        )
        pytest.skip(reason)
    return env_var, marker


def reuse_line(captured, command):
    """The one ``model constructions=`` line, or a failure explaining its absence.

    Shared by the CLI tests that drive the in-process loops (``asr``,
    ``pilot``): the printed line's shape is one contract (D2.6), so the
    assertion that reads it back is one helper rather than a copy per file.
    """
    lines = [
        line for line in captured.err.splitlines() if "model constructions=" in line
    ]
    assert len(lines) == 1, (
        f"{command} printed {len(lines)} reuse line(s), expected exactly one: "
        f"{captured.err!r}"
    )
    assert lines[0].startswith(f"{command}: "), lines[0]
    return lines[0]
