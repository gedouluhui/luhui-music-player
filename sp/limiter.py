# -*- coding: utf-8 -*-
"""真峰值前视限幅器（共享输出用）。

为什么需要它：`np.clip(x, -1, 1)` 只在**样点**上切线。热母带的采样间峰在升采样后
会超过 1.0（本机 339：48k 源样点峰 0.9886 → 96k 重建峰 1.1719；5.1 下混 1115
峰 1.6973），直接 clip 会在峰上削出方波，听感是发硬、发毛。这里在峰到来前 5 ms
把增益压下去、峰过去后 120 ms 慢慢放回；低于 ceiling 的段落输出＝延迟后的原样本，
乘的是精确的 1.0，逐位不变（tools/probe_limiter.py 实测 maxdiff=0）。

这不是多段压缩、不是响度归一，也不进独占档（独占档的承诺是 bit-perfect，
一个样本都不许动）。共享档的 ceiling 定在 0.98（−0.18 dBFS）：给 DAC 重建滤波器
留 0.02 的余量；4× Catmull-Rom 估计在 339 上实测输出真峰 0.9825。
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import maximum_filter1d

DEFAULT_CEILING = 0.98
LOOK_MS = 5.0
ATTACK_MS = 0.6
RELEASE_MS = 120.0


def lookahead_samples(rate: int, look_ms: float = LOOK_MS) -> int:
    """前视窗 L（偶常数）。单独拎出来，是为了让「进度要扣多少延迟」和「限幅器真正
    延迟多少」永远算同一个数 —— 两边各写一份，改一边就会量出「进度条与耳朵差 5 ms」。"""
    L = int(round(int(rate) * float(look_ms) / 1000.0))
    L -= L % 2
    return max(2, L)


def latency_frames(rate: int, look_ms: float = LOOK_MS) -> int:
    """整条链的延迟 D=L+2（96 kHz 下 482 帧）。"""
    return lookahead_samples(rate, look_ms) + 2


class TruePeakLimiter:
    """流式 linked-stereo 真峰值限幅器；块大小任意，每块返回与输入等长的帧。

    内部总延迟 D=L+2（96 kHz 下 482 帧 ≈ 5.02 ms）。L 是前视窗，+2 是 Catmull-Rom
    还需要的两个未来样本；增益在样本被送出前就已经由「它自己 + 未来 L+2 帧」算好，
    所以不存在「先切线再补救」。
    """

    def __init__(self, rate: int, ch: int, ceiling: float = DEFAULT_CEILING,
                 look_ms: float = LOOK_MS):
        self.rate = int(rate)
        self.ch = int(ch)
        self.ceiling = float(ceiling)
        self.L = lookahead_samples(self.rate, look_ms)   # maximum_filter1d 要奇数窗
        self.D = self.L + 2
        self.a_att = 1.0 - np.exp(-1.0 / (ATTACK_MS / 1000.0 * self.rate))
        self.a_rel = 1.0 - np.exp(-1.0 / (RELEASE_MS / 1000.0 * self.rate))
        self.prev3 = np.zeros((3, self.ch), np.float32)
        self.delay = np.zeros((self.D, self.ch), np.float32)
        pad = 2 * self.L + 4                 # 盖住开头那些负数下标（延迟线里的零）
        self.tp = np.zeros(pad, np.float64)
        self.tp_start = -pad
        self.g = 1.0
        self.n_in = 0
        # 回显/验收用（都是本段起播以来的累计值，不参与算声）
        self.frames = 0
        self.demand_frames = 0
        self.limited_frames = 0
        self.min_g = 1.0
        self.input_peak = 0.0
        self.output_peak = 0.0
        # 前两次 _tp_new 吐的是全局 −2、−1，pad 已经盖住；跳掉后才是 0、1、2…
        self._skip = 2

    def _tp_new(self, seg: np.ndarray) -> np.ndarray:
        """seg=[x[T-3],x[T-2],x[T-1],x[T],…] ⇒ 全局 T-2 … T+n-3 的真峰包络。"""
        p0, p1, p2, p3 = seg[:-3], seg[1:-2], seg[2:-1], seg[3:]
        out = np.abs(p1).astype(np.float64)
        for t in (0.25, 0.5, 0.75):
            y = 0.5 * (2.0 * p1 + (-p0 + p2) * t +
                       (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * (t * t) +
                       (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * (t * t * t))
            np.maximum(out, np.abs(y), out=out)
        return out.max(axis=1)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        n = x.shape[0]
        if n == 0:
            return x
        if x.ndim == 1:
            x = x[:, None]
        if x.shape[1] != self.ch:
            raise ValueError("channels %d != %d" % (x.shape[1], self.ch))
        # 损坏文件解出的一个 NaN/Inf 会走「需要压缩」分支，把 d 污染成 NaN，
        # 随后 self.g 永久 NaN —— 整首歌从此静音/破音，直到换曲。入口先净化，
        # 保证递推状态只见到有限值；Inf 按满幅 1.0 收，NaN 按静音 0.0。
        if not np.all(np.isfinite(x)):
            x = np.nan_to_num(x, nan=0.0, posinf=1.0, neginf=-1.0)
        T = self.n_in
        seg = np.concatenate([self.prev3, x], axis=0)
        tp_new = self._tp_new(seg)
        if self._skip:
            k = min(self._skip, tp_new.size)
            tp_new = tp_new[k:]
            self._skip -= k
        self.tp = np.concatenate([self.tp, tp_new])
        self.prev3 = seg[-3:].copy()

        j0 = T - self.D
        r0 = j0 - self.tp_start
        need = n + self.L
        sl = self.tp[r0:r0 + need]
        if sl.size < need:                   # 只在极短首/尾块发生；补最后值不补 0
            sl = np.concatenate([sl, np.full(need - sl.size, sl[-1] if sl.size else 0.0)])
        peaks = maximum_filter1d(sl, size=self.L + 1, origin=-(self.L // 2),
                                 mode="nearest")[:n]
        self.input_peak = max(self.input_peak, float(peaks.max()))

        if peaks.max() <= self.ceiling and self.g >= 1.0 - 1e-9:
            # 整块都不需要压、上一段也已经放回 1：一个样本都不乘，逐位直通（只延迟 D）。
            # 安静曲目/低音量用户因此几乎不付这条链的 CPU。
            buf = np.concatenate([self.delay, x], axis=0)
            y = buf[:n].copy()
            self.delay = buf[n:].copy()
            self.g = 1.0
            self.frames += n
        else:
            d = np.minimum(1.0, self.ceiling / np.maximum(peaks, 1e-12))
            g = self.g
            if not np.isfinite(g):
                g = 1.0        # 老状态若已被污染，别让一次坏块成为永久故障
            gg = np.empty(n, np.float64)
            att, rel = self.a_att, self.a_rel
            for i in range(n):
                t = d[i]
                if t < g:
                    g += (t - g) * att
                    # g 从上方逼近 d，任何一步都不会大于 d ⇒ 不会越过 ceiling
                else:
                    g += (t - g) * rel
                if not np.isfinite(g):
                    g = 1.0
                gg[i] = g
            self.g = g
            self.min_g = min(self.min_g, float(gg.min()))
            self.demand_frames += int(np.count_nonzero(d < 1.0 - 1e-9))
            self.limited_frames += int(np.count_nonzero(gg < 1.0 - 1e-6))
            self.frames += n
            buf = np.concatenate([self.delay, x], axis=0)
            y = (buf[:n] * gg.astype(np.float32)[:, None]).astype(np.float32)
            self.delay = buf[n:].copy()

        self.output_peak = max(self.output_peak, float(np.max(np.abs(y))))
        cut = (j0 + n) - self.tp_start
        if cut > 0:
            self.tp = self.tp[cut:]
            self.tp_start += cut
        self.n_in = T + n
        return y

    def stats(self) -> dict:
        f = max(1, self.frames)
        return {
            "ceiling": self.ceiling,
            "look_ms": round(1000.0 * self.L / self.rate, 3),
            "latency_ms": round(1000.0 * self.D / self.rate, 3),
            # 0.0＝一次都没压（min_g 精确是 1.0）；正数 dB＝本段最大压了多少。
            "max_gr_db": round(float(-20.0 * np.log10(max(self.min_g, 1e-12))), 4),
            "demand_pct": round(100.0 * self.demand_frames / f, 4),
            "gain_lt_1_pct": round(100.0 * self.limited_frames / f, 4),
            "input_peak": round(self.input_peak, 6),
            "output_peak": round(self.output_peak, 6),
            "frames": self.frames,
        }
