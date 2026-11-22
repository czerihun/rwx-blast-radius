#!/usr/bin/env python3
"""Generate synthetic single-line mutation patches against the pinned SQLGlot checkout.

Used only for local validation of the selector (labelled by running the real tests);
nothing generated here ships in the image.
"""
import argparse
import difflib
import os
import random
import re
import sys

OPS = [
    (r" is not ", " is "),
    (r" is (?!not )", " is not "),
    (r" == ", " != "),
    (r" != ", " == "),
    (r" <= ", " < "),
    (r" >= ", " > "),
    (r" < ", " <= "),
    (r" > ", " >= "),
    (r" and ", " or "),
    (r" or ", " and "),
    (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"),
    (r"\bnot ", ""),
    (r"\+", "-"),
    (r"\[0\]", "[1]"),
    (r"\[1\]", "[0]"),
    (r"\b0\b", "1"),
    (r"\b1\b", "2"),
    (r"\bNone\b", "False"),
    (r"\.lower\(\)", ".upper()"),
    (r"\bif ", "if not "),
    (r"args, 0\)", "args, 1)"),
    (r"args, 1\)", "args, 0)"),
    (r"\breturn ", "return not "),
]


def mutate_line(line, rng):
    code = line.split("#")[0]
    if not code.strip() or code.strip().startswith(("import ", "from ", '"""', "'''", "@")):
        return None
    cands = []
    for pat, rep in OPS:
        for m in re.finditer(pat, line):
            # skip matches inside simple string literals
            before = line[: m.start()]
            if before.count('"') % 2 or before.count("'") % 2:
                continue
            cands.append((m, rep))
    if not cands:
        return None
    m, rep = rng.choice(cands)
    return line[: m.start()] + rep + line[m.end():]


def make_patch(path, old_lines, new_lines):
    diff = list(difflib.unified_diff(old_lines, new_lines, f"a/{path}", f"b/{path}", n=3, lineterm=""))
    if not diff:
        return None
    return f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n" + "\n".join(diff) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--kinds", default="code")
    ap.add_argument("--prefix", default="m")
    ap.add_argument("--index", default="")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    os.makedirs(args.out, exist_ok=True)

    srcs = []
    for root, _, fns in os.walk(os.path.join(args.src, "sqlglot")):
        for fn in fns:
            if fn.endswith(".py"):
                srcs.append(os.path.relpath(os.path.join(root, fn), args.src))
    fixtures = []
    for root, _, fns in os.walk(os.path.join(args.src, "tests", "fixtures")):
        for fn in fns:
            if fn.endswith(".sql"):
                fixtures.append(os.path.relpath(os.path.join(root, fn), args.src))
    kinds = args.kinds.split(",")
    idx = None
    if args.index:
        import pickle, zlib
        idx = pickle.loads(zlib.decompress(open(args.index, "rb").read()))
    made = 0
    attempts = 0
    while made < args.n and attempts < args.n * 50:
        attempts += 1
        kind = rng.choice(kinds)
        if kind in ("code", "module"):
            path = rng.choice(srcs)
            lines = open(os.path.join(args.src, path), encoding="utf-8").read().split("\n")
            i = rng.randrange(len(lines))
            if idx is not None:
                rec = idx["src"][path]
                ln = i + 1
                if ln > rec["n"]:
                    continue
                if kind == "code":
                    if rec["lbits"][ln] <= 0:
                        continue
                else:
                    ri = rec["lmap"][ln]
                    if not ri or rec["regs"][ri][0] not in (2, 3):
                        continue
            new = mutate_line(lines[i], rng)
            if new is None or new == lines[i]:
                continue
            nl = lines[:i] + [new] + lines[i + 1:]
            patch = make_patch(path, lines, nl)
        elif kind == "delete":
            path = rng.choice(srcs)
            lines = open(os.path.join(args.src, path), encoding="utf-8").read().split("\n")
            i = rng.randrange(len(lines))
            l = lines[i].rstrip()
            if idx is not None:
                rec = idx["src"][path]
                if i + 1 > rec["n"] or rec["lbits"][i + 1] <= 0:
                    continue
            if not l.strip() or l.strip().startswith(("#", "def ", "class ", "@", "return", "import", "from")) or l[-1] in ":([{,\\":
                continue
            nl = lines[:i] + lines[i + 1:]
            patch = make_patch(path, lines, nl)
        elif kind == "fixture":
            path = rng.choice(fixtures)
            lines = open(os.path.join(args.src, path), encoding="utf-8").read().split("\n")
            i = rng.randrange(len(lines))
            l = lines[i]
            if not l.strip() or l.startswith("--"):
                continue
            j = rng.randrange(len(l) + 1)
            nl = lines[:i] + [l[:j] + rng.choice(["x", " 1", "(", "NULL "]) + l[j:]] + lines[i + 1:]
            patch = make_patch(path, lines, nl)
        elif kind == "comment":
            path = rng.choice(srcs)
            lines = open(os.path.join(args.src, path), encoding="utf-8").read().split("\n")
            i = rng.randrange(len(lines))
            nl = lines[:i] + ["    # Cheap check first"] + lines[i:]
            patch = make_patch(path, lines, nl)
        else:
            continue
        if not patch:
            continue
        name = f"{args.prefix}{made:03d}"
        open(os.path.join(args.out, name + ".patch"), "w").write(patch)
        made += 1
    print(f"wrote {made} patches to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
