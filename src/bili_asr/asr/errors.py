"""Errors implementation."""

from __future__ import annotations




class ASRDependencyError(RuntimeError):
    """The optional ASR dependency (transformers/torch) is missing or unusable."""


class ASRRuntimeCompatibilityError(ASRDependencyError):
    """Installed ASR modules cannot import the required model classes."""


class ASRModelError(RuntimeError):
    """The configured checkpoint could not be loaded, or the run could not transcribe."""


class AudioDecodeError(RuntimeError):
    """The audio file exists but the decoder refused it.

    Distinct from :class:`ASRDependencyError`: the dependencies are present, the *file* is the
    problem (unreadable, or a codec ``ffmpeg`` was not built with). Workflow
    attempts retain a bounded error classification for the failed job.

    What this class does **not** cover, stated because the difference matters for an archive whose
    download stage can be interrupted: a file truncated in the middle decodes **silently short**
    rather than raising.  Measured on a faststart ``.m4a`` cut to 90/70/50/30 % of its bytes, which
    is the shape an interrupted download leaves when ``moov`` precedes ``mdat``: the decode returned
    53.9/41.6/29.4/17.1 s of a 60 s recording, exit status 0, no stderr.  Nothing here compares the
    decoded duration with the row's ``duration_s``, so a short read is not detected — the same was
    true of the ``librosa`` path this replaced, so this is a pre-existing limit rather than a
    regression, but the reader should not infer from this class that truncation is caught.
    """
