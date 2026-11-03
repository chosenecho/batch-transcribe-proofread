"""Progress reporting survives revoked transports without hiding code failures."""

import os

import pytest

from bili_asr.asr.progress import _progress, set_progress_hook


@pytest.fixture(autouse=True)
def clear_progress_hook():
    set_progress_hook(None)
    try:
        yield
    finally:
        set_progress_hook(None)


def test_revoked_transport_disables_progress_after_the_first_failed_send():
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    attempts = []

    def send(phase):
        attempts.append(phase)
        os.write(write_fd, phase.encode())

    try:
        set_progress_hook(send)
        _progress("decoded")
        _progress("aligned")
        assert attempts == ["decoded"]

        replacement = []
        set_progress_hook(replacement.append)
        _progress("chunk_complete")
        assert replacement == ["chunk_complete"]
    finally:
        os.close(read_fd)


def test_progress_does_not_suppress_non_transport_failures():
    def invalid_observer(_phase):
        raise ValueError("invalid progress observer")

    set_progress_hook(invalid_observer)
    with pytest.raises(ValueError, match="invalid progress observer"):
        _progress("decoded")
