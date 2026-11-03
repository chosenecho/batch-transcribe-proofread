"""Fresh imports exercise public APIs and capability boundaries."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _run_fresh(code: str) -> None:
    environment = os.environ.copy()
    source = Path(__file__).resolve().parents[1] / "src"
    environment["PYTHONPATH"] = str(source) + os.pathsep + environment.get("PYTHONPATH", "")
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, encoding="utf-8", env=environment, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize("first", [
    "bili_asr.editorial_input_contract", "bili_asr.editorial_request", "bili_asr.sql_identity",
])
def test_frozen_editorial_and_sql_contracts_import_without_consumers(first):
    _run_fresh(f"""
from importlib import import_module
import sys
import_module({first!r})
assert 'bili_asr.editorial' not in sys.modules
assert 'bili_asr.editorial_budget' not in sys.modules
assert 'bili_asr.deepseek' not in sys.modules
assert 'requests' not in sys.modules
assert 'bili_asr.storage' not in sys.modules
assert 'bili_asr.storage.migration_source' not in sys.modules
assert 'bili_asr.storage.editorial_replay' not in sys.modules
""")


@pytest.mark.parametrize("first", [
    "bili_asr.platform_identity", "bili_asr.sources.protocols", "bili_asr.sources.models",
])
def test_pure_source_ports_do_not_bootstrap_storage_or_clients(first):
    _run_fresh(f"""
from importlib import import_module
import sys
import_module({first!r})
from bili_asr.sources import ContentRef, SourceAccessObservation, VideoPart, GatewayError
assert VideoPart('BVtest', 2, 99, 'p2', 1000).content_ref == ContentRef('bilibili', 'BVtest', 2)
assert SourceAccessObservation('anonymous', True).verified is True
assert GatewayError('rate_limited').code == 'rate_limited'
assert 'bili_asr.storage' not in sys.modules
assert not any(name == 'bilibili_api' or name.startswith('bilibili_api.') for name in sys.modules)
""")


def test_legacy_identity_codec_does_not_initialize_source_adapters():
    _run_fresh("""
import sys
from bili_asr.bilibili_identity import legacy_bilibili_source_ref, bilibili_source_url
source = {'bvid': 'BVtest', 'pageIndex': 2, 'videoPartId': 7,
          'url': 'https://www.bilibili.com/video/BVtest/?p=3'}
ref = legacy_bilibili_source_ref(source)
assert bilibili_source_url(ref) == source['url']
assert 'bili_asr.sources' not in sys.modules
assert 'bili_asr.storage' not in sys.modules
from bili_asr.publication_content import normalize_content
assert callable(normalize_content)
assert 'bili_asr.sources' not in sys.modules
assert 'bili_asr.storage' not in sys.modules
from bili_asr.sources.bilibili_identity import legacy_bilibili_source_ref as alias
assert alias is legacy_bilibili_source_ref
""")


@pytest.mark.parametrize("first", [
    "bili_asr.asr.provenance", "bili_asr.asr.runner", "bili_asr.storage.editorial",
    "bili_asr.publication", "bili_asr.archive", "bili_asr.workflow",
])
def test_public_apis_work_after_any_supported_import_order(first):
    _run_fresh(f"""
from importlib import import_module
from types import ModuleType
import_module({first!r})
for name in ('bili_asr.asr.runner', 'bili_asr.asr.provenance', 'bili_asr.archive',
             'bili_asr.storage.editorial', 'bili_asr.publication', 'bili_asr.workflow'):
    import_module(name)
from bili_asr import asr
from bili_asr.error_codes import validate_error_code
from bili_asr.storage import validate_error_code as storage_code
from bili_asr.publication import normalize_content
assert type(asr) is ModuleType
assert type(import_module('bili_asr.asr.provenance')) is ModuleType
assert asr.provenance()['model_name']
assert storage_code is validate_error_code
assert storage_code('rate_limited') == 'rate_limited'
content = normalize_content({{'title': 'title', 'markdown': 'body', 'summary': '', 'tags': [],
    'source': {{'bvid': 'BVtest', 'pageIndex': 2, 'videoPartId': 7,
               'url': 'https://www.bilibili.com/video/BVtest/?p=3'}},
    'attribution': 'author', 'editorNote': ''}})
assert content['markdown'] == 'body\\n'
assert content['source']['pageIndex'] == 2
assert 'platform' not in content['source']
""")


@pytest.mark.parametrize("first,forbidden", [
    ("bili_asr.storage.models", ("bili_asr.storage.database", "bili_asr.storage.metadata",
                               "bili_asr.storage.transcripts", "bili_asr.storage.workflow")),
    ("bili_asr.asr.config", ("bili_asr.asr.runner", "bili_asr.asr.backend",
                           "bili_asr.services", "bili_asr.storage")),
    ("bili_asr.cli.parser", ("bili_asr.cli.workflow", "bili_asr.cli.publication",
                            "bili_asr.services", "bili_asr.storage", "bili_asr.asr.runner")),
])
def test_config_record_and_parser_imports_load_only_their_capabilities(first, forbidden):
    _run_fresh(f"""
from importlib import import_module
import sys
import_module({first!r})
for prefix in {forbidden!r}:
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
""")


@pytest.mark.parametrize("arguments", [[], ["--help"], ["--version"],
                                       ["workflow", "run", "--help"],
                                       ["publication", "export", "--help"]])
def test_cli_help_does_not_load_repositories_workers_or_clients(arguments):
    _run_fresh(f"""
import sys
from bili_asr.cli import main
try:
    code = main({arguments!r})
except SystemExit as exc:
    code = exc.code
assert code == 0
for prefix in ('bili_asr.storage', 'bili_asr.services', 'bili_asr.workflow_runtime',
               'bili_asr.editorial_runtime', 'bili_asr.asr.runner', 'bili_asr.asr.session',
               'bilibili_api', 'torch', 'transformers'):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
""")


@pytest.mark.parametrize("entry", ["bili_asr", "bili_asr.cli", "bili_asr.cli.main"])
def test_module_cli_entrypoints_keep_help_behavior(entry):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run([sys.executable, "-m", entry, "--help"], env=environment,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert "workflow" in result.stdout
    assert "RuntimeWarning" not in result.stderr
