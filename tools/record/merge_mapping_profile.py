#!/usr/bin/env python3
"""Merge patch-independent per-test mapping read observations."""
import argparse
import ast
import json
import pickle
from pathlib import Path
import zlib


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", type=Path, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--src", type=Path, required=True, help="pinned baseline checkout")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    tests = json.loads(args.tests.read_text())["tests"]
    baseline = pickle.loads(zlib.decompress(args.index.read_bytes()))
    if [t["id"] for t in tests] != baseline["ids"] or any(
        test["number"] != number for number, test in enumerate(tests)
    ):
        raise ValueError("test list does not match the baseline index")
    files = sorted(args.records.glob("*.json"))
    if not files:
        raise ValueError("no profile records")
    sites, completed = {}, set()
    for path in files:
        record = json.loads(path.read_text())
        if record["exit"] != 0:
            raise ValueError(f"{path}: pytest exited {record['exit']}")
        completed.update(record["completed"])
        for site in record["sites"]:
            file, line, name = site.rsplit(":", 2)
            if file not in baseline["src"]:
                raise ValueError(f"unknown source in mapping profile: {site}")
            sites.setdefault(file, {}).setdefault(int(line), {}).setdefault(
                name, {"keys": {}, "whole": 0, "import_read": False}
            )
        for site in record["import_reads"]:
            file, line, name = site.rsplit(":", 2)
            sites[file][int(line)][name]["import_read"] = True
        for site, key, bits in record["reads"]:
            file, line, name = site.rsplit(":", 2)
            entry = sites[file][int(line)][name]
            bits = int(bits)
            if bits < 0 or bits & ~baseline["all"]:
                raise ValueError(f"invalid mapping bits: {site}")
            entry["keys"][key] = entry["keys"].get(key, 0) | bits
        for site, bits in record["whole"].items():
            file, line, name = site.rsplit(":", 2)
            entry = sites[file][int(line)][name]
            bits = int(bits)
            if bits < 0 or bits & ~baseline["all"]:
                raise ValueError(f"invalid mapping bits: {site}")
            entry["whole"] |= bits
    if completed != set(range(len(tests))):
        raise ValueError(f"profile completed {len(completed)} of {len(tests)} indexed tests")
    entries = {}
    for path, lines in sites.items():
        tree = ast.parse((args.src / path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            if not isinstance(node.targets[0], ast.Name) or not isinstance(node.value, ast.Dict):
                continue
            name = node.targets[0].id
            if name not in lines.get(node.lineno, {}):
                continue
            for key, value in zip(node.value.keys, node.value.values):
                if (isinstance(key, ast.Constant) and isinstance(key.value, str)
                        and key.lineno == value.end_lineno):
                    entries.setdefault(path, {}).setdefault(key.lineno, []).append(
                        (node.lineno, name, key.value)
                    )
    args.out.write_bytes(zlib.compress(pickle.dumps({
        "commit": baseline["commit"], "ids": baseline["ids"], "sites": sites, "entries": entries,
    }, protocol=4), 9))
    print(f"sites={sum(len(names) for lines in sites.values() for names in lines.values())} "
          f"tests={len(completed)} bytes={args.out.stat().st_size}")


if __name__ == "__main__":
    main()
