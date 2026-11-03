"""Protect explicit import directions, deferred cycles and persistence ownership.

Static analysis excludes TYPE_CHECKING and executable entry guards. Dynamic
facade maps and implicit package initialization are covered by import_order.
"""

import ast
import re
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "src" / "bili_asr"


def test_docstring_file_references_point_to_existing_modules():
    known = {path.stem for path in SOURCE.rglob("*.py")}
    reference = re.compile(r"`{1,2}([a-z_][a-z0-9_]*)\.py:\d+")
    missing = [
        (str(path.relative_to(SOURCE)), match.group(1))
        for path in SOURCE.rglob("*.py")
        for match in reference.finditer(path.read_text(encoding="utf-8"))
        if match.group(1) not in known
    ]

    assert missing == []

from tests.support.import_graph import import_edges, module_paths, graph_for, reachable, cycles, parse_imports


def imports(path):
    modules = module_paths(SOURCE)
    name = next(name for name, candidate in modules.items() if candidate == path)
    return {edge.target for edge in parse_imports(name, path.read_text(encoding="utf-8"),
             set(modules), package=path.name == "__init__.py") if edge.kind in {"top", "local"}}


def test_import_parser_resolves_child_aliases_relative_local_and_type_only():
    code = """
from bili_asr import cli as commands
from . import child as alias
from .child import value
import bili_asr.storage.models as records
if TYPE_CHECKING:
    from bili_asr import storage
if typing.TYPE_CHECKING:
    from . import typed
def run():
    from .. import services
    import_module('bili_asr.workflow')
if __name__ == '__main__':
    import bili_asr.cli.main
"""
    known = {"bili_asr.cli", "bili_asr.demo.child", "bili_asr.demo.typed",
             "bili_asr.storage", "bili_asr.services"}
    edges = parse_imports("bili_asr.demo.owner", code, known)
    actual = {(edge.target, edge.kind) for edge in edges}
    assert {
        ("bili_asr.cli", "top"), ("bili_asr.demo.child", "top"),
        ("bili_asr.storage.models", "top"), ("bili_asr.storage", "type"),
        ("bili_asr.demo.typed", "type"), ("bili_asr.services", "local"),
        ("bili_asr.workflow", "local"), ("bili_asr.cli.main", "entry"),
    } <= actual
    assert ("bili_asr.storage", "top") not in actual


# Audited business cycles that predate this repair. Only shrinkage is allowed.
# These are deferred publication/rights/import evidence lookups, not permission
# for new members. Package initializers are separately exercised in processes.
CYCLE_BASELINES = (
    frozenset({
        "bili_asr.content_rights", "bili_asr.manuscript_artifacts",
        "bili_asr.preserved_body_evidence", "bili_asr.publication", "bili_asr.publication_tags",
        "bili_asr.storage.import_origins", "bili_asr.storage.publication",
        "bili_asr.storage.source_supplements",
    }),
)


def unexpected_cycles(graph):
    return tuple(component for component in cycles(graph)
                 if not any(set(component) <= baseline for baseline in CYCLE_BASELINES))


CYCLE_EDGES = frozenset({
    ('bili_asr.content_rights', 'bili_asr.publication'),
    ('bili_asr.content_rights', 'bili_asr.storage.publication'),
    ('bili_asr.manuscript_artifacts', 'bili_asr.storage.publication'),
    ('bili_asr.preserved_body_evidence', 'bili_asr.manuscript_artifacts'),
    ('bili_asr.preserved_body_evidence', 'bili_asr.storage.import_origins'),
    ('bili_asr.preserved_body_evidence', 'bili_asr.storage.publication'),
    ('bili_asr.publication', 'bili_asr.content_rights'),
    ('bili_asr.publication', 'bili_asr.manuscript_artifacts'),
    ('bili_asr.publication', 'bili_asr.preserved_body_evidence'),
    ('bili_asr.publication', 'bili_asr.publication_tags'),
    ('bili_asr.publication', 'bili_asr.storage.import_origins'),
    ('bili_asr.publication', 'bili_asr.storage.publication'),
    ('bili_asr.publication_tags', 'bili_asr.storage.publication'),
    ('bili_asr.storage.import_origins', 'bili_asr.storage.publication'),
    ('bili_asr.storage.import_origins', 'bili_asr.storage.source_supplements'),
    ('bili_asr.storage.publication', 'bili_asr.content_rights'),
    ('bili_asr.storage.publication', 'bili_asr.storage.import_origins'),
    ('bili_asr.storage.publication', 'bili_asr.storage.source_supplements'),
    ('bili_asr.storage.source_supplements', 'bili_asr.storage.import_origins'),
    ('bili_asr.storage.source_supplements', 'bili_asr.storage.publication'),
})

def new_cycle_edges(graph):
    return {(a, b) for c in cycles(graph) for a in c for b in graph[a] & set(c)} - CYCLE_EDGES


def test_no_new_cycles_in_top_level_or_deferred_explicit_imports():
    modules = module_paths(SOURCE)
    edges = import_edges(SOURCE)
    assert cycles(graph_for(edges, modules, kinds=("top",))) == ()
    graph = graph_for(edges, modules)
    assert unexpected_cycles(graph) == ()
    assert new_cycle_edges(graph) == set()


def test_cycle_gate_rejects_a_new_component_or_expansion():
    assert unexpected_cycles({"new.a": {"new.b"}, "new.b": {"new.a"}})
    baseline = sorted(CYCLE_BASELINES[0])
    graph = {name: {baseline[(i + 1) % len(baseline)]} for i, name in enumerate(baseline)}
    assert not unexpected_cycles(graph)
    graph[baseline[0]].add("new.owner")
    graph["new.owner"] = {baseline[0]}
    assert unexpected_cycles(graph)


def test_pure_modules_have_no_transitive_path_to_runtime_or_storage():
    modules = module_paths(SOURCE)
    edges = import_edges(SOURCE)
    graph = graph_for(edges, modules)
    pure = (
        "canonical_json", "cue_models", "error_codes", "platform_identity",
        "publication_content", "publication_models_v2", "publication_text",
        "publication_origin_contract", "editorial_segments", "profile_codec",
        "workflow_payloads", "workflow_planning", "contracts.registry",
        "contracts.content_policies", "storage.schema_contract", "artifact_fences",
        "editorial_input_contract", "editorial_request", "sql_identity",
    )
    forbidden = ("bili_asr.storage", "bili_asr.services", "bili_asr.cli",
                 "bili_asr.sources", "bili_asr.workflow_runtime",
                 "bili_asr.editorial_runtime", "bili_asr.asr.runner", "bili_asr.asr.session")
    failures = []
    for name in pure:
        start = "bili_asr." + name
        assert start in graph, start
        for target in reachable(graph, start):
            if any(target == prefix or target.startswith(prefix + ".") for prefix in forbidden):
                failures.append((start, target))
    assert failures == []


def test_asr_never_imports_business_services_even_from_child_workers():
    assert [(edge.source, edge.target) for edge in import_edges(SOURCE)
            if edge.source.startswith("bili_asr.asr") and edge.kind in {"top", "local"}
            and (edge.target == "bili_asr.services" or edge.target.startswith("bili_asr.services."))] == []


def test_cli_main_and_ops_do_not_route_back_through_package_facade():
    for file in ("main.py", "ops.py"):
        assert "bili_asr.cli" not in imports(SOURCE / "cli" / file)


def test_guard_detects_direct_and_transitive_forbidden_dependency():
    graph = {"pure": {"helper"}, "helper": {"bili_asr.storage.database"},
             "bili_asr.storage.database": set()}
    assert "bili_asr.storage.database" in reachable(graph, "pure")
    graph["pure"] = {"bili_asr.storage.database"}
    assert "bili_asr.storage.database" in reachable(graph, "pure")


def test_high_risk_runtime_writes_belong_to_storage():
    import re
    tables = ("audio_objects", "part_audio_objects", "workflow_publications",
              "artifact_policies", "artifact_policy_runs", "artifact_release_intents")
    write = re.compile(r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+"
                       r"(?:" + "|".join(tables) + r")\b", re.IGNORECASE)
    violations = []
    for path in SOURCE.rglob("*.py"):
        if "storage" in path.relative_to(SOURCE).parts:
            continue
        # Upgrade owns historical backfill only; runtime writes use repositories.
        if path.name == "artifact_catalog_upgrade.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and write.search(node.value):
                violations.append((str(path.relative_to(SOURCE)), node.lineno))
    assert violations == []




def test_storage_and_services_never_import_command_or_application_owners():
    forbidden = {"bili_asr.cli", "bili_asr.publication", "bili_asr.workflow_runtime", "bili_asr.editorial_runtime"}
    violations = []
    for layer in ("storage", "services"):
        for path in (SOURCE / layer).glob("*.py"):
            # This module is deliberately an application composition root.
            if path.name == "workflow_application.py":
                continue
            for target in imports(path):
                if any(target == name or target.startswith(name + ".") for name in forbidden):
                    violations.append((path.name, target))
                if layer == "storage" and (target == "bili_asr.services" or target.startswith("bili_asr.services.")):
                    violations.append((path.name, target))
    assert violations == []


def test_pure_shared_models_and_policies_do_not_import_persistence_or_adapters():
    names = ("bilibili_identity", "canonical_json", "cue_models", "error_codes", "platform_identity", "transcript_selection",
             "workflow_models", "workflow_payloads", "workflow_planning", "workflow_failure_policy",
             "publication_content", "publication_identity")
    forbidden = ("bili_asr.storage", "bili_asr.services", "bili_asr.cli", "bili_asr.sources", "sqlite3", "requests")
    violations = [(name, target) for name in names for target in imports(SOURCE / f"{name}.py")
                  if any(target == prefix or target.startswith(prefix + ".") for prefix in forbidden)]
    assert violations == []


def test_executor_and_parser_have_no_back_edge_to_their_consumers():
    assert "bili_asr.storage.workflow" not in imports(SOURCE / "workflow.py")
    assert "bili_asr.quality" not in imports(SOURCE / "cues.py")
    assert "bili_asr.asr.runner" not in imports(SOURCE / "asr" / "provenance.py")


def test_contracts_do_not_import_database_adapters_or_application_owners():
    forbidden = ("bili_asr.storage", "bili_asr.services", "bili_asr.sources", "bili_asr.cli",
                 "bili_asr.workflow_runtime", "bili_asr.editorial_runtime", "sqlite3", "requests")
    assert [(path.name, target) for path in (SOURCE / "contracts").glob("*.py")
            for target in imports(path) if any(target == name or target.startswith(name + ".") for name in forbidden)] == []


def test_cycle_gate_rejects_new_edge_between_existing_exception_members():
    graph = {name: set() for name in CYCLE_BASELINES[0]}
    for source, target in CYCLE_EDGES:
        graph[source].add(target)
    assert new_cycle_edges(graph) == set()
    graph['bili_asr.preserved_body_evidence'].add('bili_asr.content_rights')
    assert new_cycle_edges(graph) == {('bili_asr.preserved_body_evidence', 'bili_asr.content_rights')}
