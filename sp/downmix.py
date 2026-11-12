# -*- coding: utf-8 -*-
"""多声道 → 立体声的**按曲预留**：不再对每一首 5.1 一律扣 −6 dB。

为什么改：原先 `DOWNMIX_TRIM = 0.501187` 是拿「最坏情况」（本机 1115 木乃伊，
下混后样点峰 1.6973）给**所有**多声道曲目垫背。量下来代价很大
（tools/_level_parity.log / tools/_downmix_policy.log）：

  · 林俊杰 5.1 FLAC：raw 峰 1.70、RMS −9.2 dBFS —— 扣 6 dB 后正好落在正常响度，
    是**该扣**的那一类；
  · Apple Music 的 Dolby Atmos（E-AC3 JOC）bed：raw 峰 0.84–1.29、RMS −21.7 dBFS
    —— 本来就不高，再扣 6 dB 等于白白压低，整组比立体声中位轻 16 dB。
    耳朵不会说「轻了 16 dB」，只会说「不如微软播放器清晰」。

所以改成：**量出这一首下混后的真峰，只扣到刚好不撞 ceiling 的程度**。
  · 真峰 1.70 → 留 0.93/1.70 = −5.2 dB（限幅器基本不介入）；
  · 真峰 1.18 → 0.93/1.18 = −2.1 dB（原先固定扣 6.00 dB，白扣 3.9 dB）；
  · 真峰 0.84 → 不扣（原先白扣 6 dB）；
  · 真峰 ≤ target → 1.0，一个样本都不动。

测量很贵（整轨解码 ≈ 88× 实时，4 分钟歌约 2.7 s），所以：
  · 结果按 (路径, 大小, mtime) 缓存在 `downmix_peaks.json`，换文件即失效；
  · 起播只查缓存；没查到就先用 −6.00 dB 兜底，并把这一首丢进后台队列慢慢量，
    **下一首/下一次播放**就用上了。不在起播路径上卡 2.7 秒。
  · `tools/warm_downmix_peaks.py` 可以把整个库提前暖一遍。

判据（tools/test_audioplay.py 里的纯函数用例 + tools/test_downmix_cache.py）：
  任何输入下 gain ∈ (0, 1]；立体声源与多声道输出恒 1.0；没量到 → 兜底；量到且
  不超 → 1.0；量到且超 → target/tp，且**乘完的真峰 ≤ target**。
"""
from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .winproc import hidden_popen_kwargs

TARGET_TP = 0.93
"""预留后希望达到的真峰（≈ −0.63 dBTP）。取 0.93 而不是贴着限幅器的 0.98：
量的是 96 kHz 下的真峰，换到别的设备率（44.1/48）重采样后真峰会有零点几 dB 的
漂移，留 0.05 的余量给这个漂移，也保证独占档（没有限幅器）不会撞到 1.0。"""

DOWNMIX_FALLBACK = 0.5011872336
"""没量到真峰时的兜底：−6.00 dB，即改版前的行为（宁可轻，不许削顶）。"""

MIN_GAIN = 0.25
"""最多扣 12 dB。再热的素材交给限幅器，不能因为一个离谱读数把声音压没。"""

CACHE_NAME = "downmix_peaks.json"
# v2：v1 的测量链把 `-ac 2` 写在滤镜之后，ebur128 拿到的仍是**源声道布局**（5.1 床），
# 于是 1115 量到 −0.0 dBFS（单声道最大值）而真正下混后的真峰是 +4.6 dBTP，
# 缓存里记 1.0 → 只扣 0.63 dB → 限幅器在 74% 的帧上做 4.1 dB 增益衰减。
# 换版本号会让旧读数整体作废（宁可重测，不拿错尺继续算）。
_CACHE_VERSION = 2
_MAX_ENTRIES = 8192


def gain_for(out_ch: int, source_ch: int, true_peak: Optional[float] = None) -> float:
    """这一首下混该乘多少。纯函数，不碰磁盘。

    · 只有「多声道源 → 立体声输出」才需要预留；
    · 没量到（None/0/负数）→ 兜底 −6.00 dB；
    · 量到了 → 只扣到真峰落在 TARGET_TP；
    · 结果永远在 (0, 1]，且乘完的真峰不会超过 TARGET_TP（除非触到 MIN_GAIN）。
    """
    if int(out_ch) != 2 or int(source_ch) <= 2:
        return 1.0
    try:
        tp = float(true_peak or 0.0)
    except (TypeError, ValueError):
        tp = 0.0
    # NaN/inf 只能来自坏缓存或坏解析：那不是「测到一个很热的峰」，是「没测到」。
    # 若把 NaN 交给 max(MIN_GAIN, NaN) 会悄悄落到 0.25（凭一个错误读数扣 12 dB）。
    if not math.isfinite(tp) or tp <= 1e-9:
        return DOWNMIX_FALLBACK
    g = TARGET_TP / tp
    if g >= 1.0:
        return 1.0
    return max(MIN_GAIN, g)


# ---------------------------------------------------------------- 缓存
_MEM: Optional[Dict[str, Dict[str, Any]]] = None
# 必须是 RLock：put() 持锁后再调 _load()，而 _load() 自己也要拿这把锁。
# 用普通 Lock 会在第一次「测量成功回写」时自锁死，后台线程永远卡住、缓存永远为空，
# 而且现象很安静（歌照放，只是永远退回兜底预留）。
_MEM_LOCK = threading.RLock()


def _cache_path() -> Path:
    from .config import data_root
    return data_root() / CACHE_NAME


def _key(path: str) -> Optional[str]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return "%s|%d|%.6f" % (os.path.normcase(os.path.abspath(path)), int(st.st_size),
                           float(st.st_mtime))


def _load() -> Dict[str, Dict[str, Any]]:
    global _MEM
    with _MEM_LOCK:
        if _MEM is not None:
            return _MEM
        data: Dict[str, Dict[str, Any]] = {}
        try:
            raw = json.loads(_cache_path().read_text(encoding="utf-8"))
            if isinstance(raw, dict) and int(raw.get("version") or 0) == _CACHE_VERSION:
                ent = raw.get("entries")
                if isinstance(ent, dict):
                    data = {str(k): v for k, v in ent.items() if isinstance(v, dict)}
        except Exception:
            data = {}
        _MEM = data
        return _MEM


def _save_locked(entries: Dict[str, Dict[str, Any]]) -> None:
    """原子落盘：临时文件 + os.replace。写失败不影响播放（缓存丢了只是下次再量）。"""
    p = _cache_path()
    try:
        if len(entries) > _MAX_ENTRIES:
            keep = sorted(entries.items(), key=lambda kv: str(kv[1].get("at") or ""),
                          reverse=True)[:_MAX_ENTRIES]
            entries = dict(keep)
        body = json.dumps({"version": _CACHE_VERSION, "entries": entries},
                          ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(prefix=".dmpeaks-", suffix=".json", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    except Exception:
        pass


def peak_for(path: str) -> Optional[float]:
    """查这一首下混后的真峰。没量过返回 None（调用方用兜底增益）。"""
    k = _key(path)
    if not k:
        return None
    ent = _load().get(k)
    if not ent:
        return None
    try:
        tp = float(ent.get("tp") or 0.0)
    except (TypeError, ValueError):
        return None
    return tp if tp > 0 else None


def lufs_for(path: str) -> Optional[float]:
    k = _key(path)
    if not k:
        return None
    ent = _load().get(k)
    if not ent:
        return None
    try:
        return float(ent["lufs"]) if ent.get("lufs") is not None else None
    except (TypeError, ValueError):
        return None


def put(path: str, tp: float, lufs: Optional[float] = None) -> None:
    k = _key(path)
    if not k or not tp or tp <= 0:
        return
    with _MEM_LOCK:
        ent = _load()
        ent[k] = {"tp": round(float(tp), 6),
                  "lufs": None if lufs is None else round(float(lufs), 2),
                  "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        _save_locked(ent)


# ---------------------------------------------------------------- 测量
def _ffmpeg() -> Optional[str]:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def measure(path: str, rate: int = 96000, timeout: float = 600.0) -> Optional[Dict[str, Any]]:
    """整轨解码成 rate/立体声，用 ebur128 读「Integrated loudness」与「True peak」。

    必须带 `-map 0:a:0`：m4a/flac 里常挂着封面图（video 流），不指定会去挑错流。

    **下混必须发生在滤镜图内部**：`-ac 2` 是输出选项，排在 `-af` 之后，ebur128 拿到
    的仍是 6 声道源（本机 1115 因此量到 −0.0 dBFS，而真正进限幅器的立体声是
    +4.6 dBTP）。这里用 `aresample=ochl=stereo:osr=…` 显式产出立体声，再量——
    和产品解码（ffmpeg `-ac 2`，同为 swresample 默认矩阵：FC/SL/SR 各 0.707、
    LFE 不混入）走同一套系数。

    返回 {"tp": 线性真峰, "lufs": 积分响度}；失败返回 None（不缓存失败，下次还会试）。
    """
    ff = _ffmpeg()
    if not ff or not os.path.isfile(path):
        return None
    cmd = [ff, "-v", "info", "-nostdin", "-i", path, "-map", "0:a:0",
           "-af", "aresample=ochl=stereo:osr=%d,ebur128=peak=true" % int(rate),
           "-f", "null", "-"]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=timeout, **hidden_popen_kwargs())
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    err = p.stderr.decode("utf-8", "replace")
    if p.returncode != 0:
        return None
    lufs = None
    tp_db = None
    in_tp = False
    for line in err.splitlines():
        s = line.strip()
        if s.startswith("True peak"):
            in_tp = True
            continue
        if s.startswith("I:") and "LUFS" in s:
            try:
                lufs = float(s.split("I:")[1].split("LUFS")[0].strip())
            except Exception:
                pass
        if in_tp and s.startswith("Peak:") and "dBFS" in s:
            try:
                tp_db = float(s.split("Peak:")[1].split("dBFS")[0].strip())
            except Exception:
                pass
    if lufs is None or tp_db is None:
        return None
    tp = float(10.0 ** (tp_db / 20.0))
    return {"tp": tp, "tp_db": tp_db, "lufs": lufs, "secs": round(time.time() - t0, 2)}


# ---------------------------------------------------------------- 后台队列
_Q: "queue.Queue[str]" = queue.Queue()
_PENDING = set()
_PENDING_LOCK = threading.Lock()
_WORKER: Optional[threading.Thread] = None
_WORKER_LOCK = threading.Lock()


def _worker_loop() -> None:
    while True:
        path = _Q.get()
        try:
            if _key(path) and peak_for(path) is None:
                r = measure(path)
                if r:
                    put(path, r["tp"], r.get("lufs"))
        except Exception:
            pass
        finally:
            with _PENDING_LOCK:
                _PENDING.discard(path)
            _Q.task_done()


def enqueue(path: str) -> bool:
    """把一首丢进后台测量队列。已在缓存/已在队列返回 False。"""
    if not path:
        return False
    k = _key(path)
    if not k or peak_for(path) is not None:
        return False
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_worker_loop, daemon=True,
                                       name="salt-downmix-peaks")
            _WORKER.start()
    with _PENDING_LOCK:
        if path in _PENDING:
            return False
        _PENDING.add(path)
    _Q.put(path)
    return True


def pending() -> int:
    with _PENDING_LOCK:
        return len(_PENDING)
