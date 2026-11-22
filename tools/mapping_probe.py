#!/usr/bin/env python3
"""Offline key-level dependency probe. It never removes tests."""
import argparse
import ast
import copy
import json
import pickle
from pathlib import Path
import sys
import time
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import selector  # noqa: E402
from tools.semantic_probe import patched_source  # noqa: E402


def mapping_change(old, new):
    """Find a single literal mapping-value edit with no other AST changes."""
    old_assigns = [n for n in ast.walk(old) if isinstance(n, (ast.Assign, ast.AnnAssign))
                   and isinstance(n.value, ast.Dict)]
    new_assigns = [n for n in ast.walk(new) if isinstance(n, (ast.Assign, ast.AnnAssign))
                   and isinstance(n.value, ast.Dict)]
    found = []
    for before, after in zip(old_assigns, new_assigns):
        names = before.targets if isinstance(before, ast.Assign) else [before.target]
        if len(names) != 1 or not isinstance(names[0], ast.Name):
            continue
        if len(before.value.keys) != len(after.value.keys):
            continue
        if any(not isinstance(key, ast.Constant) for key in before.value.keys):
            continue
        if ast.dump(ast.Tuple(elts=before.value.keys, ctx=ast.Load())) != ast.dump(
            ast.Tuple(elts=after.value.keys, ctx=ast.Load())
        ):
            continue
        changed = [i for i, (a, b) in enumerate(zip(before.value.values, after.value.values))
                   if ast.dump(a) != ast.dump(b)]
        if len(changed) != 1:
            continue
        i = changed[0]
        try:
            ast.literal_eval(before.value.values[i])
            ast.literal_eval(after.value.values[i])
        except (ValueError, TypeError):
            continue
        restored = copy.deepcopy(new)
        assignments = [n for n in ast.walk(restored) if isinstance(n, (ast.Assign, ast.AnnAssign))
                       and isinstance(n.value, ast.Dict)]
        restored_assign = assignments[new_assigns.index(after)]
        restored_assign.value.values[i] = copy.deepcopy(before.value.values[i])
        if ast.dump(restored) == ast.dump(old):
            found.append({"name": names[0].id, "key": before.value.keys[i].value,
                          "line": before.lineno})
    return found[0] if len(found) == 1 else None


def references(src, names):
    result = {name: [] for name in names}
    for path in sorted((src / "sqlglot").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            name = node.attr if isinstance(node, ast.Attribute) else (
                node.id if isinstance(node, ast.Name) else None
            )
            if name not in result or not isinstance(node.ctx, ast.Load):
                continue
            parent = parents.get(node)
            kind = "subscript" if isinstance(parent, ast.Subscript) and parent.value is node else "whole_value"
            result[name].append({"path": str(path.relative_to(src)), "line": node.lineno, "kind": kind})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    start = time.process_time()
    src = args.src.resolve()
    idx = pickle.loads(zlib.decompress((ROOT / "data/index.bin").read_bytes()))
    tests = json.loads((args.data / "tests.json").read_text())["tests"]
    if [t["id"] for t in tests] != idx["ids"] or any(t["number"] != i for i, t in enumerate(tests)):
        raise ValueError("probe requires the indexed test list")
    engine = selector.Selector(idx)
    candidates = []
    for patch in sorted((args.data / "commits").glob("*.patch")):
        files = selector.parse_patch(patch.read_text(encoding="utf-8"))
        if len(files) != 1:
            continue
        f = files[0]
        if f["status"] != "modified" or f["binary"] or not f["old"].startswith("sqlglot/") or not f["old"].endswith(".py"):
            continue
        path = (src / f["old"]).resolve()
        if not path.is_relative_to(src) or not path.is_file():
            continue
        old = path.read_text(encoding="utf-8")
        try:
            change = mapping_change(ast.parse(old), ast.parse(patched_source(old, f["groups"])))
        except SyntaxError:
            continue
        if change is None:
            continue
        bits = engine.select(files)
        durations = [(int(tests[i]["seconds"] * 1000 + 0.5), i) for i in selector.iter_bits(bits)]
        workers = selector.schedule(durations)
        cost = max((sum(int(tests[i]["seconds"] * 1000 + 0.5) for i in w) for w in workers), default=0) + 2000 * len(workers)
        candidates.append({"commit": patch.stem, "path": f["old"], **change,
                           "selected": len(durations), "cost_ceiling_seconds": cost / 1000})
    refs = references(src, {c["name"] for c in candidates})
    result = {"candidates": candidates, "references": refs, "excluded_tests": 0,
              "cpu_seconds": time.process_time() - start}
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    for c in candidates:
        usages = refs[c["name"]]
        print(f"{c['commit']} {c['path']}:{c['line']} key={c['key']!r} "
              f"tests={c['selected']} ceiling={c['cost_ceiling_seconds']:.3f}s "
              f"whole-value-reads={sum(r['kind'] == 'whole_value' for r in usages)}")
    print(f"excluded=0 cpu={result['cpu_seconds']:.3f}s; unresolved reads require fallback")


if __name__ == "__main__":
    main()
