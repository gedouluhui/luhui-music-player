"""桌面歌词小窗要用的那一份「现在在放什么、放到第几秒」。

为什么进度不在小窗里直接读主窗口：主窗口最小化或收进托盘时，Chromium 会把它那条
rAF 冻住（这条是本机 QtWebEngine/Chromium 87 实测出来的，app.js 里闲置空转那笔账
就是靠它才量准的），页面自己不再报进度 —— 而桌面歌词存在的意义恰恰是
「主窗口不在眼前时也得有词」。所以：

    主窗口（有得报的时候）── POST /api/playback ──►  这里存一份带服务端时间戳的快照
    小窗（每 0.7s）      ── GET  /api/playback ──►  按 rate 外推，不问主窗口

外推只用服务端自己的单调时钟（time.monotonic），所以主窗口那条时间线冻没冻、
对表准不准都不影响读数；主窗口停止上报（崩溃/被杀）超过 TTL_S，快照判成 stale，
小窗据此停外推，不让一个死数字一路往上涨。

线程模型：ThreadingHTTPServer 每个请求一个线程，report 和 snapshot 会真并发，
所以状态放在锁后面；锁里只做字典读写和减法，不做 IO。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

# 倍速的安全夹取：**和前端 app.js 的 RATE_MIN/RATE_MAX 必须同档**（0.5…4）。
# 这里再夹一次不是不信前端，而是 POST 是公开接口，绕过界面直接塞个 rate=1e6 不该让外推跑飞。
# 为什么要写成「同档」而不是「更宽的内部包络」：前端那把 clamp 只决定**界面显示几倍**，
# 而原生档真正出声的那一路（api._native_post 把 settings.rate 交给 ffmpeg atempo）以前
# 谁都没夹 —— 于是「界面 4.00×、耳朵 9×」是能同时成立的。两把尺范围不一样，分家那天
# 红字会被当成产品 bug。同一性由 tools/test_playback_state.py 的 rate-range-matches-ui
# 看着（它读 web/app.js 的字面量比对，两边不共用常量）。
RATE_MIN = 0.5
RATE_MAX = 4.0
# 探针/开发实例的静音旗标（值的解释见 forced_silence）。名字只在这里写一遍：
# api._native_dsp、tools/restart_dev.py、tools/silence_gate.py 都从这里取，
# 免得三处各拼一遍字符串、拼错的那处从此静音不生效而没人响。
SILENCE_ENV = "SALTPLAYER_SILENCE"
TTL_S = 60.0            # 超过这个年龄还号称在播 = 主窗口已经不管这事了
POS_MAX_S = 24 * 3600.0  # 位置 sane check：没有人能把歌听完一天

_lock = threading.Lock()
_rep: Dict[str, Any] = {}          # 空字典 = 从来没收到过上报
_mini: Dict[str, bool] = {"on_top": True}   # 小窗置顶状态：Qt 侧真的应用了才写进来


def _num(v: Any, default: Optional[float] = None) -> Optional[float]:
    """把外界给的东西收成一个有限的 float；NaN/inf/字符串一律不认。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def clamp_rate(v: Any) -> float:
    """服务端唯一的倍速夹取。两个地方要用它，所以它是公开的：
      · report()（小窗/桌面词的外推）；
      · api._native_post 交给 ffmpeg 的那个 speed。
    第二条是本轮补上的：原生档出声的那一路以前谁都没夹（前端那把 clamp 只落在**元素**和
    **界面**上），绕过界面往 settings.json 塞 9，就会出现「界面 4.00×、耳朵与进度钟 9×」。
    夹不住的东西不许叫上限。"""
    r = _num(v)
    if r is None or r <= 0:
        return 1.0
    return min(RATE_MAX, max(RATE_MIN, r))


def forced_silence(env: Optional[Dict[str, str]] = None) -> bool:
    """这台实例是不是被要求「服务端增益钉死为 0」起的（探针/开发实例专用）。

    为什么要有它：盘上 `settings.json` 是和用户自己那份**共用**的，用户自己在用的值是
    `volume=1`。2026-10-13 那趟事故就是重启用户的 App 之后往那个实例 POST 了 `action=play`
    且 `gain=1.0`，桌上响了约 2 秒（原文见 功能对照表.md §10）。在探针进程里 monkeypatch
    护不住另一个进程 ⇒ 唯一站得住的形态是「起实例时就带上这道闸，开跑前再从那个实例读回
    它真的是 0」。这里只负责**认旗标**，改增益的是 api._native_dsp 那一处，读数由
    tools/silence_gate.py 那一把尺判。

    只认旗标，不碰盘上的 settings：写 `muted`/`volume` 去「静音」等于替用户改偏好，
    而且那是第二条路（两把尺会分家）。

    认 "1"/"true"/"yes"/"on"（大小写、前后空格都吃）；其余一律 False —— 包括 "0"。
    为什么宽吃这几种而不要求恰好 "1"：这道闸失效的后果是「半夜替用户外放」，
    宁可把 `SALTPLAYER_SILENCE=TRUE` 这种手滑也当静音，也别把它当没设。
    """
    import os
    src = env if env is not None else os.environ
    v = src.get(SILENCE_ENV)
    if not isinstance(v, str):
        return False
    return v.strip().lower() in ("1", "true", "yes", "on")


def report(body: Dict[str, Any]) -> Dict[str, Any]:
    """主窗口上报当前状态：位置是绝对值，覆盖式写入；时间戳取服务端这一侧。

    只认 track_id / position / playing / rate / duration 这五个键，其余原样丢掉 ——
    这份状态是小窗唯一的真值来源，宁可少也不能把没校验过的东西传过去。
    """
    tid_raw = body.get("track_id")
    tid = None
    if tid_raw is not None:
        try:
            tid = int(tid_raw)
        except (TypeError, ValueError):
            tid = None
    pos = _num(body.get("position"))
    if pos is not None:
        pos = min(max(0.0, pos), POS_MAX_S)
    dur = _num(body.get("duration"))
    if dur is not None:
        dur = min(max(0.0, dur), POS_MAX_S)
    with _lock:
        _rep.clear()
        _rep.update({
            "track_id": tid,
            "position": pos,
            "playing": bool(body.get("playing")),
            "rate": clamp_rate(body.get("rate")),
            "duration": dur,
            "t_mono": time.monotonic(),
        })
        return dict(_rep)


def snapshot() -> Dict[str, Any]:
    """带回外推后的位置。

    state 三档：
      idle  —— 从来没上报过（服务刚起来，主窗口还没活）
      stale —— 上报太旧：主窗口不报了，外推没有依据
      live  —— 正常，playing 时按 (now - 上报时刻) * rate 往前推
    外推的终点用 duration 夹住：真放完了主窗口会报下一首，夹住只是让夹缝里的
    读数不越界，不表示「这就是片尾」。
    """
    with _lock:
        if not _rep:
            return {"ok": False, "state": "idle", "track_id": None, "position": 0.0,
                    "playing": False, "rate": 1.0, "duration": None, "age": None,
                    "on_top": _mini["on_top"]}
        d = dict(_rep)
        on_top = _mini["on_top"]
    age = time.monotonic() - d["t_mono"]
    stale = age > TTL_S
    pos = d["position"] if d["position"] is not None else 0.0
    if d["playing"] and not stale:
        pos += age * d["rate"]
    if d["duration"]:
        pos = min(pos, d["duration"])
    return {"ok": not stale, "state": "stale" if stale else "live",
            "track_id": d["track_id"], "position": round(pos, 3),
            "playing": d["playing"] and not stale, "rate": d["rate"],
            "duration": d["duration"], "age": round(age, 3), "on_top": on_top}


def set_on_top(on_top: bool) -> None:
    with _lock:
        _mini["on_top"] = bool(on_top)


def reset() -> None:
    """测试用：把这份状态清回「从来没上报过」。"""
    with _lock:
        _rep.clear()
        _mini["on_top"] = True
