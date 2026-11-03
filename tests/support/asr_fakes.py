"""Test doubles for the ASR boundary's model seam.

The boundary talks to exactly four objects — an ASR processor and model, an aligner processor and
model — plus the audio reader.  Every suite that drives the ASR path through the CLI gets those
without a GPU or a checkpoint: the model factory is replaced here, and ``tests/conftest.py``'s
``mock_torch`` supplies the tensor surface.  The audio reader is **not** replaced — it is exercised
for real, so the light members of the ASR stack (``numpy``/``soundfile``/``soxr``) are declared as
test dependencies rather than stubbed.  The heavy three (``transformers``/``accelerate``/``torch``)
stay optional.

Usage:

    fakes = install(monkeypatch)          # factory + audio read/write patched
    assert main(["asr", "--pending", ...]) == 0
    assert fakes.constructions            # what the runner built, in order

``install`` returns the :class:`ModelSet` it hands the runner, so a test can assert on what the
processor was asked to transcribe (``fakes.processor.audio``) or on the text it produced.
"""

from __future__ import annotations

import bili_asr.asr.audio as _module_asr_audio
import bili_asr.asr.runner as _module_asr_runner


#: A short two-sentence utterance: long enough that both cue rules (mark closure, fragment
#: absorption) produce a stable shape, short enough to read in a failure message.
DEFAULT_TEXT = "今天讲两件事。明天我们接着讲第三件事。"


class Tokens:
    """Just enough of a tensor: ``[:, n:]``, ``.shape``, ``.sum(-1).max()`` and ``float()``."""

    def __init__(self, length: int, shape: tuple[int, int] | None = None) -> None:
        self.length = length
        self.shape = shape or (1, length)

    def __getitem__(self, key: tuple) -> "Tokens":
        start = key[1].start or 0
        return Tokens(self.length - start, (1, self.length - start))

    def __float__(self) -> float:
        return float(self.length)

    def sum(self, *args, **kwargs) -> "Tokens":
        return self

    def max(self, *args, **kwargs) -> "Tokens":
        return self


class Inputs(dict):
    def to(self, *args, **kwargs) -> "Inputs":
        return self


class Processor:
    """Records every transcription request; returns canned text in all three decode formats."""

    def __init__(self, text: str = DEFAULT_TEXT, language: str = "Chinese") -> None:
        self.text = text
        self.language = language
        self.audio: list[object] = []
        self.prompts: list[str | None] = []

    def apply_transcription_request(self, audio=None, language=None, prompt=None) -> Inputs:
        self.audio.append(audio)
        self.prompts.append(prompt)
        return Inputs({
            "input_ids": Tokens(8),
            "input_features_mask": Tokens(100 * 10, (1, 100 * 10)),
        })

    def decode(self, tokens, return_format="raw", **kwargs):
        if return_format == "raw":
            return [f"language {self.language}<asr_text>{self.text}<|im_end|>"]
        if return_format == "parsed":
            return [{"language": self.language, "transcription": self.text}]
        return [self.text]


class Model:
    device = "cpu"
    dtype = None

    def generate(self, **inputs) -> Tokens:
        return Tokens(12)


class AlignerProcessor:
    def __init__(self, units: list[dict] | None = None) -> None:
        self.units = units if units is not None else units_for(DEFAULT_TEXT)
        self.transcripts: list[str] = []

    def prepare_forced_aligner_inputs(self, audio=None, transcript=None, language=None):
        self.transcripts.append(transcript or "")
        return Inputs({"input_ids": Tokens(16)}), [[u["text"] for u in self.units]]

    def decode_forced_alignment(self, **kwargs):
        return [list(self.units)]


class Aligner:
    device = "cpu"
    dtype = None
    config = type("Config", (), {"timestamp_token_id": 151705})()

    def __call__(self, **inputs):
        return type("Out", (), {"logits": Tokens(16)})()


class ModelSet:
    """The four objects a runner owns, in the shape ``_load_qwen_models`` returns."""

    def __init__(self, text: str = DEFAULT_TEXT, units: list[dict] | None = None) -> None:
        self.processor = Processor(text)
        self.model = Model()
        self.aligner_processor = AlignerProcessor(units)
        self.aligner = Aligner()

    @property
    def texts(self) -> list[str]:
        """Every transcript the aligner was asked to time."""

        return list(self.aligner_processor.transcripts)


def units_for(text: str, step: float = 0.4) -> list[dict]:
    """One timed unit per character of ``text`` — marks excluded, as the aligner really returns."""

    return [
        {"text": char, "start_time": index * step, "end_time": index * step + step - 0.05}
        for index, char in enumerate(text)
        if char not in "。！？!?，、；：,;: "
    ]


def _patch_audio(monkeypatch, seconds: float, reads: list[str] | None,
                 fail_when=None) -> None:
    """Make the reader work without a real audio file.

    The boundary reads and chunks audio, so it needs numpy, soundfile and soxr — the three light
    members of the ``[asr]`` extra.  All three are declared as test dependencies, so the ASR path is
    exercised on every host rather than skipped where they are absent; only the heavy three
    (``transformers``/``accelerate``/``torch``) are left to the doubles.

    ``reads`` collects the path of every recording the boundary opened.  That is where a row is
    identified now: the boundary hands the *model* decoded waveform chunks, so
    a test that needs to tell rows apart must hook the read — and ``fail_when`` turns that into a
    per-row failure, which is what the old ``generate(**kwargs)`` doubles did with ``kwargs["input"]``.
    """

    import numpy as np
    import soundfile as sf

    samples = np.zeros(int(16000 * seconds), dtype="float32")

    def resolve(path: str) -> str:
        """The file a path names, dereferencing a descriptor while it is still open."""

        if str(path).startswith("/proc/self/fd/"):
            try:
                import os

                return os.readlink(str(path))
            except OSError:  # pragma: no cover - the descriptor is gone
                return str(path)
        return str(path)

    def read(path, *args, **kwargs):
        if reads is not None:
            reads.append(resolve(path))
        if fail_when is not None and fail_when(resolve(path)):
            from bili_asr.asr.errors import ASRModelError

            raise ASRModelError("model failed")
        return samples, 16000

    monkeypatch.setattr(sf, "read", read)
    monkeypatch.setattr(sf, "write", lambda *args, **kwargs: None)


def install(monkeypatch, *, text: str = DEFAULT_TEXT, seconds: float = 3.0,
            constructions: list[dict] | None = None, reads: list[str] | None = None,
            fail_when=None, raises: BaseException | None = None) -> ModelSet:
    """Patch the boundary's factory with a canned model set, plus the audio reader.

    ``constructions`` is filled with the factory's kwargs, one entry per model set the runner built —
    the observable the CLI's reuse line reports.  ``reads`` and ``fail_when`` are documented on
    :func:`_patch_audio`.  ``raises`` makes every construction fail *after* being recorded, which is
    the shape a test needs when it asserts how many attempts a broken checkpoint cost.

    Descriptor materialization is switched off: the double reads the path it is handed, and the
    behaviour of the real copy is covered by its own unit test in ``test_asr_qwen.py``.
    """

    from bili_asr import asr

    _patch_audio(monkeypatch, seconds, reads, fail_when)
    model_set = ModelSet(text)

    def factory(**kwargs):
        if constructions is not None:
            constructions.append(dict(kwargs))
        if raises is not None:
            raise raises
        return model_set

    monkeypatch.setenv("BILI_ASR_DEVICE", "cpu")
    monkeypatch.setattr(_module_asr_audio, "_materialize_input", lambda path: (path, None))
    monkeypatch.setattr(_module_asr_runner, "_load_qwen_models", factory)
    return model_set


def raising(monkeypatch, exception: BaseException, *,
            constructions: list[dict] | None = None) -> None:
    """Patch the factory with one that always raises (a missing extra, a bad checkpoint)."""

    install(monkeypatch, constructions=constructions, raises=exception)


def forbidden(monkeypatch) -> None:
    """Patch the factory with one whose use is a test failure (the row must never reach ASR)."""

    from bili_asr import asr

    _patch_audio(monkeypatch, 3.0, None, None)

    def factory(**kwargs):
        raise AssertionError("ASR must not run for this row")

    monkeypatch.setenv("BILI_ASR_DEVICE", "cpu")
    monkeypatch.setattr(_module_asr_audio, "_materialize_input", lambda path: (path, None))
    monkeypatch.setattr(_module_asr_runner, "_load_qwen_models", factory)
