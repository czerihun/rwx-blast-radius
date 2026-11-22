"""Minimal unified-diff applier (the slim image has no `patch`). usage: applypatch.py <root> <patch>"""
import re, sys, os

root, pfile = sys.argv[1], sys.argv[2]
text = open(pfile, encoding="utf-8").read().split("\n")
i = 0
cur = None
while i < len(text):
    line = text[i]
    if line.startswith("+++ "):
        path = line[4:].split("\t")[0]
        cur = path[2:] if path.startswith("b/") else path
        full = os.path.join(root, cur)
        src = open(full, encoding="utf-8").read().split("\n")
        out = []
        pos = 0  # next unconsumed index in src
        i += 1
        while i < len(text) and (text[i].startswith("@@")):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", text[i])
            a = int(m.group(1)); oc = 1 if m.group(2) is None else int(m.group(2))
            start = a - 1 if oc else a
            out.extend(src[pos:start]); pos = start
            i += 1
            while i < len(text) and text[i][:1] in (" ", "-", "+", "\\"):
                t = text[i]
                if t[:1] == " ":
                    assert src[pos] == t[1:], (cur, pos, src[pos], t)
                    out.append(src[pos]); pos += 1
                elif t[:1] == "-":
                    assert src[pos] == t[1:], (cur, pos, src[pos], t)
                    pos += 1
                elif t[:1] == "+":
                    out.append(t[1:])
                i += 1
        out.extend(src[pos:])
        open(full, "w", encoding="utf-8").write("\n".join(out))
        continue
    i += 1
