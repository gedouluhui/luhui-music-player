"""LRC lyric parsing: multi-timestamp lines, [offset:], bilingual pairing.

Output is a normalised structure the UI can render directly:
    {"lines": [{"t": 12.34, "text": "...", "trans": "..."}], "meta": {...}, "plain": bool}
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

TIME_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
META_TAG = re.compile(r"^\[([a-zA-Z#][a-zA-Z0-9_#@]{0,7}):(.*)\]\s*$")
CRLF = re.compile(r"\r\n?")
WORD_TS = re.compile(r"<\d{1,3}:\d{1,2}(?:\.\d{1,3})?>")  # per-word karaoke tags

ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "shift_jis", "utf-16", "latin-1")

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def decode_bytes(raw: bytes) -> str:
    for enc in ENCODINGS:
        try:
            txt = raw.decode(enc)
        except Exception:
            continue
        # a wrong-codec guess usually shows up as replacement noise
        if txt.count("\ufffd") <= max(1, len(txt) // 400):
            return txt
    return raw.decode("utf-8", "replace")


def read_text(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read(2_000_000)
    # decode first: CRLF is a *str* pattern, so it must never touch bytes
    txt = decode_bytes(raw)
    return CRLF.sub("\n", txt).replace("\x00", "")


def parse(text: str) -> Dict[str, Any]:
    """Parse an LRC (or plain-text) blob."""
    if not text or not text.strip():
        return {"lines": [], "meta": {}, "plain": False}

    meta: Dict[str, str] = {}
    offset_ms = 0.0
    entries: List[tuple[float, str]] = []
    saw_time = False

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        times = TIME_TAG.findall(line)
        stripped_tags = TIME_TAG.sub("", line)
        if not times:
            m = META_TAG.match(line)
            if m:
                key, val = m.group(1).lower(), m.group(2).strip()
                if key == "offset":
                    try:
                        offset_ms = float(re.sub(r"[^\d.\-+]", "", val) or 0)
                    except Exception:
                        offset_ms = 0.0
                else:
                    meta.setdefault(key, val)
                continue
            if WORD_TS.search(line):
                continue
            # Untimed line: keep as a static block only if nothing timed was found
            if not saw_time:
                entries.append((-1.0, line))
            continue
        saw_time = True
        body = WORD_TS.sub("", stripped_tags).strip()
        if not body:
            continue
        for mm, ss, frac in times:
            s_int = int(ss)
            if int(mm) < 0 or s_int > 59:  # '[99:99]' style garbage: skip rather than land at 6039s
                continue
            t = int(mm) * 60 + s_int + (int((frac or "0").ljust(3, "0")[:3]) / 1000.0)
            entries.append((t, body))

    if not saw_time:
        return {"lines": [], "meta": meta, "plain": True, "text": "\n".join(t for _, t in entries)}

    offset = -offset_ms / 1000.0  # LRC offset shifts lyrics *earlier* when positive
    entries.sort(key=lambda e: e[0])

    # merge duplicate timestamps into primary + translation
    buckets: Dict[float, List[str]] = {}
    order: List[float] = []
    for t, body in entries:
        key = round(t + offset, 3)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        if body not in buckets[key]:
            buckets[key].append(body)

    lines: List[Dict[str, Any]] = []
    for key in sorted(order):
        texts = [b for b in buckets[key] if b]
        if not texts:
            continue
        primary = texts[0]
        trans = texts[1] if len(texts) > 1 else ""
        lines.append({"t": key, "text": primary, "trans": trans})

    # Pair "A" then near-in-time "B" written in a different script as bilingual.
    i = 0
    while i < len(lines) - 1:
        cur, nxt = lines[i], lines[i + 1]
        if (
            not cur["trans"]
            and 0.05 <= nxt["t"] - cur["t"] <= 1.2
            and bool(CJK.search(cur["text"])) != bool(CJK.search(nxt["text"]))
        ):
            cur["trans"] = nxt["text"]
            lines.pop(i + 1)
        else:
            i += 1

    return {"lines": lines, "meta": meta, "plain": False}


def find_line(lines: List[Dict[str, Any]], t: float) -> int:
    """Index of the active lyric line at time *t* (bisect)."""
    if not lines:
        return -1
    lo, hi = 0, len(lines)
    while lo < hi:
        mid = (lo + hi) // 2
        if lines[mid]["t"] <= t:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1


def to_lrc(lines: List[Dict[str, Any]], meta: Optional[Dict[str, str]] = None) -> str:
    out = []
    for k, v in (meta or {}).items():
        if v:
            out.append(f"[{k}:{v}]")
    for ln in lines:
        t = max(0.0, ln["t"])
        stamp = f"[{int(t // 60):02d}:{t % 60:05.2f}]"
        out.append(f"{stamp}{ln['text']}")
        if ln.get("trans"):
            out.append(f"{stamp}{ln['trans']}")
    return "\n".join(out) + "\n"


def strip_lrc(text: str) -> str:
    """Plain display text from an LRC/USLT blob (for the 'no timing' panel)."""
    parsed = parse(text)
    if parsed.get("plain"):
        return parsed.get("text", "")
    return "\n".join(ln["text"] for ln in parsed["lines"])
