#!/usr/bin/env python3
"""Merge patch-independent per-test comparison observations into an index."""
import argparse
import json
import pickle
from pathlib import Path
import zlib


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", type=Path, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
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
    results = {}
    completed = set()
    for path in files:
        record = json.loads(path.read_text())
        if record["exit"] != 0:
            raise ValueError(f"{path}: pytest exited {record['exit']}")
        completed.update(record["completed"])
        for key, values in record["results"].items():
            path_name, line, column, operator = key.rsplit(":", 3)
            if operator not in ("Is", "IsNot", "Lt", "LtE", "Gt", "GtE") or path_name not in baseline["src"]:
                raise ValueError(f"invalid profile entry: {key}")
            site = (path_name, int(line), int(column), operator)
            old = results.get(site, (0, 0, 0))
            new = tuple(int(value) for value in values)
            if len(new) != 3 or any(bits < 0 or bits & ~baseline["all"] for bits in new):
                raise ValueError(f"invalid profile bits: {key}")
            results[site] = tuple(a | b for a, b in zip(old, new))
    if completed != set(range(len(tests))):
        raise ValueError(f"profile completed {len(completed)} of {len(tests)} indexed tests")
    sites = {}
    for (path, line, column, operator), bits in results.items():
        sites.setdefault(path, {}).setdefault(line, []).append((column, operator, *bits))
    args.out.write_bytes(zlib.compress(pickle.dumps({
        "commit": baseline["commit"], "ids": baseline["ids"], "sites": sites,
    }, protocol=4), 9))
    print(f"sites={len(results)} tests={len(completed)} bytes={args.out.stat().st_size}")


if __name__ == "__main__":
    main()
