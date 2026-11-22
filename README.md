# Blast Radius starter

A submission for the Blast Radius track, built from the official starter kit.

## This submission

The image uses a precomputed, per-test coverage and dependency index for the
pinned SQLGlot revision. It maps old-side changes to indexed source regions,
expands selections for fixtures and dependencies, then schedules the selected
test numbers across up to eight workers. The image does not use sample answers
or run tests at selection time. Added or unindexed source and fixture files
and binary Python patches fall back to all indexed tests; tests absent from
the index are always included.

This is a heuristic, not a proof of zero misses: coverage can miss new execution
paths, import-time failures, reflection and subprocess behavior. Experimental
selection modes can additionally remove covered tests using baseline observations;
the image enables high-risk modes by default and has missed failing held-out
mutations. The Python
comment/docstring and documentation shortcuts can also miss source-inspection
or other runtime effects. Validate independently before submitting.

Run the selector regression tests with:

```sh
python3 -m unittest discover -s tests -v
```

The coverage index was recorded with `COVERAGE_CORE=ctrace`, per-test contexts,
`SKIP_INTEGRATION=0`, and the plugin in `tools/record/covplugin.py`. That plugin
substitutes synchronous process pools for recording, so subprocess behavior
remains an approximation. `tools/build_index.py` consumes the pinned checkout,
coverage database, plugin metadata, and official `tests.json`; it does not use
failure answers. The official sample and format checks remain `make sample` and
`make check` below.

## Experimental high-risk selection

The experimental image can use test-specific identity, ordering and mapping
observations recorded on the pinned, unpatched suite. The `BR_IDENTITY_MODE`,
`BR_ORDER_MODE` and `BR_MAPPING_MODE` environment variables select the
corresponding heuristic (`observed`, `optimistic`, or, where supported,
`type-optimistic` or `import-guarded`). `BR_PRUNE_FRESH_COPY`,
`BR_PRUNE_IMPORTS` and `BR_PRUNE_ADDED_NAMES` enable further heuristic pruning;
`BR_SOURCE_READ_GUARD=1` retains indexed source readers. Set pruning modes to
`off` and pruning flags to `0` to recover baseline selection; leave the source
reader guard at `1` for the more conservative fallback.

These are not proofs: baseline operands, mapping reads, and dependencies can
change on a patched or reordered execution. In particular, dictionary reads
during import or through native operations can escape test attribution.
Profiling also substitutes synchronous process pools and skips observation
while `tracemalloc` is active. The default misses a verified import-time
dictionary-dependency mutation and a source-inspection mutation. Set
`BR_MAPPING_MODE=import-guarded` and `BR_SOURCE_READ_GUARD=1` for a less
aggressive alternative that retains both of those tests, though it is still
heuristic. The image contains no sample answers.

The committed comparison and mapping profiles were recorded with
`tools/record/identity_profile.py` and `tools/record/mapping_profile.py` on
the pinned checkout with the full indexed test suite, `SKIP_INTEGRATION=0`,
and pytest-xdist. The corresponding `merge_identity_profile.py` and
`merge_mapping_profile.py` scripts require successful runs covering every
indexed test. Both profiles are patch-independent and are copied into the
image; no tests or dependencies are downloaded during the image build.
To regenerate them, set `PYTHONPATH` to include the checkout and this
repository's `tools` directory, set `IDENTITY_PROFILE_SRC`,
`IDENTITY_PROFILE_OUT`, and `IDENTITY_PROFILE_TESTS` to the checkout, an empty
output directory, and `tests.json`, respectively, then run `pytest -p
record.identity_profile -n 2 --dist loadfile`. Repeat with the corresponding
`MAPPING_PROFILE_*` variables and `record.mapping_profile`. Run each merge
script with `--help` for its required inputs.

## Offline semantic probes

The following separate probes and evaluator are offline-only; their outputs and
sample answers are not used by the selector. They do **not** justify pruning:
recorded values may differ on another execution, and the identity/equality
probe uses the recorder's synchronous process-pool approximation.

With the pinned SQLGlot checkout in `SQLGLOT_SRC` and a `br-dev` validation
image containing the packages in `requirements.lock` and `coverage`:

```sh
PROBE_DIR=$(mktemp -d)
python3 tools/semantic_probe.py --src "$SQLGLOT_SRC" --data sample --out "$PROBE_DIR/input.json"
docker run --rm --cpus 2 --network none \
  -v "$SQLGLOT_SRC:/src:ro" -v "$PWD:/repo:ro" -v "$PROBE_DIR:/out" -w /src \
  -e SKIP_INTEGRATION=0 -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPYCACHEPREFIX=/tmp/semantic-pycache -e PYTHONPATH=/src:/repo/tools \
  -e SEMANTIC_PROBE_SRC=/src -e SEMANTIC_PROBE_INPUT=/out/input.json \
  -e SEMANTIC_PROBE_OUTPUT=/out/profile.json br-dev \
  python -c 'import json,pytest; p=json.load(open("/out/input.json")); raise SystemExit(pytest.main(["-p","semantic_probe","-p","no:cacheprovider","-q",*p["tests"]]))'
python3 tools/evaluate_semantic_probe.py --data sample --answers sample/answers.json \
  --selector-output output.jsonl --input "$PROBE_DIR/input.json" --profile "$PROBE_DIR/profile.json"
python3 tools/mapping_probe.py --src "$SQLGLOT_SRC" --data sample --out "$PROBE_DIR/mapping.json"
```

The evaluator's score is an explicitly unsafe observation-only counterfactual,
not the score of the experimental selector. The mapping probe reports possible
key-level opportunities and unresolved whole-mapping reads; it excludes nothing.
The truth runner applies patches with Git on the host, including binary patches,
and maps test-collection failures to affected test files in `compare_truth.py`.
It refuses to overwrite an existing case result; use a fresh output directory
for a new run.

## The problem

Your image gets a batch of code changes to [sqlglot](https://github.com/tobymao/sqlglot) at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. For each change, print which tests to run and how to split them across up to 8 workers.

| | |
| --- | --- |
| `/data/commits/<id>.patch` | One unified diff against the pinned commit. Each stands alone: none builds on another. |
| `/data/tests.json` | Every test, numbered, with its file and the seconds it took us to run. |
| stdout | One JSON line a commit: `{"commit": "k7q2", "workers": [[12, 845, 901], [3, 77]]}` |

Each inner list is one worker's test numbers. `"workers": []` runs nothing, which is the right answer for a change that breaks nothing.

## Scoring

Lower is better. [SCORING.md](SCORING.md) has the exact rules; in short:

- A commit costs the recorded seconds of its busiest worker, plus 2 seconds for every worker.
- A commit where you left out a test that fails adds 300 seconds.
- The CPU seconds your program uses are added. Running sqlglot's tests while you decide is allowed, and you pay for the time.
- Your image runs 3 times and the middle total counts.

"Fails" means pytest reports a failure, an error or a timeout for the test, or the test's file cannot be imported. A test with subtests is one test: one failing subtest fails it.

## Try it

You need Docker, make and Python 3.9 or newer. The kit unpacks into the folder you are in, so unpack it into an empty one.

```sh
make sample   # build, run once on the 30 sample commits under the scorer's limits, and score
make check    # the output's format only
```

`make sample` prints your simulated seconds and misses, which are exact for that output. The board adds the CPU seconds your container used and takes the middle of 3 runs, so expect it a little higher. Your program never sees `sample/answers.json`: only the patches and `tests.json` are mounted.

`sample/answers.json` lists the tests each sample commit breaks and the kind of change it is. The practice and final commits are hidden, and so is everything your program prints when it runs on them. A rejected submission tells you which rule it broke and nothing more, so reproduce problems here.

## Submit

Push to a public GitHub repository with the `Dockerfile` at its top, then enter the repository and the commit hash on the track page. The final scores the commit you pick. If you pick none, your best practice submission counts.

## Rules

- Submit your own team's repository. A submission with the same repository, commit or archive as another team's is disqualified, the later one.
- Your build may download only from public package registries: PyPI, npm, Docker Hub, GitHub releases at a fixed tag, and sqlglot’s own repository at the pinned commit. Nothing from an address your team controls. The final rebuilds every image after the cutoff, so anything else could change a submission after the deadline.
- No answers worked out ahead of time and looked up by patch: no table from a patch, or from a kind of edit, to the tests it breaks. Maps built from the pinned commit (coverage, imports, fixtures) are allowed. Top entries are read.
- Only the final counts. The practice board carries no prize.

## How your image runs

- The build has the network, 4 cores, 8 GB of memory and 10 minutes. It may clone sqlglot at the pinned commit, install it and run its tests to make coverage or import maps.
- Your repository may hold 20 MB. Git submodules and Git LFS files are not fetched: clone sqlglot in the `Dockerfile` and commit maps as plain files.
- The run has no network, 2 cores, 4 GB of memory and 3 minutes for the whole batch (4.5 in the final, which has about one and a half times as many commits).
- The disk is read-only. `/tmp` holds 64 MB and is empty at the start of every run.
- The container runs as user `nobody`, with `HOME=/tmp` and at most 256 processes.
- stdout may hold 2 MB. More rejects the run.

## Things that will catch you

- Logs go to stderr. Any line on stdout that is not an answer rejects the run.
- `score.py` names a broken rule by its code, as in `Rejected: not_json on line 1`. The table in SCORING.md says what each code means.
- Files your program needs must be readable by `nobody`. A map written by `root` during the build with mode 600 is not.
- pytest 9 prints a test as `PASSED` beside `SUBFAILED` lines when one of its subtests fails. That test failed.
- sqlglot skips its executor tests unless `SKIP_INTEGRATION=0` is set. We labeled with it set, so those tests can fail.
- `requirements.lock` is every package in the environment we labeled in, on the image the `Dockerfile` starts from. To run sqlglot's tests the way we did, `pip install -r requirements.lock` and put the sqlglot checkout on `PYTHONPATH`; sqlglot's own `pip install -e .[dev]` picks newer versions.
- The scorer is linux/amd64. On an Apple Silicon Mac, build and run with `--platform linux/amd64`, as the Makefile does, or your timings and wheels will differ.
- A change that only adds a comment is not promised to be harmless.
