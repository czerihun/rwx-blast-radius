#!/usr/bin/env python3
"""Score an UNSAFE observation-only counterfactual; never used by selector."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import score  # noqa: E402
import selector  # noqa: E402


def observed_matches(test_ids, observations):
    return {test for test in test_ids
            if test in observations and observations[test][0]
            and not observations[test][1] and not observations[test][2]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--answers", type=Path, required=True, help="offline scoring only")
    parser.add_argument("--selector-output", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args()
    tests = json.loads((args.data / "tests.json").read_text())["tests"]
    answers = json.loads(args.answers.read_text())["commits"]
    inputs = json.loads(args.input.read_text())
    profile = json.loads(args.profile.read_text())
    if profile["exit"] != 0:
        raise ValueError(f"profile suite failed: exit {profile['exit']}")
    id2num = {t["id"]: t["number"] for t in tests}
    duration = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    outputs = [json.loads(line) for line in args.selector_output.read_text().splitlines()]
    by_commit = {row["commit"]: row for row in outputs}
    baseline = score.evaluate_run(args.selector_output.read_bytes(), tests, answers)
    if not baseline["ok"]:
        raise ValueError(f"invalid selector output: {baseline}")
    for candidate in inputs["candidates"]:
        commit = candidate["commit"]
        row = by_commit[commit]
        selected = {i for worker in row["workers"] for i in worker}
        candidate_numbers = {id2num[test] for test in candidate["tests"]}
        if not candidate_numbers <= selected:
            raise ValueError(f"{commit}: profile does not match selector output")
        observations = profile["observations"].get(commit, {})
        matching = observed_matches(candidate["tests"], observations)
        remaining = selected - {id2num[test] for test in matching}
        row["workers"] = selector.schedule([(duration[i], i) for i in remaining])
        print(f"{commit}: covered={len(candidate_numbers)} observed_matching={len(matching)}")
    counterfactual = score.evaluate_run(
        ("\n".join(json.dumps(row) for row in outputs) + "\n").encode(), tests, answers
    )
    if not counterfactual["ok"]:
        raise ValueError(f"invalid counterfactual output: {counterfactual}")
    print("UNSAFE observation-only counterfactual; do not use for selection")
    print(f"baseline={baseline['simulated_milliseconds'] / 1000:.3f}s "
          f"counterfactual={counterfactual['simulated_milliseconds'] / 1000:.3f}s "
          f"misses={counterfactual['missed_commits']}")


if __name__ == "__main__":
    main()
