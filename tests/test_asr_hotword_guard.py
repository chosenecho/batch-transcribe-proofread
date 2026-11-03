"""Evidence-guard tests for the 2026-09-28 hotword governance ruling.

Plan 20260928-hotword-injection-governance; closes residual
``20260922-proofread-wave R1`` (the hotword list as an insertion source).

The guard's contract, in one place:

* a token enters the inference prompt only if it occurs in the run's own
  first-pass transcript or the paired AI-subtitle text — pure string
  matching, no model calls;
* the empty shipped list is the pending-measurement state, not a guard
  result: it seeds nothing and drops nothing;
* tokens with no evidence occurrence are dropped and reported under the
  ``hotword_dropped_no_evidence`` ledger fact;
* the runner provenance records the *effective* (surviving) list, so the
  archive frontmatter always names what actually reached the prompt;
* a run whose every token is dropped is indistinguishable from a run that
  never configured hotwords: the prompt is ``None`` and the
  ``hotword_dropped_no_evidence`` key is absent (backwards-quiet).

The first-pass rebuild the guard depends on is the coordinator's/CLI's
responsibility; the runner itself stays stateless about *where* the evidence
came from (its evidence argument is the caller's declaration).
"""
from __future__ import annotations

import bili_asr.asr.config as _module_asr_config
import bili_asr.asr.constants as _module_asr_constants
import bili_asr.asr.hotwords as _module_asr_hotwords
import bili_asr.asr.runner as _module_asr_runner


import os

from bili_asr import asr


def _config(*hotwords: str) -> _module_asr_config.ASRConfig:
    return _module_asr_config.ASRConfig(model_name="m", device="cpu", hotwords=tuple(hotwords))


# ---------------------------------------------------------------------------------------
# evidence_guard_hotwords — the guard matrix.
# ---------------------------------------------------------------------------------------


def test_guard_admits_terms_present_in_evidence() -> None:
    admitted, dropped = _module_asr_hotwords.evidence_guard_hotwords(
        ("扬弃", "自在", "不存在的词"),
        ("这一段讲扬弃和自在的概念",),
    )
    assert admitted == ("扬弃", "自在")
    assert dropped == ("不存在的词",)


def test_guard_is_pure_containment_no_model_calls() -> None:
    # The guard must be plain string logic: any term occurring anywhere passes.
    admitted, _ = _module_asr_hotwords.evidence_guard_hotwords(("主义主义",), ("……主义主义……",))
    assert admitted == ("主义主义",)


def test_dedup_and_blank_filtering() -> None:
    admitted, dropped = _module_asr_hotwords.evidence_guard_hotwords(
        ("扬弃", "扬弃", " ", "自在"), ("扬弃",)
    )
    assert admitted == ("扬弃",)
    assert dropped == ("自在",)


def test_a_token_with_no_evidence_occurrence_is_dropped() -> None:
    kept, dropped = _module_asr_hotwords.filter_hotwords(("扬弃",), evidence_text="今天我们讲定在")
    assert kept == ()
    assert dropped == ("扬弃",)


def test_a_token_occurs_in_the_paired_subtitle_text_and_survives() -> None:
    # The transcript shows the homophone; the paired AI subtitle carries the
    # intended term — exactly the seeding case the guard exists to admit.
    kept, dropped = _module_asr_hotwords.filter_hotwords(("攻势",), evidence_text="公式是国际的")
    assert kept == ()
    assert dropped == ("攻势",)
    kept, dropped = _module_asr_hotwords.filter_hotwords(
        ("攻势",), evidence_text="公式是国际的", paired_subtitle_text="这里讲攻势"
    )
    assert kept == ("攻势",)
    assert dropped == ()


def test_surviving_tokens_keep_the_configured_order() -> None:
    kept, dropped = _module_asr_hotwords.filter_hotwords(("定在", "此在", "扬弃"), evidence_text="扬弃定在")
    assert kept == ("定在", "扬弃")
    assert dropped == ("此在",)


def test_duplicates_are_de_duplicated_in_operator_order() -> None:
    kept, dropped = _module_asr_hotwords.filter_hotwords(("定在", "定在", "此在"), evidence_text="定在")
    assert kept == ("定在",)
    assert dropped == ("此在",)


def test_latin_script_tokens_match_by_casefolded_substring() -> None:
    kept, dropped = _module_asr_hotwords.filter_hotwords(
        ("International", "Employment", "Tribunal"),
        evidence_text="the international employment tribunal is fake",
    )
    assert kept == ("International", "Employment", "Tribunal")
    assert dropped == ()


def test_the_empty_list_stays_empty_and_reports_nothing_dropped() -> None:
    kept, dropped = _module_asr_hotwords.filter_hotwords((), evidence_text="任何文本都可以")
    assert kept == ()
    assert dropped == ()


def test_no_evidence_text_drops_every_token() -> None:
    kept, dropped = _module_asr_hotwords.filter_hotwords(("扬弃", "定在"), evidence_text=None)
    assert kept == ()
    assert dropped == ("扬弃", "定在")


def test_a_term_occurring_only_inside_a_longer_word_is_not_evidence() -> None:
    # Substring matching is whole-string occurrence; a term must appear as a
    # run of characters, and CJK text carries no word delimiters to consult.
    kept, dropped = _module_asr_hotwords.filter_hotwords(("马恩",), evidence_text="马克思恩格斯讲过")
    assert kept == ()
    assert dropped == ("马恩",)


def test_the_guard_needs_no_model_or_torch() -> None:
    # The guard is pure string logic: running it must not pull the model stack
    # into the process.  (The test runner itself may already hold torch; what
    # matters is that the guard adds nothing.)
    import sys

    before = set(sys.modules)
    _module_asr_hotwords.filter_hotwords(("扬弃",), evidence_text="扬弃")
    _module_asr_hotwords.evidence_guard_hotwords(("定在",), ("定在",))
    added = set(sys.modules) - before
    assert not any(name.split(".")[0] in ("torch", "transformers") for name in added), (
        f"the guard path must stay pure string logic, pulled in: {sorted(added)}"
    )


# ---------------------------------------------------------------------------------------
# The default list: empty-with-guard-on — no speculative seeding while the
# keep/drop measurement is pending operator re-run.
# ---------------------------------------------------------------------------------------


def test_config_default_seeds_nothing() -> None:
    cfg = _module_asr_config.ASRConfig(model_name="m")
    assert cfg.hotwords == ()
    assert _module_asr_constants.DEFAULT_HOTWORDS == ()


def test_the_operator_extra_knob_still_appends_terms() -> None:
    import os

    config = _module_asr_config.ASRConfig(
        model_name="m",
        device="cpu",
        hotwords=_module_asr_constants.DEFAULT_HOTWORDS + _module_asr_hotwords._extra_hotwords("新词,攻势"),
    )
    assert config.hotwords == ("新词", "攻势")
    saved = os.environ.get(_module_asr_constants.ASR_HOTWORDS_ENV_VAR)
    os.environ[_module_asr_constants.ASR_HOTWORDS_ENV_VAR] = "新词,攻势"
    try:
        assert _module_asr_config.default_config().hotwords == ("新词", "攻势")
    finally:
        if saved is None:
            os.environ.pop(_module_asr_constants.ASR_HOTWORDS_ENV_VAR, None)
        else:
            os.environ[_module_asr_constants.ASR_HOTWORDS_ENV_VAR] = saved


# ---------------------------------------------------------------------------------------
# The runner: provenance names the effective list; the drop fact is per-run.
# ---------------------------------------------------------------------------------------


def test_the_provenance_records_the_effective_hotword_list() -> None:
    runner = _module_asr_runner.ASRRunner(_config("扬弃", "此在"))
    runner.set_hotword_evidence(evidence_text="我们扬弃定在", paired_subtitle_text=None)
    assert runner.provenance()["hotwords"] == "扬弃"


def test_a_run_without_evidence_leaves_the_configured_list_untouched() -> None:
    # A runner that never received evidence predates the guard: its configured
    # list is the prompt vocabulary verbatim (no retroactive filtering).
    runner = _module_asr_runner.ASRRunner(_config("扬弃", "此在"))
    assert runner.provenance()["hotwords"] == "扬弃,此在"
    assert "hotword_dropped_no_evidence" not in runner.provenance()


def test_a_run_with_dropped_tokens_records_the_fact_once_as_csv() -> None:
    runner = _module_asr_runner.ASRRunner(_config("扬弃", "此在", "定在"))
    runner.set_hotword_evidence(evidence_text="定在", paired_subtitle_text=None)
    provenance = runner.provenance()
    assert provenance["hotwords"] == "定在"
    assert provenance["hotword_dropped_no_evidence"] == "扬弃,此在"


def test_dropped_hotwords_are_redacted_in_real_archive_products(tmp_path) -> None:
    import json

    from bili_asr.archive import archive_bundle_complete, write_archive

    secrets = (
        "https://private.example/models",
        "C:\\private\\models",
        "token_private_value",
    )
    runner = _module_asr_runner.ASRRunner(_config("保留", "扬弃", *secrets))
    runner.set_hotword_evidence(evidence_text="保留", paired_subtitle_text=None)
    provenance = runner.provenance()
    assert provenance["hotwords"] == "保留"
    assert provenance["hotword_dropped_no_evidence"] == (
        "扬弃,[redacted],[redacted],[redacted]"
    )
    entry = {
        "bvid": "BV1redact", "work_id": "BV1redact:p0", "page_index": 0,
        "cid": 17, "title": "redaction", "duration_s": 1,
    }
    paths = write_archive(
        tmp_path, entry, [{"start": 0, "end": 1, "text": "保留"}],
        source="asr", asr_provenance=provenance,
    )
    assert archive_bundle_complete(tmp_path, paths)
    raw_text = (tmp_path / paths["raw_path"]).read_text(encoding="utf-8")
    md = (tmp_path / paths["md_path"]).read_text(encoding="utf-8")
    assert json.loads(raw_text)["provenance"] == provenance
    assert 'asr_hotword_dropped_no_evidence: "扬弃,[redacted]' in md
    for secret in secrets:
        assert secret not in md
        assert secret not in raw_text
        assert secret not in json.dumps(json.loads(raw_text), ensure_ascii=False)


def test_a_run_with_every_token_dropped_sends_no_prompt_but_records_the_fact() -> None:
    # Every token dropped == empty effective list == no prompt; the drop fact is
    # still recorded (the run *had* configured tokens, and the ledger must say
    # where they went).  Backwards-quietness is about the *zero-hotword* run.
    runner = _module_asr_runner.ASRRunner(_config("扬弃"))
    runner.set_hotword_evidence(evidence_text="", paired_subtitle_text="")
    provenance = runner.provenance()
    assert provenance["hotwords"] == ""
    assert provenance["hotword_dropped_no_evidence"] == "扬弃"


def test_a_zero_hotword_run_is_backwards_quiet() -> None:
    # A run that never configured hotwords stays shape-identical to the
    # pre-guard state: no prompt, no drop fact.
    runner = _module_asr_runner.ASRRunner(_config())
    runner.set_hotword_evidence(evidence_text="anything", paired_subtitle_text=None)
    provenance = runner.provenance()
    assert provenance["hotwords"] == ""
    assert "hotword_dropped_no_evidence" not in provenance


def test_set_hotword_evidence_is_idempotent_for_the_same_evidence() -> None:
    runner = _module_asr_runner.ASRRunner(_config("扬弃", "此在"))
    runner.set_hotword_evidence(evidence_text="扬弃", paired_subtitle_text=None)
    first = runner.provenance()
    runner.set_hotword_evidence(evidence_text="扬弃", paired_subtitle_text=None)
    assert runner.provenance() == first


# ---------------------------------------------------------------------------------------
# The first-pass rebuild: the second pass is seeded only with tokens the
# first pass produced, through the runner's own evidence setter.
# ---------------------------------------------------------------------------------------


def test_the_runner_rebuild_returns_kept_tokens_for_the_second_pass() -> None:
    # QC F2 contract: the return is the KEPT vocabulary; an empty kept list
    # means pass 2 cannot change the output and must not run.
    runner = _module_asr_runner.ASRRunner(_config("扬弃", "此在", "定在"))
    kept = runner.rebuild_hotwords_from_first_pass("我们要扬弃这个定在")
    assert kept == ["扬弃", "定在"]
    provenance = runner.provenance()
    assert provenance["hotwords"] == "扬弃,定在"
    assert provenance["hotword_dropped_no_evidence"] == "此在"


def test_a_second_rebuild_replaces_the_previous_fact() -> None:
    runner = _module_asr_runner.ASRRunner(_config("扬弃", "此在"))
    runner.rebuild_hotwords_from_first_pass("扬弃")
    assert runner.provenance()["hotword_dropped_no_evidence"] == "此在"
    runner.rebuild_hotwords_from_first_pass("此在")
    assert runner.provenance()["hotword_dropped_no_evidence"] == "扬弃"
    assert runner.provenance()["hotwords"] == "此在"
