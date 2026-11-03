"""UTF-8 inputs remain exact with Python UTF-8 mode disabled."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_json_fixture_does_not_depend_on_process_default_encoding(tmp_path):
    data = {"title": "\u4fdd\u7559\u539f\u6587 \u2014 \u03b1", "markdown": "\u6b63\u6587\n"}
    path = tmp_path / "fixture.json"
    original = json.dumps(data, ensure_ascii=False).encode("utf-8")
    path.write_bytes(original)
    env = os.environ.copy()
    env.update(PYTHONUTF8="0", PYTHONCOERCECLOCALE="0", LC_ALL="C", PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    probe = subprocess.run([sys.executable, "-X", "utf8=0", "-c", """
import json, sys
from pathlib import Path
from bili_asr.cli.json_input import _json_object
assert sys.flags.utf8_mode == 0
expected = json.loads(sys.argv[2])
assert _json_object(sys.argv[1]) == expected
assert json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')) == expected
""", str(path), json.dumps(data)], env=env, capture_output=True,
        text=True, encoding="utf-8", timeout=30, check=False)
    assert probe.returncode == 0, probe.stderr
    assert path.read_bytes() == original


def test_publication_regressions_pass_with_gbk_file_defaults(tmp_path):
    """Exercise the four reported nodes with a portable cp936 default adapter."""
    repository = Path(__file__).resolve().parents[1]
    fixture = tmp_path / "utf8.json"
    fixture.write_bytes('{"title":"保留原文 — α"}'.encode("utf-8"))
    env = os.environ.copy()
    env.update(PYTHONUTF8="0", PYTHONCOERCECLOCALE="0", LC_ALL="C", PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(repository / "src"), PYTEST_ADDOPTS="")
    probe = subprocess.run([sys.executable, "-X", "utf8=0", "-c", """
import sys
from pathlib import Path
import pytest
assert sys.flags.utf8_mode == 0
original_read = Path.read_text
original_write = Path.write_text
def read_text(self, encoding=None, errors=None, **kwargs):
    return original_read(self, encoding=encoding or 'cp936', errors=errors, **kwargs)
def write_text(self, data, encoding=None, errors=None, **kwargs):
    return original_write(self, data, encoding=encoding or 'cp936', errors=errors, **kwargs)
Path.read_text = read_text
Path.write_text = write_text
try:
    Path(sys.argv[1]).read_text()
except UnicodeDecodeError:
    pass
else:
    raise AssertionError('cp936 default must reject the exact UTF-8 fixture')
nodes = [
    'tests/test_publication_cli.py::test_cli_complete_release_lifecycle_and_private_review',
    'tests/test_universal_publication.py::test_mixed_historical_and_new_release_catalog_preserves_old_bytes',
    'tests/test_workflow_issue_regressions.py::test_cli_editorial_run_writes_configured_root_and_export_verifies_hash',
]
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', '--basetemp', sys.argv[2], *nodes]))
""", str(fixture), str(tmp_path / "gbk-tests")], cwd=repository, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert "4 passed" in probe.stdout
