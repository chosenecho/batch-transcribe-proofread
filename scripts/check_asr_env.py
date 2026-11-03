#!/usr/bin/env python3
"""Answer "is my GPU usable for ASR" by asserting the five verified host invariants.

The published AMD path was measured on 2026-09-12 to be wrong end to end (ROCm 5.7 has no
`gfx1101`; the PyTorch.org ROCm wheel imports but reports `torch.cuda.is_available() == False`
and aborts under DXG detection). This script asserts the invariants of the path that was
measured working on the WSL2 + RX 7800 XT target, in this order:

1. `dxg-detection`   - `/dev/dxg` exists and `HSA_ENABLE_DXG_DETECTION=1`; without DXG
                       detection the HIP runtime never sees the WSL GPU.
2. `rocm-loader-path` - a `/opt/rocm-*/lib` directory is discoverable through
                       `LD_LIBRARY_PATH` or an `/etc/ld.so.conf.d/*rocm*` entry. The version
                       is globbed, never pinned: no single machine's layout is asserted.
3. `torch-present`   - torch is importable and is a ROCm build (`torch.version.hip`).
4. `hsa-runtime`     - the `libhsa-runtime64.so*` in the venv's `torch/lib` is the system
                       runtime, not the wheel-bundled copy that aborts on WSL.
5. `device-probe`    - a **subprocess** runs `torch.cuda.is_available()` and prints the device
                       it used, because the measured failure is a hard abort inside torch's
                       bundled `librocprofiler-sdk`, not a clean `False`.

Exit status is `0` iff every stage passes, `1` otherwise (`2` for a usage error). Failure
output carries one `check: <stage> FAIL <observed>` line per failing stage, that stage's
cause and the commands that fix it, then the recipe document path once. The stage names and
the remediation text are the interface `docs/wsl-rocm-gpu.md` and the README cite, so keep
them stable.

Every remediation command quotes the interpreter as `"${VENV:?…}/bin/python"` and never as a
bare `python`/`pip`: torch lives in the venv the recipe creates (step 6), the bare names are
absent on that document's own stated OS, and an unset `VENV` must stop the shell rather than
silently reach a different interpreter.

Every probe is injectable through `Probes`, and the checks can be driven without a GPU or a
ROCm install:

    run_checks(Probes(torch=lambda: ProbeResult(True, "torch=2.9.1 hip=7.2.0")))

The script is stdlib-only on purpose: it must still run when the package itself cannot import
torch, and nothing in `bili_asr` imports it.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence, TextIO

STAGE_DXG = "dxg-detection"
STAGE_ROCM_LOADER = "rocm-loader-path"
STAGE_TORCH = "torch-present"
STAGE_HSA = "hsa-runtime"
STAGE_DEVICE = "device-probe"
STAGES: tuple[str, ...] = (STAGE_DXG, STAGE_ROCM_LOADER, STAGE_TORCH, STAGE_HSA, STAGE_DEVICE)

DOC_PATH = "docs/wsl-rocm-gpu.md"
DXG_DEVICE = Path("/dev/dxg")
DXG_ENV = "HSA_ENABLE_DXG_DETECTION"
DXG_ENABLED = "1"
ROCM_ROOT = Path("/opt")
LD_SO_CONF_DIR = Path("/etc/ld.so.conf.d")
SYSTEM_LIB_DIRS: tuple[Path, ...] = (
    Path("/usr/lib/x86_64-linux-gnu"),
    Path("/usr/lib64"),
    Path("/usr/lib"),
    Path("/usr/local/lib"),
)
HSA_SONAME = "libhsa-runtime64.so"
ABORT_SIGNATURE = "Found 0 rocprofiler agents and 2 HSA agents"
CPU_FALLBACK = "BILI_ASR_DEVICE=cpu"
PROBE_KEY = "probe"
CHILD_TIMEOUT_SECONDS = 120.0
MAX_OBSERVED_CHARS = 200
USAGE = (
    "usage: check_asr_env.py\n"
    "Assert the five stages of the verified AMD/WSL ROCm recipe (dxg-detection, "
    "rocm-loader-path, torch-present, hsa-runtime, device-probe); exit 0 iff every stage "
    f"passes. Recipe and per-stage fixes: {DOC_PATH}"
)

TORCH_PROBE_CODE = """import json

payload = {"probe": "torch", "torch": None, "hip": None}
try:
    import torch
except Exception as exc:
    payload["error"] = f"{type(exc).__name__}: {exc}"
else:
    payload["torch"] = getattr(torch, "__version__", None)
    payload["hip"] = getattr(getattr(torch, "version", None), "hip", None)
print(json.dumps(payload))
"""

DEVICE_PROBE_CODE = """import json
import pathlib


def loaded_hsa_runtime():
    try:
        mapping = pathlib.Path("/proc/self/maps").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    paths = set()
    for line in mapping.splitlines():
        fields = line.split()
        if len(fields) > 5 and "libhsa-runtime64" in fields[5]:
            paths.add(fields[5])
    return sorted(paths)[0] if paths else None


payload = {"probe": "device", "available": False, "hip": None, "hsa_runtime": None}
try:
    import torch
except Exception as exc:
    payload["error"] = f"{type(exc).__name__}: {exc}"
else:
    payload["hip"] = getattr(getattr(torch, "version", None), "hip", None)
    payload["available"] = bool(torch.cuda.is_available())
    if payload["available"]:
        properties = torch.cuda.get_device_properties(0)
        payload["name"] = str(getattr(properties, "name", "") or "")
        payload["arch"] = str(getattr(properties, "gcnArchName", "") or "")
        total_memory = getattr(properties, "total_memory", 0) or 0
        payload["vram_gb"] = round(float(total_memory) / (1024 ** 3), 1)
payload["hsa_runtime"] = loaded_hsa_runtime()
print(json.dumps(payload))
"""


@dataclass(frozen=True)
class Remediation:
    """The one-line cause of a failing stage plus the commands that clear it."""

    cause: str
    commands: tuple[str, ...]


_VENV_PYTHON = '"${VENV:?export VENV to the venv that runs bili-asr}/bin/python"'
_TORCH_INSTALL_COMMAND = (
    f"{_VENV_PYTHON} -m pip install --index-url https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2/ "
    "torch triton   # repo.radeon.com ROCm wheels into $VENV; measured pair: torch 2.9.1+rocm7.2.0.lw "
    "+ matching triton"
)
_TORCH_LIB_COMMAND = (
    f"TORCH_LIB=$({_VENV_PYTHON} -c 'import pathlib, torch; print(pathlib.Path(torch.__file__).parent / \"lib\")')"
)
TORCH_MISSING_REMEDIATION = Remediation(
    cause="torch is not importable in this interpreter, so this stage cannot be evaluated",
    commands=(
        _TORCH_INSTALL_COMMAND,
        f"{_VENV_PYTHON} scripts/check_asr_env.py   # re-run once torch imports",
    ),
)
# Step 7 of docs/wsl-rocm-gpu.md removes the bundled copies before copying the system one:
# a `cp -f` alone can leave a differently-suffixed bundled `libhsa-runtime64.so.1` in place
# next to a `libhsa-runtime64.so.1.18.70201` system copy, which is not the measured state.
_HSA_COPY_COMMANDS: tuple[str, ...] = (
    "ROCM_LIB=$(ls -d /opt/rocm-*/lib | sort -V | tail -1)",
    _TORCH_LIB_COMMAND,
    f'[ -n "$TORCH_LIB" ] && [ -n "$ROCM_LIB" ] && rm -f "$TORCH_LIB"/{HSA_SONAME}*   '
    "# drop the wheel-bundled copies first, as the recipe's step 7 does",
    f'[ -n "$TORCH_LIB" ] && cp -f "$ROCM_LIB"/{HSA_SONAME}* "$TORCH_LIB"/   '
    "# then install the system runtime",
    f'ls -l "$TORCH_LIB"/{HSA_SONAME}*',
)


REMEDIATIONS: Mapping[str, Remediation] = {
    STAGE_DXG: Remediation(
        cause=(
            "WSL2 has no usable DXG transport: the HIP runtime needs both the DXG device node "
            f"and {DXG_ENV}={DXG_ENABLED}"
        ),
        commands=(
            f"export {DXG_ENV}={DXG_ENABLED}   # persist it in ~/.bashrc for later shells",
            "ls -l /dev/dxg   # absent: install the Windows AMD driver with WSL support, run "
            '"wsl --update", then "wsl --shutdown"; in a container pass --device /dev/dxg',
            f"{DXG_ENV}={DXG_ENABLED} {_VENV_PYTHON} scripts/check_asr_env.py   "
            "# re-run with both invariants held",
        ),
    ),
    STAGE_ROCM_LOADER: Remediation(
        cause=(
            "no ROCm userspace lib directory holding "
            f"{HSA_SONAME}* is on the dynamic loader search path, so it cannot be resolved at run time"
        ),
        commands=(
            "sudo apt-get install -y --no-install-recommends rocm-hip-libraries miopen-hip "
            "roctracer rocprofiler-register   # needs the ROCm apt repository: recipe steps 1-2",
            'ROCM_LIB=$(ls -d /opt/rocm-*/lib | sort -V | tail -1); [ -n "$ROCM_LIB" ] && '
            'echo "$ROCM_LIB" | sudo tee /etc/ld.so.conf.d/rocm.conf && sudo ldconfig   '
            "# the echo is skipped, never written empty, when the glob matches nothing",
            'ROCM_LIB=$(ls -d /opt/rocm-*/lib | sort -V | tail -1); [ -n "$ROCM_LIB" ] && export '
            'LD_LIBRARY_PATH="$ROCM_LIB:$LD_LIBRARY_PATH"',
            "ldconfig -p | grep -c " + HSA_SONAME + "   # zero means the loader still cannot see it",
        ),
    ),
    STAGE_TORCH: Remediation(
        cause=(
            "torch is missing or is not a ROCm build (torch.version.hip is unset); the PyTorch.org "
            "ROCm wheel was measured as a non-working path under WSL"
        ),
        commands=(
            _TORCH_INSTALL_COMMAND,
            f"{_VENV_PYTHON} -c 'import torch; print(torch.__version__, torch.version.hip)'",
        ),
    ),
    STAGE_HSA: Remediation(
        cause=(
            f"the {HSA_SONAME}* in the venv's torch/lib is the wheel-bundled copy, which aborts on "
            "WSL; it must be the WSL-compatible system runtime"
        ),
        commands=_HSA_COPY_COMMANDS,
    ),
    STAGE_DEVICE: Remediation(
        cause=(
            "the device probe child never reported a usable device: it exited non-zero, timed out, "
            "printed no payload, or named a device without a gcnArchName. When the FAIL line above "
            f'carries `exit -6: …{ABORT_SIGNATURE}…`, the measured cause is a hard abort inside '
            "torch's bundled librocprofiler-sdk"
        ),
        commands=(
            "# which of the three measured modes this is: see the recipe's 'The three measured "
            "failure modes' section; the device the probe saw is already on the FAIL line above",
            "# verified recipe: ROCm 7.2.1 runtime + rocdxg-roct 1.2.2 + the repo.radeon.com torch "
            "2.9.1+rocm7.2.0.lw wheel + rocm-hip-libraries/miopen-hip/roctracer/rocprofiler-register + "
            "/opt/rocm-<ver>/lib on the loader path + the WSL-compatible libhsa-runtime64.so in the "
            f"venv's torch/lib + {DXG_ENV}={DXG_ENABLED}",
            f"{CPU_FALLBACK} bili-asr asr --bvid <bvid>   # CPU fallback when the device cannot be enabled",
        ),
    ),
}

# A clean `False` is a return value, not a SIGABRT, so its cause text must not borrow the
# abort signature: on those two measured hosts the operator is looking at a normal return.
DEVICE_UNAVAILABLE_REMEDIATION = Remediation(
    cause=(
        "torch.cuda.is_available() returned False — a normal return value, not the abort "
        "signature; the two measured causes are a runtime with no gfx1101 (ROCm 5.7) and a wheel "
        "that cannot work under WSL (the PyTorch.org ROCm wheel). Recipe steps 1-6 are the fix"
    ),
    commands=REMEDIATIONS[STAGE_DEVICE].commands,
)


@dataclass(frozen=True)
class DeviceInfo:
    """The device the probe actually used."""

    name: str
    arch: str
    vram_gb: float
    hip: str


@dataclass(frozen=True)
class ProbeResult:
    """One probe's verdict: a pass/fail flag, a bounded scalar, optional facts.

    A probe may carry its own `remediation` when the stage's default cause would describe a
    different failure than the one observed (a missing torch is not a bundled HSA runtime).
    """

    ok: bool
    observed: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)
    remediation: Remediation | None = None


@dataclass(frozen=True)
class StageResult:
    """One stage's verdict plus, when it failed, the remediation to print."""

    name: str
    ok: bool
    observed: str = ""
    remediation: Remediation | None = None
    device: DeviceInfo | None = None


@dataclass(frozen=True)
class ChildOutcome:
    """What a probe subprocess did, including the ways it did not finish cleanly."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str = ""


StageProbe = Callable[[], ProbeResult]
ChildRunner = Callable[[str, float], ChildOutcome]


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_child(code: str, timeout: float = CHILD_TIMEOUT_SECONDS) -> ChildOutcome:
    """Run one probe in a fresh interpreter, because the measured failure is a hard abort.

    `-P` keeps the working directory off the child's `sys.path`: with `-c` the cwd would
    otherwise be `sys.path[0]`, so a stray `torch.py`/`torch/` in whatever directory the
    operator happens to run from could answer both probes instead of the real package.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-P", "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return ChildOutcome(
            returncode=-1,
            stdout=_text(getattr(exc, "stdout", None)),
            stderr=_text(getattr(exc, "stderr", None)),
            timed_out=True,
        )
    except OSError as exc:
        return ChildOutcome(returncode=-1, error=f"{type(exc).__name__}: {exc}")
    return ChildOutcome(
        returncode=completed.returncode,
        stdout=_text(completed.stdout),
        stderr=_text(completed.stderr),
    )


def last_stderr_line(stderr: str) -> str:
    """The one child diagnostic worth keeping: the last non-empty line, never a dump."""
    for line in reversed(stderr.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def redact_observed(text: str, limit: int = MAX_OBSERVED_CHARS) -> str:
    """Collapse a diagnostic into one bounded scalar and drop the operator's home path."""
    collapsed = " ".join(text.split())
    home = str(Path.home())
    if home and home != "/":
        collapsed = collapsed.replace(home, "~")
    return collapsed[:limit]


def child_failure(outcome: ChildOutcome, timeout: float) -> str | None:
    """Classify a subprocess outcome: `None` means it finished cleanly enough to read."""
    if outcome.timed_out:
        return f"child did not finish within {timeout:g}s"
    if outcome.error:
        return redact_observed(outcome.error)
    if outcome.returncode != 0:
        detail = redact_observed(last_stderr_line(outcome.stderr) or last_stderr_line(outcome.stdout))
        return f"exit {outcome.returncode}: {detail}" if detail else f"exit {outcome.returncode}"
    return None


def parse_child_payload(stdout: str, kind: str) -> dict[str, Any] | None:
    """Read the one JSON line a probe child prints; earlier noise is ignored."""
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get(PROBE_KEY) == kind:
            return payload
    return None


def discover_rocm_lib_dirs(
    ld_library_path: str | None, ld_conf_dir: Path, *, root: Path = ROCM_ROOT
) -> tuple[Path, ...]:
    """The `<root>/rocm-*/lib` directories the dynamic loader can actually reach.

    Both sources are asserted for *discoverability*, and the version is globbed rather than
    pinned, so no single machine's `/opt/rocm-<version>/lib` becomes the contract. Shape and
    existence are not enough on their own: an empty (or stale) `rocm-5.7/lib` would otherwise
    satisfy a stage whose whole purpose is that `libhsa-runtime64.so` resolves at run time,
    so the directory must hold that shared object.
    """
    found: list[Path] = []
    for raw in (ld_library_path or "").split(os.pathsep):
        entry = raw.strip()
        if not entry:
            continue
        candidate = Path(entry)
        if _is_rocm_lib_dir(candidate, root) and _holds_hsa_runtime(candidate):
            found.append(candidate)
    config_dir = Path(ld_conf_dir)
    if config_dir.is_dir():
        for config in sorted(config_dir.glob("*rocm*")):
            if not config.is_file():
                continue
            for entry in _config_entries(config):
                candidate = Path(entry)
                if _is_rocm_lib_dir(candidate, root) and _holds_hsa_runtime(candidate):
                    found.append(candidate)
    return tuple(dict.fromkeys(found))


def _holds_hsa_runtime(directory: Path) -> bool:
    """True when `directory` is a real ROCm lib dir: it carries the HSA runtime the loader needs."""
    if not directory.is_dir():
        return False
    try:
        return any(entry.exists() for entry in directory.glob(f"{HSA_SONAME}*"))
    except OSError:
        return False


def _is_rocm_lib_dir(candidate: Path, root: Path) -> bool:
    """True for `<root>/rocm-<version>/lib`, the layout ROCm userspace packages install."""
    if candidate.name != "lib":
        return False
    vendor = candidate.parent
    return vendor.parent == root and (vendor.name == "rocm" or vendor.name.startswith("rocm-"))


def _config_entries(config: Path) -> tuple[str, ...]:
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    entries = []
    for line in text.splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            entries.append(entry)
    return tuple(entries)


def default_system_lib_dirs() -> tuple[Path, ...]:
    """Where a system `libhsa-runtime64.so*` may live, ROCm userspace dirs first."""
    discovered = discover_rocm_lib_dirs(os.environ.get("LD_LIBRARY_PATH"), LD_SO_CONF_DIR)
    return tuple(dict.fromkeys((*discovered, *SYSTEM_LIB_DIRS)))


def default_torch_lib_dir() -> Path | None:
    """`<venv>/lib/python3.12/site-packages/torch/lib` without importing torch."""
    try:
        spec = importlib.util.find_spec("torch")
    except (ImportError, ValueError, AttributeError):
        return None
    origin = getattr(spec, "origin", None)
    if not origin:
        return None
    return Path(origin).parent / "lib"


def _resolved(path: Path) -> Path | None:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None


def _digest(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError:
        return None


def _system_entries(system_dirs: Sequence[Path]) -> tuple[Path, ...]:
    """Every `libhsa-runtime64.so*` a discovered system lib dir actually holds."""
    entries: list[Path] = []
    for directory in system_dirs:
        try:
            entries.extend(candidate for candidate in directory.glob(f"{HSA_SONAME}*") if candidate.exists())
        except OSError:
            continue
    return tuple(entries)


def classify_hsa_runtime(torch_lib: Path | None, system_dirs: Sequence[Path]) -> ProbeResult:
    """Decide whether torch/lib carries the system HSA runtime instead of the bundled copy."""
    if torch_lib is None:
        return ProbeResult(
            False,
            "torch/lib is not locatable: torch is not importable in this interpreter",
            remediation=TORCH_MISSING_REMEDIATION,
        )
    entries = tuple(sorted(entry for entry in torch_lib.glob(f"{HSA_SONAME}*") if entry.exists()))
    if not entries:
        return ProbeResult(
            False,
            f"no {HSA_SONAME}* in {torch_lib}",
            remediation=Remediation(
                cause=f"the venv's torch/lib carries no {HSA_SONAME}* at all",
                commands=_HSA_COPY_COMMANDS,
            ),
        )

    searched = tuple(Path(directory) for directory in system_dirs)
    system_digests = {
        digest for digest in (_digest(candidate) for candidate in _system_entries(searched)) if digest is not None
    }
    searched_resolved = {
        resolved for resolved in (_resolved(directory) for directory in searched) if resolved is not None
    }
    lib_root = _resolved(torch_lib)
    for entry in entries:
        resolved = _resolved(entry)
        if resolved is None or (lib_root is not None and lib_root in resolved.parents):
            continue
        # A link out of torch/lib is not identity on its own: the target has to land in a
        # discovered system lib dir, or be a byte copy of the runtime found there.
        if resolved.parent in searched_resolved:
            return ProbeResult(True, f"hsa_runtime={resolved} (outside torch/lib, links into {resolved.parent})")
        digest = _digest(entry)
        if digest is not None and digest in system_digests:
            return ProbeResult(
                True, f"hsa_runtime={resolved} (outside torch/lib, bytes match a system runtime)"
            )

    digests = {digest for digest in (_digest(entry) for entry in entries) if digest is not None}
    for directory in searched:
        for system_entry in sorted(directory.glob(f"{HSA_SONAME}*")):
            if _digest(system_entry) in digests:
                return ProbeResult(True, f"hsa_runtime={HSA_SONAME} in {torch_lib} is {system_entry}")
    return ProbeResult(
        False,
        f"{HSA_SONAME} in {torch_lib} is the torch-bundled copy "
        f"(no matching system runtime in {len(searched)} searched dirs)",
    )


def classify_torch_outcome(outcome: ChildOutcome, timeout: float) -> ProbeResult:
    """Classify the torch probe: importable and a ROCm build, or the reason it is neither."""
    failure = child_failure(outcome, timeout)
    if failure is not None:
        return ProbeResult(False, failure)
    payload = parse_child_payload(outcome.stdout, "torch")
    if payload is None:
        return ProbeResult(False, "child printed no JSON line")
    error = payload.get("error")
    if error:
        return ProbeResult(False, f"import failed: {error}")
    version = payload.get("torch") or "unknown"
    hip = payload.get("hip")
    if not hip:
        return ProbeResult(False, f"torch={version} hip=absent (not a ROCm build)")
    return ProbeResult(True, f"torch={version} hip={hip}")


def classify_device_outcome(outcome: ChildOutcome, timeout: float) -> ProbeResult:
    """Classify the device probe: a named device with a `gcnArchName`, or the failure text."""
    failure = child_failure(outcome, timeout)
    if failure is not None:
        return ProbeResult(False, failure)
    payload = parse_child_payload(outcome.stdout, "device")
    if payload is None:
        return ProbeResult(False, "child printed no JSON line")
    error = payload.get("error")
    if error:
        return ProbeResult(False, f"import failed: {error}", remediation=TORCH_MISSING_REMEDIATION)
    hip = str(payload.get("hip") or "unknown")
    if not payload.get("available"):
        loaded = payload.get("hsa_runtime") or "none"
        return ProbeResult(
            False,
            f"torch.cuda.is_available() == False hip={hip} hsa_runtime={loaded}",
            remediation=DEVICE_UNAVAILABLE_REMEDIATION,
        )
    name = str(payload.get("name") or "unknown")
    arch = str(payload.get("arch") or "")
    if not arch:
        return ProbeResult(False, f"device reported without a gcnArchName name={name}")
    total_memory = payload.get("vram_gb")
    device = DeviceInfo(
        name=name,
        arch=arch,
        vram_gb=float(total_memory) if isinstance(total_memory, (int, float)) else 0.0,
        hip=hip,
    )
    return ProbeResult(True, f"device={name} arch={arch}", {"device": device})


def make_dxg_probe(
    *, device: Path = DXG_DEVICE, environ: Mapping[str, str] | None = None
) -> StageProbe:
    """Stage 1: the DXG device node exists and DXG detection is switched on."""

    def probe() -> ProbeResult:
        environment = os.environ if environ is None else environ
        # `Path.exists()` on purpose, and the observed text says exactly that: this stage
        # asserts the DXG path is there. It does not open the node and does not check its
        # type, so neither line may read as though a device had been validated.
        present = Path(device).exists()
        value = environment.get(DXG_ENV)
        state = "path-exists (not opened)" if present else "missing"
        if present and value == DXG_ENABLED:
            return ProbeResult(True, f"dxg_device={state} {DXG_ENV}={value}")
        return ProbeResult(
            False,
            f"dxg_device={state} {DXG_ENV}={value if value is not None else 'unset'}",
        )

    return probe


def make_rocm_loader_probe(
    *,
    ld_library_path: str | None = None,
    ld_conf_dir: Path = LD_SO_CONF_DIR,
    rocm_root: Path = ROCM_ROOT,
    environ: Mapping[str, str] | None = None,
) -> StageProbe:
    """Stage 2: a ROCm userspace lib dir is discoverable by the dynamic loader."""

    def probe() -> ProbeResult:
        environment = os.environ if environ is None else environ
        search_path = environment.get("LD_LIBRARY_PATH") if ld_library_path is None else ld_library_path
        directories = discover_rocm_lib_dirs(search_path, Path(ld_conf_dir), root=Path(rocm_root))
        if not directories:
            return ProbeResult(
                False,
                f"no {Path(rocm_root)}/rocm-*/lib via LD_LIBRARY_PATH or {Path(ld_conf_dir)}/*rocm*",
            )
        return ProbeResult(True, f"rocm_lib={directories[0]} count={len(directories)}")

    return probe


def make_torch_probe(*, runner: ChildRunner = run_child, timeout: float = CHILD_TIMEOUT_SECONDS) -> StageProbe:
    """Stage 3: torch is importable and reports the ROCm build's hip version."""

    def probe() -> ProbeResult:
        return classify_torch_outcome(runner(TORCH_PROBE_CODE, timeout), timeout)

    return probe


def make_hsa_probe(
    *, torch_lib: Path | None = None, system_dirs: Sequence[Path] | None = None
) -> StageProbe:
    """Stage 4: torch's own lib dir holds the system HSA runtime, not the bundled copy."""

    def probe() -> ProbeResult:
        lib_dir = default_torch_lib_dir() if torch_lib is None else torch_lib
        directories = default_system_lib_dirs() if system_dirs is None else tuple(system_dirs)
        return classify_hsa_runtime(lib_dir, directories)

    return probe


def make_device_probe(*, runner: ChildRunner = run_child, timeout: float = CHILD_TIMEOUT_SECONDS) -> StageProbe:
    """Stage 5: a subprocess reports the device torch would use, or how it died trying."""

    def probe() -> ProbeResult:
        return classify_device_outcome(runner(DEVICE_PROBE_CODE, timeout), timeout)

    return probe


@dataclass(frozen=True)
class Probes:
    """The five stage probes. Every one is injectable, so the checks run without a GPU."""

    dxg: StageProbe = field(default_factory=make_dxg_probe)
    rocm_loader: StageProbe = field(default_factory=make_rocm_loader_probe)
    torch: StageProbe = field(default_factory=make_torch_probe)
    hsa: StageProbe = field(default_factory=make_hsa_probe)
    device: StageProbe = field(default_factory=make_device_probe)


def run_checks(probes: Probes | None = None) -> tuple[StageResult, ...]:
    """Run the five stages in order; a probe that raises is a failure, never a traceback."""
    active = Probes() if probes is None else probes
    ordered: tuple[tuple[str, StageProbe], ...] = (
        (STAGE_DXG, active.dxg),
        (STAGE_ROCM_LOADER, active.rocm_loader),
        (STAGE_TORCH, active.torch),
        (STAGE_HSA, active.hsa),
        (STAGE_DEVICE, active.device),
    )
    results = []
    for name, probe in ordered:
        try:
            outcome = probe()
        except Exception as exc:
            outcome = ProbeResult(False, f"probe raised {type(exc).__name__}: {exc}")
        device = outcome.detail.get("device") if name == STAGE_DEVICE and outcome.ok else None
        results.append(
            StageResult(
                name=name,
                ok=bool(outcome.ok),
                observed=redact_observed(outcome.observed),
                remediation=None if outcome.ok else (outcome.remediation or REMEDIATIONS[name]),
                device=device if isinstance(device, DeviceInfo) else None,
            )
        )
    return tuple(results)


def device_line(device: DeviceInfo) -> str:
    return f"check: device ok name={device.name} arch={device.arch} vram_gb={device.vram_gb:.1f} hip={device.hip}"


def stage_line(result: StageResult) -> str:
    if result.ok:
        if result.name == STAGE_DEVICE and result.device is not None:
            return device_line(result.device)
        return f"check: {result.name} ok"
    return f"check: {result.name} FAIL {result.observed}"


def remediation_lines(result: StageResult) -> tuple[str, ...]:
    remediation = result.remediation
    if remediation is None:
        return ()
    return (f"  cause: {remediation.cause}", "  fix:", *(f"    {command}" for command in remediation.commands))


def report_lines(results: Sequence[StageResult]) -> tuple[str, ...]:
    """The whole transcript: stage lines, each failure's fix, the recipe path once, the verdict."""
    lines: list[str] = []
    failures = 0
    for result in results:
        lines.append(stage_line(result))
        if not result.ok:
            failures += 1
            lines.extend(remediation_lines(result))
    if failures:
        lines.append(f"  recipe: {DOC_PATH}")
        lines.append(f"asr-env: not verified ({failures} failed)")
    else:
        lines.append("asr-env: verified")
    return tuple(lines)


def main(argv: Sequence[str] | None = None, *, probes: Probes | None = None, out: TextIO | None = None) -> int:
    """Print the check transcript and return `0` iff every stage passed."""
    stream = sys.stdout if out is None else out
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        print(USAGE, file=stream)
        return 0 if arguments in (["-h"], ["--help"]) else 2
    results = run_checks(probes)
    for line in report_lines(results):
        print(line, file=stream)
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
