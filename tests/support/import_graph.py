"""Explicit Python import graph; implicit package initialization is tested in processes.

Each edge records top-level, local, type-only or executable-entry ownership.
Literal import_module calls are included; arbitrary dynamic loading is outside
this static graph and covered at supported package/CLI boundaries.
"""
from __future__ import annotations
import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ImportEdge:
    source: str
    target: str
    kind: str
    line: int


def module_paths(root: Path) -> dict[str, Path]:
    return {
        "bili_asr" + ("." + ".".join(parts) if parts else ""): path
        for path in root.rglob("*.py")
        for parts in [path.relative_to(root).with_suffix("").parts]
        for parts in [parts[:-1] if parts[-1] == "__init__" else parts]
    }


def parse_imports(source: str, code: str, modules: set[str], *, package: bool = False):
    current = source if package else source.rpartition(".")[0]
    edges = []

    def add(target, kind, line):
        if target:
            edges.append(ImportEdge(source, target, kind, line))

    def walk(node, kind="top"):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and kind == "top":
            kind = "local"
        if isinstance(node, ast.If):
            test = node.test
            if ((isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
                    or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")):
                for child in node.body:
                    walk(child, "type")
                for child in node.orelse:
                    walk(child, kind)
                return
            if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__" and len(test.comparators) == 1
                    and isinstance(test.comparators[0], ast.Constant)
                    and test.comparators[0].value == "__main__"
                    and isinstance(test.ops[0], ast.Eq)):
                for child in node.body:
                    walk(child, "entry")
                for child in node.orelse:
                    walk(child, kind)
                return
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name, kind, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            prefix = current.split(".")[:len(current.split(".")) - node.level + 1] if node.level else []
            target = ".".join([*prefix, *([node.module] if node.module else [])])
            unresolved = False
            for alias in node.names:
                candidate = target + "." + alias.name
                if candidate in modules:
                    add(candidate, kind, node.lineno)
                else:
                    unresolved = True
            if unresolved:
                add(target, kind, node.lineno)
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            function = node.func
            name = function.id if isinstance(function, ast.Name) else function.attr if isinstance(function, ast.Attribute) else ""
            target = node.args[0].value
            if name in {"import_module", "__import__"} and isinstance(target, str) and not target.startswith("."):
                add(target, kind, node.lineno)
        for child in ast.iter_child_nodes(node):
            walk(child, kind)

    walk(ast.parse(code))
    return frozenset(edges)


def import_edges(root: Path):
    paths = module_paths(root)
    return frozenset(
        edge for name, path in paths.items()
        for edge in parse_imports(name, path.read_text(encoding="utf-8"), set(paths),
                                  package=path.name == "__init__.py")
    )


def graph_for(edges, modules, *, kinds=("top", "local")):
    graph = {name: set() for name in modules}
    for edge in edges:
        if edge.kind in kinds and edge.target in graph and edge.source != edge.target:
            graph[edge.source].add(edge.target)
    return graph


def reachable(graph, start):
    seen, pending = set(), list(graph.get(start, ()))
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending.extend(graph.get(name, ()))
    return seen


def cycles(graph):
    """Tarjan components, stable independent of filesystem/hash ordering."""
    counter = 0
    stack, active, indices, low, components = [], set(), {}, {}, []

    def visit(name):
        nonlocal counter
        indices[name] = low[name] = counter
        counter += 1
        stack.append(name)
        active.add(name)
        for target in sorted(graph[name]):
            if target not in indices:
                visit(target)
                low[name] = min(low[name], low[target])
            elif target in active:
                low[name] = min(low[name], indices[target])
        if low[name] == indices[name]:
            component = []
            while True:
                target = stack.pop()
                active.remove(target)
                component.append(target)
                if target == name:
                    break
            if len(component) > 1:
                components.append(tuple(sorted(component)))
    for name in sorted(graph):
        if name not in indices:
            visit(name)
    return tuple(sorted(components))
