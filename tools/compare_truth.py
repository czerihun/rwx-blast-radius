#!/usr/bin/env python3
"""Compare selector output with full-suite truth labels. usage: compare_truth.py <data_dir> <truth_dir>"""
import json, os, subprocess, sys
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
data, truth = sys.argv[1], sys.argv[2]
tests = json.load(open(f"{data}/tests.json"))["tests"]
id2n = {t["id"]: t["number"] for t in tests}
env = dict(os.environ, BR_DATA=data)
out = subprocess.run([sys.executable, f"{root}/selector.py", data], capture_output=True, text=True, env=env).stdout
sel = {}
for l in out.splitlines():
    j = json.loads(l); sel[j["commit"]] = {x for w in j["workers"] for x in w}
miss = 0; n = 0; nonzero = 0
for name in sorted(os.listdir(truth)):
    if not name.endswith(".json"): continue
    c = name[:-5]
    r = json.load(open(f"{truth}/{name}"))
    failed = {id2n[x] for x in r["failed"] if x in id2n and "test_sqlglotc" not in x}
    if r["collect_failed"]: print("collect failures", c, r["collect_failed"])
    n += 1; nonzero += bool(failed)
    m = failed - sel[c]
    print(f"{c}: failing={len(failed)} selected={len(sel[c])}", "MISS " + str([tests[i]["id"] for i in sorted(m)][:4]) if m else "")
    miss += bool(m)
print(f"mutants={n} with_failures={nonzero} missed={miss}")
