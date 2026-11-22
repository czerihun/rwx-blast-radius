#!/usr/bin/env python3
"""Compare selector output with independently recorded full-suite failures."""
import json
import os
import subprocess
import sys


def main():
    if len(sys.argv) != 4:
        raise SystemExit("usage: compare_truth.py <data_dir> <truth_dir> <baseline.json>")
    data, truth, baseline_path = sys.argv[1:]
    with open(baseline_path, encoding="utf-8") as f:
        baseline = json.load(f)
    if baseline["exit"] != 0 or baseline["failed"] or baseline["collect_failed"]:
        raise SystemExit("invalid truth environment: unmodified baseline did not pass")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(data, "tests.json"), encoding="utf-8") as f:
        tests = json.load(f)["tests"]
    id2n = {t["id"]: t["number"] for t in tests}
    run = subprocess.run(
        [sys.executable, os.path.join(root, "selector.py"), data],
        capture_output=True,
        text=True,
        check=True,
    )
    if run.stderr:
        print(run.stderr, file=sys.stderr, end="")
    selected = {}
    for line in run.stdout.splitlines():
        result = json.loads(line)
        selected[result["commit"]] = {n for worker in result["workers"] for n in worker}

    misses = checked = invalid = failing = 0
    for name in sorted(os.listdir(truth)):
        if not name.endswith(".json") or os.path.abspath(os.path.join(truth, name)) == os.path.abspath(baseline_path):
            continue
        commit = name[:-5]
        with open(os.path.join(truth, name), encoding="utf-8") as f:
            result = json.load(f)
        unknown = set(result["failed"]) - id2n.keys()
        inconsistent_exit = result["exit"] not in (0, 1) or bool(result["exit"]) != bool(result["failed"])
        if result["collect_failed"] or unknown or inconsistent_exit or commit not in selected:
            print(f"{commit}: INVALID truth: collect={result['collect_failed']} unknown={sorted(unknown)} exit={result['exit']}")
            invalid += 1
            continue
        failed = {id2n[node] for node in result["failed"]}
        missing = failed - selected[commit]
        checked += 1
        failing += bool(failed)
        misses += bool(missing)
        print(f"{commit}: failing={len(failed)} selected={len(selected[commit])} missing={sorted(missing)}")
    print(f"mutants={checked} with_failures={failing} missed={misses} invalid={invalid}")
    if misses or invalid or not checked:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
