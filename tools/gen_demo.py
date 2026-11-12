# -*- coding: utf-8 -*-
"""
gen_demo.py —— 为 LuHui音乐播放器 生成"示例曲库"（端到端验证用）

产物：
  示例曲库/<艺术家>/<专辑>/NN - 标题 (English).wav   8 首，numpy 全合成，44100Hz/16bit/立体声
  同名 .lrc                                          6 首（其中 2 首双语，1 首带 offset）
  专辑 cover.jpg                                     600x600 抽象画（1 张专辑故意不放）
  1 张 track 级同名 .jpg                             测封面优先级

注意（踩坑记录）：
  * mutagen 1.48.1 的 WAVE 只支持 ID3-in-RIFF，没有 RIFF INFO 标签读写；
    所以这里用 mutagen._riff.RiffFile 的 chunk API 自己拼 LIST/INFO 子块，
    由它负责把 RIFF 头部 size 更新正确。
  * RIFF INFO 子块字符串传统上是 NUL 结尾 + 偶数对齐；这里统一按 UTF-8 写入
    （中文在 Python 侧读取无歧义；Windows 资源管理器会按本地代码页显示成乱码，属正常）。
  * 控制台是 GBK，必须先 reconfigure stdout。

只写 tools/gen_demo.py 与 示例曲库/**，不碰项目其它文件。
"""

import os
import re
import sys
import struct
import wave

import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SR = 44100
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIB = os.path.join(ROOT, "示例曲库")

# ------------------------------------------------------------------ 基础工具

def midi_f(m):
    return 440.0 * 2.0 ** ((m - 69) / 12.0)


def t_of(dur):
    return np.arange(int(round(dur * SR)), dtype=np.float64) / SR


def _shape(x, response):
    """离线 FFT 整形（避免 Python 递归滤波循环，全向量化）"""
    n = len(x)
    f = np.fft.rfftfreq(n, 1.0 / SR)
    h = response(f)
    return np.fft.irfft(np.fft.rfft(x) * h, n)


def lp(x, fc, order=4):
    fc = max(float(fc), 40.0)
    return _shape(x, lambda f: 1.0 / np.sqrt(1.0 + (f / fc) ** (2 * order)))


def hp(x, fc, order=2):
    fs = np.where(np.fft.rfftfreq(len(x), 1.0 / SR) == 0, 1e-6,
                  np.fft.rfftfreq(len(x), 1.0 / SR))
    fc = min(float(fc), SR * 0.45)
    h = 1.0 / np.sqrt(1.0 + (fc / fs) ** (2 * order))
    n = len(x)
    return np.fft.irfft(np.fft.rfft(x) * h, n)


def bp(x, f1, f2, order=2):
    return hp(lp(x, f2, order), f1, order)


def white(n, rng):
    return rng.standard_normal(n)


def pink(n, rng):
    """1/f 着色噪声（FFT  shaping，比 Voss 递归便宜）"""
    x = white(n, rng)
    f = np.maximum(np.fft.rfftfreq(n, 1.0 / SR), 1.0)
    h = np.sqrt(200.0 / f)
    h = np.clip(h, 0, 6.0)
    return np.fft.irfft(np.fft.rfft(x) * h, n) * 0.25


def adsr(dur, a=0.01, d=0.08, s=0.6, r=0.15):
    N = max(2, int(round(dur * SR)))
    a_n = max(1, min(int(a * SR), N - 2))
    d_n = max(1, min(int(d * SR), max(1, N - a_n - 2)))
    r_n = max(1, min(int(r * SR), max(1, N - a_n - d_n)))
    s_n = max(0, N - a_n - d_n - r_n)
    e = np.concatenate([
        np.linspace(0.0, 1.0, a_n),
        np.linspace(1.0, s, d_n),
        np.full(s_n, s, dtype=float),
        np.linspace(s, 0.0, r_n),
    ])
    if len(e) > N:
        e = e[:N]
    elif len(e) < N:
        e = np.concatenate([e, np.zeros(N - len(e))])
    return e


def fade(x, ms=6.0):
    n = len(x)
    k = max(1, int(ms * 0.001 * SR * 2) // 2)
    if n > 2 * k:
        x = x.copy()
        x[:k] *= np.linspace(0, 1, k)
        x[-k:] *= np.linspace(1, 0, k)
    return x


# ------------------------------------------------------------------ 音色

def piano(midi, dur, amp=1.0, rng=None, warm=0.0):
    """加性钢琴感：分谐波不同衰减 + 轻微失谐 + 击槌噪声"""
    rng = rng or np.random.default_rng(midi)
    f = midi_f(midi)
    t = t_of(dur)
    gains = [1.0, 0.46, 0.28, 0.17, 0.11, 0.075, 0.05, 0.03, 0.02]
    sig = np.zeros_like(t)
    for k, g in enumerate(gains, start=1):
        if f * k > 15500:
            break
        dec = 1.7 + 1.25 * (k - 1) + warm
        det = 1.0 + 0.00045 * k + rng.normal(0, 0.00035)
        sig += g * np.exp(-t * dec) * np.sin(2 * np.pi * f * k * det * t + rng.uniform(0, 6.283))
    a = max(1, int(0.005 * SR))
    sig[:a] *= np.linspace(0, 1, a)
    rel = max(1, int(min(0.32, dur * 0.4) * SR))
    sig[-rel:] *= np.linspace(1, 0, rel)
    hit = 0.055 * np.exp(-t * 190) * bp(white(len(t), rng), 1200, 7000, 1)
    return amp * lp(sig + hit, 7200 + warm * 200, 3)


def nylon(midi, dur, amp=1.0, rng=None):
    """指弹/尼龙弦：谐波少、衰减快、低通更柔"""
    rng = rng or np.random.default_rng(midi)
    f = midi_f(midi)
    t = t_of(dur)
    sig = np.zeros_like(t)
    for k, g in enumerate([1.0, 0.42, 0.20, 0.10, 0.05, 0.025], start=1):
        if f * k > 12000:
            break
        sig += g * np.exp(-t * (3.0 + 2.1 * (k - 1))) * \
            np.sin(2 * np.pi * f * k * t + rng.uniform(0, 6.283))
    a = max(1, int(0.004 * SR))
    sig[:a] *= np.linspace(0, 1, a)
    return amp * lp(sig, 4300, 3)


def tri(midi, dur, amp=1.0, vib=0.0, rng=None):
    """三角波（奇次谐波 1/k^2），可做颤音旋律"""
    rng = rng or np.random.default_rng(midi)
    f = midi_f(midi)
    t = t_of(dur)
    ph = 2 * np.pi * (f * t + (vib * f * 0.006) * np.sin(2 * np.pi * 4.8 * t))
    sig = np.zeros_like(t)
    for j, k in enumerate([1, 3, 5, 7, 9, 11], start=0):
        sign = 1 if (j % 2 == 0) else -1
        sig += sign / (k * k) * np.sin(k * ph)
    return amp * lp(sig * adsr(dur, 0.018, 0.09, 0.72, 0.11), 8000, 3)


def sawstack(freqs, dur, amp=1.0, cents=(-9, -4, 0, 5, 11), harm=8, lfo=0.13, rng=None):
    """氛围 pad：多个失谐锯齿（带限加性）+ 慢 LFO 振幅 + 长 attack/release"""
    rng = rng or np.random.default_rng(int(freqs[0]))
    t = t_of(dur)
    out = np.zeros_like(t)
    for f in freqs:
        for c in cents:
            fk = f * 2 ** (c / 1200.0)
            s = np.zeros_like(t)
            for k in range(1, harm + 1):
                if fk * k > 13000:
                    break
                s += np.sin(2 * np.pi * fk * k * t + rng.uniform(0, 6.283)) / k
            out += s
    out /= max(1, len(freqs) * len(cents)) * 2.6
    wob = 1.0 + 0.30 * np.sin(2 * np.pi * lfo * t + rng.uniform(0, 6.283))
    out *= wob * adsr(dur, dur * 0.28, dur * 0.2, 0.85, dur * 0.34)
    return amp * lp(out, 1650, 4)


def sub(midi, dur, amp=1.0, rng=None):
    t = t_of(dur)
    f = midi_f(midi)
    sig = np.sin(2 * np.pi * f * t) * adsr(dur, 0.03, 0.1, 0.8, 0.18)
    return amp * sig


def bass808(m_from, m_to, dur, amp=1.0, glide=0.16, rng=None):
    """808：正弦 + 滑音(portamento) + tanh 轻饱和产生高次谐波"""
    t = t_of(dur)
    f0, f1 = midi_f(m_from), midi_f(m_to)
    k = 1.0 - np.exp(-t / max(0.02, glide) * 2.3)
    k = np.clip(k, 0, 1)
    f = f0 + (f1 - f0) * k
    ph = 2 * np.pi * np.cumsum(f) / SR
    sig = np.tanh(1.75 * np.sin(ph))
    sig *= adsr(dur, 0.012, 0.05, 0.85, 0.1) * np.exp(-t * 1.5)
    rel = max(1, int(min(0.4, dur * 0.35) * SR))
    sig[-rel:] *= np.linspace(1, 0, rel)
    return amp * lp(sig, 900, 3)


def kick(dur=0.42, amp=1.0, f0=142, f1=45, dec=13.0, rng=None):
    rng = rng or np.random.default_rng(1)
    t = t_of(dur)
    f = f1 + (f0 - f1) * np.exp(-t * 27)
    ph = 2 * np.pi * np.cumsum(f) / SR
    sig = np.sin(ph) * np.exp(-t * dec)
    click = 0.10 * np.exp(-t * 240) * hp(white(len(t), rng), 1500, 1)
    a = max(1, int(0.003 * SR))
    sig[:a] *= np.linspace(0, 1, a)
    return amp * (sig + click)


def snare(dur=0.28, amp=1.0, rng=None):
    rng = rng or np.random.default_rng(2)
    n = bp(white(int(dur * SR), rng), 1450, 7800, 2) * np.exp(-np.arange(int(dur * SR)) / SR * 20)
    t = t_of(dur)
    body = 0.40 * np.sin(2 * np.pi * 192 * t) * np.exp(-t * 30) + \
        0.20 * np.sin(2 * np.pi * 328 * t) * np.exp(-t * 36)
    return amp * lp(fade(0.78 * n + body[:len(n)], 4.0), 8200, 2)


def hat(dur=0.07, amp=0.6, open_=False, rng=None):
    rng = rng or np.random.default_rng(3)
    t = t_of(dur)
    dec = 30 if open_ else 92
    metal = np.zeros_like(t)
    for f in (2700, 3900, 5100, 6700, 8300, 10600):
        metal += np.sin(2 * np.pi * f * t + rng.uniform(0, 6.283))
    sig = (0.42 * metal / 6.0 + 0.75 * hp(white(len(t), rng), 6800, 2)) * np.exp(-t * dec)
    return amp * fade(sig, 2.0)


def rhodes(midi, dur, amp=1.0, rng=None):
    """Lo-fi 电钢：基音 + 失谐二次音 + 颤音 + 磁带抖动"""
    rng = rng or np.random.default_rng(midi)
    f = midi_f(midi)
    t = t_of(dur)
    wob = 1.0 + 0.0035 * np.sin(2 * np.pi * 0.7 * t)
    sig = np.sin(2 * np.pi * f * wob * t)
    sig += 0.34 * np.sin(2 * np.pi * f * 2.003 * t) * np.exp(-t * 5.5)
    sig += 0.15 * np.sin(2 * np.pi * f * 3.01 * t) * np.exp(-t * 9)
    sig += 0.07 * np.sin(2 * np.pi * f * 5.98 * t) * np.exp(-t * 16)
    sig *= np.exp(-t * 1.25) * (1 + 0.07 * np.sin(2 * np.pi * 4.6 * t))
    a = max(1, int(0.006 * SR))
    sig[:a] *= np.linspace(0, 1, a)
    rel = max(1, int(min(0.3, dur * 0.35) * SR))
    sig[-rel:] *= np.linspace(1, 0, rel)
    return amp * lp(sig, 5200, 3)


def bell(midi, dur, amp=0.5, rng=None):
    rng = rng or np.random.default_rng(midi)
    f = midi_f(midi)
    t = t_of(dur)
    sig = np.sin(2 * np.pi * f * t) + 0.42 * np.sin(2 * np.pi * f * 2.76 * t) * np.exp(-t * 4)
    sig *= np.exp(-t * 1.6)
    return amp * fade(sig, 10.0)


def crackle(dur, amp=0.02, rng=None, rate=0.055):
    """黑胶底噪 + 随机爆点"""
    rng = rng or np.random.default_rng(11)
    n = int(dur * SR)
    floor = lp(white(n, rng), 3000, 2) * 0.30 + hp(white(n, rng), 4000, 2) * 0.2
    imp = (rng.random(n) < rate / SR * 40).astype(float)
    pop = np.convolve(imp, np.exp(-np.arange(int(0.006 * SR)) / SR * 900), mode="same")
    return amp * (floor + 2.4 * pop)


# ------------------------------------------------------------------ 混音/母带

class Mix:
    def __init__(self, dur):
        self.n = int(round(dur * SR))
        self.buf = np.zeros((self.n + SR, 2), dtype=np.float64)

    def add(self, sig, at=0.0, pan=0.0, gain=1.0):
        i = int(round(at * SR))
        if i >= self.n + SR - 1:
            return
        seg = np.asarray(sig, dtype=np.float64)
        seg = seg[:len(self.buf) - i] if i + len(seg) > len(self.buf) else seg
        if i < 0:
            seg = seg[-i:]
            i = 0
        th = (pan + 1.0) * np.pi / 4.0
        self.buf[i:i + len(seg), 0] += seg * gain * np.cos(th)
        self.buf[i:i + len(seg), 1] += seg * gain * np.sin(th)

    def add_mono_stereo(self, sig_l, sig_r, at=0.0, gain=1.0):
        i = int(round(at * SR))
        seg = np.asarray(sig_l, float)[:len(self.buf) - i], np.asarray(sig_r, float)[:len(self.buf) - i]
        self.buf[i:i + len(seg[0]), 0] += seg[0] * gain
        self.buf[i:i + len(seg[1]), 1] += seg[1] * gain

    def master(self, dur, fade_s=1.6, peak_db=-1.0, drive=1.25, rev=0.0, rev_decay=2.2):
        n = int(round(dur * SR))
        st = self.buf[:n].copy()
        if rev > 0:
            st = reverb_stereo(st, mix=rev, decay=rev_decay)
        st = np.tanh(st * drive) / np.tanh(drive)          # 轻微 soft-clip
        pk = float(np.max(np.abs(st))) or 1e-9
        st *= 10 ** (peak_db / 20.0) / pk                  # 归一化到 -1 dBFS
        env = np.ones(n)
        fo = int(fade_s * SR)
        env[-fo:] = np.linspace(1, 0, fo) ** 1.15          # 结尾淡出
        fi = min(int(0.012 * SR), n // 2)
        env[:fi] = np.linspace(0, 1, fi)
        st *= env[:, None]
        return st


def reverb_stereo(st, mix=0.28, decay=2.2, tail=1.15, seed=7):
    rng = np.random.default_rng(seed)
    n = len(st)
    irn = int(tail * SR)
    out = np.zeros_like(st)
    for c in range(2):
        ir = rng.standard_normal(irn) * np.exp(-np.arange(irn) / SR * decay)
        ir[0] *= 0.4
        ir /= (np.sqrt(np.sum(ir ** 2)) + 1e-9)
        L = n + irn - 1
        out[:, c] = np.fft.irfft(np.fft.rfft(st[:, c], L) * np.fft.rfft(ir, L), L)[:n]
    return (1 - mix) * st + mix * out * 2.1


def to_int16(st):
    x = np.clip(np.rint(st * 32767.0), -32768, 32767).astype("<i2")
    return x


def write_wav(path, st):
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(to_int16(st).reshape(-1).tobytes())


# ------------------------------------------------------------------ 乐理小工具

CH = {
    "C": [60, 64, 67], "Cmaj7": [60, 64, 67, 71], "Cadd9": [60, 64, 67, 74],
    "Dm": [62, 65, 69], "Dm7": [62, 65, 69, 72],
    "Em": [64, 67, 71], "Em7": [64, 67, 71, 74], "Em9": [64, 67, 71, 74, 78],
    "F": [65, 69, 72], "Fmaj7": [65, 69, 72, 76],
    "G": [67, 71, 74], "G7": [67, 71, 74, 77], "Gsus4": [67, 72, 74, 79],
    "A": [69, 73, 76], "Am": [69, 72, 76], "Am7": [69, 72, 76, 79], "Am9": [69, 72, 76, 79, 83],
    "Bm7": [71, 74, 77, 81], "E7": [76, 79, 83, 86], "D": [62, 66, 69],
    "Asus2": [69, 72, 76, 81], "Csus2": [60, 62, 67, 71], "Gmaj7": [67, 71, 74, 78],
    "Fmaj9": [65, 69, 72, 76, 79], "Amin9": [57, 60, 64, 67, 71],
}
BASS = {"C": 36, "D": 38, "E": 40, "F": 41, "G": 43, "A": 45, "B": 47}


def root_of(name):
    key = name[0]
    return BASS[key]


def prog_cycle(prog, i):
    return prog[i % len(prog)]


# ------------------------------------------------------------------ 8 首曲子

def s1_fog_city(dur, bpm, seed):
    """雾中城市：雨声白噪环境 + 三角波旋律 + 柔和鼓 + pad"""
    rng = np.random.default_rng(seed)
    m = Mix(dur)
    beat = 60.0 / bpm
    bar = 4 * beat
    nbar = int(np.ceil(dur / bar))
    prog = ["Fmaj7", "Am7", "Dm7", "Cmaj7"]
    n = int((dur + 1) * SR)
    rain = pink(n, rng)
    m.add_mono_stereo(lp(rain, 3000, 3) * 0.055, lp(pink(n, rng), 3000, 3) * 0.055, gain=1.0)
    drops = (rng.random(n) < 0.00035).astype(float)
    m.add_mono_stereo(0.05 * np.convolve(drops, np.exp(-np.arange(int(0.01 * SR)) / SR * 700), "same"),
                      0.05 * np.convolve(rng.random(n) < 0.00035, np.exp(-np.arange(int(0.01 * SR)) / SR * 700), "same"))
    mel = [(0.0, 72, 1.5), (1.5, 74, 1.0), (2.5, 76, 2.0), (4.5, 74, 1.5), (6.0, 72, 1.5), (7.5, 69, 2.0),
           (10.0, 72, 1.0), (11.0, 74, 3.0), (16.0, 76, 1.5), (17.5, 79, 1.5), (19.0, 76, 2.0),
           (21.0, 74, 1.0), (22.0, 72, 2.0), (24.0, 69, 4.0), (30.0, 71, 1.5), (31.5, 72, 2.5),
           (34.0, 74, 3.0), (37.0, 72, 3.0)]
    for b in range(nbar):
        ch = prog_cycle(prog, b)
        t0 = b * bar
        notes = CH[ch]
        padf = [midi_f(x) for x in notes]
        m.add(sawstack(padf, bar * 0.98, amp=0.088, cents=(-6, 0, 6), harm=6, lfo=0.11, rng=rng),
              at=t0, pan=-0.28, gain=1.0)
        m.add(sawstack(padf, bar * 0.98, amp=0.088, cents=(4, 0, -4), harm=6, lfo=0.09, rng=rng),
              at=t0 + 0.35, pan=0.30, gain=1.0)
        m.add(sub(root_of(ch) + 12, bar * 0.9, amp=0.20, rng=rng), at=t0, gain=1.0)
        m.add(kick(0.4, 0.86, rng=rng), at=t0, gain=1.0)
        m.add(snare(0.26, 0.34, rng=rng), at=t0 + 2 * beat, gain=1.0)
        for e in range(8):
            m.add(hat(0.06, 0.11 if e % 2 else 0.17, rng=rng), at=t0 + e * beat / 2, pan=0.18)
    for bpos, mid, dl in mel:
        t = bpos * beat
        if t < dur:
            m.add(tri(mid, dl * beat, amp=0.20, vib=1.0, rng=rng), at=t, pan=-0.05)
    return m.master(dur, fade_s=1.8, rev=0.30, rev_decay=2.0)


def s2_tram(dur, bpm, seed):
    """有轨电车：C 大调琶音钢琴 + 行走低音 + 铁轨撞击节奏"""
    rng = np.random.default_rng(seed)
    m = Mix(dur)
    beat = 60.0 / bpm
    bar = 4 * beat
    prog = ["Cadd9", "Am7", "Fmaj7", "Gsus4"]
    nbar = int(np.ceil(dur / bar))
    arp = [0, 1, 2, 3, 2, 1, 3, 2]
    for b in range(nbar):
        ch = prog_cycle(prog, b)
        t0 = b * bar
        notes = CH[ch]
        top = max(notes) + 12
        seq = [notes[i % len(notes)] + (12 if i >= len(notes) else 0) for i in arp]
        for e, mid in enumerate(seq):
            m.add(piano(mid + 12, 1.6 * beat, amp=0.24 - 0.012 * (e % 4), rng=rng),
                  at=t0 + e * beat / 2, pan=(e % 3 - 1) * 0.22)
        m.add(piano(top, 1.0 * beat, amp=0.13, rng=rng), at=t0 + 3.5 * beat, pan=0.25)
        # 简易行走低音：根-五-七-根
        walk = [root_of(ch) + 12, root_of(ch) + 19, root_of(ch) + 16, root_of(ch) + 12]
        for e, mm in enumerate(walk):
            m.add(piano(mm, 0.9 * beat, amp=0.20, warm=1.4, rng=rng), at=t0 + e * beat, gain=1.0)
        # 铁轨 clack（八分位，带 swing）
        for e in range(4):
            tt = t0 + e * beat + (0.03 * beat if e % 2 else 0)
            m.add(fade(hp(white(int(0.05 * SR), rng), 900, 2) * np.exp(-np.arange(int(0.05 * SR)) / SR * 70), 3.0),
                  at=tt, pan=-0.4 + 0.35 * (e % 2), gain=0.10)
            m.add(kick(0.3, 0.42, f0=120, f1=52, dec=20, rng=rng), at=t0 + e * beat, gain=0.55)
    lead = [(16.0, 76, 1.5), (17.5, 79, 1.5), (19.0, 81, 3.0), (22.0, 79, 2.0), (24.0, 76, 4.0)]
    for bpos, mid, dl in lead:
        if bpos * beat < dur:
            m.add(piano(mid + 12, dl * beat * 0.6, amp=0.13, rng=rng), at=bpos * beat, pan=-0.1)
    return m.master(dur, fade_s=1.6, rev=0.26, rev_decay=1.8)


def s3_tide(dur, bpm, seed):
    """潮汐手记：氛围 pad（多失谐锯齿 + 慢 LFO）+ 次低频涌浪 + 远处钟声"""
    rng = np.random.default_rng(seed)
    m = Mix(dur)
    beat = 60.0 / bpm
    bar = 4 * beat
    prog = ["Fmaj9", "Am9", "Dm7", "Gsus4", "Cmaj7", "Em9"]
    seg = dur / len(prog)
    for i, ch in enumerate(prog):
        t0 = i * seg
        notes = CH[ch]
        padf = [midi_f(x - 12 if j == 0 else x) for j, x in enumerate(notes)]
        m.add(sawstack(padf, seg + 2.5, amp=0.135, cents=(-11, -5, 0, 6, 12), harm=7, lfo=0.06 + 0.01 * i, rng=rng),
              at=t0, pan=-0.35 + 0.14 * i)
        m.add(sawstack([midi_f(x + 12) for x in notes[:2]], seg + 2.0, amp=0.055,
                       cents=(-7, 0, 8), harm=5, lfo=0.09, rng=rng), at=t0 + 1.2, pan=0.4)
        m.add(sub(root_of(ch), seg * 1.05, amp=0.15, rng=rng), at=t0, gain=1.0)
    # 潮水：被慢包络调制的着色噪声
    n = int((dur + 1) * SR)
    t = np.arange(n) / SR
    swell = 0.5 + 0.5 * np.sin(2 * np.pi * 0.085 * t + rng.uniform(0, 6.28))
    w1 = lp(pink(n, rng), 900, 3) * swell
    w2 = lp(pink(n, rng), 1400, 3) * np.roll(swell, int(0.9 * SR))
    m.add_mono_stereo(0.075 * w1 / (np.max(np.abs(w1)) + 1e-9), 0.075 * w2 / (np.max(np.abs(w2)) + 1e-9))
    for i in range(7):
        tt = rng.uniform(1.0, dur - 4.0)
        m.add(bell(rng.choice([74, 76, 79, 81, 83]), 2.6, amp=0.075, rng=rng), at=tt, pan=rng.uniform(-0.6, 0.6))
    return m.master(dur, fade_s=2.0, rev=0.42, rev_decay=1.5)


def s4_pier(dur, bpm, seed):
    """月光栈桥：指弹分解和弦（尼龙弦 Travis 风格）+ 哼唱旋律"""
    rng = np.random.default_rng(seed)
    m = Mix(dur)
    beat = 60.0 / bpm
    bar = 4 * beat
    prog = ["G", "Em7", "Cadd9", "D"]
    nbar = int(np.ceil(dur / bar))
    pattern = [0, 2.5, 1.0, 2.0, 3.5, 1.5]  # beat offsets
    for b in range(nbar):
        ch = prog_cycle(prog, b)
        t0 = b * bar
        notes = CH[ch]
        bass_m = root_of(ch) + 12
        # 低音交替
        m.add(nylon(bass_m, 1.2 * beat, amp=0.26, rng=rng), at=t0, gain=1.0)
        m.add(nylon(bass_m + 7, 1.0 * beat, amp=0.19, rng=rng), at=t0 + 2 * beat, gain=1.0)
        for e, off in enumerate(pattern):
            mid = notes[(e + b) % len(notes)] + 12
            m.add(nylon(mid, 1.4 * beat, amp=0.155, rng=rng), at=t0 + off * beat, pan=(e % 2) * 0.3 - 0.15)
    hum = [(0.0, 74, 3.0), (3.5, 76, 2.0), (6.0, 78, 4.0), (11.0, 76, 2.0), (13.5, 74, 4.5),
           (18.5, 71, 2.0), (21.0, 74, 3.0), (24.5, 72, 4.0)]
    for bpos, mid, dl in hum:
        if bpos * beat < dur:
            f = midi_f(mid)
            t = t_of(dl * beat)
            sig = (np.sin(2 * np.pi * f * t) + 0.22 * np.sin(2 * np.pi * 2 * f * t) * np.exp(-t * 3)) \
                * adsr(dl * beat, 0.14, 0.1, 0.8, 0.3)
            sig *= (1 + 0.006 * np.sin(2 * np.pi * 4.4 * t))
            m.add(lp(sig, 2600, 3) * 0.13, at=bpos * beat, pan=0.05)
    return m.master(dur, fade_s=1.5, rev=0.24, rev_decay=1.6)


def s5_nightflight(dur, bpm, seed):
    """夜航西飞：808 滑音贝斯 + trap hi-hat + 暗色小调和声 + 滑音主音"""
    rng = np.random.default_rng(seed)
    m = Mix(dur)
    beat = 60.0 / bpm
    bar = 4 * beat
    nbar = int(np.ceil(dur / bar))
    prog = [("Am", 45, 33), ("F", 41, 41), ("Em", 40, 40), ("Am", 45, 33)]
    for b in range(nbar):
        ch, bfrom, bto = prog_cycle(prog, b)
        t0 = b * bar
        m.add(bass808(bfrom + 12, bto, bar * 0.8, amp=0.44, glide=0.18, rng=rng), at=t0, gain=1.0)
        m.add(bass808(bto, bto, 0.9 * beat, amp=0.30, glide=0.05, rng=rng), at=t0 + 2.5 * beat, gain=1.0)
        notes = CH[ch]
        m.add(sawstack([midi_f(x - 12) for x in notes], bar * 0.95, amp=0.075,
                       cents=(-5, 0, 5), harm=6, lfo=0.18, rng=rng), at=t0, pan=-0.3)
        m.add(kick(0.44, 0.95, f0=125, f1=42, dec=11, rng=rng), at=t0, gain=1.0)
        m.add(kick(0.34, 0.6, f0=110, f1=46, dec=14, rng=rng), at=t0 + 2.75 * beat, gain=1.0)
        m.add(snare(0.3, 0.42, rng=rng), at=t0 + 2 * beat, pan=0.0)
        for e in range(8):
            sub_e = 0 if e % 2 else 1
            m.add(hat(0.05, 0.20, rng=rng), at=t0 + e * beat / 2, pan=0.25)
        if b % 2 == 1:  # hat roll
            for e in range(3):
                m.add(hat(0.04, 0.16, rng=rng), at=t0 + (3.5 + e * 0.16) * beat, pan=0.35)
        if b == nbar - 1:
            m.add(hat(0.24, 0.18, open_=True, rng=rng), at=t0 + 3.5 * beat, pan=0.3)
    lead = [(0.0, 69, 2.0, 72), (2.0, 72, 2.0, 76), (4.0, 76, 3.0, 76), (7.5, 72, 2.5, 69),
            (12.0, 81, 2.0, 79), (14.0, 79, 4.0, 76), (20.0, 76, 2.0, 72), (22.0, 72, 6.0, 69),
            (30.0, 69, 4.0, 69)]
    for bpos, mid, dl, gl in lead:
        t = bpos * beat
        if t > dur:
            continue
        tt = t_of(dl * beat)
        f0, f1 = midi_f(mid), midi_f(gl)
        k = np.clip(1 - np.exp(-tt / (0.16 * beat) * 2.2), 0, 1)
        fr = f0 + (f1 - f0) * k
        sig = np.sin(2 * np.pi * np.cumsum(fr) / SR) + 0.25 * np.sin(2 * np.pi * np.cumsum(fr * 2) / SR) * np.exp(-tt * 2)
        sig *= adsr(dl * beat, 0.05, 0.1, 0.8, 0.25)
        m.add(lp(sig, 4200, 3) * 0.17, at=t, pan=-0.1)
    return m.master(dur, fade_s=1.7, rev=0.22, rev_decay=1.4, drive=1.35)


def s6_runway(dur, bpm, seed):
    """机场跑道 36R：五声音阶旋律 + 太鼓式鼓组 + 持续五度 drone"""
    rng = np.random.default_rng(seed)
    m = Mix(dur)
    beat = 60.0 / bpm
    bar = 4 * beat
    nbar = int(np.ceil(dur / bar))
    penta = [72, 74, 76, 79, 81]
    for b in range(nbar):
        t0 = b * bar
        m.add(sawstack([midi_f(45), midi_f(52)], bar * 1.02, amp=0.07, cents=(-8, 0, 8), harm=6, lfo=0.1, rng=rng),
              at=t0, pan=-0.3)
        m.add(kick(0.55, 0.8, f0=100, f1=55, dec=6, rng=rng), at=t0, gain=1.0)
        m.add(kick(0.45, 0.5, f0=110, f1=60, dec=9, rng=rng), at=t0 + 2 * beat, gain=0.7)
        tom = [(1.5, 62), (3.0, 57), (3.5, 55)]
        for off, mid in tom:
            tt = t_of(0.3)
            f = midi_f(mid)
            fr = f * (1 + 0.35 * np.exp(-tt * 14))
            m.add(np.sin(2 * np.pi * np.cumsum(fr) / SR) * np.exp(-tt * 7) * 0.22, at=t0 + off * beat, pan=0.35)
        cymbal = fade(hp(white(int(0.09 * SR), rng), 5000, 2) * np.exp(-np.arange(int(0.09 * SR)) / SR * 55), 3.0)
        m.add(cymbal, at=t0 + beat, pan=-0.45, gain=0.12)
    seq = [(0, 72, 1), (1, 74, 1), (2, 76, 2), (4, 79, 1.5), (5.5, 76, 1.5), (7, 74, 2),
           (9, 76, 1), (10, 79, 1), (11, 81, 3), (14, 79, 1), (15, 76, 2), (17, 74, 2),
           (19, 72, 4), (24, 76, 1.5), (25.5, 79, 1.5), (27, 81, 2), (29, 79, 2), (31, 76, 4)]
    for bpos, mid, dl in seq:
        t = bpos * beat
        if t < dur:
            m.add(tri(mid, dl * beat, amp=0.22, vib=1.6, rng=rng), at=t, pan=-0.05)
            m.add(tri(mid - 12, dl * beat, amp=0.09, vib=0.8, rng=rng), at=t, pan=0.25)
    return m.master(dur, fade_s=1.5, rev=0.30, rev_decay=1.7)


def s7_wind_over_ridge(dur, bpm, seed):
    """山有雾：4536251 和弦进行 + 钢琴琶音 + 弦乐 pad + 低音"""
    rng = np.random.default_rng(seed)
    m = Mix(dur)
    beat = 60.0 / bpm
    bar = 4 * beat
    prog = ["F", "G", "Em", "Am", "Dm", "G", "C"]  # 4-5-3-6-2-5-1 in C
    nbar = int(np.ceil(dur / bar))
    for b in range(nbar):
        ch = prog_cycle(prog, b)
        t0 = b * bar
        notes = CH[ch]
        m.add(sub(root_of(ch) + 12, bar * 0.95, amp=0.19, rng=rng), at=t0, gain=1.0)
        m.add(sawstack([midi_f(x) for x in notes], bar * 1.05, amp=0.062,
                       cents=(-6, -2, 3, 9), harm=7, lfo=0.22, rng=rng), at=t0, pan=-0.28)
        m.add(sawstack([midi_f(x + 12) for x in notes[:2]], bar * 1.0, amp=0.034,
                       cents=(-4, 0, 5), harm=5, lfo=0.17, rng=rng), at=t0 + 0.4, pan=0.32)
        for e in range(8):
            mid = notes[e % len(notes)] + (12 if e >= 4 else 0)
            m.add(piano(mid, 1.3 * beat, amp=0.17 - 0.012 * (e % 4), rng=rng),
                  at=t0 + e * beat / 2, pan=(e % 2 - 0.5) * 0.4)
        m.add(kick(0.4, 0.55, rng=rng), at=t0, gain=1.0)
        if b % 2 == 1:
            m.add(snare(0.26, 0.26, rng=rng), at=t0 + 2 * beat, gain=1.0)
    lead = [(0, 72, 2), (2, 74, 2), (4, 76, 4), (8, 79, 3), (11, 76, 2), (13, 74, 3),
            (16, 76, 2), (18, 72, 6), (24, 77, 2), (26, 79, 4), (30, 81, 3), (33, 79, 3), (36, 76, 5)]
    for bpos, mid, dl in lead:
        t = bpos * beat
        if t < dur:
            m.add(piano(mid + 12, dl * beat * 0.75, amp=0.115, warm=0.6, rng=rng), at=t, pan=-0.06)
    return m.master(dur, fade_s=2.0, rev=0.32, rev_decay=2.1)


def s8_platform_cat(dur, bpm, seed):
    """旧站台的猫：Lo-fi 鼓组（正弦底鼓 + 噪声军鼓 + hat）+ 爵士七九和弦 + 黑胶底噪"""
    rng = np.random.default_rng(seed)
    m = Mix(dur)
    beat = 60.0 / bpm
    bar = 4 * beat
    nbar = int(np.ceil(dur / bar))
    prog = ["Dm7", "G7", "Cmaj7", "Em7", "Am9", "Dm7", "Gsus4", "Cmaj7"]
    n = int((dur + 1) * SR)
    m.add(crackle(dur + 1, amp=0.022, rng=rng), at=0, gain=1.0)
    for b in range(nbar):
        ch = prog_cycle(prog, b)
        t0 = b * bar
        notes = CH[ch]
        # swing 八分
        for e, off in enumerate([0, 0.66, 1.0, 1.66, 2.0, 2.66, 3.0, 3.66]):
            mid = notes[(e // 2) % len(notes)] + 12
            m.add(rhodes(mid, 1.1 * beat, amp=0.13 if e % 2 == 0 else 0.085, rng=rng),
                  at=t0 + off * beat, pan=(e % 3 - 1) * 0.25)
        m.add(kick(0.36, 0.72, f0=118, f1=48, dec=15, rng=rng), at=t0, gain=1.0)
        m.add(kick(0.3, 0.4, f0=105, f1=50, dec=18, rng=rng), at=t0 + 2.5 * beat, gain=1.0)
        m.add(snare(0.26, 0.36, rng=rng), at=t0 + 1 * beat, gain=1.0)
        m.add(snare(0.3, 0.28, rng=rng), at=t0 + 3.5 * beat, gain=1.0)
        for e in range(8):
            m.add(hat(0.055, 0.13 if e % 2 else 0.19, rng=rng), at=t0 + e * beat * 0.5 + (0.02 * beat), pan=0.4)
        m.add(sub(root_of(ch), 1.6 * beat, amp=0.17, rng=rng), at=t0)
        m.add(sub(root_of(ch) + 7, 1.2 * beat, amp=0.12, rng=rng), at=t0 + 2 * beat)
    for i in range(3):
        tt = 3.0 + i * 5.5
        if tt < dur:
            m.add(tri(rng.choice([76, 79, 81]), 2.0, amp=0.07, vib=2.0, rng=rng), at=tt, pan=-0.3)
    return m.master(dur, fade_s=1.5, rev=0.28, rev_decay=1.9, drive=1.2)


# ------------------------------------------------------------------ 曲目表

TRACKS = [
    dict(key="s1", build=s1_fog_city, bpm=76, dur=38.0, seed=101,
         artist="拾光社 Shiguang Lab", album="雾中城市 City in Fog",
         title="雾中城市 City in Fog", no=1, year="2024", genre="氛围电子 Ambient",
         comment="numpy 合成：雨声环境 + 三角波旋律 + Lo-fi 鼓。仅供 salt-player 界面与元数据验证。",
         lrc=dict(lang="bi", gap=3.5, start=0.5,
                  cn=["雾灯亮在第七街口", "我把影子走成一首歌", "玻璃上的雨写着名字",
                      "列车带走最后一点橙色", "城市在高架下轻轻打鼾"],
                  en=["Fog lamps glow at the seventh crossing", "I walk my shadow into a song",
                      "Rain on the glass is writing your name", "The train takes the last warm orange",
                      "The city snores softly under the viaduct"])),
    dict(key="s2", build=s2_tram, bpm=104, dur=32.0, seed=102,
         artist="拾光社 Shiguang Lab", album="雾中城市 City in Fog",
         title="有轨电车 Tram Line No.7", no=2, year="2024", genre="器乐钢琴 Instrumental",
         comment="numpy 合成：C 大调琶音钢琴 + 行走低音 + 铁轨撞击采样感。",
         lrc=dict(lang="cn", gap=3.6, start=0.6,
                  cn=["铜铃响过麦芽糖的摊", "轨道把黄昏拉得好长", "售票员数着硬币唱歌",
                      "我在第七站弄丢了伞", "车窗外的梧桐往后退", "时间坐在对面打起了盹",
                      "终点站还亮着一盏灯", "它好像在等一个迟到的归人"])),
    dict(key="s3", build=s3_tide, bpm=58, dur=42.0, seed=103,
         artist="拾光社 Shiguang Lab", album="潮汐手记 Tide Notes",
         title="潮汐手记 Tide Notes", no=1, year="2025", genre="氛围 Ambient",
         comment="numpy 合成：失谐锯齿 pad + 慢 LFO + 潮水着色噪声，纯器乐氛围长音。",
         lrc=None),
    dict(key="s4", build=s4_pier, bpm=88, dur=29.0, seed=104,
         artist="拾光社 Shiguang Lab", album="潮汐手记 Tide Notes",
         title="月光栈桥 Moonlight Pier", no=2, year="2025", genre="指弹民谣 Fingerstyle",
         comment="numpy 合成：尼龙弦指弹分解和弦 + 哼唱旋律；歌词带 offset 用于测偏移。",
         lrc=dict(lang="cn", gap=3.4, start=0.4, offset=-500,
                  cn=["木缝里还藏着白天的温度", "浪花把月亮敲成一地碎片", "我坐在栈桥尽头数影子",
                      "潮水漫过那通没接的电话", "桥灯一盏一盏熄灭成星", "海风替我说了半句话",
                      "明天再来这里坐一会儿"])),
    dict(key="s5", build=s5_nightflight, bpm=70, dur=34.0, seed=105,
         artist="白噪电台 White Noise Radio", album="夜航西飞 Nightflight",
         title="夜航西飞 Nightflight West", no=1, year="2023", genre="陷阱 Trip-Hop",
         comment="numpy 合成：808 滑音贝斯 + trap hi-hat + 滑音主音；带 track 级同名封面。",
         lrc=dict(lang="cn", gap=3.3, start=0.5,
                  cn=["引擎声里城市缩小成一枚硬币", "我把航线小心压在舌头下", "云层下面有人在等天亮",
                      "三万英尺的静默是有形状的", "时差像一把不太锋利的刀", "梦还在起落架里没有落地",
                      "星星排成一个旧的电话号码", "我看着它，一直不敢拨", "天亮之前请继续向西"])),
    dict(key="s6", build=s6_runway, bpm=96, dur=27.0, seed=106,
         artist="苏念安 Su Nian'an", album="夜航西飞 Nightflight",
         title="机场跑道 36R Runway 36R", no=2, year="2023", genre="器乐五声音阶 Instrumental",
         comment="numpy 合成：五声音阶旋律 + 太鼓式鼓组 + 五度 drone，器乐小品。",
         lrc=None),
    dict(key="s7", build=s7_wind_over_ridge, bpm=72, dur=40.0, seed=107,
         artist="木犴 Mu'an", album="山有雾 Wind Over the Ridge",
         title="山有雾 Wind Over the Ridge", no=1, year="2022", genre="新世纪 New Age",
         comment="numpy 合成：4536251 和弦进行 + 钢琴琶音 + 弦乐 pad；歌词中英双语。",
         lrc=dict(lang="bi", gap=3.7, start=0.7,
                  cn=["雾把整座山藏进白绸", "松针上挂着昨夜的雨", "我沿着溪声一直往上走",
                      "石阶尽头有牧人的烟", "风经过时钟声碎成了光", "山在那头轻轻唤我归"],
                  en=["Fog folds the whole mountain in white silk", "Pine needles hold last night's rain",
                      "I climb upward following the stream", "At the stair's end, a shepherd's smoke",
                      "Where the wind passes, bells break into light", "On the far side, a voice calls me home"])),
    dict(key="s8", build=s8_platform_cat, bpm=82, dur=26.0, seed=108,
         artist="陈屿 Yu Chen", album="山有雾 Wind Over the Ridge",
         title="旧站台的猫 Cat on the Old Platform", no=2, year="2022", genre="Lo-Fi 爵士 Jazz",
         comment="numpy 合成：正弦底鼓 + 噪声军鼓 + hat 的 Lo-fi 鼓组与七九和弦，歌词 7 行。",
         lrc=dict(lang="cn", gap=3.2, start=0.4,
                  cn=["它在旧时刻表下面舔爪", "末班车其实早就走了", "灯泡摇出一小片夏天",
                      "纸箱里卧着整个秋天", "谁的票根贴着风往前跑", "猫不抬头，尾巴量着光",
                      "站牌只好替它数着路人"])),
]

FOLDER_ARTIST = {
    "拾光社 Shiguang Lab": "拾光社 Shiguang Lab",
    "白噪电台 White Noise Radio": "白噪电台 White Noise Radio",
    "苏念安 Su Nian'an": "苏念安 Su Nian'an",
    "木犴 Mu'an": "木犴 Mu'an",
    "陈屿 Yu Chen": "陈屿 Yu Chen",
}
FOLDER_ALBUM = {
    "雾中城市 City in Fog": "雾中城市 City in Fog",
    "潮汐手记 Tide Notes": "潮汐手记 Tide Notes",
    "夜航西飞 Nightflight": "夜航西飞 Nightflight",
    "山有雾 Wind Over the Ridge": "山有雾 Wind Over the Ridge",
}
ALBUMS = ["雾中城市 City in Fog", "潮汐手记 Tide Notes", "夜航西飞 Nightflight", "山有雾 Wind Over the Ridge"]
ALBUM_COVER_PALETTE = {
    "雾中城市 City in Fog": dict(c0=(228, 230, 232), c1=(118, 144, 158), acc=(233, 176, 96), shape="rings", seed=21),
    "潮汐手记 Tide Notes": dict(c0=(214, 224, 231), c1=(96, 118, 136), acc=(176, 208, 206), shape="waves", seed=22),
    "夜航西飞 Nightflight": dict(c0=(24, 30, 56), c1=(66, 96, 128), acc=(126, 210, 218), shape="arcs", seed=23),
    "山有雾 Wind Over the Ridge": dict(c0=(224, 229, 219), c1=(140, 158, 142), acc=(204, 176, 148), shape="blocks", seed=24),
}
TRACK_COVER = {  # track 级封面：key -> 调色（专辑有 cover.jpg，用来测优先级）
    "s5": dict(c0=(46, 26, 52), c1=(124, 68, 66), acc=(228, 152, 108), shape="arcs", seed=31),
}
ALBUM_WITHOUT_COVER = "潮汐手记 Tide Notes"   # 故意不放 cover.jpg，测默认占位图回退


def file_stem(trk):
    return "%02d - %s" % (trk["no"], trk["title"])


def dir_of(trk):
    return os.path.join(LIB, FOLDER_ARTIST[trk["artist"]], FOLDER_ALBUM[trk["album"]])


# ------------------------------------------------------------------ RIFF INFO 标签

INFO_FIELDS = [("INAM", "title"), ("IART", "artist"), ("IPRD", "album"), ("ICRD", "year"),
               ("IPRT", "no_str"), ("IGNR", "genre"), ("ICMT", "comment")]


def build_info_payload(trk):
    parts = [b"INFO"]
    for fourcc, attr in INFO_FIELDS:
        val = trk[attr] if attr != "no_str" else str(trk["no"])
        data = val.encode("utf-8") + b"\x00"          # RIFF INFO 字符串 NUL 结尾
        parts.append(struct.pack("<4sI", fourcc.encode("ascii"), len(data)))
        parts.append(data)
        if len(data) % 2:
            parts.append(b"\x00")                      # 偶数对齐
    return b"".join(parts)


def write_riff_info(path, trk):
    """用 mutagen 的 RIFF chunk API 追加 LIST/INFO（mutagen 没有现成 INFO 标签类）"""
    from mutagen._riff import RiffFile
    payload = build_info_payload(trk)
    with open(path, "r+b") as f:
        rf = RiffFile(f)
        for ch in list(rf.root.subchunks()):           # 幂等：清掉旧 INFO
            if ch.id == "LIST" and getattr(ch, "name", None) == "INFO":
                ch.delete()
        chunk = rf.root.insert_chunk("LIST", payload)   # insert_chunk 会写入数据并更新 RIFF size
        f.flush()
    return payload


def read_riff_info(path):
    with open(path, "rb") as f:
        blob = f.read()
    assert blob[:4] == b"RIFF" and blob[8:12] == b"WAVE", "不是 RIFF/WAVE"
    out, pos, end = {}, 12, min(len(blob), 12 + struct.unpack("<I", blob[4:8])[0])
    while pos + 8 <= end:
        cid = blob[pos:pos + 4]
        size = struct.unpack("<I", blob[pos + 4:pos + 8])[0]
        body = blob[pos + 8:pos + 8 + size]
        if cid == b"LIST" and body[:4] == b"INFO":
            q = 4
            while q + 8 <= len(body):
                fc = body[q:q + 4].decode("ascii").strip("\x00")
                sz = struct.unpack("<I", body[q + 4:q + 8])[0]
                out[fc] = body[q + 8:q + 8 + sz].rstrip(b"\x00").decode("utf-8")
                q += 8 + sz + (sz % 2)
        pos += 8 + size + (size % 2)
    return out


def mutagen_duration(path):
    from mutagen.wave import WAVE
    return WAVE(path).info.length


# ------------------------------------------------------------------ 歌词

def stamps(n, dur, gap, start):
    """等间隔时间戳：保证落在时长内、严格递增、相邻间隔在 2.5–5.0s"""
    gap = float(np.clip(gap, 2.5, 5.0))
    last_max = dur - 1.2
    if start + (n - 1) * gap > last_max:
        gap = max(2.5, (last_max - start) / max(1, n - 1))
    if start + (n - 1) * gap > last_max:               # 行太多则减行
        n = int((last_max - start) // gap) + 1
    return [start + i * gap for i in range(n)], gap


def fmt_ts(t):
    mm = int(t // 60)
    ss = t - mm * 60
    return "[%02d:%05.2f]" % (mm, ss)


def write_lrc(path, trk):
    spec = trk["lrc"]
    dur = trk["dur"]
    if spec["lang"] == "bi":
        pairs = []
        for cn, en in zip(spec["cn"], spec["en"]):
            pairs.extend([cn, en])
        lines = pairs
    else:
        lines = list(spec["cn"])
    times, gap = stamps(len(lines), dur, spec["gap"], spec["start"])
    assert len(times) == len(lines), "歌词行数被裁短，需要重排"
    out = ["[ti:%s]" % trk["title"], "[ar:%s]" % trk["artist"], "[al:%s]" % trk["album"],
           "[by:salt-player tools/gen_demo.py（numpy 合成示例）]"]
    if spec.get("offset") is not None:
        out.append("[offset:%d]" % spec["offset"])
    body = []
    for t, txt in zip(times, lines):
        body.append("%s%s" % (fmt_ts(t), txt))
    out.extend(body)
    text = "\n".join(out) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return times, body, gap


def verify_lrc(path, dur, need_offset=False):
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    lines = [l for l in raw.split("\n") if l.strip()]
    for tag in ("[ti:", "[ar:", "[al:", "[by:"):
        assert any(l.startswith(tag) for l in lines), "%s 缺头部 %s" % (path, tag)
    if need_offset:
        assert any(l.startswith("[offset:-500]") for l in lines), "%s 缺 [offset:-500]" % path
    pat = re.compile(r"^\[(\d{2}):(\d{2})\.(\d{2})\](.+)$")
    ts, texts = [], []
    for l in lines:
        m = pat.match(l)
        if m:
            ts.append(int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / 100.0)
            texts.append(m.group(4).strip())
    assert len(ts) >= 5, "%s 行数太少" % path
    assert len(set(texts)) == len(texts), "%s 有重复歌词行" % path
    for i, t in enumerate(ts):
        assert 0 <= t < dur, "%s 第%d行时间戳 %.2f 超出时长 %.2f" % (path, i + 1, t, dur)
        if i:
            d = t - ts[i - 1]
            assert 2.5 <= d <= 5.0, "%s 第%d行间隔 %.2fs 不在 2.5–5.0s" % (path, i + 1, d)
    return len(ts), ts[-1]


# ------------------------------------------------------------------ 封面

def make_cover(path, pal, size=600, quality=88):
    from PIL import Image, ImageDraw, ImageFilter
    rng = np.random.default_rng(pal["seed"])
    w = h = size
    y, x = np.mgrid[0:h, 0:w].astype(np.float64)
    g = 0.55 * (x / w) + 0.45 * (y / h)
    g = np.clip(g + rng.normal(0, 0.035, (h, w)), 0, 1)
    c0 = np.array(pal["c0"], float)
    c1 = np.array(pal["c1"], float)
    acc = np.array(pal["acc"], float)
    img = c0[None, None, :] * (1 - g[..., None]) + c1[None, None, :] * g[..., None]
    # 柔光晕
    for _ in range(2):
        cx, cy = rng.uniform(0.15, 0.85) * w, rng.uniform(0.15, 0.85) * h
        rr = rng.uniform(0.22, 0.42) * w
        d2 = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
        glow = np.exp(-(d2 / rr) ** 2)
        img += (glow[..., None] * (acc * 0.30 + (255 - acc) * 0.06))
    base = Image.fromarray(np.uint8(np.clip(img, 0, 255)), "RGB").convert("RGBA")
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    dr = ImageDraw.Draw(layer)

    def col(c, a):
        return (int(c[0]), int(c[1]), int(c[2]), a)

    kind = pal["shape"]
    if kind == "rings":
        for i in range(6):
            r = int(w * (0.10 + 0.09 * i))
            cx = int(w * rng.uniform(0.3, 0.7))
            cy = int(h * rng.uniform(0.35, 0.7))
            dr.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col(acc, 60 + 18 * i % 90),
                       width=int(rng.integers(2, 11)))
        dr.rectangle([0, int(h * 0.72), w, int(h * 0.72 + rng.integers(12, 40))], fill=col(c1, 120))
    elif kind == "arcs":
        for i in range(7):
            cx, cy = w * 0.5, h * 1.05
            r = w * (0.28 + 0.10 * i)
            # Pillow 12：arc 的描边参数已从 outline 改为 fill
            dr.arc([cx - r, cy - r, cx + r, cy + r], start=rng.uniform(180, 220), end=rng.uniform(300, 360),
                   fill=col(acc, 40 + 26 * i), width=int(rng.integers(3, 14)))
        dr.ellipse([w * 0.62, h * 0.12, w * 0.80, h * 0.30], fill=col(acc, 150))
    elif kind == "blocks":
        for i in range(9):
            x0 = rng.integers(0, w - 90)
            y0 = rng.integers(int(h * 0.1), h - 120)
            ww = rng.integers(30, 180)
            hh = rng.integers(20, 240)
            dr.rectangle([x0, y0, x0 + ww, y0 + hh], fill=col(acc if i % 3 else c1, 44 + 14 * i))
        pts = [(0, h), (int(w * 0.36), int(h * rng.uniform(0.36, 0.6))), (int(w * 0.62), int(h * 0.72)), (w, h)]
        dr.polygon(pts, fill=col(c1, 90))
    else:  # waves
        for i in range(8):
            ybase = h * (0.22 + 0.09 * i)
            amp = rng.uniform(8, 30)
            freq = rng.uniform(1.1, 2.6)
            ph = rng.uniform(0, 6.28)
            xs = np.linspace(0, w, 240)
            ys = ybase + amp * np.sin(2 * np.pi * freq * xs / w + ph)
            dr.line(list(zip(xs.tolist(), ys.tolist())), fill=col(acc, 50 + 20 * i), width=int(rng.integers(2, 8)))
    layer = layer.filter(ImageFilter.GaussianBlur(0.6))
    out = Image.alpha_composite(base, layer).convert("RGB")
    arr = np.asarray(out).astype(np.float64)
    # 胶片颗粒
    arr += rng.normal(0, 5.2, (h, w, 1))
    # 暗角
    dv = 1 - 0.34 * ((x - w / 2) ** 2 + (y - h / 2) ** 2) / (0.5 * (w ** 2 + h ** 2))
    arr *= np.clip(dv, 0.5, 1.0)[..., None]
    out = Image.fromarray(np.uint8(np.clip(arr, 0, 255)), "RGB")
    out.save(path, "JPEG", quality=quality, optimize=True)
    return path


# ------------------------------------------------------------------ 主流程

def main():
    print("=" * 78)
    print("salt-player 示例曲库生成器（numpy 全合成，无外部音频）")
    print("输出根目录:", LIB)
    print("=" * 78)

    os.makedirs(LIB, exist_ok=True)
    rows = []
    wav_paths = []

    for trk in TRACKS:
        trk["dur"] = float(trk["dur"])
        d = dir_of(trk)
        os.makedirs(d, exist_ok=True)
        stem = file_stem(trk)
        wp = os.path.join(d, stem + ".wav")
        st = trk["build"](trk["dur"], trk["bpm"], trk["seed"])
        assert st.shape == (int(round(trk["dur"] * SR)), 2), "声道/长度不对: %s" % trk["title"]
        write_wav(wp, st)
        write_riff_info(wp, trk)
        wav_paths.append(wp)

        # ---- 回读并断言 RIFF INFO
        back = read_riff_info(wp)
        expect = {"INAM": trk["title"], "IART": trk["artist"], "IPRD": trk["album"],
                  "ICRD": trk["year"], "IPRT": str(trk["no"]), "IGNR": trk["genre"],
                  "ICMT": trk["comment"]}
        for k, v in expect.items():
            assert k in back, "%s 缺 RIFF INFO %s" % (wp, k)
            assert back[k] == v, "%s 字段 %s 回读不一致: %r != %r" % (wp, k, back[k], v)
        dur_read = mutagen_duration(wp)
        assert abs(dur_read - trk["dur"]) < 0.05, "%s 时长回读 %.3f vs %.3f" % (wp, dur_read, trk["dur"])
        assert 25.0 <= dur_read <= 45.0, "%s 时长 %0.1fs 不在 25–45s" % (wp, dur_read)

        # ---- 峰值检查（软削峰后不应顶到 0 dBFS）
        peak = float(np.max(np.abs(st)))
        assert peak <= 1.0 + 1e-6, "%s 存在硬削顶" % wp

        rows.append(dict(trk=trk, wav=wp, dur=dur_read, fields=expect, peak_db=20 * np.log10(peak)))

    # ---- 歌词
    lrc_report = {}
    for row in rows:
        trk = row["trk"]
        if trk["lrc"] is None:
            lrc_report[trk["key"]] = None
            continue
        p = os.path.join(dir_of(trk), file_stem(trk) + ".lrc")
        write_lrc(p, trk)
        n, last = verify_lrc(p, trk["dur"], need_offset=(trk["lrc"].get("offset") is not None))
        lrc_report[trk["key"]] = (p, n, last)

    # ---- 封面：4 张专辑各一套配色，其中 1 张专辑故意不落盘
    cover_report = {}
    for album in ALBUMS:
        pal = ALBUM_COVER_PALETTE[album]
        if album == ALBUM_WITHOUT_COVER:
            cover_report[album] = None
            continue
        # 同一专辑可能出现在多个艺术家目录（split album），每个目录都放一份 cover.jpg
        dirs = []
        for trk in TRACKS:
            if trk["album"] == album:
                dd = dir_of(trk)
                if dd not in dirs:
                    dirs.append(dd)
        for dd in dirs:
            cp = os.path.join(dd, "cover.jpg")
            make_cover(cp, pal)
        cover_report[album] = dirs

    # ---- track 级封面
    track_cover_paths = []
    for key, pal in TRACK_COVER.items():
        trk = [t for t in TRACKS if t["key"] == key][0]
        tp = os.path.join(dir_of(trk), file_stem(trk) + ".jpg")
        make_cover(tp, pal)
        track_cover_paths.append(tp)

    # ---- 汇总核对表
    print()
    print("【RIFF INFO 回读断言】8/8 全部字段一致，通过")
    print("-" * 78)
    hdr = "%-42s %-20s %-20s %5s %7s %7s" % ("文件名", "标题(INAM)", "艺术家(IART)", "年份", "时长", "峰值dB")
    print(hdr)
    print("-" * 78)
    for row in rows:
        name = os.path.relpath(row["wav"], ROOT).replace("\\", "/")
        print("%-42s %-20s %-20s %5s %6.2fs %7.2f" % (
            os.path.basename(row["wav"]), row["trk"]["title"][:18], row["trk"]["artist"][:18],
            row["trk"]["year"], row["dur"], row["peak_db"]))
    print("-" * 78)
    print("歌词文件：")
    for row in rows:
        trk = row["trk"]
        info = lrc_report[trk["key"]]
        if info:
            print("  [有] %-38s 行数=%d 末行=%.2fs 双语=%s offset=%s" % (
                os.path.basename(info[0]), info[1], info[2], trk["lrc"]["lang"] == "bi",
                trk["lrc"].get("offset", "-")))
        else:
            print("  [无] %-38s （故意缺，测无歌词占位态）" % (file_stem(trk) + ".lrc"))
    print("封面：")
    for album, dirs in cover_report.items():
        if dirs is None:
            print("  [缺] %-34s cover.jpg（故意缺，测默认占位图回退）" % album)
        else:
            for dd in dirs:
                print("  [有] %s" % os.path.relpath(os.path.join(dd, "cover.jpg"), ROOT).replace("\\", "/"))
    for tp in track_cover_paths:
        print("  [track 级] %s" % os.path.relpath(tp, ROOT).replace("\\", "/"))

    total = 0
    cnt = 0
    for base, _ds, fs in os.walk(LIB):
        for fn in fs:
            total += os.path.getsize(os.path.join(base, fn))
            cnt += 1
    print("-" * 78)
    print("文件数 %d，总大小 %.2f MB (%d bytes)" % (cnt, total / 1048576.0, total))
    print("ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
