from __future__ import annotations

from bili_asr import diagnostics


def test_write_stderr_does_not_dirty_real_text_wrapper_when_fd_write_fails(
    tmp_path, monkeypatch
) -> None:
    """A dead stderr descriptor must not leave a buffered flush failure behind."""

    path = tmp_path / "stderr.log"
    stream = path.open("w", encoding="utf-8")
    try:
        monkeypatch.setattr(diagnostics.sys, "stderr", stream)

        def fail_write(_fd: int, _data: bytes) -> int:
            raise OSError("broken pipe")

        monkeypatch.setattr(diagnostics.os, "write", fail_write)
        diagnostics.write_stderr("diagnostic")

        # The implementation writes directly to the descriptor, so a failed
        # descriptor write is swallowed without poisoning TextIOWrapper's
        # buffer.  A later flush/close therefore remains clean (exit status 0).
        stream.flush()
    finally:
        stream.close()

    assert path.read_text(encoding="utf-8") == ""
