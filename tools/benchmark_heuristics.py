#!/usr/bin/env python3
"""Offline scorer and held-out comparison for fixed high-risk strategies."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from score import evaluate_run  # noqa: E402
from tools.compare_truth import failed_numbers  # noqa: E402


STRATEGIES = {
    "baseline": {},
    "guarded-baseline": {"BR_SOURCE_READ_GUARD": "1"},
    "identity-observed": {"BR_IDENTITY_MODE": "observed"},
    "identity-optimistic": {"BR_IDENTITY_MODE": "optimistic"},
    "identity-type-optimistic": {"BR_IDENTITY_MODE": "type-optimistic"},
    "mapping-observed": {"BR_MAPPING_MODE": "observed"},
    "mapping-optimistic": {"BR_MAPPING_MODE": "optimistic"},
    "mapping-import-guarded": {"BR_MAPPING_MODE": "import-guarded"},
    "observed-both": {"BR_IDENTITY_MODE": "observed", "BR_MAPPING_MODE": "observed"},
    "identity-observed-mapping-optimistic": {
        "BR_IDENTITY_MODE": "observed", "BR_MAPPING_MODE": "optimistic",
    },
    "optimistic-both": {"BR_IDENTITY_MODE": "optimistic", "BR_MAPPING_MODE": "optimistic"},
    "observed-comparisons": {"BR_IDENTITY_MODE": "observed", "BR_ORDER_MODE": "observed"},
    "fresh-copy": {"BR_PRUNE_FRESH_COPY": "1"},
    "combined": {
        "BR_IDENTITY_MODE": "observed", "BR_ORDER_MODE": "observed",
        "BR_MAPPING_MODE": "optimistic", "BR_PRUNE_FRESH_COPY": "1",
    },
    "combined-prune-imports": {
        "BR_IDENTITY_MODE": "observed", "BR_ORDER_MODE": "observed",
        "BR_MAPPING_MODE": "optimistic", "BR_PRUNE_FRESH_COPY": "1",
        "BR_PRUNE_IMPORTS": "1",
    },
    "combined-prune-dependencies": {
        "BR_IDENTITY_MODE": "observed", "BR_ORDER_MODE": "observed",
        "BR_MAPPING_MODE": "optimistic", "BR_PRUNE_FRESH_COPY": "1",
        "BR_PRUNE_IMPORTS": "1", "BR_PRUNE_ADDED_NAMES": "1",
    },
    "combined-source-guard": {
        "BR_IDENTITY_MODE": "observed", "BR_ORDER_MODE": "observed",
        "BR_MAPPING_MODE": "optimistic", "BR_PRUNE_FRESH_COPY": "1",
        "BR_PRUNE_IMPORTS": "1", "BR_PRUNE_ADDED_NAMES": "1", "BR_SOURCE_READ_GUARD": "1",
    },
    "combined-import-guarded": {
        "BR_IDENTITY_MODE": "observed", "BR_ORDER_MODE": "observed",
        "BR_MAPPING_MODE": "import-guarded", "BR_PRUNE_FRESH_COPY": "1",
        "BR_PRUNE_IMPORTS": "1", "BR_PRUNE_ADDED_NAMES": "1", "BR_SOURCE_READ_GUARD": "1",
    },
    "combined-observed-mapping": {
        "BR_IDENTITY_MODE": "observed", "BR_ORDER_MODE": "observed",
        "BR_MAPPING_MODE": "observed", "BR_PRUNE_FRESH_COPY": "1",
        "BR_PRUNE_IMPORTS": "1", "BR_PRUNE_ADDED_NAMES": "1", "BR_SOURCE_READ_GUARD": "1",
    },
    "optimistic-and-dependencies": {
        "BR_IDENTITY_MODE": "optimistic", "BR_MAPPING_MODE": "optimistic",
        "BR_PRUNE_IMPORTS": "1", "BR_PRUNE_ADDED_NAMES": "1",
    },
    "type-and-dependencies": {
        "BR_IDENTITY_MODE": "type-optimistic", "BR_MAPPING_MODE": "optimistic",
        "BR_PRUNE_IMPORTS": "1", "BR_PRUNE_ADDED_NAMES": "1",
    },
}

DISABLED = {
    "BR_IDENTITY_MODE": "off",
    "BR_ORDER_MODE": "off",
    "BR_MAPPING_MODE": "off",
    "BR_PRUNE_FRESH_COPY": "0",
    "BR_PRUNE_IMPORTS": "0",
    "BR_PRUNE_ADDED_NAMES": "0",
    "BR_SOURCE_READ_GUARD": "0",
}


def selection_host(data, env):
    child_env = {key: value for key, value in os.environ.items() if not key.startswith("BR_")}
    child_env.update(env)
    result = subprocess.run([sys.executable, str(ROOT / "selector.py"), str(data)],
                            env=child_env, capture_output=True, check=True)
    if result.stderr:
        raise RuntimeError(result.stderr.decode())
    return {row["commit"]: {number for worker in row["workers"] for number in worker}
            for row in (json.loads(line) for line in result.stdout.splitlines())}


def sample_run(image, data, env):
    command = [
        "docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
        "--cpus", "2", "--memory", "4096m", "--memory-swap", "4096m", "--pids-limit", "256",
        "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "65534:65534", "-e", "HOME=/tmp",
        "-v", f"{data / 'commits'}:/data/commits:ro", "-v", f"{data / 'tests.json'}:/data/tests.json:ro",
    ]
    for key, value in (DISABLED | env).items():
        command.extend(["-e", f"{key}={value}"])
    command.extend(["--entrypoint", "sh", image, "-c",
                    'python /app/selector.py; status=$?; cat /sys/fs/cgroup/cpu.stat >&2; exit "$status"'])
    result = subprocess.run(command, capture_output=True, check=True)
    cpu = None
    for line in result.stderr.decode().splitlines():
        if line.startswith("usage_usec "):
            cpu = int(line.split()[1]) / 1_000_000
        elif not line.startswith(("user_usec ", "system_usec ", "core_sched.", "nr_", "throttled_usec ", "burst_usec ")):
            raise RuntimeError(f"unexpected selector stderr: {line}")
    if cpu is None:
        raise ValueError("missing container CPU accounting")
    return result.stdout, cpu


def heldout(data, truth, tests, env):
    selected = selection_host(data, env)
    baseline = json.loads((truth / "baseline.json").read_text())
    if baseline["exit"] or baseline["failed"] or baseline["collect_failed"]:
        raise ValueError(f"{truth}: unmodified baseline failed")
    misses, failing = [], 0
    for commit, chosen in selected.items():
        result = json.loads((truth / f"{commit}.json").read_text())
        failed = failed_numbers(result, tests)
        failing += bool(failed)
        missing = failed - chosen
        if missing:
            misses.append({"commit": commit, "missing": sorted(missing)})
    return {"cases": len(selected), "failing": failing, "misses": misses}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--answers", type=Path, required=True, help="offline scorer only")
    parser.add_argument("--heldout", type=Path, nargs=2, action="append", metavar=("DATA", "TRUTH"),
                        required=True)
    parser.add_argument("--order-patch", type=Path, required=True)
    parser.add_argument("--known-failure", nargs=2, action="append",
                        metavar=("PATCH", "TEST_ID"), help="offline targeted counterexample")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--strategy", choices=STRATEGIES, action="append")
    args = parser.parse_args()
    sample = args.sample.resolve()
    tests = json.loads((sample / "tests.json").read_text())["tests"]
    answers = json.loads(args.answers.read_text())["commits"]
    order_id = next(test["number"] for test in tests
                    if test["id"] == "tests/dialects/test_dialect.py::TestDialect::test_compare_dialect_versions")
    results = []
    with tempfile.TemporaryDirectory(prefix="rwx-benchmark-") as directory:
        order_data = Path(directory) / "order"
        order_data.mkdir()
        (order_data / "commits").mkdir()
        shutil.copy2(args.order_patch, order_data / "commits" / "order-dependent.patch")
        shutil.copy2(sample / "tests.json", order_data)
        targeted = []
        test_by_id = {test["id"]: test["number"] for test in tests}
        for index, (patch, test_id) in enumerate(args.known_failure or []):
            target_data = Path(directory) / f"target-{index}"
            (target_data / "commits").mkdir(parents=True)
            shutil.copy2(patch, target_data / "commits" / "mutation.patch")
            shutil.copy2(sample / "tests.json", target_data)
            targeted.append((target_data, str(patch), test_by_id[str(test_id)]))
        names = args.strategy or STRATEGIES
        for name in names:
            env = STRATEGIES[name]
            output, cpu = sample_run(args.image, sample, env)
            evaluation = evaluate_run(output, tests, answers)
            if not evaluation["ok"]:
                raise ValueError(f"{name}: invalid sample output {evaluation}")
            rows = [json.loads(line) for line in output.splitlines()]
            heldouts = [heldout(data.resolve(), truth.resolve(), tests, env)
                        for data, truth in args.heldout]
            order_selection = selection_host(order_data, env)["order-dependent"]
            targeted_misses = [
                patch for data, patch, number in targeted
                if number not in selection_host(data, env)["mutation"]
            ]
            result = {
                "strategy": name,
                "environment": env,
                "simulated_seconds_including_penalties": evaluation["simulated_milliseconds"] / 1000,
                "cpu_seconds": cpu,
                "total_seconds": evaluation["simulated_milliseconds"] / 1000 + cpu,
                "sample_misses": evaluation["missed_commits"],
                "zero_test_commits": sum(not row["workers"] for row in rows),
                "heldout": heldouts,
                "order_mutation_missed": order_id not in order_selection,
                "known_targeted_misses": targeted_misses,
            }
            results.append(result)
            args.out.write_text(json.dumps(results, indent=2) + "\n")
            print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
