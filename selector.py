"""Blast-radius test selector.

For each patch: map changed old-side lines to enclosing functions / statements /
classes in the pinned SQLGlot commit, select the tests whose recorded execution
(per-test coverage, class instantiation, fixture reads) can observe the change,
widen conservatively when the change cannot be analysed precisely, and split the
selected tests over at most 8 workers minimising the official cost.

All maps are derived from the pinned SQLGlot commit (data/index.bin).
"""

from __future__ import annotations

import json
import os
import pickle
import re
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("BR_DATA", "/data")
INDEX = os.environ.get("BR_INDEX", os.path.join(HERE, "data", "index.bin"))
LEVEL = os.environ.get("BR_LEVEL", "L0")  # L0: statement level, L1: function level
VERBOSE = bool(os.environ.get("BR_VERBOSE"))

K_DEF, K_STMT, K_HDR = 1, 2, 3
F_EXPR = 3

CODING = re.compile(r"coding[:=]")
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
STRINGS = re.compile(r"""('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")""")
DEF_OR_CLASS = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")
ASSIGN = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)")
DOC_SUFFIX = (".md", ".rst", ".txt", ".png", ".jpg", ".svg", ".gif", ".ico", ".lock")
DOC_DIRS = ("posts/", "benchmarks/", "pdoc/", ".github/", "sqlglot-integration-tests/", "docs/")
SETUP_FUNCS = ("setUp", "tearDown", "setUpClass", "tearDownClass", "setup_method", "teardown_method")


def is_comment_or_blank(s):
    s = s.strip()
    return not s or s.startswith("#")


# --------------------------------------------------------------------------- patch parsing
def parse_patch(text):
    """Per-file dicts: old, new, status, binary, groups.

    A group is one run of -/+ lines: (removed old line numbers, added lines,
    anchor = old line preceding the group, removed lines' text)."""
    files = []
    cur = None
    group = None
    old_ln = 0
    in_hunk = False
    rem_old = rem_new = 0
    def flush():
        nonlocal group
        if group is not None:
            cur["groups"].append(group)
        group = None

    for line in text.split("\n"):
        if in_hunk:
            c = line[:1]
            if c == "-":
                if group is None:
                    group = ([], [], old_ln - 1, [])
                group[0].append(old_ln)
                group[3].append(line[1:])
                old_ln += 1
                rem_old -= 1
            elif c == "+":
                if group is None:
                    group = ([], [], old_ln - 1, [])
                group[1].append(line[1:])
                rem_new -= 1
            elif c == "\\":
                continue
            elif c == " " or line == "":
                flush()
                old_ln += 1
                rem_old -= 1
                rem_new -= 1
            else:
                flush()
                in_hunk = False
                c = None  # not a hunk line: fall through to header parsing
            if c is not None:
                if rem_old <= 0 and rem_new <= 0:
                    flush()
                    in_hunk = False
                continue
        if line.startswith("diff --git "):
            flush()
            parts = line[11:].split(" ")
            a = parts[0][2:] if parts[0].startswith("a/") else parts[0]
            b = parts[-1][2:] if parts[-1].startswith("b/") else parts[-1]
            cur = {"old": a, "new": b, "status": "modified", "groups": [], "binary": False}
            files.append(cur)
        elif cur is None:
            continue
        elif line.startswith("new file mode"):
            cur["status"] = "added"
        elif line.startswith("deleted file mode"):
            cur["status"] = "deleted"
        elif line.startswith("rename from "):
            cur["status"] = "renamed"
            cur["old"] = line[12:]
        elif line.startswith("rename to "):
            cur["new"] = line[10:]
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            cur["binary"] = True
        elif line.startswith("--- "):
            p = line[4:].split("\t")[0]
            if p == "/dev/null":
                cur["status"] = "added"
            elif p.startswith("a/"):
                cur["old"] = p[2:]
        elif line.startswith("+++ "):
            p = line[4:].split("\t")[0]
            if p == "/dev/null":
                cur["status"] = "deleted"
            elif p.startswith("b/"):
                cur["new"] = p[2:]
        elif line.startswith("@@"):
            m = HUNK.match(line)
            if m:
                a = int(m.group(1))
                oc = 1 if m.group(2) is None else int(m.group(2))
                nc = 1 if m.group(4) is None else int(m.group(4))
                old_ln = a + 1 if oc == 0 else a
                rem_old, rem_new = oc, nc
                in_hunk = oc > 0 or nc > 0
    flush()
    return files


# --------------------------------------------------------------------------- selection
class Selector:
    def __init__(self, idx):
        self.idx = idx
        self.src = idx["src"]
        self.names = idx["names"]
        self.inst = idx["inst"]
        self.dialect = idx["dialect"]
        self.all = idx["all"]
        self.idents = idx["idents"]
        self.import_tests = idx["import_tests"]
        self.risky_bits = 0

    @staticmethod
    def group_of(path):
        parts = path.split("/")
        if len(parts) == 3 and parts[1] in ("dialects", "parsers", "generators", "typing"):
            return parts[2][:-3]
        return None

    def name_bits(self, names, allowed=None):
        b = 0
        get = self.names.get
        for n in names:
            b |= get(n, 0)
        return b & allowed if allowed is not None else b

    def novel(self, added):
        for s in added:
            s = s.split("#")[0]
            for tok in IDENT.findall(STRINGS.sub("", s)):
                if tok not in self.idents:
                    return True
        return False

    @staticmethod
    def neutral_group(rec, removed, added, anchor):
        """True when the group only touches blank / comment lines outside strings."""
        flags, text, n = rec["flags"], rec["text"], rec["n"]
        if flags is None or not all(is_comment_or_blank(s) for s in added):
            return False
        if any(CODING.search(s) or s.startswith("#!") for s in added):
            return False
        if removed:
            return all(2 < l <= n and text[l - 1] in ("", "#") and not flags[l] & 1 for l in removed)
        return anchor <= n and (anchor < 1 or not flags[anchor] & 2)

    # -- python sources
    def py_file(self, path, rec, groups):
        bits = 0
        for removed, added, anchor, _ in groups:
            if self.neutral_group(rec, removed, added, anchor):
                continue
            for l in removed:
                bits |= self.line_sel(path, rec, l)
            if any(s.strip() for s in added):
                if removed:
                    bits |= self.added_names(path, rec, removed[0], added)
                else:
                    bits |= self.insert_sel(path, rec, anchor, added)
        return bits

    def line_sel(self, path, rec, l):
        if l > rec["n"]:
            return 0
        ri = rec["lmap"][l]
        if not ri:
            return 0
        r = rec["regs"][ri]
        kind = r[0]
        if kind == K_DEF:
            if LEVEL == "L0" and l > r[6]:
                lb = rec["lbits"][l]
                if lb >= 0:
                    return lb
            return r[3]
        return self.module_sel(path, rec, r, l, l)

    def module_sel(self, path, rec, r, lo=None, hi=None):
        kind, names, cls, flavour = r[0], r[4], r[5], r[6]
        if lo is not None and kind == K_STMT:
            # a line strictly inside a multi-line lambda only matters when the lambda runs
            best = None
            for ls, le, lb in r[7]:
                if ls <= lo and hi < le and (ls < lo or hi > lo) and (best is None or le - ls < best[1] - best[0]):
                    best = (ls, le, lb)
            if best is not None:
                return best[2]
        own = r[3] | self.import_tests
        grp = self.group_of(path)
        allowed = self.dialect.get(grp) if grp else None
        inst = self.inst.get(path + ":" + cls) if cls else None
        extra = self.risky_bits
        if kind == K_HDR and flavour == 1:  # class header: bases / decorators affect the whole class
            b = own | (inst or 0) | self.name_bits(names, allowed)
            if inst is None:
                b |= rec["touch"]
            if allowed is not None:
                b |= allowed
            return b | extra
        if kind == K_HDR:  # compound statement header at module / class level
            return own | rec["touch"] | (allowed or 0) | extra
        nb = self.name_bits(names)
        if inst is not None:
            b = own | (nb & inst if names else inst)
        elif allowed is not None:
            b = own | (nb & allowed if names else allowed)
        else:
            b = own | nb
        if not names or flavour == F_EXPR:
            b |= rec["touch"]
        return b | extra

    def added_names(self, path, rec, l, added):
        names = set()
        for s in added:
            m = DEF_OR_CLASS.match(s) or ASSIGN.match(s)
            if m:
                names.add(m.group(1))
        if not names or l > rec["n"]:
            return 0
        ri = rec["lmap"][l]
        # only a replaced statement head can define new names (not keyword args inside a call)
        if ri and rec["regs"][ri][0] != K_DEF and rec["regs"][ri][1] == l:
            grp = self.group_of(path)
            return self.name_bits(names, self.dialect.get(grp) if grp else None)
        return 0

    def insert_sel(self, path, rec, anchor, added):
        regs, n, indent = rec["regs"], rec["n"], rec["indent"]
        ind = min((len(s) - len(s.lstrip()) for s in added if s.strip()), default=0)
        bits = 0
        handled = False
        for di in rec["defs"]:
            r = regs[di]
            s, e = r[1], r[2]
            if anchor + 1 < s or anchor > e:
                continue
            hind = indent[s - 1] if s - 1 < len(indent) else 0
            if (
                s <= anchor < e
                or (anchor == e and ind > hind)
                or (anchor + 1 == s and any(a.lstrip().startswith("@") for a in added))
            ):
                bits |= r[3]
                handled = True
        if handled:
            return bits
        for ln in (anchor, anchor + 1):  # inside a multi-line module/class-level statement
            if 1 <= ln <= n:
                ri = rec["lmap"][ln]
                if ri:
                    r = regs[ri]
                    if r[0] in (K_STMT, K_HDR) and r[1] <= anchor < r[2]:
                        return self.module_sel(path, rec, r, anchor + 1, anchor)
        grp = self.group_of(path)
        allowed = self.dialect.get(grp) if grp else None
        cls = ""
        if ind > 0 and 1 <= anchor <= n and rec["lmap"][anchor]:
            r = regs[rec["lmap"][anchor]]
            if r[0] != K_DEF:
                cls = r[5]
        inst = self.inst.get(path + ":" + cls) if cls else None
        names = set()
        unknown = False
        for s in added:
            if is_comment_or_blank(s) or len(s) - len(s.lstrip()) > ind:
                continue
            m = DEF_OR_CLASS.match(s) or ASSIGN.match(s)
            if m:
                names.add(m.group(1))
            else:
                unknown = True
        b = self.risky_bits
        if names:
            nb = self.name_bits(names)
            if inst is not None:
                nb &= inst
            elif allowed is not None:
                nb &= allowed
            b |= nb
        if unknown or not names:
            b |= rec["touch"] | (allowed or 0)
        return b

    # -- tests
    def test_file(self, path, rec, groups):
        bits = 0
        by_func, by_class, by_file = self.idx["by_func"], self.idx["by_class"], self.idx["by_file"]
        wide = by_file.get(path, 0)
        for imp in rec["importers"]:
            wide |= by_file.get(imp, 0)
        if rec["relative"]:
            wide = self.all
        n = rec["n"]
        for removed, added, anchor, _ in groups:
            if self.neutral_group(rec, removed, added, anchor):
                continue
            for l in removed or (anchor, anchor + 1):
                info = rec["lines"][l] if 1 <= l <= n else None
                if info is None:
                    bits |= wide
                    continue
                kind, name, cls = info
                if kind == "def" and name.startswith("test") and cls:
                    bits |= by_func.get(f"{path}::{cls}::{name}", 0)
                elif kind == "def" and name in SETUP_FUNCS and cls:
                    bits |= by_class.get(f"{path}::{cls}", 0)
                else:
                    bits |= wide
        return bits

    # -- driver
    def select(self, files):
        bits = 0
        fix = self.idx["fix"]
        for f in files:
            status = f["status"]
            path = f["new"] if status == "added" else f["old"]
            if f["binary"] and (f["old"].endswith(".py") or f["new"].endswith(".py")):
                bits |= self.all
            elif path.startswith("tests/fixtures/"):
                ent = fix.get(path)
                if status in ("added", "renamed") or ent is None or not any(ent):
                    bits |= self.all
                    continue
                bits |= ent[0] | ent[1] | ent[2]
            elif path.startswith("sqlglot/") and path.endswith(".py"):
                if status == "added":
                    bits |= self.all
                    continue
                rec = self.src.get(path)
                if rec is None or status in ("deleted", "renamed"):
                    bits |= self.all
                    continue
                grp = self.group_of(path)
                self.risky_bits = 0
                if any(self.novel(g[1]) for g in f["groups"]):
                    self.risky_bits = (self.dialect.get(grp) if grp else None) or self.all
                bits |= self.py_file(path, rec, f["groups"])
            elif path.startswith("tests/") and path.endswith(".py"):
                rec = self.idx["tests"].get(path)
                if status == "added" or rec is None:
                    bits |= self.all
                    continue
                if status in ("deleted", "renamed"):
                    bits |= self.all
                    continue
                bits |= self.test_file(path, rec, f["groups"])
            elif path.startswith("sqlglot/"):
                bits |= self.all
            elif path.startswith("sqlglotc/"):
                bits |= self.idx["by_file"].get("tests/test_sqlglotc.py", 0)
            elif path.endswith(DOC_SUFFIX) or path.startswith(DOC_DIRS):
                continue
            else:
                bits |= self.all
        return bits


# --------------------------------------------------------------------------- scheduling
def schedule(durations):
    """durations: [(ms, test)]. Best worker split under the official cost."""
    if not durations:
        return []
    items = sorted(durations, reverse=True)
    best_cost, best = None, None
    for k in range(1, min(8, len(items)) + 1):
        loads = [0] * k
        buckets = [[] for _ in range(k)]
        for d, t in items:
            j = loads.index(min(loads))
            loads[j] += d
            buckets[j].append(t)
        cost = max(loads) + 2000 * k
        if best_cost is None or cost < best_cost:
            best_cost, best = cost, buckets
    return [b for b in best if b]


def iter_bits(b):
    while b:
        low = b & -b
        yield low.bit_length() - 1
        b ^= low


def remap_bits(bits, remap, unmapped):
    result = unmapped
    for i in iter_bits(bits):
        if remap[i] is not None:
            result |= 1 << remap[i]
    return result


def main():
    data = sys.argv[1] if len(sys.argv) > 1 else DATA
    tests = json.loads(open(os.path.join(data, "tests.json")).read())["tests"]
    idx = pickle.loads(zlib.decompress(open(INDEX, "rb").read()))
    sel = Selector(idx)

    dur = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    same = [t["id"] for t in tests] == idx["ids"] and all(t["number"] == i for i, t in enumerate(tests))
    remap = unmapped = None
    if not same:
        # index was built against another test list: translate bits by test id
        id2num = {t["id"]: t["number"] for t in tests}
        remap = [id2num.get(i) for i in idx["ids"]]
        known = {n for n in remap if n is not None}
        unmapped = sum(1 << t["number"] for t in tests if t["number"] not in known)

    commits_dir = os.path.join(data, "commits")
    out = []
    for name in sorted(os.listdir(commits_dir)):
        if not name.endswith(".patch"):
            continue
        commit = name[:-6]
        text = open(os.path.join(commits_dir, name), encoding="utf-8", errors="replace").read()
        try:
            bits = sel.select(parse_patch(text))
        except Exception as ex:  # noqa - never lose a commit to an analysis bug
            print(f"analysis failed for {commit}: {ex!r}", file=sys.stderr)
            bits = idx["all"]
        if remap is not None:
            bits = remap_bits(bits, remap, unmapped)
        chosen = [(dur[i], i) for i in iter_bits(bits) if i in dur]
        out.append(json.dumps({"commit": commit, "workers": schedule(chosen)}, separators=(",", ":")))
        if VERBOSE:
            print(f"{commit}: {len(chosen)} tests {sum(d for d, _ in chosen)}ms", file=sys.stderr)
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
