"""Check release declarations against immutable evidence and actual collection.

This proves that the declared acceptance tests will participate in the test run,
not that they passed. The existing pytest shards supply that execution verdict.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .registry import catalog


def _indexed(records: list[dict[str, Any]], key: str, label: str) -> dict[str, dict[str, Any]]:
    result = {}
    for record in records:
        identity = record[key]
        if not isinstance(identity, str) or not identity or identity in result:
            raise ValueError(f"duplicate or invalid {label}: {identity}")
        result[identity] = record
    return result


def _repository_file(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if (path.is_absolute() or not path.parts or path.as_posix() != relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or "\\" in relative or ":" in relative):
        raise ValueError("release evidence path must be repository-relative")
    return root.joinpath(*path.parts)


def _collected(selector: str, node_ids: set[str]) -> bool:
    if not isinstance(selector, str) or not selector.startswith("tests/") or "::test_" not in selector:
        raise ValueError("release acceptance needs a test function node ID")
    return selector in node_ids or ("[" not in selector and any(
        node.startswith(selector + "[") for node in node_ids))


def _contract_map(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or value.get("format_version") != 1 or not value.get("contracts"):
        raise ValueError("unsupported or empty release contract baseline")
    result = _indexed(value["contracts"], "identity", "contract")
    for entry in result.values():
        for key in ("capabilities", "consumers"):
            values = entry[key]
            if (not isinstance(values, (list, tuple)) or not values
                    or any(not isinstance(item, str) or not item for item in values)
                    or len(set(values)) != len(values)):
                raise ValueError(f"invalid contract {key}: {entry['identity']}")
    return result


def _check_transitions(current: dict[str, dict[str, Any]], baselines: list[dict[str, dict[str, Any]]],
                       edges: dict[str, dict[str, Any]], acceptance: dict[str, dict[str, Any]],
                       transitions: list[dict[str, Any]]) -> None:
    declarations = _indexed(transitions, "contract", "contract transition")
    known = set().union(*(set(value) for value in baselines))
    for identity, entry in declarations.items():
        if identity not in known or entry["kind"] not in {"replacement", "reader-retirement"}:
            raise ValueError("transition must name a known contract and explicit replacement/retirement")
        source, target = tuple(entry["source"]), tuple(entry["target"])
        path = tuple(entry["path"])
        if (identity not in source or not target or not set(target) <= current.keys()
                or source == target or not path or not isinstance(entry["reason"], str)
                or not entry["reason"].strip()):
            raise ValueError(f"transition has no usable source/target path: {identity}")
        if entry["kind"] == "replacement" and identity in target:
            raise ValueError("replacement must name a different target contract")
        if set(entry["acceptance"]) != set(path) or not set(path) <= acceptance.keys():
            raise ValueError(f"transition lacks registered acceptance evidence: {identity}")
        position, seen = source, {source}
        for edge_id in path:
            edge = edges.get(edge_id)
            if edge is None or tuple(edge["source"]) != position:
                raise ValueError(f"transition is not a registered contiguous path: {identity}")
            position = tuple(edge["target"])
            if position in seen:
                raise ValueError("transition path contains a cycle")
            seen.add(position)
        if position != target:
            raise ValueError(f"transition path has the wrong target: {identity}")
    for baseline in baselines:
        for identity, before in baseline.items():
            after = current.get(identity)
            losses = ["identity"] if after is None else [key for key in ("capabilities", "consumers")
                if set(before[key]) - set(after[key])]
            if losses and identity not in declarations:
                raise ValueError(f"contract support shrank without registered transition: {identity} ({', '.join(losses)})")


def validate_release_collection(root: Path, node_ids: list[str], *,
                                candidate: dict[str, Any] | None = None,
                                previous_catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fail closed if an edge loses its collected tests, history, or support path."""
    candidate = catalog() if candidate is None else candidate
    current = _contract_map(candidate)
    gate = candidate["release_acceptance"]
    frozen = _indexed(gate["frozen_files"], "path", "frozen evidence")
    if previous_catalog is not None:
        previous_frozen = previous_catalog.get("release_acceptance", {}).get("frozen_files", [])
        for entry in previous_frozen:
            if frozen.get(entry["path"]) != entry:
                raise ValueError(f"previously published frozen evidence cannot be removed or rebound: {entry['path']}")
    for relative, entry in frozen.items():
        if re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None:
            raise ValueError("frozen evidence needs a fixed SHA256")
        path = _repository_file(root, relative)
        if not path.is_file():
            raise ValueError(f"frozen release evidence is missing: {relative}")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != entry["sha256"]:
            raise ValueError(f"frozen release evidence changed: {relative}")
    baseline_path = gate["baseline"]
    if baseline_path not in frozen:
        raise ValueError("release baseline must have registered immutable evidence")
    baselines = [_contract_map(json.loads(_repository_file(root, baseline_path).read_bytes()))]
    if previous_catalog is not None:
        baselines.append(_contract_map(previous_catalog))
    edges = _indexed(candidate["upgrades"], "identity", "upgrade edge")
    known = current.keys() | set().union(*(set(value) for value in baselines))
    for edge in edges.values():
        if (not edge["source"] or not edge["target"] or edge["source"] == edge["target"]
                or not set((*edge["source"], *edge["target"])) <= known):
            raise ValueError("upgrade edge must identify known distinct source and target combinations")
    acceptance = _indexed(gate["upgrades"], "edge", "upgrade acceptance")
    if set(edges) != set(acceptance):
        raise ValueError("every registered upgrade edge needs exactly one acceptance row")
    nodes, matched = set(node_ids), dict[str, list[str]]()
    if not gate["required_tests"]:
        raise ValueError("release acceptance needs installed package verification")
    for selector in gate["required_tests"]:
        if not _collected(selector, nodes):
            raise ValueError(f"required release test was not collected: {selector}")
    for edge_id, row in acceptance.items():
        evidence = row["frozen_files"]
        if (not evidence or not set(evidence) <= frozen.keys()
                or not any(name.endswith(".zip") for name in evidence)
                or not any(name.endswith(".json") for name in evidence)):
            raise ValueError(f"upgrade acceptance lacks frozen ZIP and expected evidence: {edge_id}")
        if not row["test_nodes"]:
            raise ValueError(f"upgrade acceptance has no tests: {edge_id}")
        matched[edge_id] = []
        for selector in row["test_nodes"]:
            if not _collected(selector, nodes):
                raise ValueError(f"upgrade acceptance test was not collected: {edge_id}: {selector}")
            matched[edge_id].extend(sorted(node for node in nodes if node == selector or (
                "[" not in selector and node.startswith(selector + "["))))
    _check_transitions(current, baselines, edges, acceptance, gate["transitions"])
    return {"registered_edges": len(edges), "frozen_files": len(frozen),
            "collected_acceptance": matched, "baseline_count": len(baselines),
            "required_tests": list(gate["required_tests"]),
            "test_execution": "required_by_pytest_shards"}
