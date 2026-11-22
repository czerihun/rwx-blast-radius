#!/usr/bin/env python3
"""Run independent full-suite truth checks, including Git binary patches."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    if len(sys.argv) < 4:
        raise SystemExit("usage: run_truth.py <patch-dir> <out-dir> <baseline|patch-name>...")
    patches, out = (Path(arg).resolve() for arg in sys.argv[1:3])
    src = Path(os.environ["SQLGLOT_SRC"]).resolve()
    image = os.environ.get("TRUTH_IMAGE", "br-dev")
    plugin = Path(__file__).resolve().parent
    out.mkdir(parents=True, exist_ok=True)
    for case in sys.argv[3:]:
        if not case or Path(case).name != case:
            raise ValueError(f"invalid case name: {case!r}")
        result = out / f"{case}.json"
        if result.exists():
            raise FileExistsError(f"{result} already exists; use a fresh truth output directory")
        print(f"Recording {case}", file=sys.stderr, flush=True)
        with tempfile.TemporaryDirectory(prefix="rwx-truth-") as directory:
            work = Path(directory)
            for name in ("sqlglot", "tests", "sqlglotc"):
                shutil.copytree(src / name, work / name,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
            shutil.copy2(src / "pyproject.toml", work)
            if case != "baseline":
                subprocess.run(["git", "-C", str(work), "apply", "--binary",
                                str(patches / f"{case}.patch")], check=True)
            command = [
                "docker", "run", "--rm", "--platform", "linux/amd64", "--cpus", "2",
                "-v", f"{work}:/work", "-v", f"{out}:/out", "-v", f"{plugin}:/plug:ro",
                "-w", "/work", "-e", "SKIP_INTEGRATION=0",
                "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPATH=/work:/plug",
                "-e", f"TRUTH_OUT=/out/{case}.json", image,
                "python", "-m", "pytest", "-p", "truthplugin", "-p", "no:cacheprovider",
                "-q", "-n", "2", "--dist", "loadfile",
            ]
            with (out / f"{case}.log").open("w", encoding="utf-8") as log:
                status = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT).returncode
            if not result.is_file():
                raise RuntimeError(f"{case}: pytest exited {status} without a truth result")
            value = json.loads(result.read_text())
            if status != value["exit"] or status not in (0, 1, 2) or (status == 2 and not value["collect_failed"]):
                raise RuntimeError(f"{case}: invalid pytest exit {status}, truth={value}")
            print(f"{case}: exit={status} failing={len(value['failed'])} "
                  f"collection_errors={len(value['collect_failed'])}", flush=True)


if __name__ == "__main__":
    main()
