"""Process-local, data-only observations of real ASR stage work."""

from collections.abc import Callable

_HOOK: Callable[[str], None] | None = None


def set_progress_hook(hook: Callable[[str], None] | None) -> None:
    global _HOOK
    _HOOK = hook


def _progress(phase: str) -> None:
    hook = _HOOK
    if hook is None:
        return
    try:
        hook(phase)
    except OSError:
        # A revoked parent may already have closed its transport.
        set_progress_hook(None)
