# -*- coding: utf-8 -*-
"""椒盐音乐 · 原生播放引擎（可选，默认仍走 Chromium <audio>）

为什么要有这一层：浏览器的 <audio> 把一切重采样到 44.1k/16bit 再经系统混音器，
高解析 FLAC/DSD 拿不到「原生 bit-perfect」，独占 WASAPI、浮点解码链路、以及椒盐那些
「会员音质」项（原生解码 / 独占 / 目标位深 / 响度均衡 / 硬件直通）在浏览器里根本做不出来。
这一层把这些落到服务端：

    文件 --ffmpeg--> float32 交织 PCM --> [numpy/scipy DSP] --> pyaudiowpatch WASAPI 输出
                       (解码/重采样/变速)   音量·平衡·EQ·响度·淡入淡出    共享 / 独占

设计取舍（都写进代码注释，别只留在这段话里）：
  · 解码交给 ffmpeg 子进程而不是 Python 库：imageio-ffmpeg 自带 7.1，能吃 flac/ape/
    dsf/dsd/wav/mp3/m4a/ogg… 一套到底，输出统一 `-f f32le`（这就是「浮点解码」——
    全链路 float32，不在中途回落整数、不叠加量化误差）。
  · 输出用 WASAPI 共享做默认（稳），独占做可选「原生」档：独占要求设备跑文件原生采样率、
    期间独占设备、系统音量失效，任何一条不满足就打不开 —— 打不开就**如实回落到共享**并
    留下失败原因，绝不假装在跑原生。独占这一档在本机自动化里无法安全实测（它会锁住用户
    的声卡），所以默认关，且文档里标「未实测」。
  · 真出声由 `preview()` 之外的 load/play 路径负责，而 load/play **只在用户把
    audio_engine 切到 native 后**才被调用；测试与自检一律走 `preview()`（只解码+DSP，
    不开设备、不出声），这样在用户机器上验链路不会突然放出声音。

状态：模块级单例 + 一把锁；同一时刻只放一首。前端在 native 档下把 <audio> 让位，
改轮询 /api/native/state 与 /api/native/spectrum。
"""
from __future__ import annotations

import collections
import functools
import os
import queue
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

import numpy as np

try:
    import imageio_ffmpeg
except Exception:  # pragma: no cover - 缺依赖时整套原生档应不可用
    imageio_ffmpeg = None

try:
    import pyaudiowpatch as _pa
except Exception:  # pragma: no cover
    _pa = None

from scipy import signal

from .limiter import TruePeakLimiter, latency_frames
from .downmix import DOWNMIX_FALLBACK, gain_for

# 多声道 → 立体声的预留。**不再是一刀切 −6 dB**：改由 sp/downmix.py 按每一首
# 下混后的实测真峰算（只扣到刚好不撞 ceiling）。DOWNMIX_TRIM 这个名字保留，
# 它现在是「没量到真峰时的兜底」——也就是改版前的行为，测试与旧文档都还认得它。
DOWNMIX_TRIM = DOWNMIX_FALLBACK

# --------------------------------------------------------------- 诊断迹线
# SALTPLAYER_ENGINE_TRACE=1 ⇒ 把「谁结束了这一曲」写成带时间戳的一行行进日志
# （后端 stdout，被 BackendService 收进 backend.log）。默认关闭，行为零改动；
# 唯一的例外：开启时 ffmpeg 的 stderr 从 DEVNULL 改成收集尾部若干行进迹线——
# 「提前 EOF」到底是文件读完还是 ffmpeg 死掉，只有它说得清。
_TRACE_ON = bool(os.environ.get("SALTPLAYER_ENGINE_TRACE"))
_TRACE_T0 = time.monotonic()


def _trace(_tag: str, **kw: Any) -> None:
    if not _TRACE_ON:
        return
    parts = []
    for k, v in kw.items():
        if isinstance(v, float):
            parts.append("%s=%.3f" % (k, v))
        else:
            parts.append("%s=%s" % (k, v))
    try:
        print("[eng-trace %9.3f %s] %s %s"
              % (time.monotonic() - _TRACE_T0, threading.current_thread().name,
                 _tag, " ".join(parts)), flush=True)
    except Exception:
        pass


def downmix_trim_for(out_ch: int, source_ch: int, true_peak: Optional[float] = None) -> float:
    """只有「多声道源下混进立体声」才预留；其余一律 1.0（整段乘法被跳过）。

    true_peak 给了就按它算最小预留，没给就走兜底（−6.00 dB）。
    纯函数，自检可以在不碰设备的前提下把每一档都验一遍。
    """
    return gain_for(out_ch, source_ch, true_peak)


# ------------------------------------------------------------- PortAudio 全局闸
# pa_init / pa_terminate / 设备查询 / 开流**不是线程安全的**，而且本机实测还带**线程黏性**：
# 谁开流，就必须和 Pa_Initialize 在同一根线程上。判别实验（tools/_diag_reopen.py，四组）：
#     A 只 play/stop、从不枚举设备                      → 3/3 次 play 全成
#     B 同一线程「枚举+play」，换个线程 stop，再重复      → 第 1 次成，之后每次 [Errno -9999]
#     C 线程甲只枚举一次设备，线程乙 play                → **第一次**就 -9999
#     D 枚举后把常驻实例 terminate 掉，再在新线程 play    → 又成了
# 为什么这会咬到用户：服务端是 ThreadingHTTPServer，每个请求一根线程、请求完就死。以前
# `_PA_ENUM` 这个常驻实例让 Pa_Terminate 永远到不了 0，WASAPI 的枚举器/COM 环境就挂在
# **第一个请求线程**上；那根线程一死，下一首歌在**新线程**上开流只剩 -9999 —— 症状正是
# 「第一首能放，换歌/重播没声，选独占反而有」（独占走 sp/wasapi.py 的 COM，不经 PortAudio）。
# 所以纪律不是「加一把锁」而是「**所有 Pa_* 调用都进唯一一根 pa-ctrl 线程**」（_pa_call），
# 它和进程同寿：实例、枚举器、开流/关流全在同一根活线程上，黏性不再是坑，并发也顺带串行化
# （8 线程 × 3 轮「无锁必 0xC0000005」的判别实验见 tools/probe_pa_concurrency.py）。
# 锁序纪律：_pa_call 里绝不取 self._lock；self._lock 里也绝不调 _pa_call
# （播放回调只碰 self._lock，所以一次枚举卡不住实时音频，反之亦然）。
_PA_CTRL_LOCK = threading.Lock()     # 只保护「启动那根线程」，不保护调用本身
_PA_CTRL: Optional[threading.Thread] = None
_PA_CTRL_Q: "queue.Queue" = queue.Queue()
_PA_ENUM = None                      # 常驻枚举实例：只在 pa-ctrl 线程上建/毁


def _pa_ctrl_worker() -> None:
    while True:
        item = _PA_CTRL_Q.get()
        if item is None:             # 进程退出用的哨兵（目前没人放，留给以后）
            return
        fn, args, kwargs, box, done = item
        try:
            box["r"] = fn(*args, **kwargs)
        except BaseException as e:   # 原样带回调用线程：Errno -9999 的原文不能丢
            box["e"] = e
        finally:
            done.set()


def _pa_call(fn, *args, **kwargs):
    """把一次 PortAudio 调用送到 pa-ctrl 线程上执行，等结果（异常原样抛回）。

    不能退化成「一把锁 + 随便哪根线程」：锁只保证不并发，不保证同线程；见文件头实验 C。
    同一个 worker 里再调（嵌套）就直接执行，免得自己等自己。
    """
    global _PA_CTRL
    if threading.current_thread() is _PA_CTRL:
        return fn(*args, **kwargs)
    with _PA_CTRL_LOCK:
        if _PA_CTRL is None or not _PA_CTRL.is_alive():
            th = threading.Thread(target=_pa_ctrl_worker, name="pa-ctrl", daemon=True)
            th.start()
            _PA_CTRL = th
    box: Dict[str, Any] = {}
    done = threading.Event()
    _PA_CTRL_Q.put((fn, args, kwargs, box, done))
    while not done.wait(0.5):
        if _PA_CTRL is None or not _PA_CTRL.is_alive():
            # 不无限等：线程没了还等下去就是「点播放永远转圈」。
            raise RuntimeError("pa-ctrl 线程已退出，PortAudio 调用未完成")
    if "e" in box:
        raise box["e"]
    return box.get("r")


# ------------------------------------------------------------ WASAPI 独占那半边
# 独占（bit-perfect 那条路）由 sp/wasapi.py 里手写的 COM 虚表承担：pyaudiowpatch 没有
# PaWasapiStreamInfo，自带 ffmpeg 也没有 wasapi 复用器，这台机器上没有第三条独占路。
# import 只做「模块导入」——真正加载 ole32/appcore 排在 wasapi._libs() 里（第一次调用才做），
# 所以在缺库/非 Windows 的机器上 import sp.audioplay 仍成功，只是独占协商会老实回一句
# 「本机导入 sp.wasapi 失败」并走共享档（fail-closed，不是崩）。
try:
    from . import wasapi as _ws
except Exception:  # pragma: no cover - 被当顶层脚本跑时没有包上下文
    try:
        from sp import wasapi as _ws
    except Exception:
        _ws = None

# 端点枚举 / 能力探针这两类 COM 调用：**不占有设备**（一次都不调 Initialize），所以用户
# 在场也能跑；但它们慢（能力探针一张表 33 次 IsFormatSupported），所以各带一份缓存。
# 锁序纪律（和 pa-ctrl 那条同构）：_WA_LOCK 里绝不取 self._lock、绝不调 _pa_call、
# 绝不取 _dev_lock；self._lock 与 _dev_lock 里也绝不取 _WA_LOCK。
# 为什么这条必须写死：真机第一次翻出 PortAudio 那把锁的坑就是「枚举和开流并发的形状」，
# 独占侧同样是「设置页枚举端点」和「POST play 里协商」并发。
_WA_LOCK = threading.Lock()
_WA_TTL = 300.0                      # 秒。拔设备/换驱动之后最多 5 分钟自然失效
_WA_ENUM = {"ts": 0.0, "rows": None}
_WA_CAPS: Dict[Any, Dict[str, Any]] = {}


def _now() -> float:
    return time.monotonic()


def _wasapi_endpoints(force: bool = False) -> List[Dict[str, Any]]:
    """COM 侧渲染端点列表 [{"index","id","name"}]；不可用时回空列表（不抛）。"""
    if _ws is None:
        return []
    with _WA_LOCK:
        if force or _WA_ENUM["rows"] is None or (_now() - _WA_ENUM["ts"]) > _WA_TTL:
            try:
                _WA_ENUM["rows"] = _ws.device_ids()
            except Exception:
                # 枚举失败时**不缓存空表**：下一次调用还要真去问一遍，
                # 否则一次 COM 抽风就把独占判死到进程结束（那是把环境故障记成产品结论）。
                _WA_ENUM["rows"] = None
                return []
            _WA_ENUM["ts"] = _now()
        return list(_WA_ENUM["rows"] or [])


def _wasapi_caps(device_id: str, ch: int) -> Optional[Dict[str, Any]]:
    """某端点的独占能力表（mix 格式、设备周期、33 档候选）。失败回 None，同样不缓存。"""
    if _ws is None or not device_id:
        return None
    key = (device_id, int(ch))
    with _WA_LOCK:
        hit = _WA_CAPS.get(key)
        if hit and (_now() - hit["ts"]) < _WA_TTL:
            return hit["caps"]
        try:
            caps = _ws.probe_device(device_id, ch)
        except Exception:
            return None
        if not caps.get("table"):
            return None
        _WA_CAPS[key] = {"ts": _now(), "caps": caps}
        return caps


# ----------------------------------------------------------------- EQ / DSP
# 十段中心频率/类型/行程必须和前端 web/app.js 的 EQ_HZ / EQ_TYPE / EQ_RANGE 一模一样：
# 同一个「均衡器」在浏览器档和原生档听感不一致是不可接受的，所以这里是唯一事实的第二副本，
# 改一处必改两处（tools/test_audioplay.py 里有把它们对齐的哨兵测试）。
EQ_HZ = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
EQ_TYPE = ["lowshelf"] + ["peaking"] * 8 + ["highshelf"]
EQ_RANGE = 12.0
EQ_Q = 1.0  # shelf 段 Q 无意义，peaking 段取 1.0 与浏览器 BiquadFilter 默认带宽接近


def _rbj_section(kind: str, db: float, f0: float, fs: float, q: float,
                 shelf_s: float = 1.0):
    """RBJ Audio EQ Cookbook 双二阶，返回归一化 sos 行 [b0,b1,b2,1,a1,a2]。

    scipy.signal 没有现成的 peaking/shelf 设计器（只有 iirpeak/butter），而图形均衡器
    要的就是 Cookbook 那三条闭式解 —— 直接写死，反而比凑 butter 更像浏览器 BiquadFilter。
    浏览器档（WebAudio BiquadFilter）用的也是同一套 Cookbook，所以两档系数可对得上。
    """
    A = 10.0 ** (db / 40.0)
    w0 = 2.0 * np.pi * min(f0, fs / 2 - 1) / fs
    cw, sw = np.cos(w0), np.sin(w0)
    if kind == "peaking":
        al = sw / (2.0 * q)
        b0, b1, b2 = 1.0 + al * A, -2.0 * cw, 1.0 - al * A
        a0, a1, a2 = 1.0 + al / A, -2.0 * cw, 1.0 - al / A
    else:  # lowshelf / highshelf
        al = sw / 2.0 * np.sqrt((A + 1.0 / A) * (1.0 / shelf_s - 1.0) + 2.0)
        k = 2.0 * np.sqrt(A) * al
        if kind == "lowshelf":
            b0 = A * ((A + 1) - (A - 1) * cw + k)
            b1 = 2 * A * ((A - 1) - (A + 1) * cw)
            b2 = A * ((A + 1) - (A - 1) * cw - k)
            a0 = (A + 1) + (A - 1) * cw + k
            a1 = -2 * ((A - 1) + (A + 1) * cw)
            a2 = (A + 1) + (A - 1) * cw - k
        else:  # highshelf
            b0 = A * ((A + 1) + (A - 1) * cw + k)
            b1 = -2 * A * ((A - 1) + (A + 1) * cw)
            b2 = A * ((A + 1) + (A - 1) * cw - k)
            a0 = (A + 1) - (A - 1) * cw + k
            a1 = 2 * ((A - 1) - (A + 1) * cw)
            a2 = (A + 1) - (A - 1) * cw - k
    return np.array([b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0], np.float64)


def _sos_eq(gains_db: List[float], sample_rate: float):
    """把十段增益（dB）编成 scipy 的 SOS（二阶节级联）。空/全 0 返回 None（不滤波）。

    用 sosfilt 而不是一堆 tf：sos 对系数舍入不敏感，10 段串起来也不会像 tf 那样数值发散。
    返回的 sos 是 (N,6)，配一个 (N,2,ch) 的状态矩阵给逐块流式滤波。
    """
    sections = []
    for g, hz, kind in zip(gains_db, EQ_HZ, EQ_TYPE):
        if abs(g) < 1e-3:
            continue
        sections.append(_rbj_section(
            kind, g, hz, sample_rate, EQ_Q if kind == "peaking" else 0.7071))
    if not sections:
        return None
    return np.vstack(sections)


def _new_sos_state(sos, ch: int):
    return np.zeros((len(sos), 2, ch))


# ffmpeg 的 atempo 单节只吃 0.5–2.0，超界必须串联（椒盐的变速到 4×）。
ATEMPO_MIN, ATEMPO_MAX = 0.5, 2.0
ATEMPO_MAX_SECTIONS = 12          # 4× 用 2 节，0.1× 用 5 节；到 12 还没收敛就是算错了


def atempo_chain(speed: float) -> List[float]:
    """把想要的倍速拆成一串 ffmpeg atempo 因子（每节 0.5–2.0）。

    乘起来＝**音频真的被拉到几倍**。所以 _decode（离线预览）和 _reader_loop（出声的那条流）
    必须共用这一份：两边各写各的，就会有一边「界面上 4×、耳朵里 2×」——
    2026-10-14 静态审计查出来的正是这个形状：_reader_loop 原先写的是
    `atempo={max(0.5, min(2.0, speed))}`，单节加夹取，于是流式播放最高只有 2×，
    而进度外推 `_pos_src_locked()` 乘的是**请求值** self._speed，4× 时进度条以两倍速前进。
    低速那头同病：0.25× 会被夹到 0.5×，听到的和界面标的不是同一个数。
    """
    want = float(speed or 1.0)
    if want <= 0:
        return []
    if abs(want - 1.0) <= 1e-3:
        return []
    factors: List[float] = []
    rest = want
    while abs(rest - 1.0) > 1e-3 and len(factors) < ATEMPO_MAX_SECTIONS:
        t = min(ATEMPO_MAX, rest) if rest > 1.0 else max(ATEMPO_MIN, rest)
        factors.append(round(t, 4))
        rest = rest / t
    return factors


def atempo_product(factors: List[float]) -> float:
    """这串因子实际合成出来的倍速（空串＝1×）。"""
    out = 1.0
    for f in factors:
        out *= float(f)
    return out


def atempo_filter(factors: List[float]) -> str:
    """因子串 → ffmpeg 的 -filter:a 值（''＝不加变速滤镜）。"""
    return ",".join("atempo=%.4f" % f for f in factors)


# 重采样规格只留这一份。swr 默认 16 抽头 / 0 相位在 44.1→96 时把 20 kHz 压掉
# 0.48 dB、24.1 kHz 还留 −43 dB 镜像；256 抽头 / 24 相位 / 关线性插值把 20 kHz
# 拉回 0.00 dB、镜像压到 −107 dB（tools/probe_resample.py 实测）。源率＝目标率时
# swr 逐位直通，所以独占 bit-perfect 那条路一个字节都不会变。
RESAMPLE_TAPS = 256
RESAMPLE_PHASES = 24
RESAMPLE_CUTOFF = 0.98


def resample_filter(rate: int) -> str:
    return ("aresample=%d:filter_size=%d:phase_shift=%d:linear_interp=0:cutoff=%.2f"
            % (int(rate), RESAMPLE_TAPS, RESAMPLE_PHASES, RESAMPLE_CUTOFF))


def stream_filters(speed: float, rate: int) -> str:
    """变速 + 重采样的完整 -filter:a 串（''＝不加滤镜）。

    离线预览 `_decode` 和**真正出声的** `_reader_loop` 必须共用这一份。2026-11-06
    就是在这里抓到两条路分家的：HQ 重采样只写进了 `_decode`（预览），播放那条流
    仍是 swr 默认 16 抽头 —— 量到的「20 kHz −0.48 dB」在用户耳朵里一点没修。
    """
    parts: List[str] = []
    filt = atempo_filter(atempo_chain(speed))
    if filt:
        parts.append(filt)
    if int(rate or 0) > 0:
        parts.append(resample_filter(rate))
    return ",".join(parts)


def _hidden_popen_kwargs() -> Dict[str, Any]:
    """Windows 上 spawn ffmpeg 时不许它弹控制台窗口（实现与说明见 sp/winproc.py）。

    留这个名字是因为它已经被自检与多处 spawn 引用；新代码可以直接用
    `from .winproc import hidden_popen_kwargs`。
    """
    from .winproc import hidden_popen_kwargs
    return hidden_popen_kwargs()


# --------------------------------------------------------------------- 引擎
def _serialized_control(fn):
    """play/seek 同一时刻只许跑一个。

    2026-11-08 真机事故（用户报「《配合》一直有回声和卡顿」）：桌面端在恢复播放的窗口里
    连发了两次 play，后端同一个进程下出现**两条解码同一文件、-ss 相同、创建在同一秒**的
    ffmpeg；两条读线程把各自的 50ms 块交错喂进同一个环形 deque ⇒ 每一段音频都被播两遍，
    听感就是持续的回声＋卡顿（应用内 underruns 仍为 0、bit-perfect 仍为真，因为样本本身
    没被动过）。根因不是设备也不是解码器：任何两个控制请求同时进引擎都会长成这个形状。
    锁用 RLock：play 的独占回退分支会递归调用自己。
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._ctl:
            return fn(self, *args, **kwargs)
    return wrapper


class NativeEngine:
    """一个进程内只应有一个；对外用模块级 get() 拿单例。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 控制面互斥（play/seek 整段串行，见 _serialized_control 的真机事故）。不保护
        # pause/resume/stop：那三条是「立刻生效」的意图，挡住它们只会让暂停变迟钝。
        self._ctl = threading.RLock()
        self._stop_evt = threading.Event()
        self._paused = True
        self._ffmpeg: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stream = None
        self._pa = None
        # 环形：deque[ (np.float32 (frames,ch)) ]，读线程塞、设备回调取
        self._chunks: collections.deque = collections.deque()
        self._frames_avail = 0
        self._cond = threading.Condition(self._lock)
        self._max_frames = 96000 * 2 * 2     # ~2s @96k 立体声的背压上限
        # 灌数闸门（_callback 里那段）：seek/起播后环形先是空的，而设备流已经在跑。
        # 默认零值＝闸门永不生效，所以未走 play/seek 的路径（含单测直接调回调）行为不变。
        self._priming = False
        self._prime_frames = 0
        self._prime_until = 0.0
        # 时钟
        self._rate = 44100
        self._ch = 2
        self._base_pos = 0.0                 # 起播/seek 的绝对秒数
        self._consumed = 0                   # 起播后已送进设备的帧数（暂停时不涨）
        self._duration = 0.0
        self._ended = True
        self._path = ""
        # DSP 状态
        self._gain = 0.8
        self._balance = 0.0                  # -1..1，负=偏左
        self._sos = None
        self._sos_state = None
        # 当前进引擎的那十条增益（dB）：只给 state() 回显用，不参与算声。
        # 为什么要留这份：均衡器在原生档是服务端 numpy 双二阶，前端 WebAudio 那条 _eqLive
        # 永远不会亮，面板/验收闸就没法回答「曲线到底进引擎了没有」。回显这份读数，
        # 「设置里的十段 == 引擎里正在用的十段」才是一条能屏上量的断言。
        self._gains_db = [0.0] * 10
        # 「这条曲线到底有没有被 configure 收进去」；和 active 是两件事：
        #   applied=False 且 active=False：还没配过（例如原生引擎刚起、前端还没推设置）；
        #   applied=True  且 active=False：配过一条全 0 曲线，引擎里就是没有滤波器。
        # 前端那句「已开启，但十条增益都是 0」只能在后一种里说，否则是替服务端猜原因。
        self._eq_applied = False
        self._loudness = False
        self._agc_peak = 0.0
        self._target_peak = 0.95
        self._fade_in_frames = 0
        # 切歌「电流声」的解药：上一首被 Stop 截在非零样本、下一首在 priming 静音后硬跳到
        # 非零首样本，两处阶跃都会通过音响放出一声爆音。起播头强制铺一段极短淡入（不依赖
        # 用户那个可听淡入偏好），放流前再从最后一帧线性收到 0。_last_out 是「最近一拍真正
        # 送进设备的最后一帧」，淡出从它起爬，保证淡出本身也不引入新阶跃。
        self._last_out = None
        # 「最近一拍真送进设备的整段波形」（最多一个设备周期）。淡出拿它倒放再乘斜坡：
        # 把末帧当直流收到 0 是一段 10ms 的低频能量，听感上是一声很轻的「噗」——
        # 用户报的「还有非常轻微的一声」很可能就是它。倒放起点恰是已播出的末帧
        # （值连续），频谱仍在音乐带内，不会凭空多出一坨低频。
        self._last_tail = None
        # 切歌**不重开流**时的过桥：起播方把这一位置真，泵/回调在下一拍先把
        # 「上一首的尾巴」线性收到 0，再让新曲接管。没有这一拍，硬切就是一声爆音。
        self._switch_fade = False
        # 虚拟扬声器（耳机交叉馈电）：把对侧声道经 0.3 ms 延时、700 Hz 一阶低通后少量混入，
        # 模拟「前方一对音箱」以减轻耳机的头中效应。**关闭时 _process_block 一个样本都不加**，
        # 所以关掉就是 bit-exact 直通。这不是多声道 HRTF/卷积环绕，界面上也照这样说。
        self._speakers = False
        self._speakers_amount = 0.35      # 0..1，映射到交叉增益 g=0.6×amount
        self._cf_zi = None                # 一阶低通的滤波器状态 (ch,1)
        self._cf_hist = None              # 延时线历史 (delay,ch)
        # 共享档的削顶处理（见 sp/limiter.py 头注）。下混余量为什么不再写死 −6 dB：
        # `-ac 2` 的下混矩阵不预留余量，实测 5.1 的 1115/1149 峰 1.697/1.726；
        # 但同一个库里 Apple Music 的 Atmos bed 只有 0.84–1.29（RMS −21.7 dBFS），
        # 一刀切 −6 dB 会把它们白压轻（整组比立体声中位轻 16 dB）。
        # 所以改成按每一首的实测真峰算最小预留（sp/downmix.gain_for），没量到才用
        # 0.501187 兜底；限幅器只在残余峰上动。独占档不走限幅器（承诺是 bit-perfect），
        # 但下混预留照样要乘——5.1 源本来就不可能 bit-perfect（界面按「下混」列阻断原因）。
        self._downmix_trim = 1.0
        self._source_tp = 0.0
        self._downmix_basis = "none"
        self._lim_on = False
        self._lim = None
        self._speed = 1.0
        # 音频**实际**被拉到的倍速（atempo 因子串的乘积）。为什么要单存一份并回显：
        # self._speed 是「请求值」，界面/进度都按它走；只有把合成值也报出来，
        # 「标的 4× 是不是真播 4×」才是一条能证的断言，而不是靠读代码保证。
        self._atempo = 1.0
        self._eof = True
        # 读线程「代际」：每次 play/seek/stop 递增，线程带着自己那一代的号跑，
        # _mark_eof 只认当代的号 —— 上一代线程被 terminate 后在 finally 里补的那记 EOF
        # 不许落到新一段的队列上（见 _mark_eof 的实测事故说明）。
        self._gen = 0
        # 频谱
        self._spec = np.zeros(2048, np.float32)
        self._spec_pos = 0
        # 诊断
        self._underruns = 0
        self._mode = "shared"
        self._native_note = ""
        # 最近一次「不是用户主动停止」的解码错误。None＝没出错。
        # 形状 {"kind": "decode_failed"|"decoder_unavailable", "detail", "rc", "frames"}。
        # 为什么单独存一份而不是复用 _ended：ended 在前端是「自然播完，可以跳下一首」
        # 的边沿；解码失败必须停在这一首并说明原因，两者不能混。
        self._error: Optional[Dict[str, Any]] = None
        # ------------------------------------------------------------ 独占侧
        # 为什么要有 _dev_lock 这把**独立**的锁：设备流的「开」和「关」必须和泵线程互斥，
        # 但都不能握着 self._lock 做——Initialize/Close 是几十毫秒级的 COM 调用，压在实时
        # 锁上就是给共享档的回调制造空拍。锁序：_dev_lock → _pa_call（_open_stream 内部），
        # _dev_lock 里绝不取 self._lock。
        self._dev_lock = threading.RLock()
        # 独占流的「指针」单独用一把微锁护：只护赋值这两行，里面一个 COM 调用都不做，
        # 这样 state()（每秒轮询）不会被一次 Initialize 卡住。
        self._ex_lock = threading.Lock()
        self._ex = None                    # ExclusiveStream 或 None
        self._ex_params: Optional[Dict[str, Any]] = None   # 重开同一条流要的全部参数
        self._ex_rec: Optional[Dict[str, Any]] = None      # 本次开流回执（含 GetBufferSize）
        self._ex_pump: Optional[threading.Thread] = None
        self._ex_stop: Optional[threading.Event] = None
        # 「施工图交给泵去开流」那只信封：(info, box, ready)。建流的人必须是用流的人，
        # 所以 play 的请求线程只写这个信封，一次 COM 都不做（原因见 _open_exclusive）。
        self._ex_pending: Optional[Any] = None
        self._ex_requested = False         # 这一档**请求**过独占（哪怕没开成）
        self._ex_released = ""             # 什么时候、为什么放的（界面要说「已释放」不是「在独占」）
        self._device_index: Optional[int] = None
        # 共享流真正落到的 WASAPI 端点索引（state 回显用；也是「为什么没声音」那条
        # 设备/宿主 API 错配能被一眼看出来的地方）。
        self._shared_device_index: Optional[int] = None
        # 「源」的三个参数只用来判 bit-perfect，不参与算声：
        # 设备侧率/位深/声道数对得上源、且全链路一个样本都没动过，才敢说 bit-perfect。
        # 默认一律 **0＝没量到**，不是 24：早先这里写死 24 当默认，等于替每一首歌假设它是
        # 24 位源，于是「设备只有 16 位独占」这句会把所有 16 位 CD rip 也说成量化过——
        # 猜错的方向不重要，猜了才重要（判据不许建立在猜测上）。
        self._source_rate = 0
        self._source_bits = 0
        self._source_ch = 0
        # 抖动开关（设置里 audio_dither）：只在「确实要量化」那一档生效，见 _negotiate_exclusive。
        self._dither_enabled = True
        self._fade_in_sec = 0.0

    # -------------------------------------------------------------- 能力探测
    @staticmethod
    def available() -> bool:
        return imageio_ffmpeg is not None and _pa is not None

    @staticmethod
    def ffmpeg_exe() -> Optional[str]:
        try:
            return imageio_ffmpeg.get_ffmpeg_exe() if imageio_ffmpeg else None
        except Exception:
            return None

    def devices(self) -> List[Dict[str, Any]]:
        """WASAPI 输出设备列表（含默认率与声道）。只读，不开流。

        「哪台是默认」要问 WASAPI 这个 host API，不能问全局默认：本机实测过这个坑 ——
        `get_default_output_device_info()` 给的是全局索引（同一块声卡在不同 host API 下
        编号不同），拿它去比 WASAPI 列表里的 index 会得到「六台里没有一台是默认」，
        于是 audio_device=None 时 _native_out_format 只能退到 devs[0] —— 那是枚举顺序，
        不是用户设的默认设备，界面还会诚实地说「跟随系统默认」。
        所以这里取 host API 信息里的 defaultOutputDevice（WASAPI 自己的编号空间）。

        整段在 pa-ctrl 线程上跑（`_pa_call`）：无并发保护的设备枚举在本机实测会把进程以
        0xC0000005 崩掉（2×2 判别实验见文件头「PortAudio 全局闸」），而线程黏性又要求枚举与
        开流同线程。服务端是 ThreadingHTTPServer，「打开设置页枚举设备」和「POST configure
        里选设备」天生会并发 —— 现在它们排同一根线程，既不会并发踩全局状态，也不会把
        WASAPI 环境留在某根很快就要死的请求线程上（2026-10-25 换歌没声的根因）。
        """
        if _pa is None:
            return []
        try:
            out = _pa_call(self._devices_on_ctrl)
        except Exception:
            return []
        # 名字→COM 端点 id 的匹配留在调用线程上：它只碰 _WA_LOCK，不碰 PortAudio，
        # 没必要占着 pa-ctrl（锁序见文件头那条纪律）。
        self._attach_wasapi_ids(out)
        return out

    def _devices_on_ctrl(self) -> List[Dict[str, Any]]:
        """真正在 pa-ctrl 线程上执行的枚举（`_PA_ENUM` 只在这里被建/毁）。"""
        global _PA_ENUM
        out: List[Dict[str, Any]] = []
        if _PA_ENUM is None:
            try:
                _PA_ENUM = _pa.PyAudio()
            except Exception:
                _PA_ENUM = None
                return []
        p = _PA_ENUM
        try:
            widx, default = None, None
            for h in range(p.get_host_api_count()):
                info = p.get_host_api_info_by_index(h)
                if "WASAPI" in info["name"]:
                    widx = h
                    # Windows 下 WASAPI 只有一个 host API 实例，找到就停
                    break
            if widx is not None:
                d = p.get_host_api_info_by_index(widx).get("defaultOutputDevice")
                default = d if isinstance(d, int) and d >= 0 else None
            if default is None:
                # 兜底也走全局默认，但只当它是「候选标记」，不当它是「选流依据」
                g = p.get_default_output_device_info().get("index")
                default = g if isinstance(g, int) and g >= 0 else None
            n = p.get_device_count()
        except Exception:
            _PA_ENUM = None      # 这一轮的句柄不可信，下一趟重建
            return out
        for i in range(n):
            try:
                d = p.get_device_info_by_index(i)
            except Exception:
                # 单台设备读不动（热插拔/权限/驱动抽风）就跳过它，不能让整张列表 500
                continue
            if widx is not None and d.get("hostApi") != widx:
                continue
            if d.get("maxOutputChannels", 0) < 1:
                continue
            out.append({
                "index": i, "name": d.get("name", ""),
                "rate": int(d.get("defaultSampleRate") or 0),
                "channels": int(d.get("maxOutputChannels") or 0),
                "is_default": i == default,
            })
        return out

    @staticmethod
    def _attach_wasapi_ids(rows: List[Dict[str, Any]]) -> None:
        """给每台 PortAudio 设备挂上对应的 WASAPI COM 端点 id（独占只认 id）。

        匹配口径＝**友好名整串相等**，不是前缀猜测。这条口径有出处：两份互不相干的实现
        （手写 COM 虚表读 PKEY_Device_FriendlyName vs PortAudio 自己的枚举）在本机量过
        名字多重集相同（`python -X utf8 -m sp.wasapi --names` 的 cross_source.agree=true）。
        对不上的行留 None ⇒ 独占协商会如实回「这台设备对不上 COM 端点」并回落共享，
        不拿「大概是同一台」当证据。
        """
        eps = _wasapi_endpoints()
        by_name: Dict[str, str] = {}
        for e in eps:
            nm = str(e.get("name") or "").strip()
            if nm and nm not in by_name:
                by_name[nm] = e.get("id") or ""
        for r in rows:
            r["wasapi_id"] = by_name.get(str(r.get("name") or "").strip()) or None

    # --------------------------------------------------------------- 离线预览
    def preview(self, path: str, seconds: float = 2.0, *, ss: float = 0.0,
                rate: int = 48000, ch: int = 2, gains_db: Optional[List[float]] = None,
                volume: float = 1.0, loudness: bool = False) -> np.ndarray:
        """解码 + DSP，返回 (frames,ch) float32，**不开设备、不出声**。给自检用。

        这是整条链路的可证伪入口：能拿到非静音、峰值在 (0,1]、时长对得上、EQ/响度
        改变了波形的实数，就说明 ffmpeg→float32→DSP 三段真的接上了，而不是只有代码形状。
        """
        raw = self._decode(path, ss=ss, seconds=seconds, rate=rate, ch=ch, speed=1.0)
        x = np.frombuffer(raw, dtype="<f4").reshape(-1, ch).astype(np.float32)
        if gains_db:
            sos = _sos_eq(gains_db, rate)
            if sos is not None:
                zi = np.zeros((len(sos), 2, ch))
                x, zi = signal.sosfilt(sos, x, axis=0, zi=zi)
        x = x * volume
        if loudness:
            x = _normalize(x, self._target_peak)
        return x

    # --------------------------------------------------------------- 解码内核
    def _decode(self, path: str, *, ss: float, seconds: Optional[float],
               rate: int, ch: int, speed: float) -> bytes:
        ff = self.ffmpeg_exe()
        if not ff:
            raise RuntimeError("缺少 ffmpeg（imageio-ffmpeg 未安装）")
        cmd = [ff, "-v", "error"]
        if ss > 0:
            cmd += ["-ss", f"{ss:.3f}"]
        cmd += ["-i", path]
        if seconds is not None:
            cmd += ["-t", f"{seconds:.3f}"]
        # 变速滤镜只在「确实不是常速」时挂：atempo_chain 对 1.0 和非法值都给空串，
        # 所以这里判的是拆出来的因子串，不再另写一遍 abs(speed-1)>1e-3（两处判据会分家）。
        # 重采样规格也共用 stream_filters —— 预览和播放各写一份就会像 2026-11-06
        # 那样「HQ 只修了预览、出声那条还是默认抽头」。
        filters = stream_filters(speed, rate)
        if filters:
            cmd += ["-filter:a", filters]
        cmd += ["-f", "f32le", "-acodec", "pcm_f32le", "-ac", str(ch), "-ar", str(rate), "pipe:1"]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           **_hidden_popen_kwargs())
        if p.returncode != 0 or not p.stdout:
            raise RuntimeError(f"ffmpeg 解码失败 (rc={p.returncode}): "
                               f"{p.stderr.decode('utf-8','replace')[-200:]}")
        return p.stdout

    # 以下 load/play/pause/... 是接设备的路径，只有 audio_engine=native 时被调用。

    # ------------------------------------------------------------ DSP（单块）
    def _process_block(self, x: np.ndarray) -> np.ndarray:
        """对一段 (frames,ch) float32 施加全部 DSP。callback 和自检共用这一条，
        所以「自检里验过的处理」＝「播放时真正跑的处理」，不是两套代码。"""
        if x.size == 0:
            return x
        ch = x.shape[1]
        # 多声道下混的静态余量：放在 EQ 之前，让后级滤波器也在留够余量的信号上工作。
        # 1.0 时这一次乘法整段跳过（单声道/立体声一个样本都不动）。
        if self._downmix_trim != 1.0:
            x = x * np.float32(self._downmix_trim)
        if self._sos is not None:
            if self._sos_state is None or self._sos_state.shape[2] != ch:
                self._sos_state = _new_sos_state(self._sos, ch)
            x, self._sos_state = signal.sosfilt(self._sos, x, axis=0, zi=self._sos_state)
        # 平衡：等功率声像（pan=-1 全左、+1 全右），左右增益平方和=1 保能量
        if abs(self._balance) > 1e-3 and ch >= 2:
            theta = (self._balance + 1.0) * np.pi / 4.0
            x = x.copy()
            x[:, 0] *= np.cos(theta)
            x[:, 1] *= np.sin(theta)
        # 虚拟扬声器（耳机交叉馈电）：对侧声道 → 0.3 ms 延时 → 700 Hz 一阶低通 → 混入。
        # 顺序放平衡之后：用户把声像推到一边时，交叉馈电仍按「最终左右」去混，
        # 而不是拿推之前的原始声道算。关闭/强度 0 时整段跳过（bit-exact）。
        if self._speakers and ch >= 2 and self._speakers_amount > 1e-4:
            amount = float(max(0.0, min(1.0, self._speakers_amount)))
            g = 0.6 * amount
            rate = max(1.0, float(self._rate))
            a1 = float(np.exp(-2.0 * np.pi * 700.0 / rate))
            b = np.array([1.0 - a1], dtype=np.float32)
            a = np.array([1.0, -a1], dtype=np.float32)
            # scipy 的 zi 形状是 (order, ch) —— 一阶就是 (1, ch)，写成 (ch,1) 会当场抛
            # “Unexpected shape for zi: expected (1,2), found (2,1)”。
            if self._cf_zi is None or self._cf_zi.shape != (1, ch):
                self._cf_zi = np.zeros((1, ch), dtype=np.float32)
            lp, self._cf_zi = signal.lfilter(b, a, x, axis=0, zi=self._cf_zi)
            d = max(1, int(round(0.0003 * rate)))
            if self._cf_hist is None or self._cf_hist.shape != (d, ch):
                self._cf_hist = np.zeros((d, ch), dtype=np.float32)
            buf = np.concatenate([self._cf_hist, lp], axis=0)
            delayed = buf[:x.shape[0]]
            self._cf_hist = buf[x.shape[0]:].copy()
            x = x.copy()
            x[:, 0] += np.float32(g) * delayed[:, 1]
            x[:, 1] += np.float32(g) * delayed[:, 0]
            # 把直通侧压回 1/(1+g)：单声道内容（L=R）经过这一支时总增益不会涨，
            # 否则「开虚拟扬声器」会被听成「音量变大」，那是最容易骗过耳朵的假效果。
            x *= np.float32(1.0 / (1.0 + g))
        # 响度均衡：追近期峰值往 target_peak 靠的软 AGC（限幅不外挂，靠增益缩放）
        if self._loudness:
            pk = float(np.max(np.abs(x))) if x.size else 0.0
            self._agc_peak = max(pk, getattr(self, "_agc_peak", 0.0) * 0.999)
            if self._agc_peak > 1e-4:
                g = min(8.0, self._target_peak / self._agc_peak)
                x = x * g
        x = x * self._gain
        # 淡入：起播头 fade_in_frames 帧线性爬升
        if self._fade_in_frames > 0:
            n = min(self._fade_in_frames, x.shape[0])
            ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
            x = x.copy()
            x[:n] *= ramp
            self._fade_in_frames -= n
        if self._lim_on:
            # 共享档：真峰值前视限幅替代硬 clip。低于 ceiling 时输出＝延迟 D 帧的
            # 原样本（乘 1.0，逐位不变），所以「没削顶的歌」不会被这条链染色。
            if (self._lim is None or self._lim.rate != self._rate
                    or self._lim.ch != ch):
                self._lim = TruePeakLimiter(self._rate, ch)
            return self._lim.process(x)
        # 独占档/自检：保留原来的满刻度处理（独占的承诺是源样本原样送设备）
        return np.clip(x, -1.0, 1.0).astype(np.float32)

    def _push_spec(self, x: np.ndarray) -> None:
        mono = x.mean(axis=1) if x.ndim == 2 and x.shape[1] > 1 else x.reshape(-1)
        m = int(mono.size)
        if m == 0:
            return
        if m >= self._spec.size:
            self._spec[:] = mono[-self._spec.size:]
            self._spec_pos = 0
            return
        end = self._spec_pos + m
        if end <= self._spec.size:
            self._spec[self._spec_pos:end] = mono
        else:
            first = self._spec.size - self._spec_pos
            self._spec[self._spec_pos:] = mono[:first]
            self._spec[:end - self._spec.size] = mono[first:]
        self._spec_pos = end % self._spec.size

    # ---------------------------------------------------------- 设备回调
    def _open_stream(self, rate: int, ch: int, device_index: Optional[int]) -> None:
        """开 WASAPI **共享**输出流（float32）。

        独占档不是「没实现」了：那一路在 `_open_exclusive` / `_exclusive_loop`，
        协商在 `_negotiate_exclusive`。这里仍是共享档的落地——它是**默认**，也是独占
        任何一步不成时的 fail-closed 去处（经系统混音器，全链路仍 float32，不中途降 16bit）。
        两条路的区别不只是「有没有混音器」：共享是 PortAudio **拉**（回调给缓冲区、给帧数），
        独占是我们按设备事件**推**（一次整整一个周期）。所以两段代码天生不共用回调。
        """
        if _pa is None:
            raise RuntimeError("缺少 pyaudiowpatch（WASAPI 输出库未安装）")
        # 建实例 + 开流整段在 pa-ctrl 线程上（见文件头「PortAudio 全局闸」）：
        # 不只是防并发 —— 开流必须和 Pa_Initialize 同线程，否则新请求线程上必 -9999。
        # 这条路径只在用户点播放/切设备时走一次，实时音频走的还是回调那条只碰 self._lock 的路。
        _pa_call(self._open_stream_on_ctrl, rate, ch, device_index)

    def _open_stream_on_ctrl(self, rate: int, ch: int, device_index: Optional[int]) -> None:
        """`_open_stream` 的本体，**只在 pa-ctrl 线程上执行**。"""
        try:
            self._pa = _pa.PyAudio()
            try:
                self._stream = self._pa.open(
                    format=_pa.paFloat32, channels=ch, rate=rate,
                    output=True, output_device_index=device_index,
                    frames_per_buffer=1024, stream_callback=self._callback,
                )
            except Exception:
                try:
                    self._pa.terminate()
                except Exception:
                    pass
                self._pa = None
                raise
        # 开流失败必须留一条现场：Errno -9999 只说「宿主错误」，不说是哪台/哪档。
        except Exception as e:
            print("[audio-open] 失败 device=%s rate=%s: %s: %s"
                  % (device_index, rate, type(e).__name__, e), flush=True)
            try:
                # 同一根线程上再建一个实例读端点：跨线程读会踩同一段线程黏性。
                d = _pa.PyAudio()
                try:
                    print("[audio-open] 该端点当前读数：%s"
                          % ({k: d.get_device_info_by_index(device_index).get(k)
                              for k in ("name", "hostApi", "defaultSampleRate",
                                        "maxOutputChannels")},), flush=True)
                finally:
                    d.terminate()
            except Exception as e2:
                print("[audio-open] 读端点信息也失败：%s" % e2, flush=True)
            raise

    # --------------------------------------------------------- 独占：协商与开流
    def _resolve_endpoint(self, device_index: Optional[int]):
        """把「设置里那台设备」翻成 COM 端点。回 (dict|None, 失败原因)。

        两条来源，各对应一个真实踩过的坑：
          · device_index 为 None（「跟随系统默认」）⇒ 问 GetDefaultAudioEndpoint(eRender,
            eConsole)。不能用 PortAudio 的 get_default_output_device：本机实测那是**全局**
            索引，同一块卡在不同 host API 下编号不同，拿它比 WASAPI 列表会得到「六台里没有
            一台是默认」（见 devices() 的 docstring，同一个坑的第二副本）。
          · 指了具体设备 ⇒ 用 devices() 挂好的 wasapi_id（名字整串相等，出处见那里）。
            对不上就**如实说不**，不猜「第 i 台对第 i 台」：PortAudio 的索引空间里还夹着
            loopback 和别的 host API 的设备，两边编号根本不同基。
        """
        if _ws is None:
            return None, "本机导入 sp.wasapi 失败（独占这条路要手写 COM）"
        if device_index is None:
            try:
                d = _ws.default_device_id()
            except Exception as e:
                return None, ("问不到系统默认渲染端点：%s: %s"
                              % (type(e).__name__, e))
            return {"id": d["id"], "name": d.get("name")}, ""
        rows = self.devices()
        me = next((r for r in rows if int(r["index"]) == int(device_index)), None)
        if me is None:
            return None, ("设置里那台设备（索引 %s）已经不在枚举表里，"
                          "重开一次设置页或换「跟随系统默认」" % (device_index,))
        if not me.get("wasapi_id"):
            return None, ("设备「%s」对不上 COM 渲染端点的名字，没法按 id 独占开"
                          % (me.get("name"),))
        return {"id": me["wasapi_id"], "name": me.get("name")}, ""

    def _negotiate_exclusive(self, rate: int, ch: int,
                             device_index: Optional[int], source_bits: int = 0,
                             source_rate: int = 0, source_ch: int = 0,
                             follow_source: bool = True):
        """问驱动「这台设备能独占开什么」——全程**不占有设备**（一次 Initialize 都不调）。

        follow_source 决定「源率」和「请求率」谁说了算：
          · True（逐位直通档）：源率优先。源率设备支持就按源率开，一路不重采样 ——
            代价是换一首不同采样率的曲子必须 Release→Initialize，驱动换率那一下
            在部分声卡上是一声很轻的咔嗒，软件淡出够不着（那是 DAC 重锁，不是样本阶跃）。
          · False（固定档）：请求率优先。所有曲子都落到**同一份施工图**上，换曲时
            `_can_reuse_exclusive` 逐项相等、流从头到尾不重开，根本不存在换率那一下；
            不同源率由解码线程的 aresample（256 抽头）重采样到请求率，256 抽头那套
            规格的实测见 `RESAMPLE_TAPS`。bit-perfect 判定仍按真实源率记账，不会冒充。

        回 (ok, why, neg_rate, info)。info 就是 `_create_exclusive_stream` 的施工图，
        所以「协商说的」和「开流用的」是同一个 dict，不存在两处各算一遍的空间。
        neg_rate 可能不等于请求的 rate：没有同率独占格式时 pick 会退到最近的一档，
        这时 **ffmpeg 必须按 neg_rate 解码**（play() 里就是为这个把 ex_rate 传给读线程的），
        否则「解 44100 的数、按 96000 放」是 8.7% 的音高偏移，不是音质问题。

        source_* 三个都允许是 0＝没量到。0 不是「小的那个值」，是**另一件事**：
        没量到就不能宣布 bit-perfect（见 `_bit_perfect_locked`），但也不能因此拒绝独占，
        否则曲库里那些 sample_rate=0 的行（本机真库确有：读不出标签的 mp4 容器）
        就永远用不上独占档了。
        """
        if _ws is None:
            return False, "本机导入 sp.wasapi 失败，独占没法协商", rate, {}
        dev, why = self._resolve_endpoint(device_index)
        if dev is None:
            return False, why, rate, {}
        try:
            caps = _wasapi_caps(dev["id"], ch)
        except Exception as e:
            caps = None
        if not caps or not caps.get("table"):
            return False, ("能力探针没拿到格式表（探针不占有设备，失败通常是 COM 起不来）"), rate, {}
        try:
            # 第一个参数是**源率**（不是请求率）：bit-perfect 的定义是「不重采样」，
            # 所以挑格式时要以源为准，请求的那档只当退路。写成 (rate, rate) 的话，
            # 「48k 的曲子 + 设备支持 48k 独占」也会被判成「没有同率格式 ⇒ 重采样到 44.1k」，
            # 而那正是这条链最该省掉的一次失真。源率不知道（0）才退回请求档。
            # 固定档（follow_source=False）反过来把源率摘掉：挑格式只看请求率，
            # 所有曲子才落得**同一份**施工图（连位深容器都一样）——换曲复用的前提。
            pick = _ws.pick_exclusive_format(caps["table"],
                                             0 if not follow_source
                                             else int(source_rate or rate),
                                             rate, int(source_bits))
        except Exception as e:
            return False, "独占协商异常：%s: %s" % (type(e).__name__, e), rate, {}
        if pick.get("mode") != "exclusive":
            # fail-closed：驱动说不行就是不行，界面上要看得见那句「为什么回落共享」
            return False, pick.get("why") or "驱动说这台设备不支持任何独占格式", rate, {}
        period_hns = int(caps.get("default_period_hns") or 100000)
        is_float = str(pick.get("tag") or "").endswith("-f32")
        # exclusive_supported 已经是「驱动点头（S_OK）那几档」（probe_device 里滤好的），
        # 所以这里的最高位深是设备**真能给**的独占位深，不是候选表里那些被拒的数。
        ex_rows = [r for r in (caps.get("exclusive_supported") or []) if r.get("hr") == _ws.S_OK]
        deepest = max([int(r.get("valid_bits") or 0) for r in ex_rows] or [0])
        # 抖动只排在「确实要量化」那一档后面：
        #   f32 直通 ⇒ 一个样本都不动，加抖动等于无中生有；
        #   源位深够（≤ 设备档）⇒ 整数量化是**无损**的（16 位放进 16 位、24 位放进 32 位容器），
        #     这时候抖一格就是往一条本来 bit-perfect 的流里注噪声；
        #   源位深没量到 ⇒ 保守当作会量化（宁可抖也不静默削）。
        quantizes = (not is_float) and (int(source_bits or 0) > int(pick["bits"])
                                        or int(source_bits or 0) <= 0)
        info = {
            "device_id": dev["id"], "device_name": dev.get("name"),
            "rate": int(pick["rate"]), "ch": int(ch),
            "bits": int(pick["bits"]), "container": int(pick["container"]),
            # 子类型按 tag 认，不按「bits==32」猜：32/32 既可能是 f32 也可能是 s32 PCM，
            # 猜错就是 Initialize 报 AUDCLNT_E_UNSUPPORTED_FORMAT（或者更糟：开着但放的是噪声）
            "subtype": (_ws.KSDATAFORMAT_SUBTYPE_IEEE_FLOAT if is_float
                        else _ws.KSDATAFORMAT_SUBTYPE_PCM),
            "tag": pick.get("tag"), "why": pick.get("why"),
            "device_bit_perfect": bool(pick.get("bit_perfect")),
            "resampled_by_device": bool(pick.get("resampled")),
            "period_hns": period_hns,
            "period_ms": round(period_hns / 10000.0, 3),
            "mix": caps.get("mix"),
            "exclusive_rates": sorted({int(r["rate"]) for r in ex_rows if r.get("rate")}),
            "exclusive_max_bits": deepest,
            "source_bits": int(source_bits or 0), "source_rate": int(source_rate or 0),
            "source_ch": int(source_ch or 0),
            # 这一份决定泵线程每次投递要不要 TPDF（`_exclusive_loop` 只读它，不再现算）：
            # 判一次、存下来、随施工图一起被重开流复用——「暂停 30 秒后自动重开的那条流
            # 换了量化方式」这种随时间漂移的行为就不可能出现。
            "dither": bool(self._dither_enabled and quantizes),
            "quantizes": bool(quantizes),
        }
        return True, "", int(pick["rate"]), info

    def exclusive_caps(self, device_index: Optional[int] = None,
                      ch: int = 2) -> Dict[str, Any]:
        """某台端点的**独占能力读数**（给设置面板用）。一次 Initialize 都不调，不占有声卡。

        为什么要有这个只读口，而不是等用户点了开关、起播之后才在状态行里告诉他「这台不支持」：
        本机实测默认端点的独占清单是 **三档、全是 16 位**（44100/48000/96000-pcm16，
        出处 tools/exclusive_probe.20261020-002924.json），也就是说 24 位源在这台设备上
        必然量化。这是一条**开流之前就能知道**的事实，界面却要到开完流才说，就等于把
        「你期待的那个 bit-perfect 拿不到」藏在一次点击后面。
        返回的 rows 是驱动点头（S_OK）那几档 —— 不是候选表里那些被拒的（被拒的档在界面上
        出现＝用户以为能开结果开不了）。
        """
        if _ws is None:
            return {"ok": False, "error": "本机导入 sp.wasapi 失败，独占没法协商"}
        dev, why = self._resolve_endpoint(device_index)
        if dev is None:
            return {"ok": False, "error": why}
        try:
            caps = _wasapi_caps(dev["id"], ch)
        except Exception as e:
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                    "device_name": dev.get("name")}
        if not caps or not caps.get("table"):
            return {"ok": False, "device_name": dev.get("name"),
                    "error": "能力探针没拿到格式表（探针不占有设备，失败通常是 COM 起不来）"}
        rows = [r for r in (caps.get("exclusive_supported") or []) if r.get("hr") == _ws.S_OK]
        return {"ok": True, "device_name": dev.get("name"), "device_id": dev["id"],
                "requested": bool(rows), "mix": caps.get("mix"),
                "period_ms": round(int(caps.get("default_period_hns") or 100000) / 10000.0, 3),
                "rates": sorted({int(r["rate"]) for r in rows if r.get("rate")}),
                "max_bits": max([int(r.get("valid_bits") or 0) for r in rows] or [0]),
                "formats": [{"tag": r.get("tag"), "rate": int(r.get("rate") or 0),
                             "bits": int(r.get("valid_bits") or 0),
                             "container": int(r.get("container_bits") or 0)} for r in rows]}

    def _create_exclusive_stream(self, p: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """真开一条独占流：Initialize(EXCLUSIVE｜EVENTCALLBACK) → SetEventHandle →
        GetService → prime_silence → Start。调用方必须已持有 self._dev_lock。
        任何一步失败抛 WasapiError —— 上层负责回落共享并带上驱动给的原因。

        prime_silence 那一步不是装饰：事件驱动下 Start 之后第一拍立刻就来，
        手里没缓冲驱动就当场 underrun（一句「嘀」）。先把整块缓冲铺成静音再 Start，
        和 probe_exclusive 真机跑通的那条顺序一致（tools/exclusive_probe.*.json）。
        """
        if _ws is None or not p:
            raise RuntimeError("没有可用的独占参数（_ex_params 为空）")
        rate, ch = int(p["rate"]), int(p["ch"])
        wf = _ws.make_wf(rate, ch, int(p["bits"]), p["subtype"], int(p["container"]))
        period_hns = int(p["period_hns"])
        st = _ws.ExclusiveStream(
            p["device_id"], wf,
            buffer_frames=max(64, _ws.block_frames(period_hns, rate) * 4),
            period_hns=period_hns)
        try:
            rec = st.open()
            st.prime_silence()
            st.start()
        except Exception as e:
            # 半途失败也必须把已经 Activate 出来的接口放掉：Initialize 成功而 SetEventHandle
            # 失败时，那条 IAudioClient 不还就等于「这台设备被一个没人用的独占流占着」。
            # close() 内部每步都兜异常，所以这里不会把原始错误盖掉。
            try:
                st.close()
            except Exception:
                pass
            _trace("ex-stream-FAIL", tag=p.get("tag") or "-",
                   err="%s: %s" % (type(e).__name__, e))
            raise
        _trace("ex-stream-open", tag=p.get("tag") or "-", rate=rate, ch=ch,
               period_hns=period_hns)
        # 三个「指针」和「为什么放」都排在同一把微锁里：它们只被赋值，不在锁里做 COM
        with self._ex_lock:
            self._ex = st
            self._ex_params = p
            self._ex_rec = rec
            self._ex_released = ""
        return rec

    # 泵把流建好（或建失败）最迟要等多久。开流本身本机实测 16ms（判据 D5），这里给的是
    # 线程调度的余量；超时就老实回落共享，不许让界面卡在「点了没反应」。
    _EX_OPEN_WAIT_S = 1.0

    def _open_exclusive(self, rate: int, ch: int, info: Dict[str, Any]) -> Dict[str, Any]:
        """起泵线程，并**等泵把流建起来**再把回执交回 play。

        为什么开流不在调用线程做（这是本机量出来的，不是保守写法）：
        2026-10-20 差分（%TEMP%\\repro_threadexit.py，同一首歌、同一条独占流、blocks≈100
        证明确实在推实时泵）——
          · 建流的线程一直活着，直到 stop 之后才退：**3/3 趟干净**（stop 正常交还设备）；
          · 建流的线程开完就走（＝ThreadingHTTPServer 的形状：每个请求一根用完即死的线程）
            ：**4/4 趟在 stop 上 0xC0000005**，faulthandler 里四条 Python 线程全在等，
            **没有一根带 Python 帧** ⇒ 死的是驱动/COM 的原生线程；`ExclusiveStream.close`
            那条桩一次都没响，所以不是「我们放早了」，是「建它的那根线程先没了」。
        讲得通的机制：`ExclusiveStream.open()` 里那次 `CoInitializeEx` 给那根线程建立了 COM
        线程状态，线程一结束系统隐式撤掉它，而 AudioClient/事件句柄这时还被音频引擎用着。
        于是把不变式写死一句话：**独占流的建、用、放都排在同一根活过它整个寿命的线程上**
        ——那根线程就是泵。建完之后泵自己也在 `_exclusive_loop` 的 finally 里放流。

        失败照旧往上抛（play 接住 ⇒ 如实回落共享并把原因带进 note）。
        """
        my_stop = threading.Event()
        ready = threading.Event()
        box: Dict[str, Any] = {}
        th = threading.Thread(target=self._exclusive_loop, args=(my_stop,),
                              daemon=True, name="salt-exclusive-pump")
        with self._ex_lock:
            # 锁序纪律不破：_ex_lock 里只赋值，一次 COM 都不做。
            self._ex_pending = (info, box, ready)
            self._ex_stop, self._ex_pump = my_stop, th
        th.start()
        deadline = time.monotonic() + self._EX_OPEN_WAIT_S
        while not ready.wait(0.02):
            if not th.is_alive() or time.monotonic() > deadline:
                my_stop.set()
                th.join(timeout=0.5)
                with self._ex_lock:
                    if self._ex_pump is th:
                        self._ex_pump = None
                        self._ex_stop = None
                raise RuntimeError("泵线程没能在 %.1fs 内把独占流开起来"
                                   "（线程还活着=%s，多半被别的收尾挡住了）"
                                   % (self._EX_OPEN_WAIT_S, th.is_alive()))
        if "err" in box:
            err = box["err"]
            my_stop.set()
            th.join(timeout=0.5)
            raise err
        return box["rec"]

    def _close_exclusive(self, reason: str) -> None:
        """放掉独占流并写下「什么时候、为什么放」。
        **只有泵线程自己**和「已经等过泵退出」的 stop() 会走到这里——反过来就是
        一边有人正在往 COM 缓冲 memmove、一边有人 Release 那个接口，那是访问违例（进程当场
        没了、stderr 全空），不是异常。泵那边现在由 `_exclusive_loop` 的 finally 兜底：
        建流的那根线程放完流才退，绝不留「线程没了、流还开着」那种形状（本机 4/4 复现的
        0xC0000005 就是这个形状，见 `_open_exclusive` 的实测记录）。"""
        with self._dev_lock:
            with self._ex_lock:
                st, self._ex = self._ex, None
            if st is None:
                return      # 手上本来就没有流：别把「没东西可放」写成一条释放原因
            try:
                st.close()
            except Exception as e:
                reason += "（close 异常：%s: %s）" % (type(e).__name__, e)
            with self._ex_lock:
                self._ex_rec = {**(self._ex_rec or {}), "final": st.status()}
                self._ex_released = reason

    def _fallback_to_shared(self, why: str) -> None:
        """独占中途用不了了：先放手（把声卡还给系统），再开共享，并把两句话都带进 note。
        顺序要紧——独占不放，共享开出来也是静音；而且放着独占不撤就是「整机没声」。"""
        _trace("fallback-shared", why=why[:200], gen=self._gen)
        with self._lock:
            rate, ch, idx, ended = self._rate, self._ch, self._device_index, self._ended
        self._close_exclusive(why)
        with self._ex_lock:
            # 泵马上自己退出（调用方在 fallback 之后直接 return），这里只是把登记清掉，
            # 免得下一次 stop() 去 join 一根已经在收尾的线程、还误以为「没等到 ⇒ 不敢关流」。
            self._ex_stop = None
            self._ex_pump = None
        if ended:
            # 曲子里外都完了，没必要再为它开一条流；下一次 play 会按设置重新协商
            with self._lock:
                self._mode = "shared"
                self._native_note = why
            return
        with self._dev_lock:
            try:
                self._open_stream(rate, ch, idx)
            except Exception as e:
                with self._lock:
                    self._mode = "shared"
                    self._native_note = "%s；回落共享也失败：%s" % (why, e)
                return
        with self._lock:
            self._mode = "shared"
            self._native_note = "%s；已回落共享输出（float32 全链路，仍经系统混音器）" % why

    # 暂停超过这个秒数就**放手**：独占期间系统其他声音是哑的，用户暂停了还占着声卡
    # 属于「软件替主人把房子锁了」。放手后恢复播放会自动重开（重不开就回落共享）。
    _EX_PAUSE_RELEASE_S = 30.0
    # 播完不立刻交还设备：前端 280ms 一轮的轮询发现 ended 就会来要下一首，这点宽限
    # 足够让「自然播完→下一首」也复用同一条流。宽限期内每拍继续投静音（独占空一拍
    # ＝真 glitch）。队列真的播完时最多多占设备这么久，之后照旧放手。
    _EX_EOF_GRACE_S = 2.0
    # 连续多少拍等不到事件就认定设备不再喂我们了（50ms × 200 ＝ 10 秒）
    _EX_MAX_CONSECUTIVE_TIMEOUTS = 200
    # 能复用现有独占流的判据：协商施工图里这些量**逐项相同**，重开一条流才会得到
    # 一模一样的结果，复用才是等价的；差一项（尤其 rate）就必须真重开——
    # 独占不做重采样，拿 44.1k 的流放 48k 的曲是 8.7% 的音高偏移。
    _EX_REUSE_KEYS = ("device_id", "rate", "ch", "bits", "container",
                      "subtype", "period_hns")
    # 防爆音淡入/淡出的最短时长：短到听不出（8ms），长到足以把阶跃抹平。起播淡入取
    # 「用户偏好」与它的较大值；放流前的淡出固定收一个设备周期（约 10ms）到 0。
    _ANTI_CLICK_S = 0.008

    def _exclusive_loop(self, my_stop: threading.Event) -> None:
        """泵的**外壳**：进去建流、出来放流，都排在同一根线程上。

        为什么要有这一层（本机 4/4 vs 0/3 的差分，出处见 `_open_exclusive`）：
        独占流的建/用/放若分给两根线程，或者**建它的那根先退**，驱动那边的原生线程就会
        踩空 ⇒ 0xC0000005，整个进程当场没了。ThreadingHTTPServer 每个请求一根用完即死的
        线程，所以「请求线程建流」这个形状必然踩；把建与放都收进泵，泵退之前先把声卡还了，
        这条不变式就有牙：**没有一条独占流会活过它自己那根线程**。

        `finally` 里那两句话（`_ex_pending` 兜底 + 放流）都是幂等的：手里没流时
        `_close_exclusive` 直接返回，不会把「没东西可放」写成一条释放原因。
        """
        try:
            self._exclusive_pump(my_stop)
        finally:
            _trace("pump-exit", gen=self._gen, stop=my_stop.is_set())
            with self._ex_lock:
                pend, self._ex_pending = self._ex_pending, None
            if pend is not None:
                # 泵还没走到建流那一步就退了出去（被 stop 抢先／线程起来就撞上异常）：
                # 必须把回执递回去，否则 play 会白等到超时，把一次正常的收尾写成「开流失败」。
                _info, box, ready = pend
                box.setdefault("err", RuntimeError("泵线程在建流之前就退出了"))
                ready.set()
            self._close_exclusive(
                "播放已停止，独占设备已释放" if my_stop.is_set()
                else "泵线程退出，独占设备已交还")

    def _exclusive_pump(self, my_stop: threading.Event) -> None:
        """独占档的推流泵：每收到一个设备事件，投**整整一个周期**。

        为什么不复用 `_callback`：那条是 PortAudio 拉（它给缓冲区指针、给帧数），
        独占是事件推（帧数固定＝GetBufferSize，一次一拍）。投少了驱动手里就空一拍
        （真 glitch），投多了直接报错。但**取数和算 DSP 那一段两档共用 `_pull_locked`**——
        两把尺会分家，所以只留一把；priming/丢帧/播完这三条判据在两条路上是同一份代码。

        COM 要在本线程先初始化一次：`com_init` 已初始化时回 'already'（含被 PyQt 按 STA
        初始化过的线程，那句 RPC_E_CHANGED_MODE 不当错误），所以这一句既不抢模型也不会抛。
        """
        if _ws is not None:
            try:
                _ws.com_init(mta=True)
            except Exception:
                pass
        # ---- 开局：流在**本线程**建（play 的请求线程只留下那只信封，见 _open_exclusive）
        with self._ex_lock:
            pend, self._ex_pending = self._ex_pending, None
        if pend is not None:
            info, box, ready = pend
            try:
                if my_stop.is_set():
                    raise RuntimeError("起泵之后立刻被要求停止，这次不开流")
                with self._dev_lock:
                    box["rec"] = self._create_exclusive_stream(info)
            except Exception as e:
                box["err"] = e
            finally:
                ready.set()
            if "err" in box:
                return
        paused_since = 0.0
        timeouts = 0
        # 曲尾宽限的起点（0＝还没到曲尾）。宽限期内流不放手，每拍继续投静音。
        eof_at = 0.0
        while not my_stop.is_set():
            with self._ex_lock:
                st, params = self._ex, self._ex_params
            if st is None:
                # 流被放掉了（长暂停 / 播完）。没在等播放就退出；要播就把流要回来。
                with self._lock:
                    want = (not self._paused) and (not self._ended)
                if not want:
                    my_stop.wait(0.05)
                    continue
                try:
                    with self._dev_lock:
                        _trace("pump-recreate", tag=(params or {}).get("tag") or "-",
                               gen=self._gen)
                        self._create_exclusive_stream(params)
                except Exception as e:
                    self._fallback_to_shared("独占重开失败：%s: %s"
                                            % (type(e).__name__, e))
                    return
                continue
            try:
                period = int(st.period_frames)
                wf = st.wf
                bits = int(wf.wValidBitsPerSample)
                container = int(wf.Format.wBitsPerSample)
                r = st.wait(50)
                if r == _ws.WAIT_FAILED:
                    # 句柄没权限/已失效：这不是「慢」，是量具失灵，继续空转就是假绿
                    self._fallback_to_shared("独占事件等待返回 WAIT_FAILED")
                    return
                if r != _ws.WAIT_OBJECT_0:
                    timeouts += 1
                    if timeouts >= self._EX_MAX_CONSECUTIVE_TIMEOUTS:
                        self._fallback_to_shared(
                            "独占连续 %.0f 秒等不到设备事件（设备可能已被系统停用/换格式）"
                            % (self._EX_MAX_CONSECUTIVE_TIMEOUTS * 0.05))
                        return
                    continue
                timeouts = 0
                with self._lock:
                    fade = self._switch_fade
                    self._switch_fade = False
                    last = None if self._last_out is None else self._last_out.copy()
                    wave = None if self._last_tail is None else self._last_tail.copy()
                if fade:
                    # 切歌复用同一条流：这一拍先放上一首的尾巴（收到 0），下一拍才轮到新曲。
                    # 先写后 pull，保证「旧声→静音→新声」之间没有硬阶跃，也不空拍。
                    _trace("pump-switch-fade", gen=self._gen)
                    self._write_fade_tail(st, last, period, wave)
                    with self._lock:
                        self._last_out = None
                        self._last_tail = None
                    eof_at = 0.0
                    continue
                with self._lock:
                    out, filled, kind = self._pull_locked(period)
                    # 记住「最近一拍真正送进设备的最后一帧/整段波形」，放流前从它淡出收到 0。
                    # 只在有音频时更新：静音拍（pause/underrun）输出已是 0，置 None 让淡出
                    # 跳过；EOF 那一拍不动它，保留上一拍音频供电尾淡出用。
                    if filled:
                        self._last_out = out[-1].copy()
                        self._last_tail = out.copy()
                    elif kind != "eof":
                        self._last_out = None
                        self._last_tail = None
                    last = None if self._last_out is None else self._last_out.copy()
                    wave = None if self._last_tail is None else self._last_tail.copy()
                    paused, ended = self._paused, self._ended
                if kind == "eof" and not eof_at:
                    # 曲子放完：**先不放手**。前端 280ms 一轮的轮询发现 ended 就会来要
                    # 下一首，这点宽限让「自然播完→下一首」也复用同一条流 —— 切歌那声
                    # 电流声就长在 Release→Acquire 上，样本淡出抹不掉它。这一拍先投一记
                    # 淡出尾巴占住节拍（独占空一拍＝真 glitch），宽限期内每拍投静音。
                    _trace("pump-eof", gen=self._gen, consumed=self._consumed,
                           avail=self._frames_avail, dur=self._duration,
                           base=self._base_pos)
                    self._write_fade_tail(st, last, period, wave)
                    with self._lock:
                        self._last_out = None
                        self._last_tail = None
                    eof_at = time.monotonic()
                    continue
                if eof_at:
                    if not self._ended:
                        _trace("pump-eof-cancel", gen=self._gen)
                        eof_at = 0.0          # 新曲已经接手，宽限作废
                    elif time.monotonic() - eof_at >= self._EX_EOF_GRACE_S:
                        _trace("pump-grace-close", gen=self._gen,
                               grace=time.monotonic() - eof_at)
                        st.write_silence()
                        self._close_exclusive("本曲播完，已释放独占设备（系统声音恢复）")
                        return
                if filled:
                    # 抖动来自施工图（开流那一次算好的），不在这里现算：同一首歌的前后两段
                    # 必须用同一套量化，否则「暂停 30 秒自动重开」会悄悄换掉噪声整形。
                    st.write(_ws.frames_to_device(out, bits, container,
                                                  dither=bool((params or {}).get("dither"))))
                else:
                    # 没货也得把这拍占住：独占的节拍由设备驱动，跳过一拍就是手里空一拍。
                    # 记进 silence_blocks（和 underruns 分家：一个是「有意铺静音」，
                    # 一个是「解码供不上」，混成一列就分不清两种病）。
                    st.write_silence()
            except Exception as e:
                self._fallback_to_shared("独占投递异常：%s: %s" % (type(e).__name__, e))
                return
            if paused:
                if not paused_since:
                    paused_since = time.monotonic()
                elif time.monotonic() - paused_since >= self._EX_PAUSE_RELEASE_S:
                    self._close_exclusive(
                        "暂停超过 %.0f 秒，已释放独占设备（继续播放会自动重开）"
                        % self._EX_PAUSE_RELEASE_S)
                    paused_since = 0.0
            else:
                paused_since = 0.0
        # 被 stop() 要求退出（切歌/停止）：流还在手上（_exclusive_loop 的 finally 才放），
        # 先把最后一帧线性收到 0 再交还设备，免得 Stop 截在非零样本上放出一声爆音。
        with self._ex_lock:
            st = self._ex
        self._drain_fadeout(st)

    def _drain_fadeout(self, st: Any) -> None:
        """放流前的防爆音淡出：从最近一帧 `_last_out` 线性收一个设备周期到 0。

        缓冲深度是 4 个周期（见 `_create_exclusive_stream` 的 buffer_frames），
        写进去的淡出尾巴排在已排队的 0~3 拍之后：只等一拍就 Stop，淡出还没出声
        就被截断，落点仍是非零样本 —— 本机听感就是「重开前还有很轻的一声」。
        所以写完必须把整段缓冲放空（`_drain_periods`）再交还设备。
        任何一步异常都静默放弃淡出、照常关流——绝不为了消爆音把收尾搅成访问违例。
        """
        if st is None or _ws is None:
            return
        with self._lock:
            last = None if self._last_out is None else self._last_out.copy()
            wave = None if self._last_tail is None else self._last_tail.copy()
        try:
            if st.wait(50) != _ws.WAIT_OBJECT_0:
                return
            if wave is not None or (last is not None
                                    and float(np.max(np.abs(last))) >= 1e-6):
                self._write_fade_tail(st, last, int(st.period_frames), wave)
            # 尾巴已经是 0 也要排水：排队里可能还有没放完的旧音频，截在那里同样是一声。
            self._drain_periods(st)
        except Exception:
            pass

    def _drain_periods(self, st: Any) -> None:
        """等设备把已排队的周期真吃掉再交还（Stop/Release 截在非零样本上＝一声轻爆）。

        事件每来一次＝设备吃掉一拍；队列空后事件不再来（wait 超时）就是已经放完。
        最多等一整个缓冲（本机 4 拍、10ms/拍）；即使设备不再给事件也不会拖过 200ms。
        """
        period = int(getattr(st, "period_frames", 0) or 0)
        buf = int(getattr(st, "buffer_frames", 0) or 0)
        n = max(1, (buf // period) if period > 0 else 1)
        for _ in range(n):
            if st.wait(50) != _ws.WAIT_OBJECT_0:
                break

    def _fade_out_block(self, n: int, last: Optional[np.ndarray],
                        wave: Optional[np.ndarray]) -> np.ndarray:
        """生成 n 帧淡出：优先拿**上一拍真播出去的波形倒放**再乘斜坡。

        为什么不是「拿最后一帧当直流收到 0」：那段 10ms 的直流斜坡本身就是一坨
        低频能量，听感上是一声很轻的「噗」。倒放的起点恰是已播出的末帧（值连续），
        频谱仍留在音乐带里，不会凭空多出一坨低频。
        """
        ramp = np.linspace(1.0, 0.0, n, dtype=np.float32)[:, None]
        if wave is not None and getattr(wave, "ndim", 0) == 2 and wave.shape[0] >= n:
            return (wave[-n:][::-1] * ramp).astype(np.float32)
        if last is None:
            return np.zeros((n, self._ch), np.float32)
        return (last[None, :] * ramp).astype(np.float32)

    def _write_fade_tail(self, st: Any, last: Optional[np.ndarray],
                         period: int, wave: Optional[np.ndarray] = None) -> None:
        """把一个设备周期的淡出投进去，**必须投满一拍**（切歌复用流/曲尾宽限用）。

        和 `_drain_fadeout` 的分工：那条是放流前用、自己等下一个设备事件；这条是事件
        已经在手上，写晚了这一拍就空了。last/wave 都缺失或已是 0 ⇒ 投静音占住节拍——
        「没有淡出可写」不等于「这一拍可以不写」，独占空一拍就是一次真 glitch。
        """
        wf = st.wf
        bits = int(wf.wValidBitsPerSample)
        container = int(wf.Format.wBitsPerSample)
        # 淡出尾巴走和正文同一套量化（协商存下的 dither），不在切歌时偷偷换噪声整形。
        with self._ex_lock:
            params = self._ex_params
        dither = bool((params or {}).get("dither"))
        if wave is None and (last is None or float(np.max(np.abs(last))) < 1e-6):
            st.write_silence()
            return
        block = self._fade_out_block(period, last, wave)
        st.write(_ws.frames_to_device(block, bits, container, dither=dither))

    # ---------------------------------------------------------- 取一帧（两档共用）
    def _pull_locked(self, frames: int):
        """在 self._lock 内从环形取 `frames` 帧，返回 (out, filled, kind)。

        kind 五家，各有明确账单（这条函数是共享回调和独占泵**唯一**的取数口）：
          'idle'     暂停或已ended ⇒ 一帧都不取、不记丢帧、不推进时钟
          'priming'  灌数闸门里 ⇒ 同上（这不是「供不上」，是「有意等一段有货再放行」，
                     混进丢帧计数会把量尺变成假数字——2026-10-13 那起「累计丢帧 7629 次」的成因）
          'eof'      取到 0 帧且读到尾 ⇒ 由这里把 _ended 结算掉（播完这件事必须**主动**收口，
                     以前只在 state() 里算，没人查的时候设备流一直空转占着）
          'empty'    取到 0 帧但还没到尾 ⇒ 记一次 underrun
          'partial'  取到 0<filled<frames ⇒ 记一次 underrun，时钟只推进 filled（耳朵说了算）
          'data'     取满 ⇒ 正常一块
        整段在锁内做：take→process→spec→consumed 必须原子，否则 configure()
        在回调中途换了 _sos/_gain 会让同一块前后用不同参数，EQ 状态串味。
        """
        out = np.zeros((frames, self._ch), np.float32)
        if self._paused or self._ended:
            return out, 0, "idle"
        if self._priming:
            # 灌数窗口（seek/起播后那 ~85ms）：宁可把时钟按住，也不让 seek 点成串空拍。
            # 早先 seek 处量到的形状：环形刚清空、设备流却已经活着 8 拍 ⇒ 85ms 硬静音
            # （2026-10-13 真机 tools/probe_native_device.afterprefill.1.txt）。
            # 三个出口，缺一不可：够量 / 读到尾（坏文件）/ 超过 _prime_until（绝不长住）。
            if (self._frames_avail >= self._prime_frames or self._eof
                    or time.monotonic() >= self._prime_until):
                self._priming = False
                _trace("priming-release", avail=self._frames_avail,
                       need=self._prime_frames, eof=self._eof)
            else:
                return out, 0, "priming"
        filled = self._take_into(out, frames)
        if filled == 0 and self._eof:
            _trace("pull-eof->ended", consumed=self._consumed,
                   base=self._base_pos, avail=self._frames_avail, gen=self._gen)
            self._ended = True
            return out, 0, "eof"
        if filled == 0:
            self._underruns += 1
            return out, 0, "empty"
        if self._consumed == 0:
            _trace("pull-first-data", frames=filled, gen=self._gen,
                   ended=self._ended)
        if filled < frames:
            # 文件尾的最后一块不是「解码供不上」：ffmpeg 正常读完，给不满一拍是必然。
            # 以前把这一记也算进 underruns，每播完一首就多一次假丢帧（诊断里最常见
            # 的那个「1 次」几乎全是它）。
            if not self._eof:
                self._underruns += 1
        out[:filled] = self._process_block(out[:filled])
        self._push_spec(out[:filled])
        self._consumed += filled
        return out, filled, ("data" if filled == frames else "partial")

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        # 共享档的唯一取数口就是 _pull_locked（判据不再有两份）。
        with self._lock:
            if self._switch_fade:
                # 切歌复用同一条共享流：这一拍先把上一首的尾巴收到 0。不许直接跳去
                # 拉新曲——旧声与静音之间那一记阶跃就是用户听到的爆音本身。
                # 淡出材料优先用上一拍真播出去的波形（见 _fade_out_block）。
                self._switch_fade = False
                out = self._fade_out_block(frames, self._last_out, self._last_tail)
                self._last_out = None
                self._last_tail = None
                return (out.tobytes(),
                        _pa.paContinue if not self._ended else _pa.paComplete)
            out, _filled, _kind = self._pull_locked(frames)
            # 记下真正送进设备的最后一帧/整段波形：下一首起播时淡出要从它起爬（只在有音频时记；
            # 静音/priming 拍把它清掉，免得宽限之后拿一段旧声当尾巴）。
            self._last_out = out[_filled - 1].copy() if _filled else None
            self._last_tail = out.copy() if _filled else None
            return (out.tobytes(), _pa.paContinue if not self._ended else _pa.paComplete)

    def _take_into(self, dst: np.ndarray, need: int) -> int:
        """从 _chunks 拷 need 帧进 dst（须在 _lock 内调用）。返回实拷帧数。"""
        got = 0
        while got < need and self._chunks:
            c = self._chunks[0]
            take = min(need - got, c.shape[0])
            dst[got:got + take] = c[:take]
            got += take
            if take == c.shape[0]:
                self._chunks.popleft()
            else:
                self._chunks[0] = c[take:]
            self._frames_avail -= take
        return got

    # ------------------------------------------------------------ 读线程
    def _reader_loop(self, path: str, ss: float, rate: int, ch: int, speed: float,
                     gen: int) -> None:
        err_lines: "collections.deque[str]" = collections.deque(maxlen=40)
        err_th: Optional[threading.Thread] = None
        total = 0
        blocks = 0
        try:
            ff = self.ffmpeg_exe()
            cmd = [ff, "-v", "error"]
            if ss > 0:
                cmd += ["-ss", f"{ss:.3f}"]
            cmd += ["-i", path, "-f", "f32le", "-acodec", "pcm_f32le",
                    "-ac", str(ch), "-ar", str(rate)]
            # 和 _decode 共用同一份滤镜串：2026-11-06 之前这里只有 atempo，重采样吃的是
            # swr 默认 16 抽头 —— 「预览测得 20 kHz 平直」并不代表用户播放时也平直。
            filt = stream_filters(speed, rate)
            if filt:
                cmd += ["-filter:a", filt]
            cmd += ["pipe:1"]
            _trace("reader-start", file=os.path.basename(path), ss=ss, rate=rate, ch=ch,
                   speed=speed, gen=gen)
            # stderr 始终接管道：解码失败时那几行是唯一能说清「为什么」的证据。
            # 常驻一根 daemon 排空线程，避免管道写满把 ffmpeg 卡住。
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    bufsize=rate * ch * 2,
                                    **_hidden_popen_kwargs())
        except Exception as e:
            # Popen / ffmpeg_exe 失败时这根线程直接死掉；不补这一段，引擎会永远
            # 「在播但没数」。错的不是用户点了暂停：置 error、不置 ended（见 _fail_segment）。
            _trace("reader-SPAWN-FAIL", gen=gen, err="%s: %s" % (type(e).__name__, e))
            self._fail_segment(gen, "decoder_unavailable",
                               "%s: %s" % (type(e).__name__, e))
            return
        err_pipe = getattr(proc, "stderr", None)
        if err_pipe is not None:
            def _drain_err(pipe) -> None:
                try:
                    for ln in iter(pipe.readline, b""):
                        err_lines.append(ln.decode("utf-8", "replace").rstrip())
                except Exception:
                    pass
            err_th = threading.Thread(target=_drain_err, args=(err_pipe,),
                                      daemon=True, name="salt-ff-err")
            err_th.start()
        # 代际对不上就别登记：这一看就是「上一次 seek/stop 没等到的那根旧线程」，
        # 把 self._ffmpeg 换成它的死进程会让下一次 stop 杀错对象、真解码进程跑在门外。
        with self._lock:
            if gen == self._gen:
                self._ffmpeg = proc
        frame_bytes = ch * 4
        block = max(1, int(rate * 0.05))            # 50ms 一块
        read_bytes = block * frame_bytes
        try:
            while not self._stop_evt.is_set():
                data = proc.stdout.read(read_bytes)
                if not data:
                    break
                total += len(data)
                blocks += 1
                x = np.frombuffer(data, dtype="<f4")
                if x.size % ch:
                    x = x[:x.size - (x.size % ch)]
                x = x.reshape(-1, ch)
                if not self._feed(x, gen):
                    break
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            stopped = self._stop_evt.is_set() or gen != self._gen
            rc = proc.poll()
            killed_by_us = False
            if rc is None and not stopped:
                # 自然读完时 stdout 先关、进程可能还差最后几毫秒才退出。先等它拿真退出码；
                # 贸然 terminate 会把「正常放完」变成非零退出，误报成解码失败。
                try:
                    rc = proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
            if rc is None:
                # 停掉的这一段也要收尸：不 wait 就留一个 running 的子进程给 GC。
                # 但「被我们 terminate 的」退出码不能当解码失败（那会自己制造假错误）。
                killed_by_us = True
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    rc = proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    rc = None
            tail = "-"
            try:
                if err_th is not None:
                    err_th.join(0.3)
                tail = " | ".join(list(err_lines)[-8:]) or "-"
            except Exception:
                pass
            try:
                # 排空线程退出后关掉这根管道；线程还活着就不碰（它正用着这个文件对象）。
                err_pipe = getattr(proc, "stderr", None)
                if err_pipe is not None and (err_th is None or not err_th.is_alive()):
                    err_pipe.close()
            except Exception:
                pass
            if not stopped and not killed_by_us and rc not in (0, None):
                # 解码进程自己死了（坏文件/编码不支持/中途崩溃）。这是错误，不是播完。
                self._fail_segment(gen, "decode_failed",
                                   tail if tail != "-" else ("ffmpeg exited %s" % rc),
                                   rc=rc, frames=total)
            else:
                self._mark_eof(gen)
            if _TRACE_ON:
                _trace("reader-exit", file=os.path.basename(path), ss=ss, gen=gen,
                       gen_now=self._gen, stop=self._stop_evt.is_set(),
                       rc=rc, bytes=total, blocks=blocks, eof=self._eof,
                       ended=self._ended, error=self._error, ffmpeg=tail)

    def _feed(self, x: np.ndarray, gen: int) -> bool:
        """把一块解码结果放进环形；返回 False＝「本线程已经过期，该退场了」。

        代际门的第二道牙（第一道是 _mark_eof）：控制面已经串行（play/seek 一把锁），
        但 stop 仍可能与 play 交错，join(1.0) 也可能等不到旧线程。只要旧代际的线程还在，
        它读到 False 就必须立刻结束——否则它的 ffmpeg 会继续把**另一份同一首歌**喂进
        这个环形，用户听到的就是 2026-11-08 那起「一直有回声和卡顿」：每一段被播两遍，
        underruns 仍为 0、bit-perfect 仍为真（样本没被动过，只是被重复了）。
        退出路径必须同时覆盖「等待前」和「等待后」：旧线程可能正卡在背压 wait 里。
        """
        with self._cond:
            if gen != self._gen:
                return False
            # 背压只看「队列满没满 + 有没有被要求停」，不看 paused：暂停时回调不再取数，
            # 若把 paused 也写进等待条件就会立刻放行、无限往 deque 里塞（长暂停＝内存泄漏）。
            # 正确做法：暂停时这里堵住，ffmpeg 因管道写满自然停在读文件处，恢复后接着流。
            while (self._frames_avail >= self._max_frames
                   and not self._stop_evt.is_set()):
                # 这里靠 0.2s 超时醒，不靠回调 notify：实时回调只碰 _lock、一次也不 notify
                # （在回调里唤醒线程等于把调度抖动引进音频路径）。容量是按这个前提定的：
                # _max_frames = 96000*2*2 ≈ 满幅 2s@96k，而 0.2s 最多被取走 19200 帧，
                # 醒不过来也饿不死 —— 2026-10-13 真机带 12 根 CPU 烧机负载实测 0.00 次/秒。
                self._cond.wait(0.2)
                if gen != self._gen:
                    return False
            if self._stop_evt.is_set() or gen != self._gen:
                return False
            self._chunks.append(x)
            self._frames_avail += x.shape[0]
            return True

    def _mark_eof(self, gen: int) -> None:
        """只有「当代」的读线程才有权把 _eof 置真。

        为什么这是一条真事故而不是一条保险（2026-10-12 在 57922 一次性服务器上量到）：
        seek() 会 terminate 旧 ffmpeg 再重开一根线程，旧线程在 finally 里补的那记
        _mark_eof() 落在**新段的空队列**上 —— state() 的收尾判据是 `_eof 且队列空`
        （见下面 state 里的 eof_empty），于是 seek 之后第一次 state()（就是 seek 响应
        自己那一次）就把引擎判成 ended:True、playing:False，进度冻在目标秒上不再走，
        前端那一拍看到的是「本曲播完」→ 直接跳下一曲。实测形状：
            seek->120 响应 ended=True position=120.0，之后 4 秒 position 恒 120.0
        没有代际门时，谁先跑到 finally 谁说话；带上代际门，旧线程那一记直接被丢掉。
        """
        with self._cond:
            if gen != self._gen:
                _trace("mark-eof-REJECT", gen=gen, gen_now=self._gen)
                return
            # 限幅器把整条流延迟了 D 帧：读到尾时往环里补 D 帧零，让最后 D 帧真音频
            # 能被推出去，而不是被截掉（5 ms，听不出，但「结尾少一截」是可测的缺陷）。
            # 只在共享档；独占档没有限幅器，也不需要这段尾巴。
            if self._lim_on:
                if self._lim is None:
                    self._lim = TruePeakLimiter(self._rate, self._ch)
                z = np.zeros((self._lim.D, self._ch), np.float32)
                self._chunks.append(z)
                self._frames_avail += z.shape[0]
            self._eof = True
            _trace("mark-eof-ACCEPT", gen=gen, avail=self._frames_avail,
                   lim=self._lim_on, paused=self._paused, ended=self._ended)
            self._cond.notify_all()

    def _fail_segment(self, gen: int, kind: str, detail: str,
                      rc: Optional[int] = None, frames: int = 0) -> None:
        """读线程发现「这一段真的坏了」时登记错误，而不是假装播完。

        为什么必须有这一条（2026-11-12 审查 A4/A5）：坏文件/生成失败时旧代码在 finally
        里照样 _mark_eof()，于是 state() 报 ended=True、playing=False —— 前端那套
        「wasPlaying && !playing && ended → 自动下一首」的边沿逻辑把它当成正常播完，
        用户看到的是「歌没播就自己跳走、列表里还没了」。错误必须与「播完」分开：
        _paused=True（停下来）、_eof 不动（不制造空队列收尾）、_error 记下原因。
        代际/停止门和 _mark_eof 同款：旧段或用户主动 stop 的那一记不算数。
        """
        with self._cond:
            if gen != self._gen or self._stop_evt.is_set():
                _trace("reader-error-REJECT", gen=gen, gen_now=self._gen,
                       stop=self._stop_evt.is_set(), kind=kind)
                return
            self._error = {"kind": str(kind), "detail": str(detail)[:500],
                           "rc": (int(rc) if rc is not None else None),
                           "frames": int(frames)}
            self._paused = True
            _trace("reader-error", gen=gen, kind=kind, rc=rc, frames=frames,
                   detail=str(detail)[:160])
            self._cond.notify_all()

    # ------------------------------------------------------------ 控制
    def configure(self, *, volume=None, balance=None, gains_db=None, rate=None,
                  loudness=None, fade_in_sec=0.0, dither=None,
                  speakers=None, speakers_amount=None) -> None:
        with self._lock:
            if volume is not None:
                self._gain = float(max(0.0, min(1.0, volume)))
            if balance is not None:
                self._balance = float(max(-1.0, min(1.0, balance)))
            if loudness is not None:
                self._loudness = bool(loudness)
                if not self._loudness:
                    self._agc_peak = 0.0
            if dither is not None:
                # 它改的是「下一次协商出来的独占流要不要加抖动」，已经开着的流不动：
                # 抖不抖是开流时算进 _ex_params 的，中途换会让同一首歌前后两段用两套量化。
                self._dither_enabled = bool(dither)
            if speakers is not None:
                self._speakers = bool(speakers)
                # 开关翻面时清一次滤波器/延时线：不清会把上一档的尾巴（13 个样本）
                # 带进新一档，听感上是一次极短的咔哒；清掉是确定性的从零开始。
                self._cf_zi = None
                self._cf_hist = None
            if speakers_amount is not None:
                self._speakers_amount = float(max(0.0, min(1.0, speakers_amount)))
            if gains_db is not None and rate:
                self._sos = _sos_eq(list(gains_db), rate)
                self._sos_state = None
                # 只在真的重算过 sos 之后才改这份回显：gains_db 有值但 rate 缺（那就不重算）时，
                # 回显必须留在旧值上，否则 state() 会报一条引擎里其实没配上的曲线。
                self._gains_db = [float(x) for x in list(gains_db)[:10]]
                self._eq_applied = True
            self._fade_in_sec = float(max(0.0, fade_in_sec or 0.0))
            self._fade_in_frames = int(self._fade_in_sec * (rate or self._rate))

    def _arm_priming(self, frac: float = 0.25, wait_s: float = 1.2) -> int:
        """挂上「灌数闸门」：环形攒到 frac 秒之前，回调吐零但**不记丢帧、不推进时钟**。返回阈值。

        调用方必须已持有 self._lock（那把是 Lock 不是 RLock，这里再 with 一层会自锁死）。

        为什么必须有这一段（2026-10-13 三条真机/离线量出来的）：
          · 顺序原本是「先开流、再起解码线程」，设备流一开就以 ~94 拍/秒向空环要数，
            而 ffmpeg 冷启动（spawn + DLL + demux）那一段是 0 帧。
          · 不带负载：回调时间轴 280 拍 0 空拍（首块 0ms —— 赶在第一拍之前到了，是运气不是设计）。
          · 带 12 根 CPU 烧机：首块 21ms，空拍 2 次全落在起播头 700ms 内，之后 0.00 次/秒。
          · seek 侧同道口更明显，而且是两次实测把它逼出来的（各留了产物）：
              8 拍/85ms —— 只在 HTTP 线程里阻塞等灌满：阻塞拦不住回调（afterprefill.1.txt）
              4 拍/43ms —— 闸门挂在 join＋起线程之后，可环形早在第一个锁块里就清空了，
                          中间那一段没人守（gate.1.txt）
            ⇒ 挂闸门的时机必须＝清环形的那一刻，所以 play/seek 都在**同一个锁块内**调它，
              seek 另在第二个锁块（_eof=False 之后）再挂一次：旧读线程可能在那之间把 _eof
              顶起来（_mark_eof 的代际门要到自增后才认），而闸门的出口之一正是 _eof。
        修的是「开局那零点几秒」，不是吞吐：稳态供不上的假设已被 tools/probe_underrun_cause.py
        否掉（裸 ffmpeg 1300–3200× 实时、正对照 0 丢帧、泵 p95 在名义周期内，12 档全 CLEAN）。

        闸门必须自己会失效，三条出口一条都不许少：够量 / 读到尾（坏文件、seek 到曲尾）/
        超过 wait_s（宁可让路、把这一段如实记成丢帧，也不许长住把进度冻死）。
        这里不阻塞调用线程：play/seek 立刻返回，用户按下去就立刻有响应。
        """
        frames = int(max(0.0, frac) * self._rate)
        self._prime_frames = frames
        self._prime_until = time.monotonic() + wait_s
        self._priming = frames > 0
        return frames

    def _can_reuse_exclusive(self, info: Dict[str, Any]) -> bool:
        """这一首能不能接着用手里那条独占流（＝切歌不 Release/Acquire 声卡）。

        用户听到的「切歌一声电流声」就长在 Stop→Initialize→Start 那一串上：样本侧的
        淡入淡出已经抹平了阶跃，所以剩下这一声是**驱动侧的硬切换**，只能在源头避免。
        复用不是「省一步」：独占流的格式在建流那一刻就定死了，判据必须逐项相等
        （见 `_EX_REUSE_KEYS`），否则就是拿 A 格式的流放 B 格式的曲。
        """
        if _ws is None or not info:
            return False
        with self._lock:
            if self._mode != "exclusive":
                return False
        with self._ex_lock:
            st, pump, stop = self._ex, self._ex_pump, self._ex_stop
            params = self._ex_params or {}
        if st is None or pump is None or stop is None:
            return False
        if stop.is_set() or not pump.is_alive():
            return False
        return all(params.get(k) == info.get(k) for k in self._EX_REUSE_KEYS)

    def _can_reuse_shared(self, rate: int, ch: int,
                          shared_idx: Optional[int]) -> bool:
        """共享档同理：同一台端点、同一档率与声道、流还活着就接着用。

        为什么要求 `not self._ended`：EOF 那一拍回调返回 paComplete，PortAudio 会把流
        **停掉**；一条已停的流不会自己再开始。自然播完后的下一首必须重开（共享档重开
        没有独占那种硬件硬切），手动切歌则走复用，省掉一次开流空隙。
        """
        with self._lock:
            return (self._mode == "shared" and self._stream is not None
                    and not self._ended
                    and int(self._rate) == int(rate)
                    and int(self._ch) == int(ch)
                    and self._shared_device_index == shared_idx)

    @_serialized_control
    def play(self, path: str, *, pos: float = 0.0, duration: float = 0.0,
             rate: int = 44100, ch: int = 2, device_index: Optional[int] = None,
             exclusive: bool = False, speed: float = 1.0,
             exclusive_rate: int = 0, exclusive_follow_source: bool = True,
             shared_device_index: Optional[int] = None,
             source_bits: int = 0, source_rate: int = 0,
             source_ch: int = 0, source_peak: Optional[float] = None) -> Dict[str, Any]:
        note = ""
        _trace("play-enter", file=os.path.basename(path), pos=float(pos),
               req_rate=rate, exclusive=exclusive, ex_req=exclusive_rate,
               follow=exclusive_follow_source, gen=self._gen,
               prev=os.path.basename(self._path or "") or "-")
        # rate 是「共享流和解码都必须用的率」。WASAPI 共享档不做重采样，只能开设备混音率：
        # 26 号端点（EDIFIER N500，混音 96 kHz）上开 44.1/48 都直接回
        # `[Errno -9997] Invalid sample rate`（2026-10-24 本机实测）。所以目标采样率只属于
        # 独占档，走 exclusive_rate；两档混用就是「不勾独占就整首没声音」那起事故。
        decode_rate = int(rate or 44100)
        ex_rate, ex_info = decode_rate, {}
        req_ex_rate = int(exclusive_rate or decode_rate)
        shared_idx = shared_device_index if shared_device_index is not None else device_index
        # 独占档必须**先协商再起解码线程**：独占不做任何重采样（共享档由系统混音器代劳），
        # 所以协商出来的采样率就是 ffmpeg 必须输出的采样率。顺序反了就会出现
        # 「解码按 44100 出数、设备按 48000 收数」——那不是音质问题，是 8.7% 的音高偏移＋变速。
        # 协商全程不调 Initialize（只 GetMixFormat/IsFormatSupported/GetDevicePeriod），
        # 所以在用户在场时也能跑；真占有设备排在后面的 `_open_exclusive`。
        if exclusive:
            ok, why, neg_rate, info = self._negotiate_exclusive(
                req_ex_rate, ch, device_index, source_bits, source_rate, source_ch,
                follow_source=exclusive_follow_source)
            if ok:
                ex_rate, ex_info = neg_rate, info
                note = why
            else:
                # fail-closed：任何一步不成都回落共享，并把**驱动给的原因**原样带回去。
                # 「设置里勾了独占但实际在共享」必须是界面能说出口的一句话，不能是沉默。
                note = "独占未生效，已回落共享输出：%s" % why
            _trace("play-neg", ok=ok, rate=ex_rate,
                   tag=(ex_info or {}).get("tag") or "-",
                   note=(note or "-")[:160])
        # 协商在前、停流在后，是这一段的要点：协商不碰设备（见 _negotiate_exclusive），
        # 所以「这一首能不能接着用手里那条流」必须在放掉它之前回答。判据逐项比施工图
        # （_can_reuse_exclusive），不是「差不多同档」；同一档时复用是等价且更干净的路径，
        # 不同档才走老的 stop→重开（独占改格式本来就只能重开，无法在活流上换率）。
        reuse = (self._can_reuse_exclusive(ex_info) if ex_info
                 else self._can_reuse_shared(decode_rate, ch, shared_idx))
        # 切歌诊断（一行，进后端日志）：复用还是重开、差在哪一项。用户报「还有一声」
        # 时先看这行——样本阶跃与驱动重开是两种病，不许靠猜。
        with self._lock:
            prev_path, prev_rate = self._path, self._rate
        reuse_diff = []
        if prev_path and not reuse and ex_info:
            with self._ex_lock:
                prev_params = self._ex_params or {}
            reuse_diff = [k for k in self._EX_REUSE_KEYS
                          if prev_params.get(k) != ex_info.get(k)]
        old_ff = old_reader = None
        if reuse:
            # 复用路径没有 stop() 帮忙收旧解码线程了：先记住它，出了锁再 terminate+join。
            # 代际门（_feed/_mark_eof）已经保证旧线程喂不进新环，这里只是把它尽快收走。
            with self._lock:
                old_ff, old_reader = self._ffmpeg, self._reader
        else:
            self.stop()
        with self._lock:
            # _rate 这里就是**设备实际那档率**（ex_rate）：priming 阈值、进度换算、
            # bit-perfect 判定都读它，留旧值会让「44100 起播、设备开在 96000」那一档
            # 的进度条按错的分母走。
            self._path, self._rate, self._ch = path, ex_rate, ch
            self._duration = duration
            self._base_pos = float(pos)
            self._consumed = 0
            self._chunks.clear(); self._frames_avail = 0
            self._stop_evt.clear(); self._ended = False
            self._paused = False
            self._eof = False
            self._error = None          # 新一段起播即清错：上一次解码失败不能挂到这一首头上
            self._gen += 1
            gen = self._gen
            self._underruns = 0
            self._speed = float(speed or 1.0)
            # 请求值 → 实际合成值，一次算好存着：state() 回显它，进度外推也乘它。
            # 为什么进度要乘「实际」而不是「请求」：因子串若有截断（atempo_chain 的 12 节上限、
            # 或有人日后把上限改小），耳朵里的倍速就不是界面标的那个数，此时时钟必须跟耳朵，
            # 不能跟界面 —— 否则进度条会跑到音频前面，而这正是本条修复要消灭的那类漂移。
            self._atempo = atempo_product(atempo_chain(self._speed)) or 1.0
            self._spec_pos = 0; self._spec[:] = 0
            self._sos_state = None; self._agc_peak = 0.0
            self._cf_zi = None; self._cf_hist = None   # 新一段从零起，不把上一首的尾巴带进来
            # 防爆音淡入地板：configure 刚按用户偏好（常为 0）设过 _fade_in_frames，这里再
            # 兜一段极短爬升，免得 priming 静音结束后第一帧硬跳到非零样本放出一声爆音。
            # 取较大值 ⇒ 用户开了更长的可听淡入时仍以用户为准。
            self._fade_in_frames = max(self._fade_in_frames,
                                       int(self._ANTI_CLICK_S * max(1, ex_rate)))
            if reuse:
                # 流不重开：让泵/回调在下一拍先把上一首的尾巴收到 0。这里**不许清**
                # _last_out/_last_tail —— 淡出要从那一帧/那段波形起收，清了就变成
                # 从满幅新样本硬切的爆音。
                self._switch_fade = True
            else:
                self._last_out = None
                self._last_tail = None
                self._switch_fade = False
            self._mode = "exclusive" if ex_info else "shared"
            self._ex_requested = bool(exclusive)
            self._device_index = device_index
            # 共享流实际开在哪台：默认「跟随系统」时也必须落成**一条明确的 WASAPI 端点**。
            # 传 None 会让 PortAudio 去问它自己的全局默认（本机是 MME 6 号），而 rate 是从
            # WASAPI 默认端点读来的 96 kHz —— MME 接 96k 后回调只按 ~26k 帧/秒供数（实测
            # 0.7 s 墙钟只走 0.192 s 进度），听感上就是坏掉/没声。所以两个索引分开存：
            # device_index 给独占（None=系统默认 COM 端点），shared_device_index 给共享。
            self._shared_device_index = shared_idx
            self._source_bits = int(source_bits or 0)
            self._source_rate = int(source_rate or 0)
            self._source_ch = int(source_ch or 0)
            # 下混预留只看「源声道 > 输出声道」，且**按这一首的实测真峰**算
            # （sp/downmix.gain_for）：真峰 1.70 才扣 5 dB，真峰 0.84 一个样本都不动。
            # 没量到真峰就退回避版前的 −6.00 dB 兜底（轻但绝不削顶）。静态、整轨一致，
            # 不会让响度随段落忽大忽小；残余峰由限幅器兜底。立体声/单声道恒 1.0。
            try:
                self._source_tp = float(source_peak) if source_peak else 0.0
            except (TypeError, ValueError):
                self._source_tp = 0.0
            self._downmix_trim = downmix_trim_for(ch, self._source_ch, self._source_tp or None)
            self._downmix_basis = ("none" if self._downmix_trim == 1.0 else
                                   ("measured" if self._source_tp > 0 else "fallback"))
            # 限幅器只在共享档开：独占档若也走它，bit-perfect 那句就成了假话。
            self._lim_on = (self._mode == "shared")
            self._lim = None          # 新一段从「没压过」起算，不把上一首的 GR 带进来
            self._native_note = note
            # 闸门与「清环形」同一个锁块：分开挂就是 gate.1.txt 量到的那 4 拍/43ms。
            prime_frames = self._arm_priming()
        _trace("play-armed", reuse=int(bool(reuse)), gen=gen, mode=self._mode,
               priming=prime_frames, spd=self._speed,
               fade=max(0, self._fade_in_frames))
        # 顺序要紧：先在同一锁块里挂闸门（见上），再起解码线程，最后开设备流。
        # 流一开就以 ~94 拍/秒向环里要数，而冷启动那一段环是空的 —— 闸门负责把这段
        # 「有意的等待」和「真供不上」分开（判据与实测形状见 _arm_priming 的 docstring）。
        if reuse and old_reader is not None:
            if old_ff is not None and old_ff.poll() is None:
                try:
                    old_ff.terminate()
                except Exception:
                    pass
            if old_reader.is_alive() and old_reader is not threading.current_thread():
                # 旧线程卡在背压时最多 0.2s 醒一次并看到代际变了；terminate 后读管道
                # 也会立刻返回。等它退干净再起新线程，避免两根 ffmpeg 同时解同一首。
                old_reader.join(timeout=0.4)
        self._reader = threading.Thread(target=self._reader_loop,
                                        args=(path, pos, ex_rate, ch, self._speed, gen),
                                        daemon=True, name="salt-native-decode")
        self._reader.start()
        # 开设备流：共享档在本线程、排在 _dev_lock 里；**独占档不在这里开**——把施工图交给
        # 泵，等泵在它自己那根线程上开好（原因与本机差分出处见 _open_exclusive）。
        # 锁序纪律照旧：_dev_lock → _pa_call，里面绝不取 self._lock；而等泵的那一会儿
        # **必须松开 _dev_lock**，否则泵来建流时正好被我挡住，每次都耗到超时才回落到共享。
        try:
            if self._mode == "exclusive":
                if reuse:
                    # 同一条流接着用：只把「这一首的参数」换上去（源位深/源率/抖动都随曲目
                    # 变，state() 与泵下一拍读的就是它），流对象一次 COM 调用都不碰。
                    # 只赋值不调 COM，锁序（_dev_lock → _ex_lock）不变。
                    # 但判据放行到这里之间，泵可能自己退了（设备掉了/出流错了，_ex 被清）：
                    # 这时还按复用走就没有任何流在收数——用户听到的是「没声」，界面却写着
                    # exclusive。同一把锁里复核一次，死了就退回重开那条路。
                    with self._ex_lock:
                        alive = self._ex is not None
                        if alive:
                            self._ex_params = ex_info
                    if not alive:
                        reuse = False
                        _trace("play-reuse-dead", gen=gen)
                        with self._lock:
                            # 新开出来的流没有「上一拍的尾巴」可淡出；不清掉旧值，
                            # 泵第一拍会从静音里放出全幅旧尾音——比硬切还响。
                            self._last_out = None
                            self._last_tail = None
                            self._switch_fade = False
                if not reuse:
                    try:
                        self._open_exclusive(ex_rate, ch, ex_info)
                    except Exception as e:
                        # 协商说行、真开不行（驱动在 Initialize 上变卦／那台正被别的独占占着）：
                        # **曲子照放**，档位如实降回共享，并把驱动原话带进 note。
                        # 这里不 fail 整次 play 是有意的：用户听到的是「有声音但没独占」，
                        # 比「没声音」诚实得多，也比「假装独占」正确。
                        fb = ("协商通过但独占开流失败，已回落共享输出：%s: %s"
                              % (type(e).__name__, e))
                        _trace("play-ex-open-FAIL", gen=gen, err=str(e)[:200],
                               ex_rate=ex_rate, dec_rate=decode_rate)
                        with self._lock:
                            self._mode = "shared"
                            self._native_note = fb
                        ex_info = {}
                        if int(ex_rate) != int(decode_rate):
                            # 独占协商出来的率（比如 44.1k）和共享档必须用的设备混音率
                            # （本机 96k）不同。这条解码线程是按 ex_rate 出数的，直接拿它开
                            # 共享流只会再吃一次 Invalid sample rate——用户听到的是「独占失败
                            # 之后彻底没声」。所以按共享率从头重开一次，位置接着走。
                            pos_now = self.position()
                            self.stop()
                            r = self.play(path, pos=pos_now, duration=duration,
                                          rate=decode_rate, ch=ch, device_index=device_index,
                                          exclusive=False, speed=speed,
                                          shared_device_index=shared_idx,
                                          source_bits=source_bits, source_rate=source_rate,
                                          source_ch=source_ch)
                            if r.get("ok") is not False:
                                with self._lock:
                                    self._native_note = fb
                            return r
                        with self._dev_lock:
                            self._open_stream(ex_rate, ch, shared_idx)
            elif not reuse:
                with self._dev_lock:
                    self._open_stream(ex_rate, ch, shared_idx)
        except Exception as e:
            # 开流失败时必须把刚起的线程收干净：早先的写法在这条路上线程还没起，
            # 现在线程在前，漏掉 stop() 就是「没人读的那根 ffmpeg 一直烧 CPU 到超时」。
            _trace("play-open-FAIL", gen=gen, mode=self._mode,
                   err="%s: %s" % (type(e).__name__, e))
            self.stop()
            self._ended = True
            return {"ok": False, "error": str(e), "mode": self._mode}
        # 响应里那份「独占读数」必须由**和 GET /api/native/state 同一个构造器**算出来。
        # 以前这里直接回 `ex_info`（协商施工图），而 api 那边是 `{**state(), **r}` ——
        # 后展开的那份把 state() 的完整视图整个盖掉，于是 play 的响应里
        # `stream_open / format / device_max_bits / device_rates` 全是空的（它们只存在于
        # 视图，不存在于施工图），前端在起播那一瞬间读到「独占开了但没格式」，要等下一次
        # 轮询才补齐。2026-10-20 真机产品路径实测抓到（判据 D2/D3/D4，
        # tools/probe_exclusive_product.py）——「两处各拼一份就会分家」这句注释原来就写在
        # `_exclusive_view_locked` 头上，而 play 正是漏掉的那一处。
        with self._ex_lock:
            st = self._ex
        ex_status = st.status() if st is not None else None   # 出锁再读：status() 不调 COM
        with self._lock:
            view = self._exclusive_view_locked(ex_status, ex_info)
            # 顶层那两个键是 play 契约的老形状（共享档、没请求独占时也要答得出），
            # 和视图里的同一个锁块内算一次，两边不可能不一致。
            bp, blocking = self._bit_perfect_locked(ex_info)
        if prev_path:
            print("[native-switch] reuse=%d %s->%s src=%s/%s tag=%s diff=%s note=%s"
                  % (int(reuse), prev_rate, ex_rate, source_bits, source_rate,
                     (ex_info or {}).get("tag") or "-",
                     ",".join(reuse_diff) or "-", note or "-"), flush=True)
        _trace("play-ok", gen=gen, mode=self._mode, rate=ex_rate,
               note=(self._native_note or "-")[:200])
        return {"ok": True, "mode": self._mode, "note": self._native_note,
                "rate": ex_rate, "channels": ch, "priming_frames": prime_frames,
                "bit_perfect": bp, "bit_perfect_blocking": blocking,
                "exclusive": view or None}

    def pause(self) -> None:
        with self._lock:
            _trace("pause", ended=self._ended, paused=self._paused, gen=self._gen)
            self._paused = True

    def resume(self) -> None:
        with self._cond:
            _trace("resume", ended=self._ended, paused=self._paused, eof=self._eof,
                   avail=self._frames_avail, gen=self._gen)
            if self._error is not None:
                # 上一次解码已经坏了：resume 只解除暂停，管道那头一个字节都不会再来。
                # 放行等于让界面重新写「正在播放」而进度空转 —— 也就是 A4 那类假播放。
                # 真要重试，走 play()（它会清 error 并重开解码）。
                _trace("resume-REJECT-error", kind=self._error.get("kind"))
                return
            self._paused = False
            self._cond.notify_all()

    @_serialized_control
    def seek(self, pos: float) -> None:
        _trace("seek-enter", pos=float(pos), gen=self._gen,
               ended=self._ended, paused=self._paused,
               file=os.path.basename(self._path or "") or "-")
        with self._lock:
            if not self._path or self._ended:
                # 没载曲 / 已播完：这里什么都该不做。旧写法只是「不改状态」，却照样往下
                # join ＋ 起新线程 —— 那是在一个已经关掉的流上再起一根 ffmpeg：白烧 CPU、
                # 进度冻在原处，看起来像「拖了没反应」。
                _trace("seek-IGNORED", pos=float(pos), path=bool(self._path),
                       ended=self._ended)
                return
            self._base_pos = float(pos)
            self._consumed = 0
            self._chunks.clear(); self._frames_avail = 0
            # seek 到新位置：交叉馈电的延时线/低通状态也一起清，别把旧位置的尾巴叠过来。
            self._cf_zi = None; self._cf_hist = None
            # 限幅器的前视/释放状态属于「上一段」：不清的话，seek 后的第一块会拿旧
            # 增益/旧延迟线开声，最轻是 5 ms 的旧音频，最重是一次没来源的压限。
            self._lim = None
            # 清环形的同一瞬间挂闸门：流还活着，晚了就是 gate.1.txt 那 4 拍/43ms。
            self._arm_priming()
            self._stop_evt.set()
            # 代际与 _eof 必须在**出锁去 join 之前**就作废。旧写法把这两步留在 join 之后
            # 的第二锁块里，于是 join 的整个窗口（真机实测 ~16 ms）都是敞开的：旧读线程
            # 恰在这期间收尾，它的 _mark_eof(旧 gen) 因代际未变而被 ACCEPT，而环形又刚
            # 被清空 ⇒ 任何一线程的 state()（2ms 轮询 / 独占泵）都满足「_eof 且队列空」，
            # 当场把 _ended 永久闩真。2026-11-11 实测 12 次 seek 中 3 次
            # （tools/_repro_seek_latch.result.txt）：seek 响应自己就是 ended=True、
            # playing=False，前端下一拍看到 ended 边沿 → 自动跳下一曲。
            # 自增提前后，那记 EOF 只会走 mark-eof-REJECT。两半都还是承重件：
            #   · 代际自增 —— 钉死「旧线程迟到/在 join 里补 EOF」这一支，离线尺：
            #     tools/test_audioplay.py::test_seek_eof_generation 与
            #     ::test_seek_join_window_eof（改前均为红）。
            #   · _eof=False —— 兜住同一个窗口里「旧 EOF 已被接受」的既有状态，以及
            #     暂停/复用路径上的陈旧 EOF；真机证伪（tools/_mut_seek_gen.py M2，
            #     2026-10-13）：只摘这一行，设备探针 1/1 翻红成 got=40.000 ended=True。
            self._gen += 1
            gen = self._gen
            self._eof = False
            self._error = None      # 拖动进度条就是「换一个落点重开解码」：旧的失败不再适用
            ff = self._ffmpeg
            if ff and ff.poll() is None:
                try:
                    ff.terminate()
                except Exception:
                    pass
            # 暂停态原样保留（以前这里写死 _paused=False）：拖一下进度条不该让一首
            # 正暂停的歌突然出声 —— chromium 档里 audio.currentTime = x 在暂停时仍保持暂停，
            # 原生档必须给同一句答复。续播是 playNative/resume 的活，不是 seek 的副作用。
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=1.0)
        old_alive = bool(self._reader and self._reader.is_alive())
        with self._lock:
            self._stop_evt.clear()
            # 再挂一次：join 期间泵可能已把第一次 priming 从超时出口放掉，而新一段的
            # 闸门要一直盖到首块有货为止。
            self._arm_priming()
            rate, ch, path = self._rate, self._ch, self._path
            # seek 后同样是「priming 静音 → 新位置首样本」的阶跃，兜一段淡入地板防爆音。
            self._fade_in_frames = max(self._fade_in_frames,
                                       int(self._ANTI_CLICK_S * max(1, rate)))
        self._reader = threading.Thread(target=self._reader_loop,
                                        args=(path, pos, rate, ch, self._speed, gen),
                                        daemon=True, name="salt-native-decode")
        self._reader.start()
        _trace("seek-restarted", pos=float(pos), gen=gen, rate=rate,
               old_reader_alive=old_alive)

    def stop(self) -> None:
        _trace("stop-enter", gen=self._gen,
               file=os.path.basename(self._path or "") or "-",
               reader_alive=bool(self._reader and self._reader.is_alive()))
        self._stop_evt.set()
        with self._lock:
            self._paused = True
            self._ended = True
            # 停止是用户的明确意图，不是「这一段坏了」：清掉解码错误，
            # 免得下一次 play 之前的面板还挂着上一首的失败横幅（A4/A5）。
            self._error = None
            # 半路停下时还挂着的「切歌过桥」作废：这一次不是换曲，尾巴由泵退出前的
            # _drain_fadeout 收（它读 _last_out，不受这个标志影响）。
            self._switch_fade = False
            # 代际一并作废：join 只等 1 秒，等不到的旧线程随后进 finally 补那记 EOF 时
            # 号已经对不上，不会把「停掉之后下一次 play」的空队列判成播完（同 _mark_eof）。
            self._gen += 1
            ff = self._ffmpeg
        with self._ex_lock:
            pump, pump_stop = self._ex_pump, self._ex_stop
            # 登记表先摘走：泵自己会退出（下面 join），摘了就不会有第二个 stop 再 join 一次，
            # 而 _ex_params 留着——它只在下一条流 _create_exclusive_stream 时被整体替换。
            self._ex_pump = None
            self._ex_stop = None
        if pump_stop is not None:
            pump_stop.set()
        if ff and ff.poll() is None:
            try:
                ff.terminate()
            except Exception:
                pass
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=1.0)
        # 独占侧收尾：**先等泵退出，再关流**。顺序反了就是「一边有人正往 COM 缓冲里 memmove、
        # 一边有人 Release 那个接口」——那是访问违例（进程当场没了、stderr 全空），
        # 不是能兜住的异常。等不到就**不关**：宁可让下一句 Initialize 报 DEVICE_IN_USE
        # 并老实回落共享，也不冒把用户整个 App 打死的风险。
        if pump is not None and pump is not threading.current_thread():
            pump.join(timeout=1.5)
            if pump.is_alive():
                with self._ex_lock:
                    self._ex_released = ("泵线程 1.5s 没退出：这次不关独占流"
                                         "（会踩到正在写的那一帧）")
            else:
                self._close_exclusive("播放已停止，独占设备已释放")
        elif pump is None:
            # 没有泵（共享档／上一次已经收干净）：但如果手上还攥着一条流，也必须放掉，
            # 否则「独占把整机声音锁在没人推流的死流上」。
            self._close_exclusive("播放已停止，独占设备已释放")
        with self._lock:
            self._chunks.clear(); self._frames_avail = 0
            st = self._stream
        if st is not None:
            # 关流 + 销毁实例也要回 pa-ctrl 线程：Pa_CloseStream / Pa_Terminate 同属那段
            # 带线程黏性的全局状态（见文件头实验 B/C）。回调只碰 self._lock，而这里调
            # _pa_call 时**没有**持 self._lock（上面 1518 那块已经放掉了），不会互锁。
            def _close_shared() -> None:
                try:
                    st.stop_stream(); st.close()
                except Exception as e:
                    print("[audio-stop] 关共享流失败：%s: %s" % (type(e).__name__, e),
                          flush=True)
            _pa_call(_close_shared)
        self._stream = None
        if self._pa is not None:
            inst = self._pa

            def _term_shared() -> None:
                try:
                    inst.terminate()
                except Exception as e:
                    print("[audio-stop] terminate 失败：%s: %s" % (type(e).__name__, e),
                          flush=True)
            _pa_call(_term_shared)
        self._pa = None
        _trace("stop-done", gen=self._gen,
               reader_alive=bool(self._reader and self._reader.is_alive()))

    # ------------------------------------------------------------ 状态 / 频谱
    def _pos_src_locked(self) -> float:
        """当前播放位置（源时间，秒）。调用方必须已持有 self._lock。

        _consumed 数的是「喂进设备的帧」，那是**输出**时间；变速（atempo=2）会让 1 秒
        输出装 2 秒源内容，所以源进度 = 输出秒 × speed。不乘这个系数，2× 变速时进度条
        只按真实的一半走（终点是源 duration，两把尺对不上）。base_pos 本身就是源秒，直接加。
        乘的是 _atempo（因子串的实际乘积）而不是 _speed（请求值）：见 play() 里那段——
        两者只在「拆分被截断」时才分家，一分家时钟就必须跟耳朵。
        """
        # 共享档的真峰值限幅器把整条流延迟了 D 帧：设备「已经吃掉」的帧里，头 D 帧
        # 是延迟线填出来的零，不是源内容。不扣掉就会在片尾报出 duration+5ms 的位置
        # （耳朵在放最后一段，进度条先跳过头）；seek 后也会让整首进度提前 5ms。
        # 只扣「设备真正消耗过」的那部分，暂停时 consumed 不涨，画面不会被拉回去。
        latency = latency_frames(self._rate) if self._lim_on else 0
        elapsed = max(0, self._consumed - latency)
        return self._base_pos + (elapsed / max(1, self._rate)) * (self._atempo or 1.0)

    def position(self) -> float:
        with self._lock:
            return self._pos_src_locked()

    def _bit_perfect_locked(self, p: Optional[Dict[str, Any]]):
        """「这一拍到底有没有动过样本」——独占 ≠ 自动 bit-perfect，所以这句必须算出来。

        逐条判，回 (bool, [原因])。原因列表直接进界面（用户能看见「为什么不是」），
        同时也是 tools/test_audioplay.py 里那条变异判据的靶子：把任何一条从函数里摘掉，
        对应那个旋钮必须翻红——摘不动就说明那条是装饰。
        为什么不自动放行「音量 0.8」：0.8 是**每个样本都乘过一遍**，那已经不是源数据了，
        这时候标 bit-perfect 就是拿「独占」这个词给一个仍在处理的数据流背书。
        分两半判：**处理侧**（增益/声像/EQ/响度/淡入/倍速）看引擎自己动没动手，
        **源与设备侧**（位深、采样率、声道数、设备给的容器）看送出去的那串字节还是不是原样。
        源侧三个量**量不到就算一条阻塞原因**（0＝不知道，不是 0 位）：宁可界面多说一句
        「不敢判」，也不许它在一台 24 位源 + 16 位独占的机器上点头。
        """
        p = p or {}
        reasons: List[str] = []
        if self._mode != "exclusive":
            reasons.append("当前是共享输出（经系统混音器）")
        if abs(self._gain - 1.0) > 1e-9:
            reasons.append("音量 %g ≠ 1.0" % self._gain)
        if abs(self._balance) > 1e-9:
            reasons.append("声像 %g ≠ 中" % self._balance)
        if self._sos is not None:
            reasons.append("均衡器挂着 %d 段二阶节" % len(self._sos))
        if self._loudness:
            reasons.append("响度均衡开着")
        if self._speakers and self._speakers_amount > 1e-4:
            reasons.append("虚拟扬声器（交叉馈电）开着")
        if self._fade_in_sec > 0:
            reasons.append("起播淡入 %.1fs" % self._fade_in_sec)
        if abs((self._atempo or 1.0) - 1.0) > 1e-9:
            reasons.append("倍速 %.2f×" % (self._atempo or 1.0))
        if p.get("device_bit_perfect") is not True:
            reasons.append(p.get("why") or "设备侧不满足（没有同率同位深的独占格式）")
        # 源侧三个量：**没量到就不许宣布 bit-perfect**。这一组是「不确定」不是「有损失」，
        # 但界面上不能拿不确定冒充确定——否则一句「勾了独占就是 bit-perfect」就成了谎话。
        # 读法用「键在不在」而不是 `p.get(k) or self._k`：施工图里明写了 0＝没量到，
        # 而 `or` 会把 0 当「没这个键」，转而拿引擎上一个字段值（可能是上一首留下的 24）来回答，
        # 于是「不确定」被悄悄换成「一个没来源的确定」。施工图是权威，它说不知道就是不知道。
        def src(key, fallback):
            return int(p[key]) if (p and key in p and p[key] is not None) else int(fallback or 0)
        sb = src("source_bits", self._source_bits)
        sc = src("source_ch", self._source_ch)
        if sb <= 0:
            reasons.append("源位深没量到（bit_depth=0）⇒ 只能说「不经混音器、不加处理」，"
                           "不能说 bit-perfect")
        elif sb > 24:
            reasons.append("源 %d 位：解码链路是 float32（尾数 24 位），"
                           "整型 32 位源在这里就装不回去了" % sb)
        sr = src("source_rate", self._source_rate)
        dr = int(p.get("rate") or 0)
        if not sr:
            reasons.append("源采样率没量到 ⇒ 按设备档解码，无法确认「不重采样」")
        elif sr and dr and sr != dr:
            reasons.append("源 %d Hz → 设备 %d Hz（重采样）" % (sr, dr))
        dch = src("ch", self._ch)
        if sc and dch and sc != dch:
            reasons.append("源 %d 声道 → 输出 %d 声道（下混，不是源数据了）" % (sc, dch))
        return (not reasons), reasons

    def _exclusive_view_locked(self, st_status: Optional[Dict[str, Any]],
                               p: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """state()/play() 里那一份「独占到底怎么样了」的读数，只有一个构造函数
        （两处各拼一份就会分家，而这一列是要进界面和验收闸的）。"""
        if not self._ex_requested and self._mode != "exclusive":
            return {}
        bp, blocking = self._bit_perfect_locked(p)
        view = {
            "requested": bool(self._ex_requested),
            "stream_open": bool(st_status is not None),
            "released": self._ex_released,
            "device_name": (p or {}).get("device_name"),
            "format": (p or {}).get("tag"),
            "why": (p or {}).get("why"),
            "bit_perfect": bp,
            "bit_perfect_blocking": blocking,
            # 下面这些是给界面**照抄**用的原始量，不是结论：面板要说「独占 pcm16@44100 ·
            # 设备最深 16 位 · 源 24/192000 · 加了 TPDF 抖动」时，一个字都不许前端自己猜。
            "device_max_bits": (p or {}).get("exclusive_max_bits"),
            "device_rates": (p or {}).get("exclusive_rates"),
            "source_bits": (p or {}).get("source_bits", self._source_bits),
            "source_rate": (p or {}).get("source_rate", self._source_rate),
            "source_ch": (p or {}).get("source_ch", self._source_ch),
            "dither": bool((p or {}).get("dither")),
            "quantizes": bool((p or {}).get("quantizes")),
            "period_ms": (p or {}).get("period_ms"),
            "note": self._native_note,
        }
        if st_status:
            view.update(st_status)
        return view

    def _limiter_view_locked(self) -> Dict[str, Any]:
        """共享档削顶处理的真值。**不拿「开关开着」冒充「真的在压」**：
        on 是这一档有没有接上限幅器；frames=0 说明还没处理过一块；max_gr_db=0
        说明这一段一次都没压；input_peak>ceiling 而 max_gr_db=0 才是真的矛盾（该报红）。"""
        if not self._lim_on:
            return {"on": False}
        if self._lim is None:
            return {"on": True, "frames": 0}
        return {"on": True, **self._lim.stats()}

    def state(self) -> Dict[str, Any]:
        # 独占那三件（流对象、参数、回执）只被赋值护在 _ex_lock 里；这里取一次快照，
        # 出锁后再读——status() 只是把 Python 整数抄进 dict，一次 COM 都不调，
        # 所以就算这一瞬间另一线程正关流，也不会踩到已经释放的接口。
        with self._ex_lock:
            st, params = self._ex, self._ex_params
        ex_status = st.status() if st is not None else None
        with self._lock:
            # 顺序有讲究：先把 ended 结算完再算 playing。反过来写的话，「刚播完」那第一拍会
            # 同时报 playing=True 和 ended=True —— 前端 nativePollTick 的跳曲边沿认的是
            # 「playing 由真转假 且 ended」，于是那一次轮询什么都不会发生，得等下一拍才跳。
            eof_empty = self._eof and self._frames_avail == 0
            if eof_empty and not self._ended:
                _trace("state-eof->ended", consumed=self._consumed,
                       base=self._base_pos, avail=self._frames_avail)
                self._ended = True
            playing = (not self._paused) and (not self._ended)
            return {
                "ok": True, "engine": "native",
                "path": self._path, "playing": playing,
                "position": self._pos_src_locked(),
                "duration": self._duration, "rate": self._rate, "channels": self._ch,
                # 设备/宿主 API 错配是「界面在播、耳朵没声」最常见的成因，所以把**真正
                # 开流的那台端点**也报出来：共享档不等于 PortAudio 的全局默认设备。
                "out_rate": self._rate, "out_device_index": self._shared_device_index,
                "ended": self._ended, "mode": self._mode, "native_note": self._native_note,
                "speed": self._speed,
                # 解码错误（A4/A5）：None＝没出错。有值时形状
                # {"kind": "decode_failed"|"decoder_unavailable", "detail", "rc", "frames"}。
                # 前端靠它说「这首歌放不了」并**停止自动跳曲**——错误和 ended 是两件事，
                # 混成一个就会复现「没播就自己跳走」。
                "error": dict(self._error) if self._error else None,
                # 耳朵里的倍速（atempo 因子串乘积）。前端/闸门拿它和 speed 对：
                # 不等就是「标 4× 播 2×」那类谎，屏幕上量得到，不靠读代码保证。
                "atempo": self._atempo,
                # priming＝「有意在等一段有货再放行」，和 underruns（真供不上）必须分得开：
                # 前端设置面板那行「累计丢帧」只有在不把闸门算进去时才是可信数字。
                "priming": self._priming,
                "underruns": self._underruns, "gain": self._gain, "balance": self._balance,
                "loudness": self._loudness,
                # 源侧三量与下混余量：界面要说「5.1 → 立体声，−6 dB 预留 + 限幅器」
                # 时，数据从这里照抄，不在前端猜。source_ch=0 是「没量到」，不是单声道。
                "source_bits": self._source_bits,
                "source_rate": self._source_rate,
                "source_ch": self._source_ch,
                "downmix_trim_db": round(float(20.0 * np.log10(max(self._downmix_trim, 1e-12))), 4),
                # 这份预留是怎么来的：measured＝按本曲实测真峰算的；fallback＝还没量到、
                # 退回 −6.00 dB；none＝不用预留（立体声/单声道）。source_tp 是那个真峰
                # （线性值，0＝没量到）。界面要能说「−2.60 dB（按本曲真峰 +1.18）」，
                # 而不是只给一个没有出处的数字。
                "downmix_basis": self._downmix_basis,
                "source_tp": round(float(self._source_tp or 0.0), 6),
                # 限幅器读数：ceiling/look/latency/max_gr_db/demand_pct/input_peak/output_peak。
                # 没有这一列，「与微软播放器的差距」只能靠耳朵争论；有了它，削了多少、
                # 还剩多少峰，屏幕上能读。独占档这里 on=False（bit-perfect 不经过它）。
                "limiter": self._limiter_view_locked(),
                # 虚拟扬声器的真值也回显：前端只说「开着 / 强度多少」，不自己推断。
                # amount=0 时虽然开关是 on，但 DSP 分支不执行 —— enabled 只说开关，effect 说真动没动。
                "speakers": {"enabled": bool(self._speakers),
                             "amount": float(self._speakers_amount),
                             "effect": bool(self._speakers and self._speakers_amount > 1e-4)},
                # 独占那一档的整份读数：请求没请求、流开没开、开了什么格式、**是不是
                # bit-perfect 以及不是的话卡在哪几条**、什么时候放的设备。面板要说
                # 「独占 pcm16@44100 · 非 bit-perfect（音量 0.8≠1）」时，界面上一个字都不许猜。
                "exclusive": self._exclusive_view_locked(ex_status, params),
                # 均衡器的真值：active 就是「此刻引擎里挂着几条二阶节」。全 0 曲线时 _sos 是
                # None（_sos_eq 空/全 0 返回 None），所以关掉均衡器这一项自己就会落回 False，
                # 不需要谁再补一个开关位。bands 让面板能说「服务端十段」而不是「已接入」。
                "eq": {"active": self._sos is not None,
                       "bands": 0 if self._sos is None else int(len(self._sos)),
                       "gains_db": list(self._gains_db),
                       # active 只说「现在挂着几条」，applied 说「这份曲线被 configure 收过没有」。
                       # 两个都为 False 时不能断言「增益都是 0」——那可能只是还没配过。
                       "applied": bool(self._eq_applied)},
            }

    def spectrum(self, bands: int = 24) -> List[float]:
        with self._lock:
            buf = self._spec.copy(); pos = self._spec_pos
            # 「有没有在出声」不能只问 PortAudio 那条流：独占档下 self._stream 永远是 None
            # （独占由泵线程推），照旧写法频谱在独占档会**一直是一条直线**——那不是没音乐，
            # 是量具看不见。两条路各问一次，任一开着就出数。
            live = self._stream is not None or self._ex is not None
            paused = self._paused
        if not live or paused:
            return [0.0] * bands
        w = np.hanning(buf.size)
        spec = np.abs(np.fft.rfft(buf * w))
        freqs = np.fft.rfftfreq(buf.size, 1.0 / self._rate)
        edges = np.geomspace(40.0, min(16000.0, self._rate / 2), bands + 1)
        out = []
        for i in range(bands):
            m = (freqs >= edges[i]) & (freqs < edges[i + 1])
            v = float(spec[m].mean()) if m.any() else 0.0
            out.append(min(1.0, v / 400.0))
        return out


def _normalize(x: np.ndarray, target_peak: float) -> np.ndarray:
    pk = float(np.max(np.abs(x))) if x.size else 0.0
    if pk < 1e-6:
        return x
    return x * (target_peak / pk)


_ENGINE: Optional[NativeEngine] = None
_ENGINE_LOCK = threading.Lock()


def get() -> NativeEngine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = NativeEngine()
        return _ENGINE
