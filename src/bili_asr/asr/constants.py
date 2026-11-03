"""Constants implementation."""

from __future__ import annotations

import re


DEFAULT_MODEL = "Qwen/Qwen3-ASR-1.7B-hf"


DEFAULT_ALIGNER_MODEL = "Qwen/Qwen3-ForcedAligner-0.6B-hf"


DEFAULT_TRANSCRIPT_LANGUAGE = "und"


_INSTALL_HINT = 'pip install -e ".[asr]"'


_RICH_TAG = re.compile(r"<\|[^|>]+\|>")


DEFAULT_CHUNK_SECONDS = 180.0


# ROCm can leave a forced-alignment kernel blocked indefinitely.  CUDA/ROCm
# workflow workers run ASR in a child process and this deadline is the hard
# boundary that lets the parent terminate that child and retry the job.
DEFAULT_INFERENCE_TIMEOUT_SECONDS = 1800.0


_CHUNK_SEARCH_EXPAND_S = 5.0


_CHUNK_MIN_WINDOW_MS = 100.0


_CHUNK_MIN_SECONDS = 0.5


SAMPLE_RATE = 16_000


COVERAGE_MIN = 0.97


COVERAGE_KEYS = ("decoded_s", "produced_s", "coverage", "coverage_min", "coverage_short")


COVERAGE_VERDICT_COVERED = "covered"


COVERAGE_VERDICT_SHORT = "short"


COVERAGE_VERDICT_NOT_EVALUABLE = "not-evaluable"


_MAX_NEW_TOKENS_PER_AUDIO_SECOND = 8


_MIN_NEW_TOKENS = 256


_SENTENCE_ENDINGS = "。！？!?"


_CUE_MAX_CHARS = 60


_CUE_MAX_GAP_SECONDS = 1.0


_CUE_MIN_CHARS = 6


_CUE_MIN_SECONDS = 1.0


_CUE_CLOSING_MARKS = "。！？!?，、；：,;:"


MAX_MODEL_LOAD_ATTEMPTS = 3


ASR_MODEL_ENV_VAR = "BILI_ASR_MODEL"


ASR_ALIGNER_ENV_VAR = "BILI_ASR_ALIGNER_MODEL"


ASR_MODEL_REVISION_ENV_VAR = "BILI_ASR_MODEL_REVISION"


ASR_MODEL_ID_ENV_VAR = "BILI_ASR_MODEL_ID"


ASR_DEVICE_ENV_VAR = "BILI_ASR_DEVICE"


ASR_LANGUAGE_ENV_VAR = "BILI_ASR_LANGUAGE"


ASR_HOTWORDS_ENV_VAR = "BILI_ASR_HOTWORDS"


ASR_CHUNK_SECONDS_ENV_VAR = "BILI_ASR_CHUNK_SECONDS"


ASR_INFERENCE_TIMEOUT_ENV_VAR = "BILI_ASR_INFERENCE_TIMEOUT_SECONDS"


DEFAULT_HOTWORDS: tuple[str, ...] = ()


MEASURED_HOTWORD_CANDIDATES: tuple[str, ...] = (
    "未明子", "主义主义", "拟态论", "国际劳工仲裁", "国际劳联", "马恩牌", "攻势", "智利",
    "根正苗红", "亚美利坚", "黑格尔", "海德格尔", "拉康", "齐泽克", "德勒兹", "康德",
    "观念论", "本体论", "现象学", "辩证法", "定在", "自为", "理念性",
    # The homophone class, added 2026-09-17 from the season run's own output
    # (workflow ``e2e-23191782-season-7686105``: 14 lectures, 25.2 h, 18 287
    # cues).  Each entry below is a term the model got *wrong* far more often
    # than right, and every one of them is the *exact homophone* of a common
    # word — which is why the decoder's prior wins and why the prompt is the
    # right lever here:
    #
    #   扬弃 (sublation)  10 correct vs 89 wrong (阳气 62, 洋气 27)  90 %
    #   自在 (in-itself)  40 vs 13 (子在)                            25 %
    #   变易 (becoming)    0 vs  7 (变异)                           100 %
    #   此在 (Dasein)      4 vs  3 (次在, 词在)                      43 %
    #   感性 (sensibility)12 vs  3 (感兴)                            20 %
    #   实存 (existence)  17 vs  3 (时存)                            15 %
    #
    # 扬弃 is the reason this block exists: it is the central operation of
    # Hegel's *Logic*, and these lectures read that book aloud, so the term is
    # spoken constantly — yet the decoder preferred the common word 阳气 nine
    # times out of ten (worst item: 《逻辑学》第二讲, 4 correct vs 57 wrong).
    # The control that makes this an argument rather than a hunch: the entries
    # already in this list that are equally homophone-prone are *error-free* on
    # the same audio (定在 145/0, 自为 34/0, 理念性 69/0).
    #
    # Evidence status, stated plainly, as for the Latin block below: the errors
    # above are measured, the *benefit* of these six is UNVERIFIED until the
    # same audio is re-transcribed.  A confidence-based fix was ruled out first
    # — the 78 mis-rendered cues score a median 0.776 against 0.812 for the
    # corpus, and only 1 of 78 falls at or below ``LOW_CONFIDENCE``, so the
    # model is confidently wrong and ``asr_low_confidence_at`` cannot find this
    # class.  Re-running one affected lecture with and without these entries is
    # the confirming measurement; like the Latin block's, that verification is
    # registered as an open residual rather than claimed here.
    "扬弃",
    "自在",
    "变易",
    "此在",
    "感性",
    "实存",
    # Latin-script terms the corpus actually speaks.  The Chinese-language model
    # fragments these into shards when they are missing from the prompt (measured
    # 2026-09-14 on the ten-video run: "International Employment Matters Tribunal"
    # came out as tryBUNAL / FOR EMP LOYMENT MAT TERS, and the ITEM/AITEM pair as
    # TEM / AITM / ITM).  The spelled-out phrase is listed together with its three
    # component words, so the decoder has the full form and each part of it.
    #
    # The bare acronyms ITEM and AITEM were **removed on 2026-09-17** after the
    # season run measured them doing harm of the kind they were added to prevent:
    # nine occurrences across the 14 archived lectures, e.g. "THE ITEMthat's the
    # question is anITEM ONE", "This is expressed in the finite on the AITEM",
    # "In accessible AITEM distance outside", "就是WHAT IS POSITIVE ITEM" — and
    # every one of them is the acronym capturing a neighbouring word rather than
    # a spoken initialism.  The spelled-out phrase stayed: it appears three times
    # and is genuine each time.  They are therefore deliberately **absent** from
    # this list, not overlooked — re-adding either needs its own measurement.
    #
    # Evidence status: the surface-form measurements are the **retired FunASR-Nano**
    # checkpoint's and do not carry over unmeasured; re-measuring this list under the
    # engine that ships, with insertions counted separately from recoveries, is T5.
    # That round ran 2026-09-26 on the frozen six-item corpus and returned PARTIAL:
    # every exercised term came out identical with and without the prompt (R=0, I=0),
    # and E=7 left the recovery clauses uninformative — so the list's benefit is still
    # unmeasured, and the absence of a falsifying result is not evidence of benefit.
    # Record: `iter-2026-09-qwen3-asr-closeout/guides/t5-hotword-measurement-results.md`.
    #
    # Two facts from the original block were dropped when this list was restored in
    # 83ba8d0 and are restored here, because nothing else in the repository carries
    # them.  (1) The harm measurement that justified the removal was taken on
    # `BV19hG56hEfV.p2`, which carries 5 of the 9 bad cues.  (2) The *benefit* side —
    # why these acronyms were ever added — was measured on `BV1eGJ46mEHQ`, and **that**
    # video is the one whose audio no longer exists, so the benefit cannot be re-measured
    # from here and must be taken from the counts above.  The asymmetry is the point:
    # the harm can be re-checked (that lecture's audio is one re-download away), the
    # benefit cannot.  Record: `plans/20260917-hotword-acronym-precision.md`, findings
    # D1 and D3.
    "International Employment Matters Tribunal",
    "International",
    "Employment",
    "Tribunal",
)


_FORBIDDEN_CREDENTIAL_MARKER = (
    r"(?:^|[^A-Za-z])(?:sessdata|cookie|token|password|secret|credential)(?![A-Za-z])"
)


_FORBIDDEN_VALUE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+.-]*://|\\\\|(?:^|[\\/])\\/|(?:^|[^A-Za-z])[A-Za-z]:[\\/]|"
    + _FORBIDDEN_CREDENTIAL_MARKER
    + r")",
    re.IGNORECASE,
)


_FORBIDDEN_LOCAL_SOURCE = _FORBIDDEN_VALUE


_FORBIDDEN_PROVENANCE = _FORBIDDEN_VALUE


_MODEL_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")


_DESCRIPTOR_PATH = re.compile(r"^/(?:proc/(?:self|\d+)/fd|dev/fd)/\d+$")


_MEL_FRAMES_PER_SECOND = 100.0
