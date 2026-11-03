import os
from pathlib import Path
import subprocess

import pytest

def ubuntu_noble():
    release = Path("/etc/os-release")
    if not release.is_file():
        return False
    fields = dict(line.split("=", 1) for line in release.read_text().splitlines() if "=" in line)
    return fields.get("ID", "").strip('"') == "ubuntu" and fields.get("VERSION_CODENAME", "").strip('"') == "noble"


pytestmark = pytest.mark.skipif(not ubuntu_noble(), reason="Provisioning targets Ubuntu 24.04 CI runners")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ensure_ci_ffmpeg.sh"


def executable(root, name, body):
    path = root / name
    path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize("mode", ["present", "fallback", "failure"])
def test_ffmpeg_provisioning_verifies_tools_and_retries_another_mirror(tmp_path, mode):
    executable(tmp_path, "ffmpeg", '[[ "$PROBE_MODE" == present || -f "$RUNNER_TEMP/installed" ]]\n')
    executable(tmp_path, "ffprobe", '[[ "$PROBE_MODE" == present || -f "$RUNNER_TEMP/installed" ]]\n')
    executable(tmp_path, "sudo", 'exec "$@"\n')
    executable(tmp_path, "timeout", 'shift 2\nexec "$@"\n')
    executable(tmp_path, "apt-get", '''
echo "$*" >> "$RUNNER_TEMP/apt.log"
for argument in "$@"; do
  if [[ "$argument" == Dir::Etc::sourcelist=* ]]; then
    cat "${argument#*=}" >> "$RUNNER_TEMP/mirrors.log"
    if grep -q 'http://archive.ubuntu.com' "${argument#*=}"; then exit 1; fi
  fi
done
[[ "$PROBE_MODE" != failure ]] || exit 1
if [[ " $* " == *" install "* ]]; then touch "$RUNNER_TEMP/installed"; fi
''')
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
               RUNNER_TEMP=str(tmp_path), PROBE_MODE=mode)
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert result.returncode == (1 if mode == "failure" else 0), result.stderr
    if mode == "present":
        assert not (tmp_path / "apt.log").exists()
    else:
        mirrors = (tmp_path / "mirrors.log").read_text()
        assert "http://archive.ubuntu.com/ubuntu" in mirrors
        assert "http://azure.archive.ubuntu.com/ubuntu" in mirrors
        assert "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg" in mirrors
        assert "Dir::Etc::sourceparts=-" in (tmp_path / "apt.log").read_text()
    assert not list(tmp_path.glob("ci-ubuntu-*.sources"))
