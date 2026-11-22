#!/bin/sh
# usage: run_truth.sh <patch-dir> <out-dir> [baseline|patch-name...]
# Set SQLGLOT_SRC to the pinned checkout and optionally TRUTH_IMAGE to the
# development image containing requirements.lock and pytest-xdist.
set -eu
PATCHES=$1; OUT=$2; shift 2
SRC=${SQLGLOT_SRC:?Set SQLGLOT_SRC to the pinned SQLGlot checkout}
IMAGE=${TRUTH_IMAGE:-br-dev}
PLUG=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
mkdir -p "$OUT"
for p in "$@"; do
  [ -s "$OUT/$p.json" ] && continue
  echo "Recording $p" >&2
  docker run --rm --platform linux/amd64 --cpus 2 \
    -v "$SRC:/orig:ro" -v "$PATCHES:/patches:ro" -v "$OUT:/out" -v "$PLUG:/plug:ro" \
    -e SKIP_INTEGRATION=0 -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/work:/plug \
    -e TRUTH_OUT="/out/$p.json" -e CASE="$p" "$IMAGE" sh -c '
      set -e
      mkdir /work
      cp -r /orig/sqlglot /orig/tests /orig/sqlglotc /orig/pyproject.toml /work/
      cd /work
      if [ "$CASE" != baseline ]; then
        python /plug/applypatch.py /work "/patches/$CASE.patch"
      fi
      status=0
      python -m pytest -p truthplugin -p no:cacheprovider -q -n 2 --dist loadfile >"/out/$CASE.log" 2>&1 || status=$?
      tail -3 "/out/$CASE.log"
      [ "$status" -le 1 ] && [ -s "$TRUTH_OUT" ]
    '
done
