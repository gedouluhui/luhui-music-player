# -*- coding: utf-8 -*-
"""原生引擎 sp/audioplay.py 的离线自检（含变异证伪）。全程 preview/纯函数，不开设备、不出声。

要钉住的东西，以及为什么这条尺有牙：
  1) 均衡器三段事实（EQ_HZ / EQ_TYPE / EQ_RANGE）必须和前端 web/app.js 完全一致 ——
     同一个「均衡器」在浏览器档和原生档听感不同不可接受。这条靠**解析 app.js** 取真值对撞，
     不是抄一份常量再自比（那是循环参照，打不红）。
  2) 解码→float32→DSP 真的接上了：拿库里一段真 FLAC 走 preview，断言非静音、峰值∈(0,1]、
     帧数=秒×采样率、并且 bass 预设确实**只抬低频**（分频带 FFT 能量比）——
     只看整体 rms 会被「任何增益都涨」蒙混，分频带才证得出是「均衡器」而不是「音量旋钮」。
  3) 平衡用等功率声像：balance=0 时 L/R 增益平方和=1；±1 时把能量整侧推。
  4) 响度均衡把峰值归一到 target_peak。
  5) 环形缓冲 _take_into 的跨块取数（半块消耗后剩余仍在队首）——这是实时回调的供数内核，
     错了就会吞帧/重复帧。
  6) seek 的「读线程代际」门：旧线程迟到的 EOF 不许把新一段判成播完（真机拖进度条时
     实测到过整条死路，见 test_seek_eof_generation 的 docstring）。
  7) 播完由回调自己收口：没人 GET state() 的时候也要立 ended、并停止把空队列虚记成丢帧
     （真机量到一曲唱完后 underruns 从 2 涨到 7629，见 test_end_of_track_self_closes）。
  8) seek 原样保留暂停态：暂停中拖进度条只挪指针、不出声，播放中拖也不许被顺手按停
     —— 原生档原先在 seek() 里写死 _paused=False，等于「拖一下条就出声」，和 chromium 档
     的 audio.currentTime 语义不一致（见 test_seek_preserves_pause）。

变异证伪（feedback-mutation-falsification）：每条关键断言都配一处「把它注坏」的变异，
要求自检当场翻红并归因到**具体断言名**；注坏却还全绿＝这条尺是瞎的。
"""
import json
import os
import re
import subprocess
import sys
import time
import types
import pathlib
import threading
import numpy as np

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, ROOT.as_posix())
# 逐行刷出去：这套自检要跑几分钟（并发枚举那档），重定向到文件时块缓冲会让「跑到第几条」
# 不可信 —— 真挂死时日志是 0 字节，等于事故现场没留痕（2026-10-20 实发：整趟挂住，
# 盘上那份 _ap_run.txt 一个字节都没有，只能靠进程 CPU 时间反推）。
try:
    # 编码也要兜住：断言名里有 ⇒/× 这类符号，重定向到文件时 Windows 默认 GBK 会让
    # print 抛 UnicodeEncodeError —— 量具自己的 rc 会随「接终端 / > 文件」变形状，
    # 看起来像产品红。统一成 UTF-8（errors=replace），三种调用形状都必须跑完。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            _s.reconfigure(line_buffering=True)
except Exception:
    pass
from sp import audioplay  # noqa: E402

FAILS = []
LIB = ROOT.parent  # 库里音频在用户桌面，下面用 db 里第一条真实路径


def check(name, cond, detail=""):
    if not cond:
        FAILS.append("%s%s" % (name, (" — " + detail) if detail else ""))
        print("  FAIL " + name + (" — " + detail if detail else ""))
    else:
        print("  ok   " + name)


def _fixture_wav():
    """干净 checkout 用的合成夹具：44.1k / 16bit / 立体声，6 秒，峰值 < 1。

    为什么不直接读库：公开仓库里没有用户曲库，`db` 也不该被一条自检顺手建出来。
    内容覆盖低频/中频/高频三段，低频 55 Hz 与 3k/9k 常驻，保证均衡器分频带断言有东西可量。
    """
    import tempfile
    import wave
    path = pathlib.Path(tempfile.gettempdir()) / "luhui-audioplay-fixture" / "tone-44100-16.wav"
    if path.exists() and path.stat().st_size > 10000:
        return str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sr, secs = 44100, 6.0
    t = np.arange(int(sr * secs), dtype=np.float64) / sr
    env = 0.55 + 0.45 * np.sin(2 * np.pi * 0.25 * t)
    left = (0.22 * np.sin(2 * np.pi * 55 * t)
            + 0.12 * np.sin(2 * np.pi * 440 * t)
            + 0.07 * np.sin(2 * np.pi * 3000 * t)
            + 0.04 * np.sin(2 * np.pi * 9000 * t))
    right = (0.22 * np.sin(2 * np.pi * 55 * t + 0.4)
             + 0.10 * np.sin(2 * np.pi * 660 * t)
             + 0.07 * np.sin(2 * np.pi * 5000 * t))
    data = np.stack([left, right], 1) * env[:, None] * 0.8
    pcm = (np.clip(data, -0.98, 0.98) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return str(path)


def first_real_audio_path():
    forced = os.environ.get("SALT_AUDIO_FIXTURE")
    if forced and pathlib.Path(forced).is_file():
        return forced
    try:
        from sp.config import db_path
        if db_path().exists():
            from sp import db
            rows = db.query("SELECT path FROM tracks WHERE missing=0 AND fmt IN ('flac','wav') ORDER BY id LIMIT 1")
            if not rows:
                rows = db.query("SELECT path FROM tracks WHERE missing=0 ORDER BY id LIMIT 1")
            if rows:
                return rows[0]["path"]
    except Exception:
        pass
    return _fixture_wav()


def test_eq_matches_frontend():
    js = (ROOT / "web" / "app.js").read_text("utf-8")
    m_hz = re.search(r"const\s+EQ_HZ\s*=\s*\[([\d,\s]+)\]", js)
    m_ty = re.search(r"const\s+EQ_TYPE\s*=\s*\[([^\]]+)\]", js)
    m_rg = re.search(r"const\s+EQ_RANGE\s*=\s*([\d.]+)", js)
    check("app.js 有 EQ_HZ", bool(m_hz))
    hz = [float(x) for x in m_hz.group(1).split(",")]
    check("EQ_HZ 前后端一致", hz == [float(x) for x in audioplay.EQ_HZ],
          "js=%s py=%s" % (hz, audioplay.EQ_HZ))
    check("EQ_RANGE 前后端一致", m_rg and float(m_rg.group(1)) == audioplay.EQ_RANGE,
          "js=%s py=%s" % (m_rg.group(1) if m_rg else None, audioplay.EQ_RANGE))
    # EQ_TYPE: js 写成 ['lowshelf', ...Array(8).fill('peaking'), 'highshelf']
    check("EQ_TYPE 形状一致", audioplay.EQ_TYPE[0] == "lowshelf"
          and audioplay.EQ_TYPE[-1] == "highshelf"
          and set(audioplay.EQ_TYPE[1:-1]) == {"peaking"}
          and len(audioplay.EQ_TYPE) == 10)


def band(x, lo, hi, fs=48000):
    mono = x.mean(1)
    X = np.abs(np.fft.rfft(mono * np.hanning(mono.size)))
    f = np.fft.rfftfreq(mono.size, 1.0 / fs)
    m = (f >= lo) & (f < hi)
    return float(X[m].mean()) if m.any() else 0.0


def test_preview_dsp():
    e = audioplay.get()
    p = first_real_audio_path()
    check("库里取到真实音频文件", bool(p) and pathlib.Path(p).is_file(), str(p))
    if not p:
        return
    flat = e.preview(p, seconds=1.0, rate=48000, ch=2)
    check("解码形状 = (秒×率, 声道)", flat.shape == (48000, 2), str(flat.shape))
    check("非静音且峰值不超范围", flat.size and 0 < float(np.max(np.abs(flat))) <= 1.0,
          "peak=%s" % float(np.max(np.abs(flat))))
    bass = [9, 8, 6, 3, 0, -1, -2, -2, -1, 0]
    boosted = e.preview(p, seconds=1.0, rate=48000, ch=2, gains_db=bass)
    check("bass 预设抬低频", band(boosted, 20, 120) > band(flat, 20, 120) * 1.5,
          "low %s->%s" % (band(flat, 20, 120), band(boosted, 20, 120)))
    check("bass 预设收高频", band(boosted, 4000, 16000) < band(flat, 4000, 16000),
          "hi %s->%s" % (band(flat, 4000, 16000), band(boosted, 4000, 16000)))
    half = e.preview(p, seconds=1.0, rate=48000, ch=2, volume=0.5)
    r = float(np.sqrt((half ** 2).mean())) / float(np.sqrt((flat ** 2).mean()))
    check("音量 0.5 近似减半 rms", 0.45 < r < 0.55, "ratio=%s" % r)
    loud = e.preview(p, seconds=1.0, rate=48000, ch=2, loudness=True)
    check("响度均衡归一到峰值≈0.95", abs(float(np.max(np.abs(loud))) - 0.95) < 0.02,
          "peak=%s" % float(np.max(np.abs(loud))))


def test_balance_equal_power():
    e = audioplay.get()
    x = np.full((64, 2), 0.4, np.float32)
    e._sos = None; e._sos_state = None; e._loudness = False
    e._gain = 1.0; e._fade_in_frames = 0
    e._balance = 0.0
    c = e._process_block(x.copy())
    check("平衡 0：等功率 L²+R²=输入²×(cos²+sin²)",
          abs(c[0, 0] ** 2 + c[0, 1] ** 2 - 2 * 0.4 ** 2) < 1e-4 or
          abs((c[0, 0] ** 2 + c[0, 1] ** 2) / (0.4 ** 2 + 0.4 ** 2) - 1.0) < 1e-3,
          "got L=%.4f R=%.4f" % (c[0, 0], c[0, 1]))
    e._balance = 1.0
    r = e._process_block(x.copy())
    check("平衡 +1 推到右声道", r[0, 0] < 1e-3 and r[0, 1] > 0.39, "L=%.4f R=%.4f" % (r[0, 0], r[0, 1]))
    e._balance = -1.0
    l = e._process_block(x.copy())
    check("平衡 -1 推到左声道", l[0, 1] < 1e-3 and l[0, 0] > 0.39, "L=%.4f R=%.4f" % (l[0, 0], l[0, 1]))
    e._balance = 0.0


def test_virtual_speaker():
    """虚拟扬声器＝耳机交叉馈电：关掉必须 bit-exact，开着必须真把对侧声音送过来。

    这条尺防的是两种最容易骗过耳朵的假实现：
      · 「开关翻面了但 DSP 没接上」—— 屏上开关是绿的、耳朵里什么都没变；
      · 「只是把整体音量拧大」—— 单声道内容也会变响，听感上像有效果，其实不是空间处理。
    所以断言分成两半：硬左脉冲后右声道**在延时之后**才出现能量；单声道内容开与不开
    的稳态幅度相同（±2%）。
    """
    e = audioplay.get()
    e.stop()
    _arm(e)
    e._sos, e._sos_state = None, None
    e._loudness = False
    e._gain, e._balance = 1.0, 0.0
    e._fade_in_frames = 0
    e._speakers, e._speakers_amount = False, 0.35
    e._cf_zi = e._cf_hist = None
    rng = np.random.RandomState(7)
    x = (rng.rand(512, 2).astype(np.float32) - 0.5) * 1.2
    off = e._process_block(x.copy())
    check("虚拟扬声器关：输出与输入逐样本相同（bit-exact 直通）",
          np.array_equal(off, np.clip(x, -1, 1).astype(np.float32)),
          "maxdiff=%s" % float(np.max(np.abs(off - np.clip(x, -1, 1)))))

    # 硬左脉冲：右声道在 0.3 ms 延时之前必须是零，之后必须收到低通后的左声道。
    e._speakers, e._speakers_amount = True, 1.0
    e._cf_zi = e._cf_hist = None
    imp = np.zeros((512, 2), np.float32)
    imp[200, 0] = 0.9
    out = e._process_block(imp.copy())
    d = max(1, int(round(0.0003 * e._rate)))
    pre = float(np.max(np.abs(out[:200 + d, 1])))
    post = float(np.max(np.abs(out[200 + d:, 1])))
    left = float(out[200, 0])
    check("交叉馈电：对侧（右）在延时前没有声音", pre < 1e-7, "pre_peak=%g" % pre)
    check("交叉馈电：对侧（右）在延时后收到左声道的低通副本", post > 0.02,
          "post_peak=%g delay=%d" % (post, d))
    check("交叉馈电：直通侧按 1/(1+g) 收回（不靠整体加音量冒充效果）",
          abs(left - 0.9 / 1.6) < 1e-4, "left=%g expected=%g" % (left, 0.9 / 1.6))

    # 单声道稳态：L=R，交叉馈电不该让它变响（g=0.6·1.0，直通侧 1/(1+g)）。
    e._cf_zi = e._cf_hist = None
    mono = np.full((800, 2), 0.5, np.float32)
    mout = e._process_block(mono.copy())
    check("交叉馈电：单声道内容稳态幅度不变（不是音量旋钮）",
          abs(float(mout[-1, 0]) - 0.5) < 0.02 and abs(float(mout[-1, 1]) - 0.5) < 0.02,
          "L=%g R=%g" % (mout[-1, 0], mout[-1, 1]))

    # amount=0 是合法档：开关 on 但 DSP 分支不执行，state 里 effect=False。
    e.configure(speakers=True, speakers_amount=0)
    st = e.state().get("speakers") or {}
    check("强度 0：state 说 effect=False（开关 on 不等于真的在动样本）",
          st.get("enabled") is True and st.get("effect") is False,
          json.dumps(st, ensure_ascii=False))
    zero = e._process_block(mono.copy())
    check("强度 0：样本逐位直通", np.array_equal(zero, np.clip(mono, -1, 1).astype(np.float32)))
    e.configure(speakers_amount=5)
    st2 = e.state().get("speakers") or {}
    check("强度夹到 1：configure(5) 落在 1.0",
          abs(float(st2.get("amount", -1)) - 1.0) < 1e-9, json.dumps(st2, ensure_ascii=False))
    e.configure(speakers_amount=-1)
    st3 = e.state().get("speakers") or {}
    check("强度夹到 0：configure(-1) 落在 0.0",
          abs(float(st3.get("amount", -1))) < 1e-9, json.dumps(st3, ensure_ascii=False))

    # 证伪：把 _process_block 换成「只乘增益」的旧实现，上面那条右声道能量断言必须失去证据。
    e.configure(speakers=True, speakers_amount=1.0)
    e._cf_zi = e._cf_hist = None
    orig = audioplay.NativeEngine._process_block

    def bypass(self, arr):
        return np.clip(arr * self._gain, -1.0, 1.0).astype(np.float32)

    audioplay.NativeEngine._process_block = bypass
    try:
        bad = e._process_block(imp.copy())
    finally:
        audioplay.NativeEngine._process_block = orig
    check("变异(交叉馈电完全没接进 DSP)被『右声道有能量』抓到",
          float(np.max(np.abs(bad[:, 1]))) < 1e-7 and post > 0.02,
          "mut_peak=%g real_peak=%g" % (float(np.max(np.abs(bad[:, 1]))), post))

    # 清场：别把交叉馈电开着留给后面的 bit-perfect 测试（_arm 也会清，双保险）。
    e._speakers, e._speakers_amount = False, 0.35
    e._cf_zi = e._cf_hist = None


def test_ring_take():
    e = audioplay.get()
    with e._lock:
        e._chunks.clear(); e._frames_avail = 0
        e._chunks.append(np.ones((10, 2), np.float32) * 1)
        e._chunks.append(np.ones((10, 2), np.float32) * 2)
        e._chunks.append(np.ones((10, 2), np.float32) * 3)
        e._frames_avail = 30
        dst = np.zeros((25, 2), np.float32)
        n = e._take_into(dst, 25)
    check("环形取数取满请求帧", n == 25, "n=%s" % n)
    check("跨块顺序正确(1..1,2..2,3 部分)",
          dst[9, 0] == 1 and dst[10, 0] == 2 and dst[24, 0] == 3, str(dst[[9, 10, 24], 0]))
    with e._lock:
        rest = sum(c.shape[0] for c in e._chunks)
    check("半块消耗后剩余 5 帧留在队首", rest == 5, "rest=%s" % rest)


def test_mutation_is_caught():
    """把处理注坏，要求相应断言翻红；注坏却还全绿＝这条尺是瞎的（或变异本身是 NOP）。"""
    e = audioplay.get()
    p = first_real_audio_path()
    if not p:
        return
    bass = [9, 8, 6, 3, 0, -1, -2, -2, -1, 0]

    # 变异 A：把每段增益取负 —— bass 预设就该变成「削低频、抬高频」。
    # 为什么用整链取负而不是只翻 highshelf：只翻 shelf 对 4–16k 是 NOP（那一段的
    # 衰减主要来自 4k/8k/16k 三个 peaking，不是 shelf），尺会「注坏了还全绿」。
    orig = audioplay._rbj_section
    audioplay._rbj_section = lambda kind, db, f0, fs, q, shelf_s=1.0: orig(kind, -db, f0, fs, q, shelf_s)
    try:
        flat = e.preview(p, seconds=1.0, rate=48000, ch=2)
        neg = e.preview(p, seconds=1.0, rate=48000, ch=2, gains_db=bass)
        low_wrong = not (band(neg, 20, 120) > band(flat, 20, 120) * 1.5)   # 低频没被抬＝被抓
        hi_wrong = not (band(neg, 4000, 16000) < band(flat, 4000, 16000))  # 高频没收＝被抓
    finally:
        audioplay._rbj_section = orig
    check("变异(增益取负)被『抬低频』抓到", low_wrong)
    check("变异(增益取负)被『收高频』抓到", hi_wrong)

    # 变异 B：_take_into 注坏 —— 每轮无条件 popleft（哪怕只取半块），跨块取数会吞帧。
    # 单次 take(25) 触发不了（最后一块取半后 while 就退出，不 popleft）—— 那才是 NOP。
    # 用「连续两次 take(15)」才逼出差别：第一次取半块时注坏版会把那半块连同整块一起丢弃，
    # 于是第二次拿到的帧数/内容与正确版不同。
    def run(take_impl):
        dst = np.zeros((30, 2), np.float32)
        with e._lock:
            e._chunks.clear(); e._frames_avail = 0
            for v in (1, 2, 3):
                e._chunks.append(np.ones((10, 2), np.float32) * v)
            e._frames_avail = 30
            n1 = take_impl(e, dst[:15], 15)
            n2 = take_impl(e, dst[15:30], 15)
            left = sum(c.shape[0] for c in e._chunks)
        return n1 + n2, dst[:n1 + n2, 0].tolist(), left
    good_total, good_vals, good_left = run(audioplay.NativeEngine._take_into)
    def buggy(self, dst, need):
        got = 0
        while got < need and self._chunks:
            c = self._chunks[0]
            take = min(need - got, c.shape[0])
            dst[got:got + take] = c[:take]
            got += take
            self._chunks.popleft()          # BUG：半块也整块丢弃
            self._frames_avail -= take
        return got
    bad_total, bad_vals, bad_left = run(buggy)
    check("变异(吞半块)被『连续跨块取数』抓到",
          (good_total, good_vals, good_left) != (bad_total, bad_vals, bad_left),
          "good=%s/%s bad=%s/%s" % (good_total, good_left, bad_total, bad_left))


def _balanced_call(src: str, start: int) -> str:
    """从 start（`subprocess.` 处）起按括号配对取整段调用文本。"""
    i = src.find("(", start)
    if i < 0:
        return ""
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    return ""


def _spawn_calls(src: str):
    """源码里所有 subprocess.run/Popen 真调用（类型注解 `Optional[subprocess.Popen]` 不算）。"""
    return [_balanced_call(src, m.start())
            for m in re.finditer(r"subprocess\.(?:run|Popen)\s*\(", src)]


def test_hidden_ffmpeg_window():
    """ffmpeg 是控制台子系统程序；GUI 父进程直接 spawn 它 = 弹黑窗并抢前台。

    2026-10-22 02:2x 实发：播放/切歌时一扇 ffmpeg-win-x86_64-v7.1.exe 的黑窗冒出来，
    把 App 窗口的焦点顶掉，CDP 的 Input.dispatchMouseEvent 连续超时。查前台句柄才定位到它
    （不是 QtWebEngine，也不是测试脚本）。产品修法是两处 spawn 都带
    **_hidden_popen_kwargs()（CREATE_NO_WINDOW + SW_HIDE）。

    这里钉四件事：① 本机（Windows）返回的 kwargs 真含这两个字段，且真能被 subprocess
    接受（字段名拼错会在这里 TypeError，而不是上线后才发现）；② 源码里**每一处**
    subprocess 调用都带上了它 —— 今天三处（audioplay 2 + downmix 1），以后加第四处
    没带就红；③ audioplay 漏传会被点名；④ downmix 的测量线程漏传也会被点名
    （那条是后台跑的，漏了就是「播多声道歌时莫名闪一扇黑窗」）。
    """
    if os.name != "nt":
        print("  skip hidden-ffmpeg-window：非 Windows，没有控制台窗口这回事")
        return
    kw = audioplay._hidden_popen_kwargs()
    cnw = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    check("隐藏 spawn：带 CREATE_NO_WINDOW", bool(kw.get("creationflags", 0) & cnw),
          "creationflags=%r" % (kw.get("creationflags"),))
    si = kw.get("startupinfo")
    check("隐藏 spawn：STARTUPINFO 指定 SW_HIDE",
          si is not None and bool(si.dwFlags & subprocess.STARTF_USESHOWWINDOW)
          and si.wShowWindow == subprocess.SW_HIDE,
          "dwFlags=%r wShowWindow=%r" % (getattr(si, "dwFlags", None),
                                          getattr(si, "wShowWindow", None)))

    files = ((ROOT / "sp" / "audioplay.py", 2), (ROOT / "sp" / "downmix.py", 1))
    srcs = {}
    all_calls = []
    for path, expect in files:
        src_i = path.read_text("utf-8")
        srcs[path.name] = src_i
        calls_i = _spawn_calls(src_i)
        all_calls += calls_i
        check("%s 里仍是 %d 处 spawn" % (path.name, expect), len(calls_i) == expect,
              "找到 %d 处：%r" % (len(calls_i), [c[:48] for c in calls_i]))
        missing_i = [c for c in calls_i if "hidden_popen_kwargs()" not in c]
        check("%s 每一处 spawn 都带 hidden kwargs" % path.name,
              bool(calls_i) and not missing_i,
              "漏了 %d 处：%r" % (len(missing_i), [c[:90] for c in missing_i]))

    # 实测一次真实 spawn：kwargs 拼错（比如 startupinfo 拼成 si）会在这里 TypeError。
    try:
        p = subprocess.run([sys.executable, "-c", "import sys; sys.exit(3)"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
        check("隐藏 kwargs 是可用的真实参数", p.returncode == 3, "rc=%r" % p.returncode)
    except BaseException as e:
        check("隐藏 kwargs 是可用的真实参数", False, "%s: %s" % (type(e).__name__, e))

    # 证伪 1：把函数注回「旧行为」（什么都不传）—— 上面两条字段断言必须翻红。
    orig = audioplay._hidden_popen_kwargs
    audioplay._hidden_popen_kwargs = lambda: {}
    try:
        bad = audioplay._hidden_popen_kwargs()
        caught = not (bad.get("creationflags", 0) & cnw) and bad.get("startupinfo") is None
    finally:
        audioplay._hidden_popen_kwargs = orig
    check("变异(不隐藏窗口)被『两个字段』抓到", caught)

    # 证伪 2：从源码里抹掉一处 **kwargs —— 扫描器必须点名那一处。抹不掉（锚点缺失）
    # 记 FAIL 而不是 skip：那说明这道闸自己失去了变异的对象。
    for fname, needle, expect_total in (
            ("audioplay.py", "**_hidden_popen_kwargs()", 2),
            ("downmix.py", "**hidden_popen_kwargs()", 1)):
        src = srcs[fname]
        mutated = src.replace(needle, "**{}", 1)
        check("变异(%s 漏传 kwargs)确实落盘" % fname,
              needle in src and mutated != src,
              "锚点出现 %d 次" % src.count(needle))
        if mutated == src:
            continue
        bad_calls = _spawn_calls(mutated)
        bad_missing = [c for c in bad_calls if "hidden_popen_kwargs()" not in c]
        check("变异(%s 漏传 kwargs)被『每一处都带』抓到" % fname,
              len(bad_calls) == expect_total and len(bad_missing) == 1,
              "注坏后有 %d/%d 处没带" % (len(bad_missing), len(bad_calls)))


def test_devices_default_flag():
    """「哪台是默认设备」必须问 WASAPI 那个 host API，不能拿全局索引去比 WASAPI 的编号。

    量法要独立于被测实现，否则是循环参照：这里用**两次不同编号空间的查询**对撞 ——
      A) 全局默认输出（`get_default_output_device_info`，本机给的是 DirectSound 侧的 index）
      B) devices() 里标了 is_default 的那台（WASAPI 侧的 index）
    两者 index 不同、但必须指向**同一块设备**（按 name 比）。旧写法拿 A 的 index 去比 B 的
    列表，结果是「六台里一台都不默认」，于是 audio_device=None 时选到 devs[0]（枚举顺序），
    而界面上写的是「跟随系统默认」—— 说明和实现对不上。
    """
    import pyaudiowpatch as pa
    e = audioplay.get()
    items = e.devices()
    if not items:
        print("  skip devices() 为空（这台机器没有 WASAPI 输出设备），本条不作数")
        return
    p = pa.PyAudio()
    try:
        gname = p.get_default_output_device_info()["name"]
        widx = None
        for h in range(p.get_host_api_count()):
            info = p.get_host_api_info_by_index(h)
            if "WASAPI" in info["name"]:
                widx = h
                break
        wasapi_default = p.get_host_api_info_by_index(widx)["defaultOutputDevice"] if widx is not None else None
    finally:
        p.terminate()
    flagged = [d for d in items if d.get("is_default")]
    check("默认设备标记恰好一台", len(flagged) == 1,
          "标了 %d 台 / 共 %d 台：%s" % (len(flagged), len(items),
                                      [(d["index"], d["name"]) for d in items]))
    if flagged:
        # 独立 oracle：全局查询给的「名字」，必须等于 WASAPI 侧被标默认那台的名字
        check("被标默认那台 == 系统默认输出（跨编号空间按名字对撞）",
              flagged[0]["name"] == gname, "wasapi=%s(%s) global=%s" % (flagged[0]["name"], flagged[0]["index"], gname))
    if isinstance(wasapi_default, int) and wasapi_default >= 0:
        hit = [d for d in items if d["index"] == wasapi_default]
        check("host API 的 defaultOutputDevice 落在列表里", bool(hit),
              "default=%s items=%s" % (wasapi_default, [d["index"] for d in items]))
        # 变异：把 is_default 全标 False（旧写法的后果），断言必须翻红
        old = audioplay.NativeEngine.devices
        audioplay.NativeEngine.devices = lambda self: [dict(d, is_default=False) for d in old(self)]
        try:
            mutated = [d for d in audioplay.get().devices() if d.get("is_default")]
        finally:
            audioplay.NativeEngine.devices = old
        check("变异(不标默认)被『恰好一台』抓到", len(mutated) != 1,
              "注坏后标了 %d 台（该为 0，证明上面那条有牙）" % len(mutated))
        # 变异二：标了「恰好一台」但标错了一台（枚举顺序第一台 ≠ 默认设备）。
        # 这条专治『恰好一台』的盲区：只数数量会被蒙过去，按名字对撞才会红。
        # 挑「不是默认那台」来标，否则在这块声卡只有一台的机器上变异是 NOP（注坏了也测不出差别）。
        others = [d["index"] for d in items if d["name"] != gname]
        if not others:
            print("  skip 变异二：列表里每台名字都等于系统默认，注不出「标错设备」这种坏样本")
        else:
            pick = others[0]

            def wrong_default(self):
                return [dict(d, is_default=(d["index"] == pick)) for d in old(self)]
            audioplay.NativeEngine.devices = wrong_default
            try:
                bad = [d for d in audioplay.get().devices() if d.get("is_default")]
            finally:
                audioplay.NativeEngine.devices = old
            check("变异(标错设备)被『按名字对撞』抓到",
                  len(bad) == 1 and bad[0]["name"] != gname,
                  "注坏后标的是 %s（和系统默认 %s 不是同一台才算抓到）" % (bad[0]["name"] if bad else None, gname))


def _reset_pa_enum():
    """把 audioplay._PA_ENUM 打回冷态（在 pa-ctrl 线程上 terminate 再置 None）。

    为什么必须冷：会崩的那段是**并发 pa_init**（2×2 里「每次新建 + 无锁」那一格），
    不是并发读设备表。上一版这条自检先 `base = e.devices()` 把缓存预热了，再开 8 个线程
    —— 8 个线程进锁以后看到的都是现成实例，抢的只是只读查询，那段窗口根本没被踩到。
    结果就是把 devices() 的锁整块摘掉，8 线程 × 3 轮连跑三趟仍然全绿：尺是瞎的。
    当场量出来的对照（本机，同一份探针只换树路径）：
        在位的锁  3/3 趟活下来；摘掉 devices() 那把锁  3/3 趟 rc=139（0xC0000005 访问违例）
    —— 加了这个冷启动，摘锁就真的跑不完这套自检了。

    2026-10-25 起「锁」换成了「唯一一根 pa-ctrl 线程」：实例的 terminate 也必须在
    同一根线程上做（线程黏性，见 test_pa_calls_pinned_to_ctrl_thread），所以这里走 _pa_call。
    """
    def cold():
        p = audioplay._PA_ENUM
        audioplay._PA_ENUM = None
        if p is not None:
            try:
                p.terminate()   # 只在 pa-ctrl 上、非并发地销毁，免得攒一堆没 terminate 的实例
            except BaseException:
                pass
    try:
        audioplay._pa_call(cold)
    except BaseException:
        pass


def test_pa_calls_pinned_to_ctrl_thread():
    """所有 Pa_* 调用必须落在同一根 pa-ctrl 线程上 —— 「换歌没声」的根因尺。

    判别实验（tools/_diag_reopen.py，2026-10-25，本机 EDIFIER N500 / 96 kHz）：
        A 只 play/stop、从不枚举设备              → 3/3 次 play 全成
        B 同一线程「枚举+play」、换个线程 stop     → 第 1 次成，之后每次 [Errno -9999]
        C 线程甲只枚举一次，线程乙 play           → **第一次**就 -9999
        D 枚举后把常驻实例 terminate，新线程 play  → 又成了
    原因：Pa_Initialize 给 WASAPI 建的 COM 环境有线程黏性，而服务端每个请求一根线程、
    请求完就死。这条**不真开流**（不开设备、不出声），只钉纪律：devices() 里建 PyAudio
    必须发生在 pa-ctrl 上。谁把它挪回请求线程，这条当场红。
    """
    real = audioplay._pa
    if real is None:
        print("  skip（没有 pyaudiowpatch）")
        return
    made = []
    orig = real.PyAudio

    class Spy:
        def __init__(self, *a, **k):
            made.append(threading.current_thread().name)
            self._impl = orig(*a, **k)

        def __getattr__(self, nm):
            return getattr(self._impl, nm)

    real.PyAudio = Spy
    try:
        _reset_pa_enum()
        box = {}
        th = threading.Thread(target=lambda: box.setdefault("rows", audioplay.get().devices()),
                              name="http-like")
        th.start(); th.join(30)
        check("devices() 里建 PyAudio 落在 pa-ctrl 线程",
              bool(made) and all(n == "pa-ctrl" for n in made),
              "建实例的线程=%s（这条要能抓到旧形状：旧版是 http-like）" % (made,))
        # 反向哨兵：请求线程本身不叫 pa-ctrl —— 否则上面那条可以恒绿。
        check("调用 devices() 的线程不是 pa-ctrl（反向哨兵）", th.name != "pa-ctrl",
              "请求线程=%s" % th.name)
    finally:
        real.PyAudio = orig
        _reset_pa_enum()


def test_devices_concurrent():
    """并发枚举设备不许把进程撬崩 —— 这条的「红法」和别的断言不一样，先说清楚。

    2×2 判别实验（tools/probe_pa_concurrency.py，本机 8 线程 × 3 轮 × 4 轮/格）：
        无锁 → 4/4 轮 0xC0000005 访问违例（进程当场没了）；有锁 → 0/4 崩。
    所以这里的压力形状照抄那把尺（8 线程 × 3 轮），而不是随手写 4 个线程。
    Python 的 try/except 抓不住段错误，所以这条翻红有两种形状：
      A) 整套自检跑到这里就没有 PASS 行、退出码 3221225477（bash 里显示成 139）—— 最响的红；
      B) 活下来了但某一趟拿到空/残缺列表或抛异常 —— 常规红。
    2026-10-25 起这把「锁」由唯一一根 pa-ctrl 线程承担（`_pa_call`）：并发调用被串行化，
    而且 Pa_Initialize/开流/关流全在同一根活线程上（线程黏性，见
    test_pa_calls_pinned_to_ctrl_thread）。这条压力尺仍然有牙：谁把 devices() 改成绕开
    _pa_call 直连 PortAudio，8 线程就会回到上面那格「无锁」形状。
    """
    import threading
    e = audioplay.get()
    base = e.devices()
    if not base:
        print("  skip 并发枚举：这台机器没有 WASAPI 设备可枚举")
        return
    base_idx = sorted(d["index"] for d in base)
    results = []
    errors = []

    def worker():
        try:
            results.append(sorted(d["index"] for d in e.devices()))
        except BaseException as ex:
            errors.append(repr(ex))

    for rnd in range(3):
        _reset_pa_enum()          # 每趟都冷：让 8 个线程真的同时撞「建实例」那一段
        ts = [threading.Thread(target=worker) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)
    check("并发枚举没抛异常", not errors, "; ".join(errors[:3]))
    check("并发枚举每趟都拿到同一张表", len(results) == 24 and all(r == base_idx for r in results),
          "趟数=%d 不一致的有 %d 趟" % (len(results), sum(1 for r in results if r != base_idx)))
    # 默认标记在并发下也要只有一台（缓存实例被并发写坏时最容易丢的就是这个）
    flags = []

    def flag_worker():
        flags.append(sum(1 for d in e.devices() if d.get("is_default")))

    _reset_pa_enum()          # 同样先冷下来，否则这一趟又变成只读查询的自比
    ts = [threading.Thread(target=flag_worker) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=20)
    check("并发下默认设备仍恰好一台", flags and all(f == 1 for f in flags), "各趟标了 %s 台" % flags)


def test_callback_sim():
    """直接调设备回调本体（PortAudio 线程跑的就是这个函数），用真解码数据喂它，
    全程不开设备、不出声，但验的是 play 路径最易错的那段：取数→DSP→频谱→consumed
    →不足算 underrun→ended 时返回 paComplete。"""
    e = audioplay.get()
    p = first_real_audio_path()
    if not p:
        return
    import pyaudiowpatch as pa
    # 用真 PCM 灌满环形；模拟共享流 48k 立体声
    frames = np.frombuffer(e._decode(p, ss=0.0, seconds=0.5, rate=48000, ch=2, speed=1.0),
                           dtype="<f4").reshape(-1, 2)
    e.stop()
    with e._lock:
        e._ch, e._rate = 2, 48000
        e._paused, e._ended = False, False
        e._chunks.clear(); e._frames_avail = 0
        for i in range(0, frames.shape[0], 480):
            e._chunks.append(frames[i:i + 480].copy())
            e._frames_avail += min(480, frames.shape[0] - i)
        e._consumed = 0
        e._gain = 0.7; e._sos = None; e._sos_state = None
        e._loudness = False; e._balance = 0.0; e._fade_in_frames = 0
        e._underruns = 0
        # 这条测的是「数据没供上」而不是「文件读完」：必须自己把 _eof 摆成 False。
        # 单例上一条测试留下的 _eof=True 会让 partial 走「正常收尾」分支（underruns 不涨），
        # 于是这条断言红得莫名其妙 —— 漏摆一个字段，同一把尺就量出另一个答案。
        e._eof = False
        e._lim_on, e._lim, e._downmix_trim = False, None, 1.0
    total = frames.shape[0]
    got = 0
    conts = 0
    for _ in range(30):
        data, code = e._callback(None, 1024, None, 0)
        assert len(data) == 1024 * 2 * 4, "回调返回字节数 ≠ 请求帧×声道×4"
        if code == pa.paContinue:
            conts += 1
        got += 1024
        if got >= total + 1024:
            break
    with e._lock:
        consumed = e._consumed
        underruns = e._underruns
        spec_energy = float(np.max(np.abs(e._spec)))
    check("回调按请求帧吐数(字节对得上)", got >= total)
    check("consumed 随取数推进", consumed >= total - 1024,
          "consumed=%s fed=%s" % (consumed, total))
    check("取空后计 underrun(数据不足如实记)", underruns >= 1, "underruns=%s" % underruns)
    check("DSP 后的数据进了频谱缓冲(非全零)", spec_energy > 0.01, "spec=%s" % spec_energy)
    # ended 后必须回 paComplete
    with e._lock:
        e._ended = True
    _data, code = e._callback(None, 1024, None, 0)
    check("ended 时回调返回 paComplete", code == pa.paComplete, "code=%s" % code)
    e.stop()


def test_end_of_track_self_closes():
    """曲子放完由回调自己收口，不等有人 GET state()。

    坏样本是真机量来的，不是编的：57922 那轮里一首歌唱完到下一次有人查状态之间，
    underruns 从 2 涨到 **7629**（面板那行「累计丢帧 N 次」就是它），而那段时间真实丢帧
    只有起播的 2 次。机制在代码里：旧版只在 state() 里结算 ended，没人查的时候回调仍在
    按 ~94 拍/秒空转，每拍 filled<frames 都记一次「丢帧」，设备流也一直占着。
    这条尺不开设备、也不调 state()，所以它量的正是「没人在看的时候引擎自己收不收口」。
    同一份 fixture 把自收口摘掉注坏跑一遍，实测留在 underruns=26、ended=False（下面那条
    变异断言要求的就是这个形状）—— 健康版应当 ≤3 且 ended=True。
    """
    e = audioplay.get()
    p = first_real_audio_path()
    if not p:
        return
    import pyaudiowpatch as pa
    frames = np.frombuffer(e._decode(p, ss=0.0, seconds=0.3, rate=48000, ch=2, speed=1.0),
                           dtype="<f4").reshape(-1, 2)
    e.stop()
    with e._lock:
        e._ch, e._rate = 2, 48000
        e._paused, e._ended = False, False
        e._eof = True                      # 当作解码已经到头
        e._chunks.clear(); e._frames_avail = 0
        for i in range(0, frames.shape[0], 480):
            e._chunks.append(frames[i:i + 480].copy())
            e._frames_avail += min(480, frames.shape[0] - i)
        e._consumed = 0
        e._gain = 0.7; e._sos = None; e._sos_state = None
        e._loudness = False; e._balance = 0.0; e._fade_in_frames = 0
        e._underruns = 0
    total = frames.shape[0]
    codes = []
    for _ in range(40):                    # 40×1024 帧 > 0.3s 的 14400 帧，够抽干
        _d, code = e._callback(None, 1024, None, 0)
        codes.append(code)
        if code == pa.paComplete:
            break
    with e._lock:
        ended, und = e._ended, e._underruns
    check("没查 state() 也能自己收口（ended=True 且回调回 paComplete）",
          ended is True and codes[-1] == pa.paComplete,
          "fed=%d 跑了%d拍 ended=%s code=%s" % (total, len(codes), ended, codes[-1]))
    check("收尾空转不再虚记丢帧（≤3 次）", und <= 3, "underruns=%s" % und)
    base = und
    for _ in range(20):                    # 已经 ended，再空转 20 拍必须一动不动
        e._callback(None, 1024, None, 0)
    with e._lock:
        und2 = e._underruns
    check("ended 之后再空转 20 拍，丢帧计数一动不动", und2 == base, "%s → %s" % (base, und2))

    # 变异证伪：把自收口摘掉（回到旧形状 —— 只有 state() 才结算 ended），上面两条必须翻红。
    # 注坏版顺手把 DSP 那两步省了：这条尺量的是记账与收口，不是采样值。
    def old_callback(self, indata, frames, time_info, status):
        with self._lock:
            out = np.zeros((frames, self._ch), np.float32)
            filled = 0
            if not self._paused and not self._ended:
                filled = self._take_into(out, frames)
                if filled < frames:
                    self._underruns += 1
                if filled:
                    self._consumed += filled
            return (out.tobytes(), pa.paContinue if not self._ended else pa.paComplete)

    real_callback = e._callback
    e._callback = lambda *a: old_callback(e, *a)
    try:
        with e._lock:
            e._paused, e._ended, e._eof = False, False, True
            e._chunks.clear(); e._frames_avail = 0
            for i in range(0, frames.shape[0], 480):
                e._chunks.append(frames[i:i + 480].copy())
                e._frames_avail += min(480, frames.shape[0] - i)
            e._consumed = 0
            e._underruns = 0
        ocodes = []
        for _ in range(40):
            _d, code = e._callback(None, 1024, None, 0)
            ocodes.append(code)
            if code == pa.paComplete:
                break
        with e._lock:
            oended, ound = e._ended, e._underruns
    finally:
        e._callback = real_callback
    check("变异(摘掉自收口)被『自己收口』抓到",
          oended is False and ound > 3,
          "注坏后 ended=%s underruns=%s 跑了%d拍（健康版：%d 次）" % (oended, ound, len(ocodes), und))
    e.stop()


def test_position_source_time():
    """进度把「喂进设备的输出帧」折算成「源时间」：变速时两者差一个 speed 系数。
    不乘它，2× 下一首歌的进度条只按真实的一半爬，终点(源 duration)永远到不了。
    全程只设字段＋读 position()，不开设备。

    注意：position()/state() 各自 `with self._lock`（非重入锁）—— 设值要单独加锁后放开，
    再在锁外调它们；在持锁时调用会当场死锁（这条测试第一版就是这么挂死的）。

    2026-10-14 起，折算系数取的是 **_atempo（耳朵里的倍速＝atempo 因子串乘积）**，
    不再直接取 _speed（界面标的那个数）。play() 两个一起设，所以这里也必须两个一起设——
    只设 _speed 会读到 1×，那不是产品错，是这份测试自己绕过了 play() 的装配。
    """
    e = audioplay.get()
    e.stop()

    def at_speed(v):
        """照 play() 的装配把请求值和实际合成值一起钉上。"""
        with e._lock:
            e._speed = v
            e._atempo = audioplay.atempo_product(audioplay.atempo_chain(v)) or 1.0
    with e._lock:
        e._base_pos, e._rate = 10.0, 48000
        e._consumed = 48000        # 1 秒输出 = 2 秒源内容
        # 这条尺量的是「输出秒 × atempo」，不是共享档的限幅器延迟。单例上一条测试若
        # 把 _lim_on 留成 True，这里会被扣掉 D 帧（96k 下 5 ms）而红在第四位小数。
        e._lim_on, e._lim, e._downmix_trim = False, None, 1.0
    at_speed(2.0)
    pos = e.position()
    check("变速下进度按源时间推进(1+2 秒)", abs(pos - 12.0) < 1e-6, "pos=%s 期望 12.0" % pos)
    at_speed(1.0)
    pos1 = e.position()
    check("1× 下进度=输出秒", abs(pos1 - 11.0) < 1e-6, "pos=%s 期望 11.0" % pos1)
    st = e.state()
    check("state() 用同一把源时间尺", abs(st["position"] - 11.0) < 1e-6,
          "state.position=%s" % st["position"])
    check("state() 把 speed 一起报出来", st.get("speed") == 1.0, "speed=%s" % st.get("speed"))
    check("state() 把 atempo（耳朵那份）也报出来", st.get("atempo") == 1.0,
          "atempo=%s" % st.get("atempo"))
    # 4× 这一档必须跟着走：单节夹取时它是 2.0，进度和耳朵就差一倍（本文件开头那条缺陷）
    at_speed(4.0)
    pos4 = e.position()
    check("4× 下进度按 4 倍源时间推进（不是被夹成的 2 倍）", abs(pos4 - 14.0) < 1e-6,
          "pos=%s 期望 14.0；atempo=%s" % (pos4, e._atempo))
    # 变异证伪：把系数摘掉（退回旧公式 base+consumed/rate），2× 那条必须翻红。
    orig = e._pos_src_locked
    e._pos_src_locked = lambda: e._base_pos + e._consumed / max(1, e._rate)
    at_speed(2.0)
    mutated = e.position()          # position() 自己加锁，_pos_src_locked 不再嵌套加锁
    e._pos_src_locked = orig
    check("变异(不乘 speed)被『源时间推进』抓到", abs(mutated - 11.0) < 1e-6,
          "注坏后=%s（该落在 11 而非 12，证明断言 12 有牙）" % mutated)
    at_speed(1.0)          # 这是进程内单例，跑完复位，别把 2× 留给后面的测试


def _loaded_engine():
    """一台「已经载着曲子、正在播」的引擎，但不碰设备、不起真 ffmpeg。

    做法：实例属性遮蔽 _reader_loop（seek/play 起的那根线程于是只登记代际号、不解码），
    字段照 play() 的成功分支置好。这里只演状态机，而 seek 事故整个就长在状态机上。
    """
    e = audioplay.NativeEngine()
    log = []

    def fake_reader(path, ss, rate, ch, speed, gen):
        log.append(gen)
        e._stop_evt.wait(0.4)          # 像真线程那样活着一下，然后自然收尾

    e._reader_loop = fake_reader
    e._path, e._rate, e._ch, e._duration = "dummy.flac", 48000, 2, 100.0
    e._paused, e._ended, e._eof = False, False, False
    e._base_pos = 0.0
    return e, log


def test_seek_eof_generation():
    """离线复现 2026-10-12 真机（一次性服务器 57922）量到的事故：拖一次进度条＝本曲播完。

    机制是读代码读出来的，不是猜的：seek() terminate 旧 ffmpeg、清空环形队列、再起一根读
    线程；**旧线程**在 finally 里补的那记 _mark_eof() 落在新一段的空队列上，而 state() 的
    收尾判据是 `_eof 且 _frames_avail==0` ⇒ seek 响应自己那一次 state() 就报 ended=True、
    playing=False，进度冻在目标秒不再走（实测：seek→120 之后连 4 秒 position 恒 120.0、
    underruns 不再增长，说明回调根本没在取数），前端下一拍按「播完」处理 → 直接跳下一曲。
    """
    e, log = _loaded_engine()
    g_old = e._gen
    e.seek(30.0)
    check("seek 起了新读线程并带来新一代",
          len(log) == 1 and log[0] == e._gen == g_old + 1, "log=%s gen=%s" % (log, e._gen))
    e._mark_eof(g_old)                 # 事故现场：上一代线程迟到的那记 EOF
    st = e.state()
    check("旧线程迟到的 EOF 不把 seek 后的引擎判成播完",
          st["ended"] is False and st["playing"] is True,
          "ended=%s playing=%s eof=%s" % (st["ended"], st["playing"], e._eof))
    # 牙不能只朝一边：把真 EOF 也挡了的话，「播完」这条边沿就永远不触发、不会自动跳下一曲
    e._mark_eof(e._gen)
    st2 = e.state()
    check("当代线程的真 EOF 仍能把播完判出来",
          st2["ended"] is True and st2["playing"] is False,
          "ended=%s playing=%s" % (st2["ended"], st2["playing"]))
    n = len(log)
    g2 = e._gen
    e.seek(40.0)                       # 已播完之后再拖
    check("播完后的 seek 不再另起解码线程",
          len(log) == n and e._gen == g2, "log=%d→%d gen=%d→%d" % (n, len(log), g2, e._gen))
    e._stop_evt.set()                  # 让假线程立刻收摊，别留到下一段测试里

    # 变异证伪：把代际门摘掉（恢复成「谁跑到 finally 谁说话」），上面那条必须翻红。
    m, mlog = _loaded_engine()
    g0 = m._gen
    m.seek(30.0)
    m._mark_eof = lambda gen: setattr(m, "_eof", True)   # 注坏：无视代际
    m._mark_eof(g0)
    sm = m.state()
    check("变异(摘掉代际门)被『迟到的 EOF』抓到", sm["ended"] is True,
          "注坏后 ended=%s —— 若仍为 False 说明这条尺压根没量到代际" % sm["ended"])


def test_seek_join_window_eof():
    """2026-11-11 真机事故的离线复现：EOF 落在 seek 的 join 窗口里，被当代际接受。

    上一支测试钉的是「代际自增**之后**迟到的 EOF」；但这起事故的时序整个发生在自增
    **之前**。真机迹线（tools/_repro_seek_latch.trace.txt，12 次 seek 命中 3 次）：

        mark-eof-ACCEPT gen=8 avail=0 paused=False ended=False   ← join(1.0) 窗口内
        state-eof->ended consumed=0 base=80.000 avail=0          ← 2ms 轮询抢到 _lock
        seek-restarted pos=80.000 gen=9                          ← 16 ms 之后才进第二锁块

    旧写法把 `_gen += 1` / `_eof = False` 留在 join 之后的第二锁块，窗口里代际没变，
    旧读线程 finally 补的 EOF 被接受，而环形刚被清空 ⇒ 任何 state()（或独占泵）都满足
    `_eof 且队列空`，把 `_ended` 永久闩真；前端下一拍看到 ended 边沿 → 自动跳下一曲。
    seek 响应自己就带着 ended=True（真机 trial3/8/9 逐字段同形）。

    这里用一根「在 join 里补 EOF、并像轮询线程那样读一次 state」的假旧线程，把那个
    16ms 窗口钉成确定性时序 —— 不靠真线程赛跑。
    """
    e, log = _loaded_engine()

    class _OldReader:
        def __init__(self, engine, gen):
            self._e = engine
            self._gen = gen
            self._alive = True
            self.polled = None

        def is_alive(self):
            return self._alive

        def join(self, timeout=None):
            # 旧读线程死前的 finally：先补 EOF（此时代际还没自增 ⇒ 旧代码会 ACCEPT），
            # 然后外部轮询线程恰好在这个窗口里读了一次 state()。
            self._e._mark_eof(self._gen)
            self.polled = self._e.state()
            self._alive = False

    old = _OldReader(e, e._gen)
    e._reader = old
    e.seek(30.0)

    check("join 窗口里旧线程补的 EOF 不许让 state() 判成播完",
          old.polled is not None and old.polled["ended"] is False
          and old.polled["playing"] is True,
          "窗口内 state ended=%s playing=%s"
          % (None if old.polled is None else old.polled["ended"],
             None if old.polled is None else old.polled["playing"]))
    st = e.state()
    check("seek 返回后引擎仍是播放态（闩没被闩上）",
          st["ended"] is False and st["playing"] is True,
          "seek 后 ended=%s playing=%s eof=%s" % (st["ended"], st["playing"], e._eof))
    t0 = time.time()
    while len(log) < 1 and time.time() - t0 < 0.5:
        time.sleep(0.01)
    check("seek 仍然起了新一代读线程",
          len(log) == 1 and e._gen == old._gen + 1,
          "log=%s gen=%s old_gen=%s" % (log, e._gen, old._gen))
    e._stop_evt.set()                  # 让假线程立刻收摊，别留到下一段测试里

    # 变异证伪：把旧次序演回来（join 期不自增代际、窗口里读 state），这条尺必须翻红。
    # 旧次序的实质就是「清环 → 旧线程补 EOF → 轮询抢到 state → 才自增/清 _eof」。
    m, _ = _loaded_engine()
    with m._lock:
        m._chunks.clear(); m._frames_avail = 0
        m._stop_evt.set()
    m._mark_eof(m._gen)                # join 期：代际未变 ⇒ ACCEPT（旧代码正是如此）
    sm = m.state()                     # 轮询抢在第二锁块之前
    check("变异(代际自增留在 join 之后)被『窗口内 state()』抓到", sm["ended"] is True,
          "旧次序下 ended=%s —— 若为 False 说明这条尺量不到 join 窗口" % sm["ended"])


def test_feed_generation_gate():
    """离线复现 2026-11-08 真机（用户报「《配合》一直有回声和卡顿」）：两次并发 play
    各起一根解码线程，两条线程把同一首歌的两份 50ms 块交错喂进**同一个**环形 deque ⇒
    每一段音频被播两遍。现场证据：同一后端进程下两条 ffmpeg，-ss 完全相同、创建在同一秒，
    而应用内 underruns=0、bit-perfect=True（样本没被动过，只是被重复了）。

    这道门是第二道牙（第一道是 _mark_eof 的代际门）：旧代际线程的块一律拒收并让它退场，
    覆盖「进门前」和「卡在背压 wait 里醒来后」两条路。全程纯内存，不开设备、不起 ffmpeg。
    """
    e = audioplay.NativeEngine()
    e._gen = 7
    x = np.zeros((480, 2), np.float32)
    check("当代线程的块进环形",
          e._feed(x.copy(), 7) is True and e._frames_avail == 480,
          "avail=%s" % e._frames_avail)
    check("旧代际线程的块被拒收且要求退场",
          e._feed(x.copy(), 6) is False and e._frames_avail == 480,
          "avail=%s（旧块不该进）" % e._frames_avail)

    # 背压里卡住的线程：进 wait 时还是当代，醒来时代际已被新 play 换掉 ⇒ 也必须退场
    e._max_frames = 480                  # 环已满，下一块只能等
    res = {}

    def waiter():
        res["ok"] = e._feed(x.copy(), 7)

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    time.sleep(0.05)                     # 让它真的进到 cond.wait 里
    e._gen = 8                           # 新 play/seek 接手
    t.join(timeout=1.5)
    check("背压里醒来的过期线程也退场（False）",
          res.get("ok") is False and not t.is_alive(), "res=%s" % res)
    check("过期线程没有往环形塞第二份", e._frames_avail == 480,
          "avail=%s" % e._frames_avail)

    # 变异证伪：把代际门摘掉（恢复成「谁来都收」），旧块必须真的进环形——
    # 否则上面那条「拒收」断言就是没量到东西的装饰。
    m = audioplay.NativeEngine()
    m._gen = 7

    def ungated(chunk, gen):
        with m._cond:
            m._chunks.append(chunk)
            m._frames_avail += chunk.shape[0]
        return True

    m._feed = ungated
    m._feed(x.copy(), 6)
    check("变异(摘掉代际门)后旧线程的块真的进了环形", m._frames_avail == 480,
          "注坏后 avail=%s —— 若仍为 0 说明『拒收』那条尺是瞎的" % m._frames_avail)


def test_seek_preserves_pause():
    """暂停中拖进度条＝只挪指针，不许把歌放出来。

    对的是 chromium 档的既有语义：`audio.currentTime = x` 在暂停时仍保持暂停。原生档原先在
    seek() 里写死 `self._paused = False`，等于「拖一下条就出声」，两档答复不一样（2026-10-13
    前端接线复验时对代码查出来）。牙朝两边都长：把播着的一首按停也是错的。
    """
    e, _log = _loaded_engine()
    e._paused = True
    e.seek(30.0)
    st = e.state()
    check("暂停中 seek 后仍是暂停（拖进度条不出声）",
          e._paused is True and st["playing"] is False,
          "paused=%s playing=%s" % (e._paused, st["playing"]))
    check("暂停中 seek 的进度就是拖到的那一秒",
          abs(float(st["position"]) - 30.0) < 0.01, "pos=%s" % st["position"])
    e._paused = False
    e.seek(55.0)                       # 播着的时候拖：不许反过来把它按停
    st2 = e.state()
    check("播放中 seek 后仍在播（seek 也不许顺手按停）",
          st2["playing"] is True and abs(float(st2["position"]) - 55.0) < 0.01,
          "playing=%s pos=%s" % (st2["playing"], st2["position"]))
    e._stop_evt.set()

    # 变异：把旧那句写死的 `self._paused = False` 原样贴回 seek 之后 —— 第一条尺必须翻红，
    # 否则这条尺量的就不是「保留暂停态」这件事。
    m, _mlog = _loaded_engine()
    m._paused = True
    real_seek = m.seek

    def buggy(pos, _r=real_seek, _m=m):
        _r(pos)
        _m._paused = False                  # ← 事故前的那一行
    m.seek = buggy
    m.seek(30.0)
    sm = m.state()
    check("变异(seek 写回 _paused=False)被『暂停中拖条不出声』抓到",
          (m._paused is True and sm["playing"] is False) is False,
          "注坏后 paused=%s playing=%s —— 没翻红说明这条尺是空的" % (m._paused, sm["playing"]))
    m._stop_evt.set()


def test_eq_echo():
    """state().eq：把「设置里的十段 == 引擎里正挂着的十段」变成可离线量的读数。

    为什么引擎要自己报这份：原生档的均衡器在服务端 numpy 上算，前端 WebAudio 的 _eqLive
    永远是 false ⇒ 面板那句「链路会在第一次播放时接上」就成了说不完的假话，屏上闸也没有
    native 侧的等价物可断。这份回显只读不写，不参与算声。
    """
    e = audioplay.NativeEngine()
    gains = [5.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    # applied 与 active 是两件事：刚起的引擎还没配过曲线，这时「十条增益都是 0」
    # 是替服务端猜原因（设置里可能明明有曲线），前端那句只能停在「还没同步」。
    eq_never = (e.state() or {}).get("eq") or {}
    check("没配过时 applied=False（未配置 ≠ 曲线全 0）",
          eq_never.get("applied") is False and eq_never.get("active") is False,
          json.dumps(eq_never, ensure_ascii=False))
    e.configure(volume=0.5, gains_db=gains, rate=48000)
    eq = (e.state() or {}).get("eq") or {}
    check("state().eq 把挂上的二阶节数报出来", eq.get("active") is True and eq.get("bands") == 2,
          json.dumps(eq, ensure_ascii=False))
    check("state().eq 的增益就是等长十段的那一份", eq.get("gains_db") == gains,
          str(eq.get("gains_db")))
    check("configure 收下曲线后 applied=True", eq.get("applied") is True,
          json.dumps(eq, ensure_ascii=False))
    # 均衡器关掉＝全 0 曲线 ⇒ _sos_eq 返回 None ⇒ active 自己落回 False（不需要额外开关位）
    e.configure(gains_db=[0.0] * 10, rate=48000)
    eq0 = (e.state() or {}).get("eq") or {}
    check("全 0 曲线时 eq.active 落回 False", eq0.get("active") is False and eq0.get("bands") == 0,
          json.dumps(eq0, ensure_ascii=False))
    check("全 0 曲线时回显仍是十段（不是空表）", eq0.get("gains_db") == [0.0] * 10,
          str(eq0.get("gains_db")))
    check("全 0 曲线被 configure 收过 ⇒ applied 仍是 True（与「没配过」分得开）",
          eq0.get("applied") is True, json.dumps(eq0, ensure_ascii=False))
    # rate 缺 ⇒ 不重算 sos ⇒ 回显也不许动。写成断言是因为「赋值挪到 if 外面」这种改法
    # 会让 state() 报一条引擎里其实没挂上的曲线（读数比实现更乐观 = 假绿光）。
    e.configure(gains_db=[0.0] * 10, rate=48000)
    e.configure(gains_db=gains, rate=None)
    eqn = (e.state() or {}).get("eq") or {}
    check("rate 缺位时 eq 回显留在旧值（不许报没配上的曲线）",
          eqn.get("gains_db") == [0.0] * 10 and eqn.get("active") is False,
          json.dumps(eqn, ensure_ascii=False))

    # 变异 A：configure 不写回显（只算 sos）⇒ 屏上那条「曲线进没进引擎」的尺会读到全 0。
    orig_cfg = audioplay.NativeEngine.configure

    def cfg_noecho(self, **kw):
        orig_cfg(self, **kw)
        self._gains_db = [0.0] * 10          # ← 注坏：算完了但不告诉任何人
    audioplay.NativeEngine.configure = cfg_noecho
    try:
        m = audioplay.NativeEngine()
        m.configure(gains_db=gains, rate=48000)
        caught_a = ((m.state() or {}).get("eq") or {}).get("gains_db") != gains
    finally:
        audioplay.NativeEngine.configure = orig_cfg
    check("变异(configure 不写回显)被『eq 的增益就是那一份』抓到", caught_a,
          "注坏后回显没变 —— 这条尺是空的")

    # 变异 B：state() 不回显 eq（旧形状：state 里根本没有这一项）。
    orig_state = audioplay.NativeEngine.state

    def state_noeq(self):
        d = dict(orig_state(self))
        d.pop("eq", None)
        return d
    audioplay.NativeEngine.state = state_noeq
    try:
        m = audioplay.NativeEngine()
        m.configure(gains_db=gains, rate=48000)
        caught_b = "eq" not in (m.state() or {})
    finally:
        audioplay.NativeEngine.state = orig_state
    check("变异(state 不报 eq)被『state().eq 存在』抓到", caught_b,
          "注坏后 eq 还在 —— 这条尺是空的")

    # 变异 C：state() 把 applied 摘掉（旧形状）。前端分不出「没配过」与「全 0 曲线」，
    # 那句「十条增益都是 0」就会在还没同步时说出来。
    def state_noapplied(self):
        d = dict(orig_state(self))
        e2 = dict(d.get("eq") or {})
        e2.pop("applied", None)
        d["eq"] = e2
        return d
    audioplay.NativeEngine.state = state_noapplied
    try:
        m = audioplay.NativeEngine()
        m.configure(gains_db=gains, rate=48000)
        caught_c = ((m.state() or {}).get("eq") or {}).get("applied") is not True
    finally:
        audioplay.NativeEngine.state = orig_state
    check("变异(state 不报 applied)被『configure 后 applied=True』抓到", caught_c,
          "注坏后 applied 还在 —— 这条尺是空的")

    # 变异 D：configure 收下了曲线却不立 applied（回显永远说「还没同步」）。
    def cfg_noapplied(self, **kw):
        orig_cfg(self, **kw)
        self._eq_applied = False
    audioplay.NativeEngine.configure = cfg_noapplied
    try:
        m = audioplay.NativeEngine()
        m.configure(gains_db=gains, rate=48000)
        caught_d = ((m.state() or {}).get("eq") or {}).get("applied") is not True
    finally:
        audioplay.NativeEngine.configure = orig_cfg
    check("变异(configure 不立 applied)被同一条断言抓到", caught_d,
          "注坏后 applied 仍是 True —— 这条尺是空的")


def test_atempo_chain():
    """倍速拆分：界面上标的那个数必须＝耳朵里的那个数。

    这条是 2026-10-14 静态审计查出来的真缺陷的反面教材：_reader_loop（出声的那条流）原先写
    `atempo={max(0.5,min(2.0,speed))}` —— ffmpeg 的 atempo 单节只吃 0.5–2.0，所以
    「4×」在原生档实际只有 2×，而进度外推乘的是**请求值**，界面因此比耳朵快一倍；
    低速那头 0.25× 会被夹成 0.5×。离线预览那条（_decode）早就串了两节，所以只有播放会错——
    两条路各写一份规则，就是「两把尺分家」的标准形状。
    """
    AC, AP, AF = audioplay.atempo_chain, audioplay.atempo_product, audioplay.atempo_filter
    check("常速不挂滤镜（1.0 与容差内的 1.0005/0.9995 都算常速）",
          AC(1.0) == [] and AC(1.0005) == [] and AC(0.9995) == [],
          [AC(1.0), AC(1.0005), AC(0.9995)])
    check("差得出来的那一档要挂（1.01 不是常速；容差边界按 ±1e-3 判）",
          AC(1.01) != [] and AC(0.99) != [], [AC(1.01), AC(0.99)])
    check("非法/缺位倍速不挂滤镜（不许造出一条空 -filter:a 让 ffmpeg 报错）",
          AC(0) == [] and AC(None) == [] and AF(AC(0)) == "", str(AC(0)))
    check("4× 拆成 2×2", AC(4.0) == [2.0, 2.0], str(AC(4.0)))
    check("3× 拆成 2×1.5", AC(3.0) == [2.0, 1.5], str(AC(3.0)))
    check("0.25× 拆成 0.5×0.5", AC(0.25) == [0.5, 0.5], str(AC(0.25)))
    check("滤镜串的形状是 ffmpeg 认的逗号串联",
          AF(AC(3.0)) == "atempo=2.0000,atempo=1.5000", AF(AC(3.0)))
    # 逐档扫：每一节都要落在 ffmpeg 允许的区间，且合成值要等于请求值。
    sweep = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
    off = [s for s in sweep if abs(AP(AC(s)) - s) > max(1e-3, s * 1e-3)]
    check("扫 0.25–4×：合成倍速＝请求倍速（差>0.1% 的档点名）", not off, off)
    out_of_range = [(s, f) for s in sweep for f in AC(s) if not 0.5 <= f <= 2.0]
    check("扫 0.25–4×：每一节都在 atempo 允许的 0.5–2.0 内", not out_of_range, out_of_range)
    too_long = [s for s in sweep if len(AC(s)) > audioplay.ATEMPO_MAX_SECTIONS]
    check("因子数不超上限（不收敛要停，别造一条几百节的命令行）", not too_long, too_long)

    # 已知坏样本＝修复前那一行。上面的判据必须能把它打红，否则那些 check 是摆设。
    def old_single(speed):
        return [max(0.5, min(2.0, float(speed or 1.0)))]
    check("（对照）旧的单节夹取在 4× 上只合成 2×，会被『合成＝请求』抓到",
          abs(AP(old_single(4.0)) - 4.0) > 1.0, AP(old_single(4.0)))
    check("（对照）旧的单节夹取在 0.25× 上合成 0.5×，同样被抓",
          abs(AP(old_single(0.25)) - 0.25) > 0.2, AP(old_single(0.25)))

    # 进度外推乘的是「耳朵」那份（_atempo），不是「界面」那份（_speed）。
    e = audioplay.NativeEngine()
    e._rate, e._ch = 48000, 2
    e._base_pos, e._consumed = 0.0, 48000        # 正好喂进设备 1 秒的输出
    drift = []
    for s in sweep:
        e._speed = s
        e._atempo = AP(AC(s)) or 1.0
        got = e._pos_src_locked()
        if abs(got - s) > max(1e-3, s * 1e-3):
            drift.append((s, got))
    check("进度外推：输出满 1 秒时源秒数＝倍速（逐档扫 0.25–4×）", not drift, drift)
    e._speed, e._atempo = 4.0, 1.0
    check("（变异对照）_atempo 忘了跟着请求值更新时，这条尺会翻红",
          abs(e._pos_src_locked() - 4.0) > 1.0, e._pos_src_locked())
    e._speed, e._atempo = 4.0, AP(AC(4.0))
    check("state() 把两个数都报出来（speed=请求、atempo=实际），屏上闸才量得到谎",
          "atempo" in (e.state() or {}) and (e.state() or {}).get("speed") == 4.0
          and (e.state() or {}).get("atempo") == 4.0,
          json.dumps({k: (e.state() or {}).get(k) for k in ("speed", "atempo")}))
    # 注坏 state()：不报 atempo ⇒ 上面那条必须红（证伪这条尺自己）
    orig_state = audioplay.NativeEngine.state

    def state_no_atempo(self):
        d = dict(orig_state(self))
        d.pop("atempo", None)
        return d
    audioplay.NativeEngine.state = state_no_atempo
    try:
        caught = "atempo" not in (e.state() or {})
    finally:
        audioplay.NativeEngine.state = orig_state
    check("（变异对照）state 不报 atempo 时被抓到", caught, "注坏了还在——尺是空的")

    # 最后一刀：量**真要交给 ffmpeg 的那行命令**，不是量上面那套算式。
    # 为什么要这一步：上面全绿只证明 atempo_chain 这个函数对；_reader_loop 若没调用它
    # （或调用了又把结果丢掉），4× 照样是 2× —— 本仓库栽过的「文本匹配全绿但消费它的程序不认」。
    cmds = _capture_reader_cmds(e, {"4x": 4.0, "0.5x": 0.5, "1x": 1.0})
    check("流式那条路真的把 -filter:a atempo=2.0000,atempo=2.0000 交给 ffmpeg（4×）",
          "atempo=2.0000,atempo=2.0000" in cmds.get("4x", ""), cmds.get("4x", ""))
    check("0.5× 挂一节 atempo=0.5000（低速也走同一份拆分）",
          "atempo=0.5000" in cmds.get("0.5x", ""), cmds.get("0.5x", ""))
    check("1× 挂的是 HQ 重采样而不是空滤镜（播放路径的质量承诺）",
          "aresample=48000:filter_size=256:phase_shift=24:linear_interp=0:cutoff=0.98"
          in cmds.get("1x", "") and "atempo" not in cmds.get("1x", ""),
          cmds.get("1x", ""))
    check("4× 的滤镜串顺序固定：先 atempo，再 HQ 重采样",
          "atempo=2.0000,atempo=2.0000,aresample=48000:filter_size=256"
          in cmds.get("4x", ""), cmds.get("4x", ""))
    check("（对照）旧写法在 4× 那行里只有一个 atempo=2.0000，会被上面第一条抓到",
          cmds.get("4x", "").count("atempo=") == 2, cmds.get("4x", ""))


def _capture_reader_cmds(engine, speeds):
    """跑一遍 _reader_loop，抓它交给 Popen 的 argv（不真起 ffmpeg）。

    桩只做两件事：按调用顺序记下命令行、给一根立刻 EOF 的 stdout。读线程因此第一次 read
    就退出，不会往环里塞数，也不需要设备 —— 锁屏/没声卡照样跑。
    返回值：{倍速档名: 那行命令}，顺序按传进来的 dict 顺序（3.7+ 保序）。
    """
    import io
    import types
    seen = []
    real = audioplay.subprocess

    class _Proc:
        def __init__(self, cmd):
            self.stdout = io.BytesIO(b"")     # 第一次 read 就 EOF ⇒ 循环立刻 break
        def poll(self):
            return 0
        def terminate(self):
            pass
        def kill(self):
            pass
        def wait(self):
            return 0

    def fake_popen(cmd, *a, **k):
        seen.append(" ".join(str(x) for x in cmd))
        return _Proc(cmd)

    class _Sub(types.SimpleNamespace):
        pass
    sub = _Sub()
    sub.PIPE = real.PIPE
    sub.DEVNULL = real.DEVNULL
    sub.Popen = fake_popen
    audioplay.subprocess = sub
    try:
        for _key, sp in speeds.items():
            engine._reader_loop("dummy.mp3", 0.0, 48000, 2, sp, 0)
    finally:
        audioplay.subprocess = real
    return dict(zip(list(speeds), seen))


# ================================================================== 独占档（离线）
# 这一组全程**不开设备、不出声**：只借真 sp.wasapi 里的纯函数（挑格式、make_wf、整型量化），
# 凡会碰 COM 的（枚举端点、能力探针、ExclusiveStream、com_init）一律换成确定性替身。
# 判的是引擎这一侧的形状：整周期投递 / 静音与丢帧分家 / 播完放手 / 长暂停放手后续播重开 /
# 协商 fail-closed / bit-perfect 逐条判 / 独占档频谱不再是一条直线。
# 真机那一半另有人在 tools/probe_exclusive.py（会占有声卡，只在 desk_claim=agent 时跑）；
# 两边不许互相冒充：这里绿只证「代码形状对」，不证「这台声卡认」。
from sp import wasapi as W        # noqa: E402

# 本机默认端点（「扬声器 (EDIFIER N500)」）的独占能力表，照回执原样填：
# 出处 tools/exclusive_probe.20261020-002924.json 的 exclusive_supported（三档全 16 位）
# 与 mix（f32@96000/2h）、default_period_hns=100000。
HOST_TABLE = [
    {"tag": "44100-pcm16", "rate": 44100, "valid_bits": 16, "container_bits": 16, "hr": 0},
    {"tag": "48000-pcm16", "rate": 48000, "valid_bits": 16, "container_bits": 16, "hr": 0},
    {"tag": "96000-pcm16", "rate": 96000, "valid_bits": 16, "container_bits": 16, "hr": 0},
    {"tag": "44100-f32", "rate": 44100, "valid_bits": 32, "container_bits": 32,
     "hr": W.AUDCLNT_E_UNSUPPORTED_FORMAT},
]
HOST_ENDPOINTS = [
    {"index": 0, "id": "{0.0.0.00000000}.{272aa891-2962-416d-aab0-80f3d1f8f6af}",
     "name": "扬声器 (EDIFIER N500)"},
    {"index": 1, "id": "{0.0.0.00000000}.{fb4d0830-e27c-40cd-9347-c88772d430d3}",
     "name": "Mi Monitor (NVIDIA High Definition Audio)"},
]
# 名字也是实测那份（tools/wasapi_names.probe.json 的 cross_source：COM 与 PortAudio
# 两条独立实现读出来的名字多重集相同），所以「按名字对 id」这条口径不是猜的。


class _ShimWS:
    """假 wasapi 模块：纯函数走真的，COM 全换替身。
    pick/make_wf/block_frames 故意**不重写**——重写就等于测我自己的副本。"""
    KSDATAFORMAT_SUBTYPE_PCM = W.KSDATAFORMAT_SUBTYPE_PCM
    KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = W.KSDATAFORMAT_SUBTYPE_IEEE_FLOAT
    WAIT_OBJECT_0 = W.WAIT_OBJECT_0
    WAIT_TIMEOUT = W.WAIT_TIMEOUT
    WAIT_FAILED = W.WAIT_FAILED
    S_OK = W.S_OK
    WasapiError = W.WasapiError
    pick_exclusive_format = staticmethod(W.pick_exclusive_format)
    make_wf = staticmethod(W.make_wf)
    block_frames = staticmethod(W.block_frames)
    frames_to_device = staticmethod(W.frames_to_device)

    def __init__(self, table=None, endpoints=None, fail_init=False):
        self.table = list(table if table is not None else HOST_TABLE)
        self.endpoints = list(endpoints if endpoints is not None else HOST_ENDPOINTS)
        self.fail_init = fail_init
        self.probe_calls = []
        self.default_calls = 0

    def com_init(self, mta=True):
        return "already"

    def device_ids(self, mask=1):
        return list(self.endpoints)

    def default_device_id(self, role=0):
        self.default_calls += 1
        return dict(self.endpoints[0])

    def probe_device(self, device_id, channels=2):
        self.probe_calls.append((device_id, channels))
        return {"device": device_id,
                "mix": ("f32@96000/2h", 96000, 32, 32, 2),
                "default_period_hns": 100000, "min_period_hns": 30000,
                "default_period_ms": 10.0,
                "exclusive_supported": [r for r in self.table if r.get("hr") == 0],
                "table": ([] if self.fail_init else self.table)}


class _FakeStream:
    """假独占流：只记账，不碰 COM。写进来的字节数必须**正好一个周期**，这是这条尺的核心。

    pace 不是装饰：真设备的节拍是「每 10ms 一个事件」，替身若让 wait() **立即**返回，
    泵会在 0.25 秒里空转十几万拍，于是「供得上时不该记丢帧」这把尺量的其实是
    「喂进环形的块数够不够十几万次」——量具自己把被测机制的下限撞穿了。
    所以 wait() 默认按 4ms 睡一拍（比真机快 2.5 倍，让自检能在秒级跑完）。
    """

    def __init__(self, rate=44100, ch=2, bits=16, container=16, period=441,
                 wait_result=None, pace=0.004, buffer_frames=0):
        self.wf = W.make_wf(rate, ch, bits, W.KSDATAFORMAT_SUBTYPE_PCM, container)
        self.period_frames = period
        # 真机缓冲是 4 拍（_create_exclusive_stream 的 buffer_frames）；默认 0 表示
        # 「替身不管缓冲深度」，要量排水行为的测试显式给值。
        self.buffer_frames = buffer_frames
        self.waits = 0
        self.rate, self.ch, self.bits, self.container = rate, ch, bits, container
        self.n_data = 0
        self.n_silence = 0
        self.bad_sizes = []
        self.closed = False
        self.writes = []
        self._wait_result = wait_result
        self.pace = pace
        # 谁建的、谁放的：**线程身份**而不是布尔。2026-10-20 本机差分（真设备）量到
        # 「建流的那根线程在流还开着时就退了」⇒ stop 上 0xC0000005（4/4），而建/用/放
        # 排在同一根活过流寿命的线程上时 3/3 干净。这条不变式只能靠这两个数判，
        # 用「是不是泵」猜会漏掉「同一根请求线程建又同一根请求线程放」那种假绿。
        self.born_tid = threading.get_ident()
        self.born_name = threading.current_thread().name
        self.dead_tid = None
        self.dead_name = None

    def wait(self, ms=100):
        self.waits += 1
        if self.pace:
            time.sleep(self.pace)
        return W.WAIT_OBJECT_0 if self._wait_result is None else self._wait_result

    def _size_ok(self, n):
        want = self.period_frames * self.wf.Format.nBlockAlign
        if n != want:
            self.bad_sizes.append((n, want))

    def write(self, data):
        self.n_data += 1
        self._size_ok(len(data))
        self.writes.append(bytes(data))
        return self.period_frames

    def write_silence(self):
        self.n_silence += 1
        return self.period_frames

    def status(self):
        return {"mode": "exclusive", "started": not self.closed,
                "rate": self.rate, "channels": self.ch, "valid_bits": self.bits,
                "container_bits": self.container, "period_frames": self.period_frames,
                "blocks": self.n_data + self.n_silence, "silence_blocks": self.n_silence,
                "written_frames": self.n_data * self.period_frames}

    def close(self):
        self.closed = True
        self.dead_tid = threading.get_ident()
        self.dead_name = threading.current_thread().name


def _arm(e, rate=44100, ch=2, paused=False, ended=False, eof=False, priming=False,
         frames=None):
    """把引擎摆成「刚起播」的内部形状（不走 play()，所以一次设备都不碰）。"""
    with e._lock:
        e._rate, e._ch = rate, ch
        e._paused, e._ended = paused, ended
        e._eof = eof
        e._priming = priming
        e._prime_frames = 0
        e._prime_until = 0.0
        e._chunks.clear(); e._frames_avail = 0
        e._consumed = 0
        e._underruns = 0
        e._gain, e._balance = 1.0, 0.0
        e._sos, e._sos_state = None, None
        e._loudness = False
        e._fade_in_frames, e._fade_in_sec = 0, 0.0
        e._speakers, e._speakers_amount = False, 0.35
        e._cf_zi, e._cf_hist = None, None
        # 限幅器/下混余量也必须归零：它们是 play() 才设置的，_arm 不摆就会被上一条测试
        # 留下的 _lim_on=True 带进下一条（同一把尺在不同顺序下量出不同答案）。
        e._lim_on, e._lim = False, None
        e._downmix_trim = 1.0
        e._atempo, e._speed = 1.0, 1.0
        e._source_rate = rate
        e._source_bits = 16
        # _arm 必须把判据读的源侧字段**全部**摆好：漏一个（比如 _source_ch），那条腿读到的
        # 就是上一个测试留下的残值——同一把尺在不同测试顺序下量出不同答案，红了也不知道是自己。
        e._source_ch = ch
        e._dither_enabled = True
        e._mode = "exclusive"
        e._ex_requested = True
        e._native_note = ""
        e._ex_released = ""
        # 切歌过桥/淡出起点也必须清：留着上一条测试的尾巴，泵第一拍会往假流里
        # 投一段没来源的「旧声」，把「这一拍该投什么」的判断搅浑。
        e._last_out = None
        e._last_tail = None
        e._switch_fade = False
        if frames:
            e._chunks.append(frames)
            e._frames_avail = int(frames.shape[0])


def _feed_ring(e, periods, period=441, ch=2, rate=44100):
    with e._lock:
        e._chunks.clear(); e._frames_avail = 0
        for i in range(periods):
            e._chunks.append(np.full((period, ch), float(i + 1), np.float32))
            e._frames_avail += period
        e._rate = rate


def _start_pump(e):
    """起一根**活得比较久**的泵线程，返回 (stop, th, 等条件成立的助手)。
    「放手→续播→重开」这条路必须让**同一根**线程跨越整个场景：每次换个新线程，
    测的就变成「新线程会不会重开」，而产品里干那件事的是原来那根没退出的泵。"""
    stop = threading.Event()
    with e._ex_lock:
        e._ex_stop = stop
        th = threading.Thread(target=e._exclusive_loop, args=(stop,), daemon=True)
        e._ex_pump = th
    th.start()
    return stop, th


def _wait_until(fn, seconds=1.5):
    """轮询到条件成立（或超时）。返回实际等了多久，None＝超时没成立。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        if fn():
            return time.monotonic() - t0
        time.sleep(0.01)
    return None


def _stop_pump(e, stop, th, timeout=1.5):
    stop.set()
    th.join(timeout=timeout)
    with e._ex_lock:
        e._ex_stop = None
        if e._ex_pump is th and not th.is_alive():
            e._ex_pump = None
    return th


def _run_pump(e, seconds=0.25):
    """起真泵线程跑最多 seconds 秒后收摊（线程自己退了就不再等）。返回已收摊的线程。"""
    stop, th = _start_pump(e)
    _wait_until(lambda: not th.is_alive(), seconds)
    return _stop_pump(e, stop, th)


def _clear_wa_cache():
    audioplay._WA_ENUM["ts"] = 0.0
    audioplay._WA_ENUM["rows"] = None
    audioplay._WA_CAPS.clear()


def _with_shim(shim):
    """把 audioplay._ws 换成替身，并把端点/能力缓存清干净（否则测的是上一轮的缓存）。"""
    orig = audioplay._ws
    audioplay._ws = shim
    _clear_wa_cache()
    return orig


def _restore_ws(orig):
    audioplay._ws = orig
    _clear_wa_cache()


def test_pull_locked_kinds():
    """取数口那六家各一条：这是共享回调和独占泵**共用**的那一把尺，判错一家两种输出一起错。"""
    e = audioplay.get()
    e.stop()
    period = 441
    _arm(e, paused=True)
    with e._lock:
        _out, filled, kind = e._pull_locked(period)
    check("暂停 ⇒ idle（不取数、不记丢帧）", kind == "idle" and filled == 0, kind)
    _arm(e, ended=True)
    with e._lock:
        _out, filled, kind = e._pull_locked(period)
    check("已 ended ⇒ idle（不取数、不记丢帧）", kind == "idle" and filled == 0, kind)

    _arm(e, priming=True)
    with e._lock:
        e._prime_frames = 1000
        e._prime_until = time.monotonic() + 5.0
        e._frames_avail = 10
        _out, filled, kind = e._pull_locked(period)
    with e._lock:
        und = e._underruns
    check("灌数闸门里 ⇒ priming，且**不**记 underrun",
          kind == "priming" and filled == 0 and und == 0, "%s und=%s" % (kind, und))
    # 闸门三条出口逐条走一遍（少一条就是「闸门长住、进度冻死」那种事故）
    with e._lock:
        e._frames_avail = 1000
        _out, filled, kind = e._pull_locked(period)
    check("够量 ⇒ 出闸门（这一拍开始真取数）", e._priming is False, str(e._priming))
    _arm(e, priming=True, eof=True)
    with e._lock:
        e._prime_frames = 1000
        e._prime_until = time.monotonic() + 5.0
        _out, filled, kind = e._pull_locked(period)
    check("读到尾 ⇒ 出闸门（坏文件不许把闸门关死）", kind in ("eof", "idle") or filled == 0,
          "%s %s" % (kind, filled))

    _arm(e)
    with e._lock:
        _out, filled, kind = e._pull_locked(period)
    with e._lock:
        und = e._underruns
    check("空环且没到尾 ⇒ empty 且记一次 underrun",
          kind == "empty" and und == 1, "%s und=%s" % (kind, und))

    _arm(e)
    _feed_ring(e, 1, period=period)
    with e._lock:
        out, filled, kind = e._pull_locked(period)
    with e._lock:
        und, consumed = e._underruns, e._consumed
    check("取满 ⇒ data，consumed 推进 period，不记 underrun",
          kind == "data" and filled == period and consumed == period and und == 0,
          "%s filled=%s consumed=%s und=%s" % (kind, filled, consumed, und))
    check("取满那块真的是 DSP 后的数（非零、长度对）",
          float(np.max(np.abs(out))) > 0.5 and out.shape == (period, 2), str(out.shape))

    _arm(e)
    _feed_ring(e, 1, period=period)
    with e._lock:
        _out, filled, kind = e._pull_locked(period + 60)
    with e._lock:
        und, consumed = e._underruns, e._consumed
    check("半截 ⇒ partial，记一次 underrun，但时钟只按**实取**推进（耳朵说了算）",
          kind == "partial" and filled == period and und == 1 and consumed == period,
          "%s filled=%s und=%s consumed=%s" % (kind, filled, und, consumed))

    _arm(e, eof=True)
    with e._lock:
        _out, filled, kind = e._pull_locked(period)
        ended = e._ended
    check("空环且读到尾 ⇒ eof 并当场把 ended 结算掉（不等有人 GET state）",
          kind == "eof" and ended is True, "%s ended=%s" % (kind, ended))
    with e._lock:
        und = e._underruns
    _arm(e, eof=True, ended=True)
    with e._lock:
        _out, _f, kind2 = e._pull_locked(period)
    check("ended 之后继续空转只算 idle ⇒ 丢帧计数一动不动（7629 那起事故的根）",
          kind2 == "idle" and e._underruns == und, "%s %s/%s" % (kind2, e._underruns, und))
    e.stop()


def test_exclusive_negotiation_failclosed():
    """协商失败的每一条路都必须**如实回落共享**，并带一句能上界面的原因。"""
    e = audioplay.get()
    e.stop()
    p = first_real_audio_path()
    opened = []
    orig_open = e._open_stream
    e._open_stream = lambda rate, ch, idx: opened.append((rate, ch, idx))
    orig_ws = audioplay._ws

    def shim(**kw):
        # 每换一份替身都要清缓存：能力表是按**端点 id** 缓存的，而这几条路用的是同一台默认端点。
        # 不清的话第③条拿到的是第②条那张「驱动全否」的表，判据量的其实是缓存而不是新替身。
        # （这句不是假想：本套第一跑就在这条翻红，打印的原因是第②条那句驱动原话。）
        _clear_wa_cache()
        audioplay._ws = _ShimWS(**kw)
    try:
        # ① 整个模块都不可用（缺库/非 Windows）
        _clear_wa_cache()
        audioplay._ws = None
        ok, why, neg_rate, info = e._negotiate_exclusive(44100, 2, None)
        check("_ws 缺失 ⇒ ok=False 且给了原因（不是抛、不是静默成功）",
              ok is False and why and neg_rate == 44100 and info == {}, "%s %r" % (why, info))
        if p:
            r = e.play(p, exclusive=True, rate=44100, ch=2)
            check("协商不成 ⇒ 实际档位落回 shared，note 里带那句原因",
                  r["mode"] == "shared" and "独占未生效" in (r["note"] or ""),
                  json.dumps({k: r[k] for k in ("mode", "note")}, ensure_ascii=False))
            check("回落共享时**仍把流开起来**（不是干脆不出声）", len(opened) == 1, str(opened))
            e.stop()
            opened.clear()
        # ② 驱动对全部候选说不 ⇒ 同样回落
        shim(table=[{"tag": "x", "rate": 44100, "valid_bits": 16,
                     "container_bits": 16,
                     "hr": W.AUDCLNT_E_UNSUPPORTED_FORMAT}])
        ok, why, neg_rate, info = e._negotiate_exclusive(44100, 2, None)
        check("驱动全否 ⇒ ok=False，原因来自驱动那句话",
              ok is False and "都说不" in why, "%s %r" % (ok, why))
        # ③ 能力探针拿不到表（COM 起不来）
        shim(table=[])
        ok, why, _r, _i = e._negotiate_exclusive(44100, 2, None)
        check("探不到格式表 ⇒ ok=False 并点名是探针失败（空表不等于成功）",
              ok is False and "格式表" in why, why)
        # ④ 选了台对不上 COM 名字的设备
        _clear_wa_cache()
        audioplay._ws = _ShimWS(endpoints=[{"index": 0, "id": "{x}", "name": "别的名字"}])
        orig_devices = e.devices
        e.devices = lambda: [{"index": 7, "name": "扬声器 (EDIFIER N500)", "rate": 96000,
                             "channels": 2, "is_default": True, "wasapi_id": None}]
        try:
            ok, why, _r, _i = e._negotiate_exclusive(44100, 2, 7)
            check("设备对不上 COM 端点 ⇒ 拒绝独占并说清是哪台（不猜第 i 台对第 i 台）",
                  ok is False and "对不上" in why, why)
        finally:
            e.devices = orig_devices
    finally:
        audioplay._ws = orig_ws
        e._open_stream = orig_open
        _clear_wa_cache()
        e.stop()


def test_exclusive_negotiation_table():
    """协商在**本机实测那张表**上的三种落点，逐个对账（这是 bit-perfect 那句结论的来源）。"""
    e = audioplay.get()
    e.stop()
    shim = _ShimWS()
    orig = _with_shim(shim)
    try:
        # 源 16bit/44.1k：同率、位深够 ⇒ 真 bit-perfect 那条
        ok, why, rate, info = e._negotiate_exclusive(44100, 2, None, 16, 44100)
        check("本机表 + 源 16bit ⇒ 独占 44100-pcm16 且 device_bit_perfect=True",
              ok and rate == 44100 and info["tag"] == "44100-pcm16"
              and info["device_bit_perfect"] is True and info["resampled_by_device"] is False,
              json.dumps({k: info[k] for k in ("tag", "device_bit_perfect")},
                         ensure_ascii=False))
        check("默认端点走的是 GetDefaultAudioEndpoint（不是枚举第 0 台）",
              shim.default_calls == 1 and info["device_id"] == HOST_ENDPOINTS[0]["id"],
              "%s %s" % (shim.default_calls, info.get("device_id")))
        check("周期取的是驱动自己报的 default_period（10ms），不是我拍的数",
              info["period_hns"] == 100000 and info["period_ms"] == 10.0,
              "%s %s" % (info["period_hns"], info["period_ms"]))
        # 源 48k 而请求档是 44.1k：bit-perfect 的定义是「不重采样」，所以**必须按源率开**。
        # 这条就是给「(rate, rate) 那次笔误」立的尺：改回去这里当场翻红（44100≠48000）。
        ok, why, rate, info = e._negotiate_exclusive(44100, 2, None, 16, 48000)
        check("源 48k＋请求 44.1k ⇒ 按源率开 48000，且不重采样",
              ok and rate == 48000 and info["rate"] == 48000
              and info["resampled_by_device"] is False
              and info["device_bit_perfect"] is True,
              "%s %s" % (rate, json.dumps({k: info.get(k) for k in
                                          ("tag", "resampled_by_device")}, ensure_ascii=False)))
        # 源按 24bit 处理：同率只有 16 位 ⇒ 仍同率，但要量化，且不许谎称重采样
        ok, why, rate, info = e._negotiate_exclusive(44100, 2, None, 24, 44100)
        check("源 24bit ⇒ 仍选同率那档，device_bit_perfect=False 且 resampled=False",
              ok and info["rate"] == 44100 and info["bits"] == 16
              and info["device_bit_perfect"] is False
              and info["resampled_by_device"] is False, json.dumps(info["why"], ensure_ascii=False))
        # 请求 192k：本机独占没有 ⇒ 退到最近的 96k 并明说重采样
        ok, why, rate, info = e._negotiate_exclusive(192000, 2, None, 16, 192000)
        check("请求率不在独占清单里 ⇒ 退到最近档（96k）并如实标 resampled=True",
              ok and rate == 96000 and info["resampled_by_device"] is True,
              "%s %s" % (rate, info["tag"]))
        check("退档后的率**必须**回传给调用方（ffmpeg 要按它解码，否则是音高偏移不是音质）",
              info["rate"] == rate, "%s/%s" % (info["rate"], rate))
        # 缓存：同一端点第二次不再重探（一次探针是 33 次 IsFormatSupported）
        n_before = len(shim.probe_calls)
        e._negotiate_exclusive(44100, 2, None, 16, 44100)
        e._negotiate_exclusive(44100, 2, None, 16, 44100)
        check("能力表按端点缓存（连续协商只探一次）",
              len(shim.probe_calls) == n_before, "%s" % (shim.probe_calls,))
    finally:
        _restore_ws(orig)


def test_exclusive_dither_and_view():
    """量化那一步的两个旋钮各要一条已答对照：抖动**只在真会量化的档上加**，
    界面拿到的是**原始量**而不是前端自己推的结论。

    为什么这两件事值得单独一组（而不是「代码看着对」就放过）：
      · 抖错了档（f32 直通也抖、16 位放进 16 位也抖）＝往一条本来 bit-perfect 的流里注噪声，
        属于「为了听起来专业而牺牲专业」，只有照抄真表量出来的答案能抓；
      · 泵用的是**协商那一次**存进 _ex_params 的布尔，不在每一拍现算：现算的话
        「暂停 30 秒后自动重开的那条流」会换一套量化，同一首歌前后两段两种噪声，
        而界面上一个字都不会变——所以这里量的不是「有没有 dither 这个键」，
        是「写进设备的那一次调用里，dither 参数到底等于什么」。
    """
    e = audioplay.get()
    e.stop()
    # 表里加一档 f32@44100（hr=0）：源 24 位时 pick 会选它（位深够、同率），
    # 于是这条就是「设备给 f32 ⇒ 逐样本直通 ⇒ 一个噪声都不许加」那一档的真表形状。
    f32_table = ([{"tag": "44100-f32", "rate": 44100, "valid_bits": 32,
                   "container_bits": 32, "hr": 0}] + list(HOST_TABLE))
    shim = _ShimWS(table=f32_table)
    orig = _with_shim(shim)
    try:
        # ---- 1) 协商层：三种源位深 ⇒ 三种该不该抖的答案
        ok, why, rate, info = e._negotiate_exclusive(44100, 2, None, 24, 44100)
        check("源 24 位 + 表里有 f32 ⇒ 开的是 f32 档：不量化 ⇒ 不许加抖动",
              ok and info["tag"] == "44100-f32" and info["quantizes"] is False
              and info["dither"] is False,
              json.dumps({k: info.get(k) for k in ("tag", "quantizes", "dither")},
                         ensure_ascii=False))
        check("f32 那档报的位深是 32 容器，抖动开关关着 ⇒ 设备侧最深位深这列也不能漏",
              info["bits"] == 32 and info["exclusive_max_bits"] == 32, str(info.get("bits")))
        # 变异证伪逼出来的一条：源位深**没量到**时「保守当作会量化」是错的——
        # 设备给的是 f32，那一路连量化这个动作都不存在（frames_to_device 对 f32 是逐字节搬运，
        # 抖动参数根本不看），于是界面会说「加了 TPDF 抖动」而实际一个噪声都没加。
        # 这条断言是 `quantizes = (not is_float) and ...` 里 `not is_float` 唯一的牙：
        # 把它摘掉，整套里只有这里翻红（已实测：摘掉后其余各条全绿）。
        ok, why, rate, infoU = e._negotiate_exclusive(44100, 2, None, 0, 44100)
        check("f32 档 + 源位深没量到 ⇒ 不许说「会量化/加了抖动」（那一路根本没有量化）",
              ok and infoU["tag"] == "44100-f32" and infoU["quantizes"] is False
              and infoU["dither"] is False,
              json.dumps({k: infoU.get(k) for k in ("tag", "quantizes", "dither")},
                         ensure_ascii=False))
        check("但同一次协商里 bit-perfect 那格仍然不许点头（没量到就是没量到，与容器无关）",
              infoU["source_bits"] == 0, str(infoU.get("source_bits")))
        shim2 = _ShimWS()                       # 真机那张表：独占清单只有 16 位三档
        _with_shim(shim2)
        ok, why, rate, info24 = e._negotiate_exclusive(44100, 2, None, 24, 44100)
        check("源 24 位而设备只给到 16 ⇒ 确实要量化 ⇒ 该加 TPDF",
              ok and info24["quantizes"] is True and info24["dither"] is True
              and info24["exclusive_max_bits"] == 16,
              json.dumps({k: info24.get(k) for k in ("tag", "quantizes", "dither",
                                                     "exclusive_max_bits")},
                         ensure_ascii=False))
        ok, why, rate, info16 = e._negotiate_exclusive(44100, 2, None, 16, 44100)
        check("源 16 位放进 16 位容器 ⇒ 整数量化无损 ⇒ 不许加（加了就是注噪声）",
              ok and info16["quantizes"] is False and info16["dither"] is False,
              json.dumps({k: info16.get(k) for k in ("quantizes", "dither")},
                         ensure_ascii=False))
        ok, why, rate, info0 = e._negotiate_exclusive(44100, 2, None, 0, 0)
        check("源位深没量到 ⇒ 保守当作会量化（宁可抖，也不静默削），但仍不许宣布 bit-perfect",
              ok and info0["quantizes"] is True and info0["dither"] is True
              and info0["source_bits"] == 0,
              json.dumps({k: info0.get(k) for k in ("source_bits", "quantizes", "dither")},
                         ensure_ascii=False))
        # 反向哨兵：这四条不是「恒 True」也不是「恒 False」——同一段代码，答案会翻面
        check("同一张表、同一个源率，只有源位深变 ⇒ 抖与不抖真的分得开（不是常量）",
              info24["dither"] is True and info16["dither"] is False, "%s/%s" % (
                  info24["dither"], info16["dither"]))
        # 用户旋钮：关掉抖动之后「会不会量化」这条事实不许跟着变（那是设备的属性）
        e.configure(dither=False)
        ok, why, rate, infoOff = e._negotiate_exclusive(44100, 2, None, 24, 44100)
        check("关掉抖动 ⇒ dither=False 但 quantizes 仍是 True（量化照旧发生，只是不补偿）",
              infoOff["quantizes"] is True and infoOff["dither"] is False,
              json.dumps({k: infoOff.get(k) for k in ("quantizes", "dither")},
                         ensure_ascii=False))
        e.configure(dither=True)

        # ---- 2) 泵层：写进设备那一次调用里的 dither 实参，就是协商存下的那个布尔
        seen = []
        real = W.frames_to_device

        def spy(out, bits, container, dither=False):
            seen.append(bool(dither))
            # 委托回真函数：字节数才能继续满足「正好一个周期」那把尺（替身自己算尺寸
            # 就等于把这条尺的承重件搬到测试里，红了分不清是产品还是量具）
            return real(out, bits, container, dither=dither)

        shim2.frames_to_device = spy
        try:
            for want, label in ((True, "协商说该抖"), (False, "协商说不该抖")):
                del seen[:]
                _arm(e)
                _feed_ring(e, 120, period=441)
                st = _FakeStream(period=441)
                p = dict(info24)
                p["dither"] = want
                with e._ex_lock:
                    e._ex, e._ex_params = st, p
                _run_pump(e, 0.12)
                with e._ex_lock:
                    e._ex = None
                check("%s ⇒ 泵投递时传的就是那个值（且不是每拍自己重算）" % label,
                      len(seen) >= 3 and all(x is want for x in seen),
                      "n=%s values=%s" % (len(seen), sorted(set(seen))))
        finally:
            try:
                del shim2.frames_to_device
            except AttributeError:
                pass
        check("摘掉替身后真函数就回来了（这条尺不许把 spy 留在模块上污染后面的测试）",
              shim2.frames_to_device is real, repr(shim2.frames_to_device))

        # ---- 3) 界面读数：state().exclusive 要给**原始量**，前端一个字都不许猜
        _arm(e, ch=2)
        with e._lock:
            e._source_bits, e._source_rate, e._source_ch = 16, 44100, 2
        st = _FakeStream(period=441)
        with e._ex_lock:
            e._ex, e._ex_params = st, dict(info24)
        v = e.state().get("exclusive") or {}
        check("exclusive 视图带上源侧三个量与设备侧两个量（界面不用自己推）",
              v.get("source_bits") == 24 and v.get("source_rate") == 44100
              and v.get("device_max_bits") == 16 and v.get("device_rates") == [44100, 48000, 96000],
              json.dumps({k: v.get(k) for k in ("source_bits", "source_rate",
                                                "device_max_bits", "device_rates")},
                         ensure_ascii=False))
        check("视图里的 dither/quantizes/period_ms 是从施工图抄的，不是第二次计算",
              v.get("dither") is True and v.get("quantizes") is True
              and v.get("period_ms") == 10.0,
              json.dumps({k: v.get(k) for k in ("dither", "quantizes", "period_ms")},
                         ensure_ascii=False))
        check("源 24 位落 16 位独占 ⇒ 视图自己就把 bit_perfect 判 False（结论不外包给前端）",
              v.get("bit_perfect") is False
              and any("量化" in r or "位深" in r for r in v.get("bit_perfect_blocking") or []),
              json.dumps(v.get("bit_perfect_blocking"), ensure_ascii=False))
        with e._ex_lock:
            e._ex, e._ex_params = None, None
    finally:
        _restore_ws(orig)
        e.stop()


def _born_on_pump(st, main_tid, pump_name="salt-exclusive-pump"):
    """这条流是不是**泵线程**建的（不是调用 play 的那根请求线程）。"""
    return (st is not None and st.born_tid is not None
            and st.born_name == pump_name and st.born_tid != main_tid)


def _closed_by_born(st):
    """放流的那根线程＝建流的那根（而且必须真的放过）。"""
    return (st is not None and st.closed and st.dead_tid is not None
            and st.dead_tid == st.born_tid)


def test_exclusive_thread_ownership():
    """独占流的建/用/放排在同一根线程上——这是本机那次 AV 的直接归因，写成机器判据。

    为什么值得单立一条（而不是靠 `test_exclusive_pump` 里那句「泵退出前自己放流」）：
    2026-10-20 真设备差分（%TEMP%\\repro_threadexit.py）——同一首歌、同一条独占流、
    blocks≈100 证明确实在推泵：**建流的线程开完就走 ⇒ stop 上 0xC0000005，4/4**；
    建流的线程活到 stop 之后 ⇒ 3/3 干净。产品用的是 ThreadingHTTPServer，每个请求一根
    用完即死的线程，所以「在请求线程上开流」这个形状**每一次 stop 都会踩**。修法是把
    Initialize 挪进泵，泵退之前先交还设备；这条判据钉的就是这两半，而且用的是**线程身份**
    （tid/名字），不是「有没有 closed 布尔」——后者拿一根假泵也能糊过去。
    """
    e = audioplay.get()
    e.stop()
    orig = _with_shim(_ShimWS())
    main_tid = threading.get_ident()
    made = []

    def fake_create(p):
        s = _FakeStream(period=441)        # 在**调用它的线程**上出生 ⇒ 线程身份是跑出来的，不是写的
        made.append(s)
        with e._ex_lock:
            e._ex = s
            e._ex_params = p
            e._ex_rec = {"mode": "exclusive", "period_frames": 441}
            e._ex_released = ""
        return {"mode": "exclusive", "period_frames": 441, "rate": p["rate"]}
    orig_create = e._create_exclusive_stream
    e._create_exclusive_stream = fake_create
    try:
        _arm(e)
        info = {"device_id": "{x}", "rate": 44100, "ch": 2, "bits": 16,
                "subtype": W.KSDATAFORMAT_SUBTYPE_PCM, "container": 16,
                "period_hns": 100000, "dither": False}
        rec = e._open_exclusive(44100, 2, info)     # ← 从「请求线程」调，模拟 api 那一手
        with e._ex_lock:
            st = e._ex
        check("T1 建流的线程是泵，不是调用 play 的那根（请求线程建流＝本机 4/4 的 AV 形状）",
              _born_on_pump(st, main_tid),
              "born=%s tid=%x 调用线程 tid=%x 流=%s"
              % (getattr(st, "born_name", None), getattr(st, "born_tid", 0) or 0,
                 main_tid, bool(st)))
        check("T2 回执仍从 _open_exclusive 交回调用线程（play 的响应契约不塌：mode/period 有值）",
              bool(rec) and rec.get("mode") == "exclusive" and rec.get("period_frames") == 441,
              json.dumps(rec, ensure_ascii=False)[:120])
        check("T3 等回执这件事没把 _dev_lock 攥在手里（泵能拿到锁才建得出流）",
              len(made) == 1, "建流次数=%s" % len(made))
        th = e._ex_pump
        e.stop()
        check("T4 放流的与建流的是同一根线程（泵退之前先把声卡交还）",
              _closed_by_born(st),
              "born=%s dead=%s closed=%s" % (st.born_name, st.dead_name, st.closed))
        with e._ex_lock:
            cur = e._ex
        check("T5 stop 之后引擎手上不攥流（指针清空，不是只写了句「已释放」）",
              cur is None, str(cur))
        # 反向哨兵：把旧形状（同一根请求线程建、同一根请求线程放）摆出来，这把尺必须说假。
        st_old = _FakeStream(period=441)
        st_old.close()
        check("T6 反向哨兵：旧形状（请求线程自建自放）在这把尺下必须露出来",
              not _born_on_pump(st_old, main_tid) and st_old.dead_tid == st_old.born_tid,
              "born=%s dead=%s" % (st_old.born_name, st_old.dead_name))
        # 另一条反向哨兵：泵建、**别的线程**放 ⇒ _closed_by_born 必须说假。
        # 出生必须在名字是泵的那根线程上跑出来（不是在 MainThread 上造好再改标签——
        # 那样 `_born_on_pump` 会因为真名字而说假，哨兵就测错了对象：02:5x 第一版就这么红了）。
        holder = {}

        def _born_on_pump_thread():
            s = _FakeStream(period=441)
            holder["st"] = s
        t_born = threading.Thread(target=_born_on_pump_thread, name="salt-exclusive-pump")
        t_born.start(); t_born.join()
        st_x = holder["st"]
        t_x = threading.Thread(target=st_x.close, name="not-the-pump")
        t_x.start(); t_x.join()
        check("T7 反向哨兵：泵建、外人放 ⇒ 同一根线程那把尺要说假（不是恒绿）",
              _born_on_pump(st_x, main_tid) and not _closed_by_born(st_x),
              "born=%s dead=%s" % (st_x.born_name, st_x.dead_name))
        check("T8 泵线程确实收干净了（没有「登记清了但线程还在」的孤儿泵）",
              th is not None and not th.is_alive(), str(th))

        # ---- 证伪两条（判据自己得能被注坏）：
        # ① 把 _open_exclusive 退回「在请求线程上直接建流」的旧形状 ⇒ T1 那把尺必须说假。
        #    这不是凭空设想：旧形状在本机真设备上 4/4 撞 0xC0000005（docstring 里的出处）。
        def old_shape(rr, cc, ii):
            with e._dev_lock:
                return e._create_exclusive_stream(ii)
        e._open_exclusive = old_shape
        try:
            _arm(e)
            rec_old = e._open_exclusive(44100, 2, info)
            with e._ex_lock:
                st_old = e._ex
            check("T9 证伪：退回「请求线程建流」的旧形状 ⇒ 出生线程那把尺必须翻红",
                  bool(rec_old) and not _born_on_pump(st_old, main_tid),
                  "born=%s（该是 MainThread 才算注坏成功）" % st_old.born_name)
        finally:
            e.__dict__.pop("_open_exclusive", None)
            e.stop()
        # ② 摘掉泵退出时的尾放流 ⇒ 「同一根线程放流」那把尺必须说假，而且必须**没人**放过
        #    （否则 close 可能是别处补的，T4 就成了恒绿）。
        e.stop()
        _arm(e)
        st_tail = _FakeStream(period=441)
        orig_close = e._close_exclusive
        e._close_exclusive = lambda reason: None       # 整项能力摘干净：尾放流与 stop 那一路都哑
        try:
            with e._ex_lock:
                e._ex = st_tail
            _feed_ring(e, 300, period=441)
            _run_pump(e, 0.15)
            check("T10 证伪：摘掉放流那一只手 ⇒ 流没被任何人放过（不是 close 恒真的形状）",
                  st_tail.closed is False, "closed=%s" % st_tail.closed)
            st_tail.dead_tid = st_tail.born_tid       # 只给个「同一根线程」的假象：判据仍要说假
            check("T10b 证伪：没真放过时 _closed_by_born 不许点头（closed 布尔是前提）",
                  not _closed_by_born(st_tail), "closed=%s" % st_tail.closed)
        finally:
            e._close_exclusive = orig_close
            with e._ex_lock:
                e._ex = None
    finally:
        e._create_exclusive_stream = orig_create
        _restore_ws(orig)
        e.stop()


def test_wasapi_id_match():
    """PortAudio 名字 → COM 端点 id 的匹配：对上要给 id，对不上要给 None（不是猜）。"""
    rows = [{"index": 12, "name": "扬声器 (EDIFIER N500)"},
            {"index": 13, "name": "Mi Monitor (NVIDIA High Definition Audio)"},
            {"index": 14, "name": "这台 COM 侧没有"}]
    orig = _with_shim(_ShimWS())
    try:
        audioplay.NativeEngine._attach_wasapi_ids(rows)
        check("名字整串相等的两台都挂上了对应 id",
              rows[0]["wasapi_id"] == HOST_ENDPOINTS[0]["id"]
              and rows[1]["wasapi_id"] == HOST_ENDPOINTS[1]["id"],
              str([r.get("wasapi_id") for r in rows]))
        check("对不上的那台是 None，不是**邻近那台的 id**",
              rows[2]["wasapi_id"] is None, str(rows[2]))
        # 空名/None 名也不能被匹配上（否则所有无名设备都会拿到同一串 id）
        e = audioplay.get()
        _arm(e)
        dev, why = e._resolve_endpoint(999)
        check("索引根本不在表里 ⇒ 拒开并说「已经不在枚举表里」",
              dev is None and "不在" in why, why)
    finally:
        _restore_ws(orig)


def test_exclusive_pump():
    """泵线程的投递形状：一次整整一个周期、没货铺静音但和丢帧分家、consumed 跟耳朵。"""
    e = audioplay.get()
    e.stop()
    orig = _with_shim(_ShimWS())
    st = _FakeStream(period=441)
    try:
        _arm(e)
        # 供数要**远多于**这一趟能要走的量：pace=4ms × 0.25s ≈ 60 拍，喂 300 块（≈1200 拍），
        # 于是「underruns 必须为 0」判的是「供得上就不许记丢帧」，不是「喂的块够不够几千次空转」。
        _feed_ring(e, 300, period=441)
        with e._ex_lock:
            e._ex = st
        th = _run_pump(e, 0.25)
        with e._lock:
            consumed, und = e._consumed, e._underruns
        check("泵线程跑起来了且投过若干整周期", st.n_data >= 3, str(st.n_data))
        check("每一次投递的字节数**正好**一个周期（441×2ch×16bit＝1764）",
              not st.bad_sizes and all(len(b) == 1764 for b in st.writes),
              "bad=%s sizes=%s" % (st.bad_sizes, [len(b) for b in st.writes][:5]))
        check("consumed 按实取的帧数推进（独占档进度也是同一把尺）",
              consumed >= 441 * 3 and consumed % 441 == 0, str(consumed))
        check("供得上时不该记丢帧", und == 0, "underruns=%s" % und)
        check("泵**退出前自己把流放掉**（建/用/放同一根线程；泵裸退而流还开着＝本机 4/4 复现的 AV）",
              st.closed is True and st.dead_tid == th.ident,
              "closed=%s 放手线程=%s 泵线程=%s" % (st.closed, st.dead_name, th.name))

        # 空环（解码供不上）：铺静音 + 记 underrun，两件事都要有且分得开。
        # 这里必须换**一条新流**：上一条已经被退出那根泵放掉了（正是上面那条判据要的），
        # 拿它继续量「这一趟投没投静音」就会把「流已交还」误读成「泵没干活」。
        st_b = _FakeStream(period=441)
        n_s0 = st_b.n_silence
        _feed_ring(e, 0, period=441)
        with e._ex_lock:
            e._ex = st_b
        with e._lock:
            e._underruns = 0
        _run_pump(e, 0.12)
        with e._lock:
            und2 = e._underruns
        check("没货 ⇒ 投的是静音块（不空转），且如实记 underrun",
              st_b.n_silence > n_s0 and und2 >= 1,
              "silence+%s underruns=%s" % (st_b.n_silence - n_s0, und2))
        # WAIT_FAILED：句柄没权限＝量具失灵，不许继续空转假绿，要放手并回落共享
        st2 = _FakeStream(period=441, wait_result=W.WAIT_FAILED)
        opened = []
        orig_open = e._open_stream
        e._open_stream = lambda rate, ch, idx: opened.append((rate, ch, idx))
        try:
            _arm(e)
            with e._ex_lock:
                e._ex = st2
            th = _run_pump(e, 0.2)
            with e._lock:
                mode, note = e._mode, e._native_note
            check("WAIT_FAILED ⇒ 立刻收摊、把独占流关掉、并回落共享（不拿空转冒充在放）",
                  st2.closed and mode == "shared" and "WAIT_FAILED" in note
                  and len(opened) == 1,
                  "closed=%s mode=%s note=%s opened=%s" % (st2.closed, mode, note, opened))
        finally:
            e._open_stream = orig_open
    finally:
        with e._ex_lock:
            e._ex = None
        _restore_ws(orig)
        e.stop()


def test_exclusive_pump_releases():
    """「独占不许把整机声音锁住」那两条放手路径：播完即放、长暂停放＋续播重开。"""
    e = audioplay.get()
    e.stop()
    orig = _with_shim(_ShimWS())
    try:
        # ① 播完：环形空 + _eof ⇒ 关流、写原因、线程自己退。
        # 宽限（_EX_EOF_GRACE_S）按产品默认是 2s；这一条量的是「宽限结束后会放手」，
        # 所以实例上把它调 0（判的是行为，不是秒数）。宽限本身由 test_exclusive_eof_grace 量。
        st = _FakeStream(period=441)
        _arm(e, eof=True)
        e._EX_EOF_GRACE_S = 0
        with e._ex_lock:
            e._ex = st
        stop, th = _start_pump(e)
        gone = _wait_until(lambda: not th.is_alive(), 2.0)
        with e._ex_lock:
            cur, released = e._ex, e._ex_released
        with e._lock:
            ended = e._ended
        check("播完 ⇒ 泵自己退出、独占流已关、并写下「已释放」",
              gone is not None and st.closed and cur is None
              and "播完" in (released or "") and ended is True,
              "gone=%s closed=%s ex=%s released=%r" % (gone, st.closed, cur, released))
        _stop_pump(e, stop, th)
        e.__dict__.pop("_EX_EOF_GRACE_S", None)   # ②③ 不需要曲尾宽限，恢复类默认

        # ② 长暂停：超过阈值放手；续播时重开一条新流（重开走 _create_exclusive_stream）
        e.stop()
        st_p = _FakeStream(period=441)
        _arm(e, paused=True)
        e._EX_PAUSE_RELEASE_S = 0.05          # 实例上把阈值调到能等的量级（判的是行为不是秒数）
        created = []
        orig_create = e._create_exclusive_stream

        def fake_create(p):
            s2 = _FakeStream(period=441)
            created.append(s2)
            with e._ex_lock:
                e._ex = s2
                e._ex_params = p
            return {"mode": "exclusive", "period_frames": 441}
        e._create_exclusive_stream = fake_create
        stop2 = th2 = None
        try:
            with e._ex_lock:
                e._ex = st_p
                e._ex_params = {"rate": 44100, "ch": 2, "bits": 16, "container": 16,
                                "period_hns": 100000, "device_id": "{x}"}
            # 一根线程跨越「暂停→放手→续播→重开」整段。原先这里每段各起一根新泵，
            # 于是「续播要回流」那条断言永远等不到（重开是**原来那根**的活，新泵根本没这职责），
            # 报的是 created=0 —— 一次量具装错，比少写一条断言更坑，因为它看着像在测。
            stop2, th2 = _start_pump(e)
            rel = _wait_until(lambda: st_p.closed, 1.5)
            with e._ex_lock:
                after_pause = e._ex
            check("暂停超过阈值 ⇒ 放手（独占期间系统是哑的，占着不给用户声音是错的）",
                  st_p.closed and (after_pause is None or after_pause is not st_p),
                  "closed=%s cur=%s 等了=%s" % (st_p.closed, after_pause, rel))
            with e._lock:
                e._paused = False
            rec = _wait_until(lambda: len(created) >= 1, 1.5)
            check("续播 ⇒ 同一根泵自动把独占流要回来（不是「一放就再也不独占」）",
                  rec is not None and len(created) >= 1,
                  "created=%s 等了=%s" % (len(created), rec))
            check("重开用的还是协商那份施工图（不会拿默认值偷偷换格式）",
                  bool(created) and created[0].rate == 44100
                  and created[0].bits == 16 and created[0].container == 16,
                  str([(s.rate, s.bits, s.container) for s in created]))
            _feed_ring(e, 300, period=441)      # 续播有货：投的该是数据块而不是静音块
            _wait_until(lambda: created and created[0].n_data >= 1, 1.0)
            check("重开后确实在投数（不是「开了条流挂在那里」）",
                  bool(created) and created[0].n_data >= 1,
                  str([s.n_data for s in created]))
        finally:
            if th2 is not None:
                _stop_pump(e, stop2, th2)
            e._create_exclusive_stream = orig_create
            del e.__dict__["_EX_PAUSE_RELEASE_S"]
        # ③ 重开失败 ⇒ 回落共享，并带原因（不许「独占放掉了、共享也没开、于是整机没声」）
        e.stop()
        _arm(e, paused=True)
        opened = []
        orig_open = e._open_stream
        e._open_stream = lambda rate, ch, idx: opened.append((rate, ch, idx))

        def boom(p):
            raise W.WasapiError("Initialize(EXCLUSIVE) 失败：AUDCLNT_E_DEVICE_IN_USE", 0x8889000A)
        e._create_exclusive_stream = boom
        try:
            with e._lock:
                e._paused = False
            with e._ex_lock:
                e._ex = None
                e._ex_params = {"rate": 44100, "ch": 2, "bits": 16, "container": 16,
                                "period_hns": 100000, "device_id": "{x}"}
            _run_pump(e, 0.2)
            with e._lock:
                mode, note = e._mode, e._native_note
            check("重开失败 ⇒ 回落共享并把驱动那句话带出来",
                  mode == "shared" and "DEVICE_IN_USE" in note and len(opened) >= 1,
                  "mode=%s note=%r opened=%s" % (mode, note, opened))
        finally:
            e._open_stream = orig_open
            e._create_exclusive_stream = orig_create
    finally:
        with e._ex_lock:
            e._ex = None
        _restore_ws(orig)
        e.stop()


def test_exclusive_stream_reuse():
    """同一档格式切歌 ⇒ 绝不 Release/Acquire 声卡；差一项 ⇒ 必须真重开。

    用户听到的「切歌一声电流声」长在 Stop→Initialize→Start 那一下；样本侧的淡入淡出
    抹不掉它，只能在源头避免。这条尺有牙：`_can_reuse_exclusive` 写死 False，第一组
    翻红；`_EX_REUSE_KEYS` 里摘掉 rate，第二组（差格式也复用）翻红。
    """
    e = audioplay.get()
    e.stop()
    p = first_real_audio_path()
    if not p:
        check("独占复用：库里有可播文件", False, "no track")
        return
    orig = _with_shim(_ShimWS())
    orig_create = e._create_exclusive_stream
    try:
        ok, why, rate, info = e._negotiate_exclusive(44100, 2, None, 16, 44100)
        check("复用前置：协商能拿到 44.1k/16 的施工图", ok and info.get("rate") == 44100, why)
        st = _FakeStream(period=441)
        _arm(e)
        with e._ex_lock:
            e._ex = st
            e._ex_params = dict(info)
        stop, th = _start_pump(e)
        with e._lock:
            e._last_out = np.array([0.5, -0.5], np.float32)
        created = []

        def rec(p_):
            # 复用若失效，这条**必须**只碰假流：绝不因为一条测试断言去开真设备。
            s = _FakeStream(period=441)
            created.append(p_)
            with e._ex_lock:
                e._ex = s
                e._ex_params = p_
            return {"mode": "exclusive", "period_frames": 441}
        e._create_exclusive_stream = rec
        r = e.play(p, exclusive=True, rate=44100, ch=2,
                   source_bits=16, source_rate=44100, source_ch=2)
        with e._ex_lock:
            cur, params = e._ex, e._ex_params
        with e._lock:
            mode = e._mode
        check("同档切歌 ⇒ 流对象没被放掉（closed=False，同一条流）",
              st.closed is False and cur is st, "closed=%s same=%s" % (st.closed, cur is st))
        check("同档切歌 ⇒ 一次 _create_exclusive_stream 都没调（没有 Initialize）",
              not created, str(created))
        check("同档切歌 ⇒ 仍是 exclusive 档", r.get("mode") == "exclusive" and mode == "exclusive",
              "%s/%s note=%r" % (r.get("mode"), mode, r.get("note")))
        check("施工图换成这一首的（复用不等于沿用上一首的源参数）",
              params.get("source_bits") == 16 and params.get("source_rate") == 44100,
              json.dumps({k: params.get(k) for k in ("source_bits", "source_rate")},
                         ensure_ascii=False))
        _wait_until(lambda: not e._switch_fade, 1.0)
        with e._lock:
            fade = e._switch_fade
        check("切歌过桥被泵消费（不是挂在那里等下一次）", fade is False, str(fade))
        check("复用后泵还在往设备投递（不是开了条没人喂的死流）",
              th.is_alive() and st.closed is False,
              "alive=%s closed=%s" % (th.is_alive(), st.closed))

        # ---- 差一项（48000 的施工图）⇒ 必须真重开
        e.stop()
        st2 = _FakeStream(period=441)
        _arm(e)
        with e._ex_lock:
            e._ex = st2
            e._ex_params = dict(info, rate=48000, tag="48000-pcm16")
        stop2, th2 = _start_pump(e)
        made = []

        def rec2(p_):
            s = _FakeStream(period=441)
            made.append(p_)
            with e._ex_lock:
                e._ex = s
                e._ex_params = p_
            return {"mode": "exclusive", "period_frames": 441}
        e._create_exclusive_stream = rec2
        try:
            e.play(p, exclusive=True, rate=44100, ch=2,
                   source_bits=16, source_rate=44100, source_ch=2)
        finally:
            e._create_exclusive_stream = orig_create
        with e._ex_lock:
            cur2 = e._ex
        check("格式差一项（48k 的旧施工图）⇒ 旧流被放掉并按新施工图重开",
              st2.closed is True and len(made) == 1 and cur2 is not st2,
              "closed=%s made=%s same=%s" % (st2.closed, len(made), cur2 is st2))
        check("重开用的就是这一首协商出来的 44100（不是上一首的 48000）",
              bool(made) and made[0].get("rate") == 44100,
              str([m.get("rate") for m in made]))
        # 这里不要碰登记表：stop2/th2 已经退场，而 _ex_pump/_ex_stop 现在指向
        # 重开出来的那根新泵；_stop_pump 会把它的 stop 事件摘掉，让它活着等下去。
        th2.join(timeout=1.0)
    finally:
        e._create_exclusive_stream = orig_create
        with e._ex_lock:
            e._ex = None
        _restore_ws(orig)
        e.stop()


def test_exclusive_fixed_rate_reuse():
    """固定档：不同源率/位深的曲子必须协商出**同一份**施工图 ⇒ 换曲只复用、不重开。

    这是「切歌一声」的源头修法：换率那条路必须 Release→Initialize，驱动/DAC 换时钟那
    一下在部分声卡上就是一声轻咔嗒，样本淡出够不着。固定档让流从头到尾不换格式，
    根本没有那一下。这条尺有牙：把 `_negotiate_exclusive` 里固定档的 `0 if not
    follow_source else …` 改回「总按源率」，第二条（复用键相等）与真调用链那条一齐翻红
    （前者 96000≠48000，后者 created=1 走了重开）。
    """
    e = audioplay.get()
    e.stop()
    p = first_real_audio_path()
    if not p:
        check("固定档复用：库里有可播文件", False, "no track")
        return
    orig = _with_shim(_ShimWS())
    orig_create = e._create_exclusive_stream
    try:
        ok1, why1, r1, i1 = e._negotiate_exclusive(96000, 2, None, 16, 44100,
                                                   follow_source=False)
        ok2, why2, r2, i2 = e._negotiate_exclusive(96000, 2, None, 24, 48000,
                                                   follow_source=False)
        check("固定档：44.1k 源也开到请求的 96000（不跟源跑）",
              ok1 and r1 == 96000 and i1["rate"] == 96000,
              "%s %s %s" % (r1, i1.get("tag"), why1))
        check("固定档：16/44.1 与 24/48 两首的复用键全项相等（换曲不会 Release）",
              ok1 and ok2 and all(i1.get(k) == i2.get(k) for k in e._EX_REUSE_KEYS),
              json.dumps({k: (i1.get(k), i2.get(k)) for k in e._EX_REUSE_KEYS},
                         ensure_ascii=False))
        check("固定档：源率≠设备率仍如实标重采样、bit-perfect 不冒充",
              i1["resampled_by_device"] is True and i1["device_bit_perfect"] is False
              and i1["source_rate"] == 44100,
              json.dumps({k: i1.get(k) for k in ("resampled_by_device",
                                                 "device_bit_perfect")},
                         ensure_ascii=False))
        # 真调用链：手里已有一条 96k 流，换一首 48k 的曲子 ⇒ 必须复用同一条流
        st = _FakeStream(period=960)
        _arm(e)
        with e._ex_lock:
            e._ex = st
            e._ex_params = dict(i1)
        _start_pump(e)
        created = []

        def rec(p_):
            s = _FakeStream(period=960)
            created.append(p_)
            with e._ex_lock:
                e._ex = s
                e._ex_params = p_
            return {"mode": "exclusive", "period_frames": 960}
        e._create_exclusive_stream = rec
        r = e.play(p, exclusive=True, rate=44100, ch=2, exclusive_rate=96000,
                   exclusive_follow_source=False,
                   source_bits=24, source_rate=48000, source_ch=2)
        with e._ex_lock:
            cur, params = e._ex, e._ex_params
        check("固定档切歌（48k 源）⇒ 流没被放掉、一次 Initialize 都没有",
              st.closed is False and cur is st and not created,
              "closed=%s same=%s created=%s" % (st.closed, cur is st, created))
        check("固定档切歌 ⇒ 仍报 exclusive/ok，且施工图记的是这一首的源参数",
              r.get("mode") == "exclusive" and r.get("ok") is not False
              and (params or {}).get("source_rate") == 48000,
              json.dumps({k: r.get(k) for k in ("ok", "mode", "note")},
                         ensure_ascii=False))
        _wait_until(lambda: not e._switch_fade, 1.0)
    finally:
        e._create_exclusive_stream = orig_create
        with e._ex_lock:
            e._ex = None
        _restore_ws(orig)
        e.stop()


def test_exclusive_reuse_race_falls_back():
    """判据放行之后泵自己退了（设备掉了/出流错了）⇒ 必须改走重开，而不是「复用」一条不存在的流。

    这条尺有牙：把 play() 里 `alive` 那次复核删掉（无条件写 _ex_params），第一条翻红
    （created 为空：没有任何流在收数，界面却报 exclusive）；把重开前的 `_last_out = None`
    删掉，第二条翻红（新流第一拍会从静音里放出全幅旧尾音，比硬切还响）。
    """
    e = audioplay.get()
    e.stop()
    p = first_real_audio_path()
    if not p:
        check("复用竞态：库里有可播文件", False, "no track")
        return
    orig = _with_shim(_ShimWS())
    orig_can = e._can_reuse_exclusive
    orig_create = e._create_exclusive_stream
    try:
        ok, why, rate, info = e._negotiate_exclusive(44100, 2, None, 16, 44100)
        check("复用竞态前置：协商拿得到 44.1k/16 的施工图", ok, why)
        st = _FakeStream(period=441)
        _arm(e)
        with e._ex_lock:
            e._ex = st
            e._ex_params = dict(info)
        with e._lock:
            e._last_out = np.array([0.5, -0.5], np.float32)
            e._switch_fade = False
        created = []

        def rec(p_):
            s = _FakeStream(period=441)
            created.append(p_)
            with e._ex_lock:
                e._ex = s
                e._ex_params = p_
            return {"mode": "exclusive", "period_frames": 441}

        def dying(_info):
            # 判据刚放行、还没来得及挂参数，泵就在这一刻自己退了——不是测试假想：
            # 设备拔掉/出流错误都会走 _close_exclusive，而它清的就是 _ex。
            with e._ex_lock:
                e._ex = None
            return True

        e._can_reuse_exclusive = dying
        e._create_exclusive_stream = rec
        try:
            r = e.play(p, exclusive=True, rate=44100, ch=2,
                       source_bits=16, source_rate=44100, source_ch=2)
        finally:
            e._can_reuse_exclusive = orig_can
            e._create_exclusive_stream = orig_create
        with e._ex_lock:
            cur = e._ex
        with e._lock:
            fade = e._switch_fade
            last = e._last_out
        check("泵在判据后死掉 ⇒ 真的重开了一条流（而不是复用个 None）",
              len(created) == 1 and cur is not None and cur is not st,
              "created=%s cur=%r" % (len(created), cur))
        check("重开前把旧尾巴作废（新流第一拍不许从静音里放出全幅旧尾音）",
              fade is False and last is None,
              "fade=%s last=%r" % (fade, None if last is None else last.tolist()))
        check("这次 play 仍报 exclusive 且 ok（复用只省一次开流，不是成败条件）",
              r.get("ok") is not False and r.get("mode") == "exclusive",
              json.dumps({k: r.get(k) for k in ("ok", "mode", "note")}, ensure_ascii=False))
        check("已经死掉的旧流不再被关第二次", st.closed is False, "closed=%s" % st.closed)
    finally:
        e._can_reuse_exclusive = orig_can
        e._create_exclusive_stream = orig_create
        with e._ex_lock:
            e._ex = None
        _restore_ws(orig)
        e.stop()


def test_exclusive_release_drains_fade():
    """放流前必须把 4 拍缓冲里的淡出真放完：只等一拍就 Stop＝淡出让 Release 截在半截。

    这条尺有牙：`_drain_fadeout` 里去掉 `_drain_periods` 调用（老写法），waits 从 5
    掉回 2，第二条翻红；把淡出换成直接静音，第一条翻红。
    """
    e = audioplay.get()
    e.stop()
    orig = _with_shim(_ShimWS())
    try:
        _arm(e)
        st = _FakeStream(period=441, buffer_frames=4 * 441)
        with e._lock:
            e._last_out = np.array([0.6, -0.6], np.float32)
        e._drain_fadeout(st)
        tail = st.writes[-1][-4:] if st.writes else b""
        vals = np.frombuffer(tail, "<i2") if len(tail) == 4 else np.array([9])
        check("放流前投了淡出尾巴，且末帧为 0（不是截在非零样本上）",
              st.n_data >= 1 and int(np.max(np.abs(vals))) == 0,
              "writes=%s tail=%s" % (st.n_data, list(vals)))
        check("交还前等够一个完整缓冲（对齐 1 拍 + 排水 4 拍），不是只等一拍",
              st.waits >= 5, "waits=%d" % st.waits)
    finally:
        _restore_ws(orig)
        e.stop()


def test_switch_fade_uses_waveform():
    """切歌淡出必须用上一拍**真实波形倒放**，不是「末帧当直流收 0」。

    直流斜坡本身就是一段 10ms 的低频能量（听感＝很轻的一声「噗」）；波形淡出起点
    恰等于已播出的末帧（值连续），频谱留在音乐带内。这条尺有牙：`_fade_out_block`
    忽略 wave、退回 last 的直流斜坡，第一条 allclose 当场翻红。
    """
    e = audioplay.get()
    e.stop()
    orig = _with_shim(_ShimWS())
    try:
        _arm(e)
        n, ch = 441, 2
        t = np.arange(n, dtype=np.float32)
        wave = np.stack([0.5 * np.sin(2 * np.pi * 20 * t / n)] * ch, axis=1).astype(np.float32)
        with e._lock:
            e._last_out = wave[-1].copy()
            e._last_tail = wave.copy()
        st = _FakeStream(rate=44100, ch=2, bits=32, container=32, period=n, pace=0,
                         buffer_frames=4 * n)
        e._drain_fadeout(st)
        got = np.frombuffer(st.writes[-1], dtype="<f4").reshape(-1, 2)
        ramp = np.linspace(1.0, 0.0, n, dtype=np.float32)[:, None]
        want = (wave[::-1] * ramp).astype(np.float32)
        check("淡出块＝上一拍波形倒放×斜坡（不是一条直流斜坡）",
              got.shape == want.shape and bool(np.allclose(got, want, atol=2e-4)),
              "maxdiff=%.5f" % (float(np.max(np.abs(got - want))) if got.shape == want.shape else -1))
        check("淡出首帧＝已播出的末帧（值连续，不引入新阶跃）",
              abs(float(got[0, 0]) - float(wave[-1, 0])) < 1e-4,
              "first=%.5f last=%.5f" % (got[0, 0], wave[-1, 0]))
        check("淡出末帧≈0（Stop 不截在非零样本）",
              abs(float(got[-1, 0])) < 1e-5, "last=%.6f" % float(got[-1, 0]))
    finally:
        _restore_ws(orig)
        e.stop()


def test_exclusive_eof_grace():
    """自然播完不立刻交还设备：留一段复用窗口，宽限期内每拍继续投静音。

    没有这段宽限，每首歌自然放完都要 Release→Acquire 一次，用户听到的是
    「每首结束都响一下」。这条尺有牙：`_EX_EOF_GRACE_S` 改 0，前两条翻红。
    """
    e = audioplay.get()
    e.stop()
    orig = _with_shim(_ShimWS())
    try:
        st = _FakeStream(period=441)
        _arm(e, eof=True)
        with e._ex_lock:
            e._ex = st
            e._ex_params = {"rate": 44100, "ch": 2, "bits": 16, "container": 16,
                            "period_hns": 100000, "device_id": "{x}", "dither": False}
        stop, th = _start_pump(e)
        closed_soon = _wait_until(lambda: st.closed, 0.4)
        with e._ex_lock:
            cur = e._ex
        check("曲尾宽限期内：流没放、泵还活着（不再每首结束都硬切一次）",
              closed_soon is None and st.closed is False and th.is_alive() and cur is st,
              "closed=%s alive=%s closed_after=%s" % (st.closed, th.is_alive(), closed_soon))
        _wait_until(lambda: st.n_silence >= 2, 1.0)
        check("宽限期每拍都有投递（静音占拍；独占空一拍＝真 glitch）",
              st.n_silence >= 2 and st.closed is False,
              "silence=%s closed=%s" % (st.n_silence, st.closed))
        # 模拟下一首在同一条流上接上：清掉 ended、喂货、放开闸门。
        with e._lock:
            e._ended = False
            e._paused = False
            e._eof = False
            e._prime_frames = 0
            e._priming = False
        _feed_ring(e, 60, period=441)
        got = _wait_until(lambda: st.n_data > 0, 1.0)
        check("宽限期内下一首接上 ⇒ 同一条流开始投数据（没有重开）",
              got is not None and st.closed is False and st.n_data > 0,
              "n_data=%s closed=%s 等了=%s" % (st.n_data, st.closed, got))
        check("复用后的投递仍是整周期", not st.bad_sizes, str(st.bad_sizes[:4]))
        _stop_pump(e, stop, th)
    finally:
        with e._ex_lock:
            e._ex = None
        _restore_ws(orig)
        e.stop()


class _FakeShared:
    """假共享流：只回答「有没有被关掉」，不碰 PortAudio。"""

    def __init__(self):
        self.closed = False

    def stop_stream(self):
        self.closed = True

    def close(self):
        self.closed = True


def test_shared_stream_reuse():
    """共享档同一台/同一率切歌 ⇒ 流不重开（省掉 PortAudio 的关/开空隙）。

    共享档重开没有独占那种硬件硬切，这条不声称「共享档有爆音」；它只证明复用路径
    真的没去 stop/open，以及切歌过桥确实挂上了。把 `_can_reuse_shared` 写死 False，
    第一条即翻红。
    """
    e = audioplay.get()
    e.stop()
    p = first_real_audio_path()
    if not p:
        check("共享复用：库里有可播文件", False, "no track")
        return
    fake = _FakeShared()
    opened = []
    orig_open = e._open_stream
    e._open_stream = lambda rate, ch, idx: opened.append((rate, ch, idx))
    try:
        with e._lock:
            e._stream = fake
            e._pa = object()
            e._mode = "shared"
            e._rate, e._ch = 44100, 2
            e._shared_device_index = None
            e._ended = False
            e._paused = False
            e._path = p
            e._last_out = np.array([0.5, 0.5], np.float32)
            e._switch_fade = False
        r = e.play(p, exclusive=False, rate=44100, ch=2)
        with e._lock:
            st_now, fade = e._stream, e._switch_fade
        check("共享档同档切歌 ⇒ 没走 _open_stream（流没被关掉再开）",
              st_now is fake and not opened and fake.closed is False,
              "same=%s opened=%s closed=%s" % (st_now is fake, opened, fake.closed))
        check("共享档复用 ⇒ 档位仍是 shared", r.get("mode") == "shared", str(r.get("mode")))
        check("共享档切歌过桥已挂上（第一拍先淡出旧声，不硬切）", fade is True, str(fade))
    finally:
        with e._lock:
            e._stream = None
            e._pa = None
        e._open_stream = orig_open
        e.stop()


def _bp(e, info=None):
    with e._lock:
        return e._bit_perfect_locked(info if info is not None else _bp_info(e))


def _bp_info(e):
    """默认那份协商结果：**源侧的键一个都不放**。
    判据读的是 `p.get("source_rate") or self._source_rate`，默认里写上 source_rate=e._rate
    就等于把「源侧三条 mutate」全变成 NOP —— 测试改引擎字段，判据却只看传入的字典，
    于是红了也不知道是判据没牙还是量具把被测项短路了。留空才走 fallback，
    这一组源侧尺量的才是「引擎里那个数没量到时界面敢不敢点头」。"""
    return {"device_bit_perfect": True, "rate": e._rate, "bits": 16, "ch": e._ch}


def test_bit_perfect_knobs():
    """bit-perfect 必须是**逐条算出来的结论**，不是「勾了独占」这三个字的同义词。"""
    e = audioplay.get()
    e.stop()
    _arm(e)
    ok0, why0 = _bp(e)
    check("全直通＋同率同位深＋独占开着 ⇒ 才敢说 bit-perfect", ok0 is True and not why0,
          str(why0))
    cases = [
        ("音量 0.8（每个样本都乘过一遍，已经不是源数据了）",
         lambda: setattr(e, "_gain", 0.8), "音量", None),
        ("声像偏了一边", lambda: setattr(e, "_balance", 0.4), "声像", None),
        ("响度均衡开着", lambda: setattr(e, "_loudness", True), "响度", None),
        ("虚拟扬声器（交叉馈电）开着", lambda: setattr(e, "_speakers", True),
         "虚拟扬声器", None),
        ("倍速 1.5×", lambda: setattr(e, "_atempo", 1.5), "倍速", None),
        ("起播淡入 0.5s（头半秒被 ramp 过）",
         lambda: setattr(e, "_fade_in_sec", 0.5), "淡入", None),
        ("源 44.1k 而设备开在 96k（重采样）",
         lambda: None, "重采样",
         {"device_bit_perfect": True, "rate": 96000, "source_rate": 44100, "bits": 16}),
        ("共享档（压根没独占）", lambda: setattr(e, "_mode", "shared"), "共享", None),
        # ---- 源侧「没量到」这一家：不是「有损失」，是「不确定」。不确定也不许点头，
        #      因为界面那句 bit-perfect 是要给用户当结论用的（api 里 0＝不知道，不是 0 位）。
        ("源位深没量到（bit_depth=0）", lambda: setattr(e, "_source_bits", 0), "位深", None),
        ("源采样率没量到", lambda: setattr(e, "_source_rate", 0), "采样率", None),
        ("源 32 位（float32 链路尾数只有 24 位）",
         lambda: setattr(e, "_source_bits", 32), "32 位", None),
        ("源 5.1 而输出只有 2 声道（下混）",
         lambda: setattr(e, "_source_ch", 6), "声道", None),
    ]
    for name, mutate, kw, info in cases:
        e.stop()
        _arm(e)
        if mutate:
            mutate()
        if info is None:
            info = _bp_info(e)
        ok, why = _bp(e, info)
        check("bit-perfect 判据抓得住：%s" % name,
              ok is False and any(kw in r for r in why), "%s -> %s" % (ok, why))
    # 施工图说「没量到」、引擎字段还挂着上一首的 24 —— 这种时候必须听施工图的。
    # 用 `p.get(k) or self._k` 读的话 0 会被 or 短路掉，界面就把「不确定」报成「24 位、没问题」；
    # 这一条是给**读法本身**立的尺（把 src() 换回 or 短路，只有这里翻红）。
    e.stop()
    _arm(e)
    with e._lock:
        e._source_bits, e._source_ch = 24, 6
    ok, why = _bp(e, {"device_bit_perfect": True, "rate": e._rate, "bits": 16, "ch": 2,
                      "source_bits": 0, "source_rate": 44100, "source_ch": 0})
    check("施工图明写 source_bits=0/source_ch=0 ⇒ 不许拿引擎里上一首的残值当答案",
          ok is False and any("没量到" in r for r in why)
          and not any("声道" in r for r in why), str(why))
    # EQ 那一条单独走（要的是「十段里真的挂了几节」这个真值，不是开关位）
    e.stop()
    _arm(e)
    with e._lock:
        e._sos = audioplay._sos_eq([6, 0, 0, 0, 0, 0, 0, 0, 0, 0], 44100)
    ok, why = _bp(e)
    check("bit-perfect 判据抓得住：均衡器挂着二阶节", ok is False and "均衡器" in "".join(why),
          str(why))
    e.stop()


def test_spectrum_live_in_exclusive():
    """独占档下 self._stream 恒为 None —— 旧写法会让频谱**一直是一条直线**。
    这条尺给一对反向哨兵：同一份数据，_ex 在场要出数，_ex 不在场要全零。"""
    e = audioplay.get()
    e.stop()
    orig = _with_shim(_ShimWS())
    try:
        _arm(e)
        with e._lock:
            e._spec[:] = np.sin(np.arange(2048) * 0.1).astype(np.float32)
            e._spec_pos = 0
            e._paused = False
        st = _FakeStream(period=441)
        with e._ex_lock:
            e._ex = st
        live = e.spectrum(24)
        with e._ex_lock:
            e._ex = None
        dead = e.spectrum(24)
        check("独占档（_stream 为 None 但 _ex 在场）频谱出数", max(live) > 0.01,
              "max=%s" % max(live))
        check("两条流都不在场时仍是全零（反向哨兵：没这条，上一条可以是恒绿）",
              max(dead) == 0.0, "max=%s" % max(dead))
    finally:
        _restore_ws(orig)
        e.stop()


def test_exclusive_mutation_is_caught():
    """把独占侧的三处判据注坏，要求对应断言翻红；注坏还全绿＝这条尺是瞎的。"""
    e = audioplay.get()
    e.stop()
    orig = _with_shim(_ShimWS())
    try:
        # 变异 A：priming 那家也记 underrun（当年「累计丢帧 7629」就是这么来的）
        _pull_good = audioplay.NativeEngine._pull_locked

        def buggy_pull(self, frames):
            out, filled, kind = _pull_good(self, frames)
            if kind == "priming":
                # **不在这再取一次 self._lock**：_pull_locked 的契约是「调用方已持锁」，
                # 而 self._lock 是普通 Lock（不可重入），变异体里补一记锁就是当场自锁死——
                # 本套第一跑就挂在这里 25 秒没吐一行日志（挂死不是抓到，是量具自己坏了）。
                self._underruns += 1
            return out, filled, kind
        audioplay.NativeEngine._pull_locked = buggy_pull
        try:
            _arm(e, priming=True)
            with e._lock:
                e._prime_frames = 1000
                e._prime_until = time.monotonic() + 5.0
                e._frames_avail = 10
                _o, _f, kind = e._pull_locked(441)
                und = e._underruns
        finally:
            audioplay.NativeEngine._pull_locked = _pull_good
        check("变异(把灌数闸门记成丢帧)被『priming 不记 underrun』抓到",
              kind == "priming" and und == 1, "kind=%s und=%s" % (kind, und))

        # 变异 B：投递按「实取帧数」而不是整周期（独占下这就是把半截周期塞给驱动）
        st = _FakeStream(period=441)
        e.stop()
        _arm(e)
        _feed_ring(e, 1, period=441)
        with e._lock:
            out, filled, _k = e._pull_locked(441)
        short = out[:filled // 2]                     # 注坏版：只投半截
        want = st.period_frames * st.wf.Format.nBlockAlign
        got = len(W.frames_to_device(short, st.bits, st.container))
        # 判据用的是**泵里那把尺本身**（_FakeStream._size_ok），不是我另算一遍：
        # 另算的话「441 帧是奇数、折半得 880 而整周期 1764」这种边角就会把我自己的
        # 算术错记成被测代码的错（本套第一跑正是 got*2==want 差 4 个字节翻红）。
        st._size_ok(got)
        check("变异(投半截周期)在『字节数必须正好一个周期』这把尺下要露出来",
              got != want and st.bad_sizes == [(got, want)],
              "got=%s want=%s bad=%s" % (got, want, st.bad_sizes))
        check("而按整周期投时那把尺是绿的（反向哨兵：上一条可以是恒红）",
              len(W.frames_to_device(out, st.bits, st.container)) == want,
              str(len(W.frames_to_device(out, st.bits, st.container))))
        st.bad_sizes = []
        st._size_ok(want)
        check("把整周期喂进同一把尺 ⇒ 不记 bad（尺本身不恒红）",
              not st.bad_sizes, str(st.bad_sizes))

        # 变异 C：bit_perfect 只看「是不是独占」不看音量 ⇒ 音量 0.8 也说 bit-perfect
        orig_bp = audioplay.NativeEngine._bit_perfect_locked

        def weak(self, p):
            return (self._mode == "exclusive"), []
        audioplay.NativeEngine._bit_perfect_locked = weak
        try:
            e.stop()
            _arm(e)
            with e._lock:
                e._gain = 0.8
                ok_weak, why_weak = e._bit_perfect_locked(
                    {"device_bit_perfect": True, "rate": 44100, "source_rate": 44100})
            e._gain = 1.0
            with e._lock:
                ok_strong, why_strong = e._bit_perfect_locked(
                    {"device_bit_perfect": True, "rate": 44100, "source_rate": 44100})
                e._gain = 0.8
                ok_real, why_real = orig_bp(e, {"device_bit_perfect": True, "rate": 44100,
                                                "source_rate": 44100})
        finally:
            audioplay.NativeEngine._bit_perfect_locked = orig_bp
        check("变异(bit-perfect 只看独占不看音量)被抓到：注坏版在 0.8 时也报 True",
              ok_weak is True and ok_real is False and "音量" in "".join(why_real),
              "weak=%s real=%s %s" % (ok_weak, ok_real, why_real))
        check("且这组对照不是 NOP：同一个实例同一份参数，摘掉音量那条才会翻面",
              ok_weak != ok_real, "%s vs %s" % (ok_weak, ok_real))
    finally:
        _restore_ws(orig)
        e.stop()


# ================================================================== 真峰值限幅 / 下混
def _tp_peak(x, ratio=8, rate=96000, guard_ms=50):
    """独立量具：用 resample_poly（FIR 多相）估真峰，故意不走限幅器那套 Catmull-Rom。

    两边各留 50 ms 保护带再取中间：直接对整段做多相重采样，切片两端的零填充会把
    FIR 振铃算成「真峰」（纯 0.98 正弦在 15 kHz 上会被量成 1.02+，是量具的假阳性）。
    中间段对应的索引按保护带换算，不凭感觉裁。"""
    from scipy import signal as _signal
    x = np.asarray(x, np.float64)
    n = x.shape[0]
    g = int(rate * guard_ms / 1000)
    lo, hi = min(g, n), max(min(g, n), n - g)
    if hi <= lo:
        return float(np.max(np.abs(x))) if x.size else 0.0
    y = _signal.resample_poly(x[lo - g:hi + g], ratio, 1, axis=0)
    a, b = g * ratio, g * ratio + (hi - lo) * ratio
    seg = y[int(a):int(b)]
    return float(np.max(np.abs(seg))) if seg.size else 0.0


def _engine_limiter(rate=96000, ch=2):
    e = audioplay.get()
    _arm(e, rate=rate, ch=ch)
    e._lim_on, e._lim = True, None
    return e


def _run_blocks(e, x, sizes):
    outs, pos, i = [], 0, 0
    while pos < x.shape[0]:
        n = sizes[i % len(sizes)]; i += 1
        outs.append(e._process_block(x[pos:pos + n]))
        pos += n
    return np.concatenate(outs, axis=0) if outs else np.zeros((0, x.shape[1]), np.float32)


def test_limiter_transparency_and_chunking():
    """低电平输入必须「只延迟、不染色」，而且结果与分块大小无关。

    这条同时钉两件事：① 没超 ceiling 的段落乘的是精确 1.0，逐位不变；② tp 历史/
    延迟线/切片在任意块边界上对齐（首块 1 帧、次块 2 帧都在里面），不会因块大小
    变出不同的声音。"""
    e = _engine_limiter()
    rng = np.random.default_rng(20261026)
    n = 96000 * 2
    x = (rng.standard_normal((n, 2)) * 0.2).astype(np.float32)
    sizes = [1, 2, 3, 5, 8, 13, 257, 1024, 4096, 97]
    y = _run_blocks(e, x, sizes)
    D = e._lim.D
    check("限幅器：低电平 2 秒流输出长度不变", y.shape == x.shape, "%s vs %s" % (y.shape, x.shape))
    check("限幅器：前 D 帧是延迟线里的零（D=%d）" % D,
          float(np.max(np.abs(y[:D]))) == 0.0,
          "peak=%g" % float(np.max(np.abs(y[:D]))))
    check("限幅器：D 帧之后与源逐位相同（透明，不是『差不多』）",
          np.array_equal(y[D:], x[:n - D]),
          "maxdiff=%g" % float(np.max(np.abs(y[D:] - x[:n - D]))))
    st = e._lim.stats()
    check("限幅器：低电平段一次都没压（max_gr_db=0、demand=0）",
          abs(st["max_gr_db"]) < 1e-12 and st["demand_pct"] == 0.0,
          json.dumps(st, ensure_ascii=False))

    # 分块不变性：同样一条流，用「整块一次喂」再跑一遍，输出必须逐位相同。
    e2 = _engine_limiter()
    y2 = _run_blocks(e2, x, [n])
    check("限幅器：任意分块与整块处理逐位相同（块边界不串味）",
          np.array_equal(y, y2), "maxdiff=%g" % float(np.max(np.abs(y - y2))))
    _arm(e, rate=96000)          # 清场：单例上不留 _lim_on/_lim 给下一条测试


def test_limiter_hot_signal():
    """热信号：真峰 1.17 的正弦过限幅器后，样点峰 ≤ ceiling、且没有一个样点被削平。
    对照：同一信号走旧 np.clip，会出现成片的 ±1.0 平顶。"""
    e = _engine_limiter()
    rate = 96000
    t = np.arange(rate * 2, dtype=np.float64) / rate
    base = (1.17 * np.sin(2.0 * np.pi * 997.0 * t)).astype(np.float32)
    x = np.stack([base, base * np.float32(0.8)], axis=1)
    y = _run_blocks(e, x, [1024])
    st = e._lim.stats()
    peak = float(np.max(np.abs(y)))
    flat = int(np.count_nonzero(np.abs(y) >= 0.9999))
    tp = _tp_peak(y)
    check("限幅器：1.17 热正弦输出样点峰 ≤ 0.9801", peak <= 0.9801, "peak=%.6f" % peak)
    check("限幅器：输出没有一个样点贴近满刻度（没有硬削平顶）", flat == 0, "flat=%d" % flat)
    check("限幅器：独立多相重采样估出的真峰也回到 ceiling 附近", tp <= 0.99,
          "tp=%.6f" % tp)
    check("限幅器：这段真压了（max_gr_db>1dB，demand>99%）",
          st["max_gr_db"] > 1.0 and st["demand_pct"] > 99.0,
          json.dumps(st, ensure_ascii=False))
    check("限幅器：记下的输入峰≈1.17（不是拿输出的 0.98 冒充输入）",
          abs(st["input_peak"] - 1.17) < 0.01 and st["output_peak"] <= 0.9801,
          "in=%.4f out=%.4f" % (st["input_peak"], st["output_peak"]))

    old = np.clip(x, -1.0, 1.0)
    old_flat = int(np.count_nonzero(np.abs(old) >= 0.9999))
    old_tp = _tp_peak(old)
    check("对照（旧 np.clip）：同一信号出现成片平顶、真峰仍 >1 —— 这才是要被替掉的行为",
          old_flat > 1000 and old_tp > 1.0,
          "flat=%d tp=%.4f" % (old_flat, old_tp))

    # 变异：把 process 换成旧 np.clip，上面的两条判据必须当场翻红。这不是 NOP：
    # 同一个输入、同一组断言，只换处理函数。
    orig = audioplay.TruePeakLimiter.process

    def mutated(self, arr):
        return np.clip(np.asarray(arr, np.float32), -1.0, 1.0)
    audioplay.TruePeakLimiter.process = mutated
    try:
        e._lim = None
        bad = _run_blocks(e, x, [1024])
    finally:
        audioplay.TruePeakLimiter.process = orig
    bad_flat = int(np.count_nonzero(np.abs(bad) >= 0.9999))
    check("变异(限幅器换成硬 clip)被『无平顶』+『峰≤0.9801』同时抓到",
          bad_flat > 1000 and float(np.max(np.abs(bad))) == 1.0,
          "flat=%d peak=%.4f" % (bad_flat, float(np.max(np.abs(bad)))))
    _arm(e, rate=96000)


def test_downmix_trim_decision_and_order():
    """下混预留不再是「所有 5.1 一律 −6 dB」，而是按**这一首实测真峰**只扣到刚好：

      · 没量到真峰          → 兜底 0.5011872336（−6.00 dB，改版前行为，宁可轻不许削）；
      · 真峰 1.6973（1115） → 0.93/1.6973 = 0.5479（−5.23 dB）；
      · 真峰 1.1848（1704） → 0.7849（−2.10 dB）；
      · 真峰 0.8397（1694） → 1.0（一个样本都不动）；
      · 立体声源 / 非立体声输出 → 恒 1.0。

    为什么必须按真峰：同一个库里 Atmos bed 的峰只有 0.84，一刀切 −6 dB 会把整组
    白压轻 16 dB（tools/_level_parity.log）——听感上就是「不如微软播放器清晰」。
    也验余量乘在限幅器之前、且实测路径不会让限幅器介入。
    """
    trim = audioplay.DOWNMIX_TRIM
    # 兜底常量的真值是 10^(-6/20)=0.5011872336，即 −6.00 dB —— 不是 −6.02（0.5）。
    # 这条以前写成 −6.02 却按 0.01 容差比，改版前一直侥幸绿；现在按真值钉死，
    # 也防止有人把常量换成 0.5 却假装「行为没变」（差 0.02 dB，听不出，但账要真）。
    check("下混兜底：没量到真峰 = −6.00 dB（与改版前逐值一致）",
          abs(audioplay.downmix_trim_for(2, 6) - trim) < 1e-12
          and abs(20 * np.log10(trim) + 6.0) < 1e-6,
          "trim=%.10f dB=%.6f" % (trim, 20 * np.log10(trim)))
    check("下混实测：真峰 1.6973 → −5.23 dB（比兜底少扣 0.79 dB）",
          abs(20 * np.log10(audioplay.downmix_trim_for(2, 6, 1.6973)) + 5.226) < 0.02,
          "%.4f dB" % (20 * np.log10(audioplay.downmix_trim_for(2, 6, 1.6973))))
    check("下混实测：真峰 1.1848 → −2.10 dB",
          abs(20 * np.log10(audioplay.downmix_trim_for(2, 6, 1.1848)) + 2.096) < 0.02,
          "%.4f dB" % (20 * np.log10(audioplay.downmix_trim_for(2, 6, 1.1848))))
    check("下混实测：真峰 0.8397 不超 target → 1.0（一个样本都不动）",
          audioplay.downmix_trim_for(2, 6, 0.8397) == 1.0)
    check("下混实测：乘完的真峰 ≤ TARGET_TP（0.93）",
          all(audioplay.downmix_trim_for(2, 6, tp) * tp <= 0.93 + 1e-9
              for tp in (0.94, 1.0, 1.18, 1.4, 1.7, 2.2)),
          "tps 0.94..2.2")
    check("下混实测：离谱读数最多扣 12 dB（MIN_GAIN=0.25）",
          audioplay.downmix_trim_for(2, 6, 9.9) == 0.25,
          "%.4f" % audioplay.downmix_trim_for(2, 6, 9.9))
    check("下混余量：立体声/单声道源不扣（1.0，整段乘法被跳过）",
          audioplay.downmix_trim_for(2, 2) == 1.0 and audioplay.downmix_trim_for(2, 1) == 1.0
          and audioplay.downmix_trim_for(2, 0) == 1.0)
    check("下混余量：输出不是立体声时不扣（不做没来源的衰减）",
          audioplay.downmix_trim_for(6, 6) == 1.0 and audioplay.downmix_trim_for(1, 6) == 1.0)

    e = _engine_limiter()
    e._downmix_trim = audioplay.downmix_trim_for(2, 6)
    x = np.full((4800, 2), 1.6973, np.float32)
    y = e._process_block(x)
    check("下混兜底路径：1.6973 的热下混块先变 0.8506，限幅器一次都没介入",
          abs(float(np.max(np.abs(y))) - 0.8506) < 0.002 and e._lim.stats()["max_gr_db"] == 0.0,
          "peak=%.4f gr=%.4f" % (float(np.max(np.abs(y))), e._lim.stats()["max_gr_db"]))
    _arm(e, rate=96000)

    e = _engine_limiter()
    e._downmix_trim = audioplay.downmix_trim_for(2, 6, 1.6973)
    e._downmix_basis, e._source_tp, e._source_ch = "measured", 1.6973, 6
    # 探针不能用「整块常值」：从 0 一步跳到 0.93 是矩形阶跃，限幅器的 FIR 真峰尺
    # 在阶跃沿上会合法地振铃超 ceiling（实测 GR 0.135 dB），那是探针的假象，不是
    # 余量没生效。1 kHz 正弦从零相位起、幅度正是下混后的 1.6973×trim=0.93，
    # 没有阶跃、真峰就是 0.93 < 0.98 —— 这才是「实测路径无需限幅器介入」的正确证法。
    n = 4800
    t = np.arange(n) / 96000.0
    sine = (1.6973 * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)
    y = e._process_block(np.repeat(sine[:, None], 2, axis=1))
    check("下混实测路径：同样 1.6973 只落到 0.9299（比兜底响 0.79 dB），限幅器仍不介入",
          abs(float(np.max(np.abs(y))) - 0.93) < 0.003 and e._lim.stats()["max_gr_db"] == 0.0,
          "peak=%.4f gr=%.4f" % (float(np.max(np.abs(y))), e._lim.stats()["max_gr_db"]))
    st = e.state()
    check("state().downmix_basis/source_tp：把「这份预留怎么来的」如实回显",
          st.get("downmix_basis") == "measured" and abs(float(st.get("source_tp") or 0) - 1.6973) < 1e-6
          and abs(float(st.get("downmix_trim_db") or 0) + 5.226) < 0.02,
          json.dumps({k: st.get(k) for k in ("downmix_basis", "source_tp", "downmix_trim_db")},
                     ensure_ascii=False))
    _arm(e, rate=96000)


def test_limiter_off_keeps_clip():
    """独占/自检路径不许被新限幅器偷改：_lim_on=False 时仍是逐样点 np.clip。"""
    e = audioplay.get()
    _arm(e, rate=96000)
    e._lim_on, e._lim = False, None
    x = np.full((2048, 2), 1.3, np.float32)
    y = e._process_block(x)
    check("_lim_on=False：仍旧走旧 np.clip（样点被切在 1.0）",
          np.array_equal(y, np.clip(x, -1, 1).astype(np.float32))
          and float(np.max(np.abs(y))) == 1.0,
          "max=%.4f" % float(np.max(np.abs(y))))
    check("_lim_on=False：没有偷偷造一个限幅器实例", e._lim is None)
    _arm(e, rate=96000)


def test_limiter_state_is_truthful():
    """state().limiter 不许拿「开关开着」冒充「真的在压 / 真的处理过」。"""
    e = _engine_limiter()
    _arm(e, rate=96000)
    e._lim_on, e._lim = True, None
    e._mode, e._ex_requested = "shared", False
    st = e.state()
    lim = st.get("limiter") or {}
    check("state().limiter：还没处理过一块时 frames=0（on 只说明接上了）",
          lim.get("on") is True and lim.get("frames") == 0,
          json.dumps(lim, ensure_ascii=False))
    e._process_block(np.full((1024, 2), 0.5, np.float32))
    st2 = e.state()
    lim2 = st2.get("limiter") or {}
    check("state().limiter：处理后 frames=1024 且 output_peak 是真实输出",
          lim2.get("frames") == 1024 and 0.49 < lim2.get("output_peak", 0) <= 0.5001,
          json.dumps(lim2, ensure_ascii=False))
    check("state().downmix_trim_db：立体声源显示 0.0（不是 −6）",
          st2.get("downmix_trim_db") == 0.0, str(st2.get("downmix_trim_db")))
    e._lim_on = False
    lim3 = e.state().get("limiter") or {}
    check("state().limiter：独占档 on=False，且没有冒充读数",
          lim3 == {"on": False}, json.dumps(lim3, ensure_ascii=False))
    _arm(e, rate=96000)


def test_limiter_eof_tail_and_position():
    """限幅器延迟 D 帧：读到尾必须在环里补 D 帧零，否则最后 5 ms 真音频留在延迟线里。
    同时进度要扣掉这 D 帧，不然片尾会报出 duration+5ms。"""
    e = audioplay.get()
    rate, n = 96000, 5000
    _arm(e, rate=rate)
    e._lim_on, e._lim = True, None
    e._base_pos = 10.0
    ramp = np.linspace(-0.4, 0.4, n, dtype=np.float32)[:, None].repeat(2, axis=1)
    with e._lock:
        e._chunks.append(ramp.copy()); e._frames_avail = n
    e._mark_eof(e._gen)
    D = audioplay.latency_frames(rate)
    with e._lock:
        avail = e._frames_avail
        outs = []
        while True:
            out, filled, _kind = e._pull_locked(1024)
            if filled == 0:
                break
            outs.append(out[:filled].copy())
        y = np.concatenate(outs, axis=0) if outs else np.zeros((0, 2), np.float32)
        pos, ended, und = e._pos_src_locked(), e._ended, e._underruns
    check("EOF 补尾：环里多了 D=%d 帧零（5 ms@96k）" % D, avail == n + D, "avail=%d" % avail)
    check("EOF 补尾：输出长度 = 原长 + D，一个真帧都没丢", y.shape[0] == n + D,
          "%d vs %d" % (y.shape[0], n + D))
    check("EOF 补尾：最后 D 帧之后不再是零尾巴，末样点就是源末样点",
          y.shape[0] >= D + n and np.array_equal(y[D:], ramp),
          "last=%s src_last=%s" % (y[-1].tolist(), ramp[-1].tolist()))
    check("EOF 进度：扣掉延迟后正好落在 base + 原长（不越界 5 ms）",
          abs(pos - (10.0 + n / rate)) < 1e-9, "pos=%.9f" % pos)
    check("EOF 收口：ended 立起来、补零段不算丢帧", ended and und == 0,
          "ended=%s und=%d" % (ended, und))

    # 反面对照：同样的环但不补 D 帧零，末样点会停在源的第 N−D−1 个 —— 证明上面那条有牙。
    _arm(e, rate=rate)
    e._lim_on, e._lim = True, None
    with e._lock:
        e._chunks.append(ramp.copy()); e._frames_avail = n
        e._eof = True                       # 绕过 _mark_eof 的补零
        outs2 = []
        while True:
            out, filled, _kind = e._pull_locked(1024)
            if filled == 0:
                break
            outs2.append(out[:filled].copy())
        y2 = np.concatenate(outs2, axis=0)
    check("反面对照（不补 D 帧零）：末尾确实少一截 —— 证明补尾不是装饰",
          y2.shape[0] == n and not np.array_equal(y2[-D:], ramp[-D:]),
          "len=%d last=%s src_last=%s" % (y2.shape[0], y2[-1].tolist(), ramp[-1].tolist()))

    # 代际门：seek 后旧读线程迟到的 _mark_eof 不许往新段补零/置 eof。
    _arm(e, rate=rate)
    e._lim_on, e._lim = True, None
    e._gen = 7
    e._mark_eof(6)
    with e._lock:
        stale_frames, stale_eof = e._frames_avail, e._eof
    check("EOF 代际：旧代际的 _mark_eof 不补零、不把新段判成播完",
          stale_frames == 0 and stale_eof is False,
          "frames=%d eof=%s" % (stale_frames, stale_eof))
    e._mark_eof(7)
    with e._lock:
        fresh_frames, fresh_eof = e._frames_avail, e._eof
    check("EOF 代际：当代 _mark_eof 才补 D 帧零", fresh_frames == D and fresh_eof is True,
          "frames=%d eof=%s" % (fresh_frames, fresh_eof))
    _arm(e, rate=96000)          # 清场：_lim_on/_lim/_eof 都不许留给下一条


def test_anti_click_ramps():
    """切歌「电流声」的两处阶跃都要抹平：起播首样本淡入从 0 爬升；放流前最后一帧淡出收到 0。

    全程纯函数 + 假独占流，不开设备、不出声。这条尺有牙：把 _process_block 的淡入段摘掉，
    「首样本≈0」当场翻红；把 _drain_fadeout 的 ramp 改成恒等（不收到 0），「末帧≈0」翻红。
    """
    e = audioplay.NativeEngine()
    e._rate, e._ch, e._gain = 48000, 2, 1.0
    block = np.full((480, 2), 0.5, np.float32)
    # ① 起播淡入：先证「没有淡入时首样本就是非零阶跃」（爆音源的形状），再证地板把它抹平。
    e._fade_in_frames = 0
    hard = e._process_block(block.copy())
    check("无淡入：首样本是非零阶跃（爆音源形状）", abs(float(hard[0, 0])) > 0.4,
          "first=%.4f" % float(hard[0, 0]))
    e._fade_in_frames = int(e._ANTI_CLICK_S * e._rate)
    soft = e._process_block(block.copy())
    check("淡入地板：首样本被收到≈0", abs(float(soft[0, 0])) < 0.02,
          "first=%.4f" % float(soft[0, 0]))
    check("淡入地板：爬升结束后恢复满幅", abs(float(soft[-1, 0]) - 0.5) < 0.02,
          "last=%.4f" % float(soft[-1, 0]))
    check("淡入地板：单调不降（不是随便乘个系数）",
          bool(np.all(np.diff(soft[:, 0]) >= -1e-6)))
    # ② 放流淡出：假流用 32-in-32 float 直通，便于把投进去的波形读回来量首末帧。
    st = _FakeStream(rate=48000, ch=2, bits=32, container=32, period=480, pace=0)
    e._last_out = np.array([0.6, 0.6], np.float32)
    e._drain_fadeout(st)
    check("淡出投了一个整周期（不溢出缓冲）", st.n_data == 1 and not st.bad_sizes,
          "n_data=%d bad=%s" % (st.n_data, st.bad_sizes))
    tail = np.frombuffer(st.writes[-1], dtype="<f4").reshape(-1, 2)
    check("淡出首帧≈last_out（淡出本身不引入新阶跃）", abs(float(tail[0, 0]) - 0.6) < 0.02,
          "first=%.4f" % float(tail[0, 0]))
    check("淡出末帧≈0（Stop 不再截在非零样本）", abs(float(tail[-1, 0])) < 1e-6,
          "last=%.6f" % float(tail[-1, 0]))
    check("淡出单调不增", bool(np.all(np.diff(tail[:, 0]) <= 1e-6)))
    # ③ last_out 缺失／已是 0 ⇒ 跳过淡出，不无中生有往设备里塞声音。
    st2 = _FakeStream(rate=48000, ch=2, bits=32, container=32, period=480, pace=0)
    e._last_out = None
    e._drain_fadeout(st2)
    check("无 last_out ⇒ 不投淡出", st2.n_data == 0, "n_data=%d" % st2.n_data)
    st3 = _FakeStream(rate=48000, ch=2, bits=32, container=32, period=480, pace=0)
    e._last_out = np.zeros(2, np.float32)
    e._drain_fadeout(st3)
    check("last_out 已为 0 ⇒ 不投淡出", st3.n_data == 0, "n_data=%d" % st3.n_data)


def main():
    # 卡死留栈：这套里有泵线程和几把**不可重入**的锁，真锁死时「没日志」不等于「没跑完」，
    # 而是「跑不完且说不出卡在哪一行」。每 150 秒把所有线程的栈打到 stderr，收尾 cancel。
    # （150 秒 > 单条断言的最长合法耗时：并发枚举那档一次也就几十秒。）
    try:
        import faulthandler
        faulthandler.dump_traceback_later(150.0, repeat=True, exit=False)
    except Exception:
        pass
    test_eq_matches_frontend()
    test_eq_echo()
    test_atempo_chain()
    test_seek_eof_generation()
    test_seek_join_window_eof()
    test_feed_generation_gate()
    test_seek_preserves_pause()
    test_anti_click_ramps()
    test_devices_default_flag()
    test_pa_calls_pinned_to_ctrl_thread()
    test_devices_concurrent()
    test_preview_dsp()
    test_balance_equal_power()
    test_virtual_speaker()
    test_limiter_transparency_and_chunking()
    test_limiter_hot_signal()
    test_downmix_trim_decision_and_order()
    test_limiter_off_keeps_clip()
    test_limiter_state_is_truthful()
    test_limiter_eof_tail_and_position()
    test_ring_take()
    test_position_source_time()
    test_callback_sim()
    test_end_of_track_self_closes()
    # ---- 独占档（离线：全程不开设备，见下面那段的模块注释）
    test_pull_locked_kinds()
    test_exclusive_negotiation_failclosed()
    test_exclusive_negotiation_table()
    test_exclusive_dither_and_view()
    test_wasapi_id_match()
    test_exclusive_thread_ownership()
    test_exclusive_pump()
    test_exclusive_pump_releases()
    test_exclusive_stream_reuse()
    test_exclusive_fixed_rate_reuse()
    test_exclusive_reuse_race_falls_back()
    test_exclusive_release_drains_fade()
    test_switch_fade_uses_waveform()
    test_exclusive_eof_grace()
    test_shared_stream_reuse()
    test_bit_perfect_knobs()
    test_spectrum_live_in_exclusive()
    test_exclusive_mutation_is_caught()
    test_hidden_ffmpeg_window()
    # 变异段的 check 只在「注坏却没被抓到」时才 append 到 FAILS —— 那是真缺陷，不清空。
    test_mutation_is_caught()
    try:
        import faulthandler
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
    print()
    if FAILS:
        print("RESULT: %d 条不合格" % len(FAILS))
        return 1
    print("PASS audioplay self-test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
