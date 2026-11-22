#!/usr/bin/env python3
"""Build the compact impact index consumed by selector.py.

Inputs (all derived from the pinned SQLGlot checkout, never from answers):
  --src   SQLGlot checkout at the pinned commit
  --cov   per-test coverage database (tools/record/covplugin.py output)
  --meta  per-test metadata (fixture opens, dialect instances, durations)
  --tests the official tests.json (maps pytest node ids to test numbers)

Output: data/index.bin (zlib-compressed pickle). Test sets are Python ints
used as bitsets, bit i = test number i.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import pickle
import re
import sys
import tokenize
import zlib
from collections import defaultdict

import coverage

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DYNAMIC_CALLS = {"eval", "exec", "__import__", "globals", "locals", "setattr", "delattr"}

K_DEF, K_STMT, K_HDR = 1, 2, 3
F_PLAIN, F_IMPORT, F_DOC, F_EXPR = 0, 1, 2, 3


def split_lines(src):
    lines = src.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def string_flags(src, nlines):
    """flag bit 1: line is inside a multi-line string (not its first line).
    flag bit 2: an insertion right after this line would land inside a token
    (multi-line string) or a backslash continuation."""
    flags = [0] * (nlines + 2)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    fdepth = 0
    fstart = 0
    for tok in toks:
        tname = tokenize.tok_name.get(tok.type, "")
        if tname == "FSTRING_START":
            if fdepth == 0:
                fstart = tok.start[0]
            fdepth += 1
            continue
        if tname == "FSTRING_END":
            fdepth -= 1
            if fdepth == 0 and tok.end[0] > fstart:
                for ln in range(fstart + 1, tok.end[0] + 1):
                    flags[ln] |= 1
                for ln in range(fstart, tok.end[0]):
                    flags[ln] |= 2
            continue
        if fdepth == 0 and tok.type == tokenize.STRING and tok.end[0] > tok.start[0]:
            for ln in range(tok.start[0] + 1, tok.end[0] + 1):
                flags[ln] |= 1
            for ln in range(tok.start[0], tok.end[0]):
                flags[ln] |= 2
    for i, line in enumerate(split_lines(src), 1):
        if line.rstrip().endswith("\\") and not flags[i] & 1:
            flags[i] |= 2
    return flags


def target_names(node, out):
    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            target_names(e, out)
    elif isinstance(node, ast.Starred):
        target_names(node.value, out)
    elif isinstance(node, ast.Attribute):
        out.add(node.attr)
        target_names(node.value, out)
    elif isinstance(node, ast.Subscript):
        target_names(node.value, out)


def stmt_names(node):
    names = set()
    if isinstance(node, ast.Assign):
        for t in node.targets:
            target_names(t, names)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        target_names(node.target, names)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            names.add((a.asname or a.name).split(".")[0])
            names.add(a.name.split(".")[-1])
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
    return names


def dynamic_flag(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name in DYNAMIC_CALLS:
                return 1
            if name in ("getattr", "hasattr") and not (
                len(n.args) >= 2 and isinstance(n.args[1], ast.Constant)
            ):
                return 1
    return 0


def analyze_source(src, exec_bits, names_all):
    """Analyse one source file; exec_bits maps line -> bitset of executing tests."""
    tree = ast.parse(src)
    lines = split_lines(src)
    n = len(lines)

    leaf = []  # (start, end) ranges coverage treats as one statement
    defs = []  # (start, end, node, parent_def_index)
    stmts = []  # (start, end, node, kind, class_qual, flavour)

    def header_end(node, start):
        body = getattr(node, "body", None)
        if body:
            return max(start, body[0].lineno - 1)
        return start

    def visit(body, parent_def, in_def, class_stack):
        for node in body:
            start = node.lineno
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.decorator_list:
                    start = min(start, min(d.lineno for d in node.decorator_list))
            end = node.end_lineno or start
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                idx = len(defs)
                defs.append((start, end, node, parent_def))
                leaf.append((start, header_end(node, start)))
                visit(node.body, idx, True, class_stack)
            elif isinstance(node, ast.ClassDef):
                hend = header_end(node, start)
                leaf.append((start, hend))
                if not in_def:
                    stmts.append((start, hend, node, K_HDR, ".".join(class_stack + [node.name]), 1))
                visit(node.body, parent_def, in_def, class_stack + [node.name])
            else:
                subs = []
                for field in ("body", "orelse", "finalbody"):
                    v = getattr(node, field, None)
                    if isinstance(v, list) and v and isinstance(v[0], ast.stmt):
                        subs.append(v)
                if hasattr(node, "handlers"):
                    for h in node.handlers:
                        subs.append(h.body)
                        leaf.append((h.lineno, header_end(h, h.lineno)))
                if hasattr(node, "cases"):
                    for c in node.cases:
                        subs.append(c.body)
                if subs:
                    hend = header_end(node, start)
                    leaf.append((start, hend))
                    if not in_def:
                        stmts.append((start, hend, node, K_HDR, ".".join(class_stack), 0))
                    for s in subs:
                        visit(s, parent_def, in_def, class_stack)
                else:
                    leaf.append((start, end))
                    if not in_def:
                        flavour = F_PLAIN
                        if isinstance(node, (ast.Import, ast.ImportFrom)):
                            flavour = F_IMPORT
                        elif isinstance(node, ast.Expr):
                            flavour = (
                                F_DOC
                                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                                else F_EXPR
                            )
                        stmts.append((start, end, node, K_STMT, ".".join(class_stack), flavour))

    visit(tree.body, None, False, [])

    lbits = [0] * (n + 2)
    lleaf = bytearray(n + 2)
    for s, e in leaf:
        e = min(e, n)
        b = 0
        for ln in range(s, e + 1):
            b |= exec_bits.get(ln, 0)
        for ln in range(s, e + 1):
            lbits[ln] |= b
            lleaf[ln] = 1

    regs = [None]
    lmap = [0] * (n + 2)

    def_ridx = []
    for i, (s, e, node, parent) in enumerate(defs):
        b = 0
        for ln in range(s, min(e, n) + 1):
            b |= lbits[ln]
        regs.append([K_DEF, s, e, b, dynamic_flag(node), node.name, header_end(node, s)])
        def_ridx.append(len(regs) - 1)
    # defs are listed outer-before-inner, so inner assignments overwrite
    for i, (s, e, node, parent) in enumerate(defs):
        for ln in range(s, min(e, n) + 1):
            lmap[ln] = def_ridx[i]
    # header lines of nested defs are evaluated by the parent function too
    for i, (s, e, node, parent) in enumerate(defs):
        if parent is not None:
            hend = header_end(node, s)
            regs.append([K_DEF, s, e, regs[def_ridx[i]][3] | regs[def_ridx[parent]][3], 0, node.name, hend])
            hidx = len(regs) - 1
            for ln in range(s, min(hend, n) + 1):
                lmap[ln] = hidx

    for s, e, node, kind, cls, flavour in stmts:
        b = 0
        for ln in range(s, min(e, n) + 1):
            b |= lbits[ln]
        names = tuple(sorted(stmt_names(node)))
        lams = []
        if kind == K_STMT:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Lambda) and sub.end_lineno > sub.lineno:
                    lb = 0
                    for ln in range(sub.lineno, sub.end_lineno + 1):
                        lb |= exec_bits.get(ln, 0)
                    lams.append((sub.lineno, sub.end_lineno, lb))
        regs.append([kind, s, e, b, names, cls, flavour, tuple(lams)])
        ridx = len(regs) - 1
        for ln in range(s, min(e, n) + 1):
            if not lmap[ln]:
                lmap[ln] = ridx

    # code lines nobody claimed (else:, finally:, ...)
    for ln in range(1, n + 1):
        if not lmap[ln]:
            t = lines[ln - 1].strip()
            if t and not t.startswith("#"):
                regs.append([K_HDR, ln, ln, 0, (), "", 0])
                lmap[ln] = len(regs) - 1

    touch = 0
    for b in exec_bits.values():
        touch |= b

    for s, e, node, kind, cls, flavour in stmts:
        names_all.update(stmt_names(node))
    for s, e, node, parent in defs:
        names_all.add(node.name)
    rows = []
    for s, e in leaf:
        e = min(e, n)
        b = lbits[s]
        if b:
            rows.append((set(IDENT.findall("\n".join(lines[s - 1 : e]))), b))

    rec = {
        "n": n,
        "lmap": lmap,
        "regs": regs,
        "lbits": [b if lleaf[i] else -1 for i, b in enumerate(lbits)],
        "flags": string_flags(src, n),
        "text": [l.strip()[:1] for l in lines],
        "indent": [len(l) - len(l.lstrip()) if l.strip() else -1 for l in lines],
        "touch": touch,
        "defs": def_ridx,
    }
    return rec, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--cov", required=True)
    ap.add_argument("--meta", required=True)
    ap.add_argument("--tests", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--commit", default="")
    args = ap.parse_args()

    tests = json.load(open(args.tests))["tests"]
    nid2num = {t["id"]: t["number"] for t in tests}
    ntests = len(tests)
    meta = json.load(open(args.meta))

    cd = coverage.CoverageData(basename=args.cov)
    cd.read()
    ctx2bit = {}
    unknown = 0
    for c in cd.measured_contexts():
        if c in nid2num:
            ctx2bit[c] = nid2num[c]
        elif c:
            unknown += 1
    print(f"contexts mapped {len(ctx2bit)} unknown {unknown}", file=sys.stderr)

    abs_prefix = "/src/"
    files = {}
    for f in cd.measured_files():
        rel = f[len(abs_prefix):] if f.startswith(abs_prefix) else os.path.relpath(f, args.src)
        d = defaultdict(int)
        for ln, ctxs in cd.contexts_by_lineno(f).items():
            b = 0
            for c in ctxs:
                bit = ctx2bit.get(c)
                if bit is not None:
                    b |= 1 << bit
            if b:
                d[ln] = b
        files[rel] = d

    src_files = []
    for root, _, fns in os.walk(os.path.join(args.src, "sqlglot")):
        for fn in fns:
            if fn.endswith(".py"):
                src_files.append(os.path.relpath(os.path.join(root, fn), args.src))
    src_files.sort()

    names_all = set()
    index_src = {}
    name_rows = []
    for rel in src_files:
        text = open(os.path.join(args.src, rel), encoding="utf-8").read()
        rec, rows = analyze_source(text, files.get(rel, {}), names_all)
        index_src[rel] = rec
        name_rows.extend(rows)

    nameidx = defaultdict(int)
    for idents, b in name_rows:
        for tok in idents & names_all:
            nameidx[tok] |= b
    # a name used only while building another module/class-level name affects
    # whatever uses the latter at test time: propagate until stable
    edges = defaultdict(set)
    for rel in src_files:
        rec = index_src[rel]
        text_lines = split_lines(open(os.path.join(args.src, rel), encoding="utf-8").read())
        for r in rec["regs"][1:]:
            if r[0] in (K_STMT, K_HDR) and r[4]:
                seg = "\n".join(text_lines[r[1] - 1 : r[2]])
                for tok in set(IDENT.findall(seg)) & names_all:
                    for tgt in r[4]:
                        if tgt != tok:
                            edges[tok].add(tgt)
    changed = True
    while changed:
        changed = False
        for tok, tgts in edges.items():
            cur = nameidx.get(tok, 0)
            new = cur
            for tgt in tgts:
                new |= nameidx.get(tgt, 0)
            if new != cur:
                nameidx[tok] = new
                changed = True
    all_idents = set()
    for rel in src_files:
        all_idents.update(IDENT.findall(open(os.path.join(args.src, rel), encoding="utf-8").read()))

    by_file = defaultdict(int)
    by_class = defaultdict(int)
    by_func = {}
    for t in tests:
        parts = t["id"].split("::")
        f = parts[0]
        by_file[f] |= 1 << t["number"]
        cls = parts[1] if len(parts) == 3 else ""
        if cls:
            by_class[(f, cls)] |= 1 << t["number"]
        by_func[(f, cls, parts[-1])] = 1 << t["number"]

    py_tests = []
    for root, _, fns in os.walk(os.path.join(args.src, "tests")):
        for fn in fns:
            if fn.endswith(".py"):
                py_tests.append(os.path.relpath(os.path.join(root, fn), args.src))
    py_tests.sort()

    def modname(p):
        m = p[:-3].replace("/", ".")
        return m[: -len(".__init__")] if m.endswith(".__init__") else m

    mod2path = {modname(p): p for p in py_tests}
    imports = {}
    for rel in py_tests:
        tree = ast.parse(open(os.path.join(args.src, rel), encoding="utf-8").read())
        imp = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if node.module in mod2path:
                    imp.add(mod2path[node.module])
                for a in node.names:
                    if node.module + "." + a.name in mod2path:
                        imp.add(mod2path[node.module + "." + a.name])
            elif isinstance(node, ast.ImportFrom) and node.level:
                imp.add("<relative>")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in mod2path:
                        imp.add(mod2path[a.name])
        imports[rel] = imp
    importers = defaultdict(set)
    for rel, imp in imports.items():
        for m in imp:
            importers[m].add(rel)

    def all_importers(rel):
        seen, stack = set(), [rel]
        while stack:
            x = stack.pop()
            for y in importers.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return seen

    str_consts = {}
    test_files = {}
    for rel in py_tests:
        text = open(os.path.join(args.src, rel), encoding="utf-8").read()
        tree = ast.parse(text)
        n = len(split_lines(text))
        defs = []

        def visit(body, cls):
            for node in body:
                s = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
                if isinstance(node, ast.ClassDef):
                    defs.append((s, node.end_lineno, "class", node.name, node.name))
                    visit(node.body, node.name)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defs.append((s, node.end_lineno, "def", node.name, cls))
                    str_consts[(rel, cls or "", node.name)] = {
                        c.value for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, str)
                    }

        visit(tree.body, None)
        lines_def = [None] * (n + 2)
        for s, e, kind, name, cls in sorted(defs, key=lambda d: -(d[1] - d[0])):
            for ln in range(s, e + 1):
                lines_def[ln] = (kind, name, cls)
        ls = split_lines(text)
        test_files[rel] = {
            "n": n,
            "lines": lines_def,
            "flags": string_flags(text, n),
            "text": [l.strip()[:1] for l in ls],
            "indent": [len(l) - len(l.lstrip()) if l.strip() else -1 for l in ls],
            "importers": sorted(all_importers(rel)),
            "relative": "<relative>" in imports[rel],
        }

    fix_root = os.path.join(args.src, "tests", "fixtures")
    all_fix = []
    for root, _, fns in os.walk(fix_root):
        for fn in fns:
            all_fix.append(os.path.relpath(os.path.join(root, fn), args.src))
    fix = {rel: [0, 0, 0] for rel in all_fix}  # call, setup (file-wide), import (all)
    for nid, phases in meta["opens"].items():
        num = nid2num.get(nid)
        for phase, paths in phases.items():
            for p in paths:
                rel = p[len(abs_prefix):] if p.startswith(abs_prefix) else p
                fix.setdefault(rel, [0, 0, 0])
                if nid == "<import>":
                    fix[rel][2] = (1 << ntests) - 1
                elif num is None:
                    continue
                elif phase == "call":
                    fix[rel][0] |= 1 << num
                else:
                    fix[rel][1] |= by_file[nid.split("::")[0]]
    for rel in fix:
        fixrel = rel[len("tests/fixtures/"):] if rel.startswith("tests/fixtures/") else rel
        stem = fixrel.rsplit(".", 1)[0]
        base = os.path.basename(fixrel)
        for (f, cls, fn), consts in str_consts.items():
            if fixrel in consts or stem in consts or base in consts:
                fix[rel][0] |= by_func.get((f, cls, fn), 0)

    sys.path.insert(0, args.src)
    import importlib

    dialect_users = defaultdict(int)
    mods = {}
    for nid, classes in meta.get("dialects", {}).items():
        num = nid2num.get(nid)
        if num is None:
            continue
        for qn in classes:
            mod, _, cname = qn.rpartition(".")
            try:
                m = mods.get(mod) or importlib.import_module(mod)
                mods[mod] = m
                cls = m
                for part in cname.split("."):
                    cls = getattr(cls, part)
                mro = cls.__mro__
            except Exception as ex:  # noqa
                print("dialect resolve failed", qn, ex, file=sys.stderr)
                continue
            for k in mro:
                km = k.__module__
                if km.startswith("sqlglot.dialects.") and km != "sqlglot.dialects.dialect":
                    dialect_users[km.rsplit(".", 1)[1]] |= 1 << num

    # tests that instantiated a class (or any subclass of it)
    inst_users = defaultdict(int)
    cls_cache = {}

    def resolve(qn):
        if qn in cls_cache:
            return cls_cache[qn]
        mod, _, cname = qn.rpartition(".")
        res = None
        while mod:
            try:
                m = importlib.import_module(mod)
            except Exception:  # noqa
                mod, _, part = mod.rpartition(".")
                cname = part + "." + cname
                continue
            obj = m
            try:
                for part in cname.split("."):
                    obj = getattr(obj, part)
                res = obj
            except AttributeError:
                res = None
            break
        cls_cache[qn] = res
        return res

    for nid, classes in meta.get("inst", {}).items():
        num = nid2num.get(nid)
        if num is None:
            continue
        for qn in classes:
            cls = resolve(qn)
            if cls is None or not hasattr(cls, "__mro__"):
                continue
            for k in cls.__mro__:
                km = getattr(k, "__module__", "")
                if km.startswith("sqlglot"):
                    mf = sys.modules[km].__file__
                    key = os.path.relpath(mf, args.src) + ":" + k.__qualname__
                    inst_users[key] |= 1 << num

    groups = defaultdict(int)
    for rel, rec in index_src.items():
        parts = rel.split("/")
        if len(parts) == 3 and parts[1] in ("dialects", "parsers", "generators", "typing"):
            groups[parts[2][:-3]] |= rec["touch"]
    dialect_names = set(groups) - {"__init__", "dialect", "base"}
    mentions = defaultdict(int)
    for (f, cls, fn), consts in str_consts.items():
        b = by_func.get((f, cls, fn), 0)
        low = {c.lower() for c in consts}
        for d in dialect_names:
            if d in low or any(d in c and len(c) < 40 for c in low):
                mentions[d] |= b
    dialect_allowed = {}
    for d in dialect_names:
        dialect_allowed[d] = dialect_users.get(d, 0) | groups.get(d, 0) | mentions.get(d, 0)
        # tests/dialects/test_<name>.py always count
        dialect_allowed[d] |= by_file.get(f"tests/dialects/test_{d}.py", 0)

    out = {
        "commit": args.commit,
        "ntests": ntests,
        "ids": [t["id"] for t in tests],
        "src": index_src,
        "names": dict(nameidx),
        "idents": all_idents,
        "tests": test_files,
        "by_file": dict(by_file),
        "by_class": {f"{k[0]}::{k[1]}": v for k, v in by_class.items()},
        "by_func": {f"{k[0]}::{k[1]}::{k[2]}": v for k, v in by_func.items()},
        "fix": fix,
        "dialect": dialect_allowed,
        "inst": dict(inst_users),
        "all": (1 << ntests) - 1,
        # tests that run sqlglot in a subprocess (coverage cannot see them)
        "import_tests": by_func.get(("tests/dialects/test_dialect.py", "TestDialect", "test_lazy_load"), 0),
    }
    blob = zlib.compress(pickle.dumps(out, protocol=5), 9)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    open(args.out, "wb").write(blob)
    print(f"index {len(blob)/1e6:.2f} MB, names {len(nameidx)}, files {len(index_src)}", file=sys.stderr)


if __name__ == "__main__":
    main()
