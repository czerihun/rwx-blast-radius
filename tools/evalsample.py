#!/usr/bin/env python3
"""Run selector.py on the sample set locally and report misses in detail."""
import json, subprocess, sys, os, time, resource
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sample = os.path.join(root, "sample")
env = dict(os.environ, BR_DATA=sample, BR_VERBOSE="1")
t = time.process_time()
r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
p = subprocess.run([sys.executable, os.path.join(root, "selector.py"), sample], capture_output=True, text=True, env=env)
r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
cpu = (r1.ru_utime + r1.ru_stime) - (r0.ru_utime + r0.ru_stime)
open("/tmp/out_eval.jsonl", "w").write(p.stdout)
sys.stderr.write(p.stderr)
tests = json.load(open(f"{sample}/tests.json"))["tests"]
ans = json.load(open(f"{sample}/answers.json"))["commits"]
sc = subprocess.run([sys.executable, os.path.join(root, "score.py"), sample, f"{sample}/answers.json", "/tmp/out_eval.jsonl", "--cpu-seconds", f"{cpu:.3f}"], capture_output=True, text=True)
print(sc.stdout, sc.stderr)
sel = {}
for line in p.stdout.splitlines():
    j = json.loads(line); sel[j["commit"]] = {x for w in j["workers"] for x in w}
for c, v in ans.items():
    miss = [i for i in v["failing"] if i not in sel[c]]
    if miss:
        print("MISS", c, v["kind"], [tests[i]["id"] for i in miss])
