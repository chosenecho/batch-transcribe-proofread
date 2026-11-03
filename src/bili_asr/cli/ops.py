"""Host check, export, and verification handlers."""

from __future__ import annotations

from bili_asr.diagnostics import write_stderr

from pathlib import Path
import argparse

import json
import os
import sys



def _parse_status_filter(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    return {item.strip() for value in values for item in value.split(",") if item.strip()}

def _cmd_check_asr_env(args: argparse.Namespace) -> int:
    """Run the host self-check the README and spec 01 D1.2 name.

    The check is the stdlib-only ``check_asr_env.py`` helper. It is shipped
    beside the installed package and remains available from ``scripts/`` in a
    source checkout; the CLI loads it rather than reimplementing its stages.

    Resolution order, first hit wins:

    1. ``$BILI_ASR_CHECK_SCRIPT`` — an explicit override, for a host that keeps
       the script somewhere unusual.
    2. ``check_asr_env.py`` beside the installed ``bili_asr`` package.
    3. ``scripts/check_asr_env.py`` relative to this file's package root
       (``src/bili_asr/cli.py`` → ``../../scripts/``), which is the checkout
       layout every documented example assumes.
    4. ``scripts/check_asr_env.py`` under the current working directory, i.e.
       the product directory the README tells the operator to run from.

    When none exists the command reports that and exits ``1`` — a missing
    prerequisite is stated, never simulated as a pass.  The script's own exit
    codes pass through unchanged (``0`` verified, ``1`` a failed stage, ``2``
    usage).
    """

    if getattr(args, "backend", "amd-wsl") != "amd-wsl":
        from bili_asr.asr.config import default_config
        from bili_asr.asr.deployment import deployment_report
        try:
            report = deployment_report(default_config(), backend=args.backend, cache_root=args.cache_root,
                                       check_gpu=args.probe_gpu, timeout_seconds=args.probe_timeout)
        except (ValueError, OSError) as exc:
            write_stderr(f"check-asr-env: {exc}")
            return 1
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 1 if report["state"] == "blocked" else 0
    if getattr(args, "probe_gpu", False) or getattr(args, "cache_root", None) is not None:
        write_stderr("check-asr-env: choose cuda, rocm or hcu for the deployment report and optional tensor probe")
        return 1

    import importlib.util

    candidates: list[Path] = []
    override = os.environ.get("BILI_ASR_CHECK_SCRIPT")
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(Path(__file__).resolve().parents[1] / "check_asr_env.py")
    candidates.append(Path(__file__).resolve().parents[3] / "scripts" / "check_asr_env.py")
    candidates.append(Path.cwd() / "scripts" / "check_asr_env.py")

    for candidate in candidates:
        if candidate.is_file():
            script = candidate
            break
    else:
        write_stderr(
            "check-asr-env: no check script found; looked for "
            + ", ".join(str(path) for path in candidates)
            + " (set BILI_ASR_CHECK_SCRIPT to point at it)"
        )
        return 1

    # The script carries its own argparse-free usage contract: no arguments on
    # the command line, so it is called with none.  Loaded by path and invoked
    # as a function rather than through `runpy`: the script's `main()` defaults
    # to `sys.argv[1:]`, which here still holds this subcommand's own name, so
    # the `__main__` route would answer every invocation as a usage error.
    spec = importlib.util.spec_from_file_location("_bili_asr_env_check", script)
    if spec is None or spec.loader is None:
        write_stderr(f"check-asr-env: cannot load {script}")
        return 1
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: the script defines frozen dataclasses, and
    # `dataclasses._process_class` resolves `cls.__module__` through
    # `sys.modules`, which a bare `module_from_spec` does not populate.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        write_stderr(f"check-asr-env: cannot load {script}")
        return 1
    check_main = getattr(module, "main", None)
    if not callable(check_main):
        write_stderr(f"check-asr-env: {script} has no main()")
        return 1
    try:
        return int(check_main([]))
    except SystemExit as exc:  # the script's own `raise SystemExit(main())` guard
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1


def _cmd_export(args: argparse.Namespace) -> int:
    from bili_asr.export import export_records
    from bili_asr.services.workflow_projection import WORKFLOW_STATUSES

    status_filter = _parse_status_filter(args.status)
    if status_filter is not None:
        invalid = status_filter - WORKFLOW_STATUSES
        if invalid:
            write_stderr(
                f"export: invalid status filter: {sorted(invalid)}; "
                f"valid statuses: {sorted(WORKFLOW_STATUSES)}"
            )
            return 1

    try:
        content = export_records(
            archive_root=args.archive_root,
            fmt=args.format,
            out_path=args.out,
            status_filter=status_filter,
            with_text=args.with_text,
            private=getattr(args, "private", False),
            artifact_roots=args.artifact_roots,
        )
        if not args.out or args.out == "-":
            sys.stdout.write(content + ("\n" if not content.endswith("\n") else ""))
            sys.stdout.flush()
    except ValueError as exc:
        write_stderr(f"export: {exc}")
        return 1
    except Exception:
        write_stderr("export: unexpected error")
        return 1
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from bili_asr.integrity import BACKLOG_CATEGORY, DEFECT_CATEGORY, IntegrityVerifier
    report = IntegrityVerifier().verify(
        Path(args.archive_root), scope=args.scope,
        artifact_roots=args.artifact_roots,
    )
    payload = report.to_dict()
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"checked: {payload['checked']}")
        print(f"defects: {payload['defect_count']}")
        for defect in payload["defects"]:
            if defect["category"] != DEFECT_CATEGORY:
                continue
            print(f"{defect['work_id']}: {defect['code']}")
        print(f"backlog: {payload['backlog_count']}")
        for defect in payload["defects"]:
            if defect["category"] != BACKLOG_CATEGORY:
                continue
            print(f"{defect['work_id']}: {defect['code']}")
        for diagnostic in payload["diagnostics"]:
            print(f"diagnostic: {diagnostic}")
    if getattr(args, "strict", False):
        # Pre-cutover gate (contract §2): any finding of either class.
        return 0 if not payload["defects"] and not payload["diagnostics"] else 1
    # Default gate (contract §2): defect-class findings and diagnostics only.
    # Backlog rows are printed in their own section and never move the exit code.
    return 1 if payload["defect_count"] or payload["diagnostics"] else 0
