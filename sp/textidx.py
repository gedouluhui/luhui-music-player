# -*- coding: utf-8 -*-
"""拼音首字母查表（零运行依赖）。

产品运行时只用这张离线生成的表（sp/_pinyin_table.py），不 import pypinyin。
为什么不用网传 23 项 GB2312 边界表：实测它会把 不/说/时/手/江/春 这些最常用的字整个
归错字母（首字母并不严格按 GBK 码分段对齐，边界一刀切就串组）——用错表做首字母索引，
等于把用户点名要修的 bug 再抄一遍。这里逐双字节存首字母，查一个字就是一次 O(1) 定位。

_首字母只认「标题/艺人名/专辑名」的第一个非空字符：
  · ASCII 字母 → 大写；数字与符号 → '#'（经典点唱机把这些归到 #，符合“R 开头”字面直觉）；
  · 汉字 → 编成 GBK 双字节查表；不在表里（GBK 覆盖不到的怪字/日文谚文等）→ '#'。
"""
from __future__ import annotations

from ._pinyin_table import TABLE, SLOT_COUNT

# 索引规则必须与 tools/gen_pinyin_table.py 的 idx_of 逐字一致；test_textidx.py 里拿生成器
# 那份实现交叉断言，防止两处悄悄改歪。（这里重写一份是为了产品不依赖 tools/。）
_TRAIL_LO = tuple(range(0x40, 0x7F)) + tuple(range(0x80, 0xFF))  # GBK 尾字节合法集（跳 0x7F）


def _slot_index(hi: int, lo: int) -> int:
    """GBK 双字节 → 查表串下标；不合法返回 -1。与生成器 idx_of 同式。"""
    if 0x40 <= lo <= 0x7E:
        off = lo - 0x40
    elif 0x80 <= lo <= 0xFE:
        off = 63 + (lo - 0x80)
    else:
        return -1
    if not (0x81 <= hi <= 0xFE):
        return -1
    idx = (hi - 0x81) * 190 + off
    return idx if 0 <= idx < SLOT_COUNT else -1


def pinyin_initial(s: str) -> str:
    """取一段文字的首字母：A–Z 或 '#'。空串按 '#'。"""
    if not s:
        return "#"
    # 跳过前导空白，找第一个“真字符”（'  江南' 归 J，不是 #）。
    for ch in s:
        if ch.isspace():
            continue
        o = ord(ch)
        if 0x41 <= o <= 0x5A:      # A-Z
            return ch
        if 0x61 <= o <= 0x7A:      # a-z
            return ch.upper()
        if o < 0x80:               # 其它 ASCII（数字、符号）→ #
            return "#"
        try:
            b = ch.encode("gbk")
        except (UnicodeEncodeError, LookupError):
            return "#"
        if len(b) != 2:
            return "#"
        i = _slot_index(b[0], b[1])
        if i < 0:
            return "#"
        letter = TABLE[i]
        return letter if letter != "-" else "#"
    return "#"  # 全是空白


def az_sort_key(s: str):
    """给「按拼音分组、组内按码点」排序用的键：先首字母，再原始串（NOCASE 近似）。"""
    return (pinyin_initial(s), (s or "").lower(), s or "")
