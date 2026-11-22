#!/bin/sh
# usage: run_truth.sh <patch-dir> <out-dir> <baseline|patch-name>...
# Set SQLGLOT_SRC to the pinned checkout and optionally TRUTH_IMAGE to the
# image containing the locked test dependencies.
set -eu
exec python3 "$(dirname "$0")/run_truth.py" "$@"
