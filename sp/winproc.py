# -*- coding: utf-8 -*-
"""Windows 上 spawn 控制台子程序（ffmpeg 等）时的「不许弹黑窗」参数。

ffmpeg.exe 是控制台子系统程序，而本 App 是 GUI 进程：不带 CREATE_NO_WINDOW 时
Windows 会给它新建一扇黑窗，并且**把那扇窗推到前台**。用户看到的是播放/切歌时
闪黑框；测试侧看到的是 App 窗口被抢焦点，CDP 的 Input.dispatchMouseEvent
连续超时（2026-10-22 02:2x 的 verify_app/feature_batch 真鼠标失败就是这个形状，
查前台句柄抓到的是 ffmpeg-win-x86_64-v7.1.exe）。参数与解码结果都不受影响。

任何新加的 ffmpeg/ffprobe spawn 都必须带上 `**hidden_popen_kwargs()`；
tools/test_audioplay.py::test_hidden_ffmpeg_window 会扫源码点名漏网的那一处。
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, Dict


def hidden_popen_kwargs() -> Dict[str, Any]:
    if os.name != "nt":
        return {}
    kw: Dict[str, Any] = {}
    kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kw["startupinfo"] = si
    except Exception:
        pass
    return kw
