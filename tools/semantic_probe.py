#!/usr/bin/env python3
"""Offline-only experiment for identity-to-equality changes.

The recorded values are observations, not proofs; this tool does not alter
selector output or provide an index consumed by the submission.
"""

import argparse
import ast
import builtins
import importlib.machinery
import json
import os
import pickle
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import selector  # noqa: E402


def operator_changes(old, new, parent=None):
    if isinstance(old, ast.AST) and isinstance(new, ast.AST):
        if type(old) is not type(new):
            if (
                isinstance(parent, ast.Compare)
                and len(parent.ops) == 1
                and (type(old), type(new)) in ((ast.Is, ast.Eq), (ast.IsNot, ast.NotEq))
            ):
                return [(parent.lineno, parent.col_offset, type(old).__name__)]
            return None
        result = []
        for field, value in ast.iter_fields(old):
            changes = operator_changes(value, getattr(new, field), old)
            if changes is None:
                return None
            result.extend(changes)
        return result
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            return None
        result = []
        for left, right in zip(old, new):
            changes = operator_changes(left, right, parent)
            if changes is None:
                return None
            result.extend(changes)
        return result
    return [] if old == new else None


def patched_source(source, groups):
    lines = source.splitlines(keepends=True)
    patched = lines[:]
    for removed, added, anchor, removed_text in reversed(groups):
        start = removed[0] - 1 if removed else anchor
        if removed and (
            removed != list(range(removed[0], removed[0] + len(removed)))
            or [line.rstrip("\n") for line in lines[start:start + len(removed)]] != removed_text
        ):
            raise ValueError("patch does not match pinned source")
        patched[start:start + len(removed)] = [line + "\n" for line in added]
    return "".join(patched)


def prepare(src, data, out):
    idx = pickle.loads(zlib.decompress((ROOT / "data/index.bin").read_bytes()))
    tests = json.loads((data / "tests.json").read_text())["tests"]
    if [t["id"] for t in tests] != idx["ids"] or any(t["number"] != i for i, t in enumerate(tests)):
        raise ValueError("probe requires the indexed test list")
    engine = selector.Selector(idx)
    candidates = []
    selected = set()
    for patch in sorted((data / "commits").glob("*.patch")):
        files = selector.parse_patch(patch.read_text(encoding="utf-8"))
        if len(files) != 1:
            continue
        f = files[0]
        if f["status"] != "modified" or f["binary"] or not f["old"].startswith("sqlglot/") or not f["old"].endswith(".py"):
            continue
        path = (src / f["old"]).resolve()
        if not path.is_relative_to(src.resolve()) or not path.is_file():
            continue
        old = path.read_text(encoding="utf-8")
        try:
            new = patched_source(old, f["groups"])
            changes = operator_changes(ast.parse(old), ast.parse(new))
        except SyntaxError:
            continue
        if changes is None or len(changes) != 1:
            continue
        line, column, op = changes[0]
        ids = [tests[i]["id"] for i in selector.iter_bits(engine.select(files))]
        candidates.append({"commit": patch.stem, "path": f["old"], "line": line,
                           "column": column, "operator": op, "tests": ids})
        selected.update(ids)
    out.write_text(json.dumps({"candidates": candidates, "tests": sorted(selected)}, indent=2) + "\n")
    print(f"candidates={len(candidates)} unique_selected_tests={len(selected)}")
    for c in candidates:
        print(f"{c['commit']} {c['path']}:{c['line']} tests={len(c['tests'])}")


_profile = None
_current = None
_result = {}


def _pure_equality(value):
    eq = type(value).__eq__
    return eq in (object.__eq__, type(None).__eq__, str.__eq__, int.__eq__, float.__eq__, bytes.__eq__)


def _compare(left, right, key, op):
    old = left is right
    if op == "IsNot":
        old = not old
    stats = _result.setdefault(key, {}).setdefault(_current or "<import>", [0, 0, 0])
    stats[0] += 1
    if not (_pure_equality(left) and _pure_equality(right)):
        stats[2] += 1
    else:
        alternative = left == right
        if op == "IsNot":
            alternative = not alternative
        stats[1] += bool(old != alternative)
    return old


def pytest_configure(config):
    global _profile
    import concurrent.futures
    from record.covplugin import _SyncPool

    _profile = json.loads(Path(os.environ["SEMANTIC_PROBE_INPUT"]).read_text())
    targets = {}
    for c in _profile["candidates"]:
        targets.setdefault(c["path"], {})[(c["line"], c["column"])] = (c["commit"], c["operator"])
    builtins.__semantic_probe_compare__ = _compare
    original = importlib.machinery.SourceFileLoader.source_to_code

    def source_to_code(loader, data, path, *, _optimize=-1):
        relative = os.path.relpath(path, os.environ["SEMANTIC_PROBE_SRC"])
        if relative not in targets:
            return original(loader, data, path, _optimize=_optimize)
        tree = ast.parse(data, filename=path)

        class Instrument(ast.NodeTransformer):
            def visit_Compare(self, node):
                node = self.generic_visit(node)
                key = targets[relative].get((node.lineno, node.col_offset))
                if key is None:
                    return node
                call = ast.Call(
                    func=ast.Name(id="__semantic_probe_compare__", ctx=ast.Load()),
                    args=[node.left, node.comparators[0], ast.Constant(key[0]), ast.Constant(key[1])],
                    keywords=[],
                )
                return ast.copy_location(call, node)

        tree = ast.fix_missing_locations(Instrument().visit(tree))
        return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)

    importlib.machinery.SourceFileLoader.source_to_code = source_to_code
    concurrent.futures.ProcessPoolExecutor = _SyncPool


def pytest_runtest_protocol(item, nextitem):
    # This hook is a wrapper so setup, call and teardown share the test context.
    global _current
    _current = item.nodeid
    yield
    _current = None


# pytest must know that the generator hook wraps the other implementations.
try:
    import pytest
except ImportError:
    pytest = None
if pytest is not None:
    pytest_runtest_protocol = pytest.hookimpl(hookwrapper=True)(pytest_runtest_protocol)


def pytest_sessionfinish(session, exitstatus):
    Path(os.environ["SEMANTIC_PROBE_OUTPUT"]).write_text(json.dumps({
        "exit": int(exitstatus), "observations": _result
    }, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.src, args.data, args.out)


if __name__ == "__main__":
    main()
