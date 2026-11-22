#!/bin/sh
# usage: run_truth.sh <patch-dir> <out-dir> [names...]
# Applies each patch to a fresh copy of the pinned checkout and records which tests fail.
PATCHES=$1; OUT=$2; shift 2
mkdir -p "$OUT"
for p in "$@"; do
  [ -s "$OUT/$p.json" ] && continue
  docker run --rm --platform linux/amd64 --cpus 2 \
    -v /tmp/work/sqlglot:/orig:ro -v "$PATCHES":/patches:ro -v "$OUT":/out \
    -v /workspaces/rwx-blast-radius/tools/truth:/plug:ro \
    -e SKIP_INTEGRATION=0 -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/work:/plug -e TRUTH_OUT=/out/$p.json \
    br-dev sh -c "mkdir /work && cp -r /orig/sqlglot /orig/tests /orig/pyproject.toml /work/ && cd /work && python /plug/applypatch.py /work /patches/$p.patch && python -m pytest -p truthplugin -p no:cacheprovider -q -n 2 --dist loadfile >/out/$p.log 2>&1; tail -3 /out/$p.log" 2>&1 | tail -1
done
