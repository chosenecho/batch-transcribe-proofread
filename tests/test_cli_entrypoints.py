"""Validate declared dynamic owners directly, independently of facade caches."""
import pytest

from tests.test_import_order import _run_fresh


def test_registered_handlers_resolve_in_a_fresh_process():
    _run_fresh("""
from importlib import import_module
from bili_asr.cli.registry import COMMANDS
for command, spec in COMMANDS.items():
    owner = import_module(spec.handler_module)
    assert callable(getattr(owner, spec.handler)), (command, spec)
""")


@pytest.mark.parametrize("module,handler,error", [
    ("bili_asr.cli.missing_owner", "_cmd_missing", "ModuleNotFoundError"),
    ("bili_asr.cli.registry", "_cmd_missing", "AttributeError"),
    ("bili_asr.cli.registry", "COMMANDS", "AssertionError"),
])
def test_invalid_entrypoint_is_rejected_even_with_cached_facade(module, handler, error):
    _run_fresh(f"""
from importlib import import_module
from bili_asr import cli
cli._cmd_missing = lambda: None
try:
    owner = import_module({module!r})
    assert callable(getattr(owner, {handler!r}))
except {error}:
    pass
else:
    raise AssertionError('invalid dynamic entrypoint accepted')
""")
