"""Lazy CLI compatibility exports; registry owns production dispatch."""
from importlib import import_module
_EXPORTS = {
    "main": ("bili_asr.cli.main", None),
    "build_parser": ("bili_asr.cli.parser", "build_parser"),
    "DEFAULT_ARCHIVE_ROOT": ("bili_asr.cli._shared", "DEFAULT_ARCHIVE_ROOT"),
    "_dispatch_command": ("bili_asr.cli.main", "_dispatch_command"),
    "_main": ("bili_asr.cli.main", "_main"),
}


def __getattr__(name):
    target = _EXPORTS.get(name)
    if target is None and name.startswith("_cmd_"):
        from bili_asr.cli.registry import COMMANDS
        spec = next((spec for spec in COMMANDS.values() if spec.handler == name), None)
        if spec is not None:
            target = (spec.handler_module, name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = target
    owner = import_module(module)
    value = owner if attribute is None else getattr(owner, attribute)
    globals()[name] = value
    return value


__all__ = ["DEFAULT_ARCHIVE_ROOT", "build_parser", "main"]
