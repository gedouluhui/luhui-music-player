# -*- coding: utf-8 -*-
"""Ad-hoc check for the sidecar lyric read path (run: python tools/test_lyrics.py)."""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sp import lyrics as L  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "示例曲库")

fail = 0
for folder, _dirs, files in os.walk(DEMO):
    for fn in sorted(files):
        if not fn.lower().endswith((".lrc", ".txt")):
            continue
        p = os.path.join(folder, fn)
        try:
            txt = L.read_text(p)
        except Exception as e:
            print("READ-FAIL", os.path.relpath(p, ROOT), repr(e))
            fail += 1
            continue
        parsed = L.parse(txt)
        lines = parsed.get("lines") or []
        trans = sum(1 for ln in lines if ln.get("trans"))
        print("OK", os.path.relpath(p, ROOT), "lines", len(lines), "trans", trans,
              "plain", parsed.get("plain"), "meta", json.dumps(parsed.get("meta"), ensure_ascii=False))
        if lines:
            print("    first:", json.dumps(lines[0], ensure_ascii=False))
            print("    last :", json.dumps(lines[-1], ensure_ascii=False))
        if not lines and not parsed.get("plain"):
            print("    !! no lines and not plain")
            fail += 1

# a direct bytes-vs-str regression guard
try:
    L.read_text.__doc__
    out = L.decode_bytes(b"[00:01.00]\xe4\xbd\xa0\xe5\xa5\xbd\r\n[00:03.50]hi\r\n")
    assert "\r" not in L.CRLF.sub("\n", out), "CRLF left"
    print("GUARD crlf+bom ok")
except Exception as e:
    print("GUARD-FAIL", repr(e))
    fail += 1

print("RESULT", "PASS" if fail == 0 else f"FAIL({fail})")
sys.exit(1 if fail else 0)
