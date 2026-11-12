# -*- coding: utf-8 -*-
"""Run the real WinUI shell on a private desktop and report boot evidence.

No window ever appears on the user's desktop, no audio is produced.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

import win32process
import win32service

ROOT = Path(__file__).resolve().parent
EXE = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    ROOT / "SaltPlayer.Native" / "bin" / "x64" / "Debug" /
    "net8.0-windows10.0.19041.0" / "win-x64" / "LuHuiMusic.exe")

DESKTOP = "SaltNativeAudit"
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.CreateDesktopW.restype = wintypes.HANDLE
user32.CreateDesktopW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID)
user32.CloseDesktop.argtypes = (wintypes.HANDLE,)

DESKTOP_ALL_ACCESS = 0x000F01FF


def crop_png(path: Path) -> None:
    """RenderTargetBitmap 会把根布局外溢的背景光晕也算进画布，裁到不透明区再存档。"""
    if "mini" in path.name:
        return   # 桌面歌词是半透明药丸，裁到 alpha>240 只会剩文字
    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return
    try:
        with Image.open(path) as im:
            rgba = im.convert("RGBA")
            alpha = np.asarray(rgba)[:, :, 3]
            mask = alpha > 240
            if not mask.any():
                return
            ys, xs = np.where(mask)
            box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
            if box == (0, 0, rgba.width, rgba.height):
                return
            rgba.crop(box).save(path)
    except Exception as exc:
        print("crop failed", path.name, exc)


def sample_idle(pid: int, audit: Path) -> None:
    """在 C# 侧的 8 秒闲置窗口里采样整棵进程树的 CPU 与内存。"""
    try:
        import json
        import psutil
    except Exception as exc:
        print("idle sample skipped:", exc)
        return
    try:
        parent = psutil.Process(pid)
        tree = [parent] + parent.children(recursive=True)
        for p in tree:
            p.cpu_percent(None)
        time.sleep(6)
        rows = []
        total_cpu = 0.0
        total_mem = 0.0
        for p in tree:
            try:
                cpu = p.cpu_percent(None) / (psutil.cpu_count() or 1)
                mem = p.memory_info().rss / 1048576
                total_cpu += cpu
                total_mem += mem
                rows.append({"pid": p.pid, "name": p.name(), "cpu_pct": round(cpu, 2), "mem_mb": round(mem, 1)})
            except psutil.Error:
                pass
        report = {"window_s": 6, "total_cpu_pct": round(total_cpu, 2),
                  "total_mem_mb": round(total_mem, 1), "processes": rows}
        (audit / "idle.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("---- idle ----")
        print(json.dumps(report))
    except Exception as exc:
        print("idle sample failed:", exc)


def main() -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    audit = Path(tempfile.gettempdir()) / f"salt-native-audit-{stamp}"
    data = audit / "data"
    data.mkdir(parents=True)
    trace = audit / "trace.log"
    shot = audit / "shot.png"

    source = os.environ.get("SALTPLAYER_AUDIT_DATA")
    if source and Path(source).is_dir():
        shutil.copytree(source, data, dirs_exist_ok=True)
        print("data copied from", source)

    ui_audit = os.environ.get("SALTPLAYER_NATIVE_AUDIT") == "1"
    ui_dir = audit / "ui"

    env = os.environ.copy()
    env["SALTPLAYER_DATA"] = str(data)
    env["SALTPLAYER_SILENCE"] = "1"
    env["SALTPLAYER_NATIVE_TRACE"] = str(trace)
    if ui_audit:
        env["SALTPLAYER_NATIVE_AUDIT"] = str(ui_dir)
    else:
        env["SALTPLAYER_NATIVE_SHOT"] = str(shot)
    env.pop("SALTPLAYER_NATIVE_API", None)

    hdesk = user32.CreateDesktopW(DESKTOP, None, None, 0, DESKTOP_ALL_ACCESS, None)
    if not hdesk:
        raise OSError(ctypes.get_last_error(), "CreateDesktopW failed")

    si = win32process.STARTUPINFO()
    si.lpDesktop = DESKTOP

    proc = thread = pid = None
    try:
        proc, thread, pid, _ = win32process.CreateProcess(
            str(EXE), f'"{EXE}"', None, None, False, 0, env, str(EXE.parent), si)
        print("pid", pid, "audit", audit)

        deadline = time.time() + 120
        ready = False
        while time.time() < deadline:
            text = trace.read_text(encoding="utf-8", errors="replace") if trace.exists() else ""
            if "ready" in text:
                ready = True
                break
            if "boot-error" in text or "error " in text:
                break
            time.sleep(0.25)

        shot_deadline = time.time() + (180 if ui_audit else 45)
        idle_sampled = False
        while ready and time.time() < shot_deadline:
            text = trace.read_text(encoding="utf-8", errors="replace") if trace.exists() else ""
            if ui_audit:
                if "audit-done" in text or "audit-error" in text:
                    break
                if not idle_sampled and "audit idle-begin" in text:
                    idle_sampled = True
                    sample_idle(pid, audit)
            elif "shot " in text or "shot-error" in text:
                break
            time.sleep(0.25)

        print("---- trace ----")
        print(trace.read_text(encoding="utf-8", errors="replace") if trace.exists() else "NO TRACE")
        if ui_audit:
            shots = sorted(ui_dir.glob("*.png")) if ui_dir.exists() else []
            for p in shots:
                crop_png(p)
            print("---- ui shots ----")
            for p in shots:
                print(" ", p.name, p.stat().st_size)
        else:
            if shot.exists():
                crop_png(shot)
            print("---- shot ----", "ok" if shot.exists() else "missing", shot)
        log = Path(tempfile.gettempdir()) / "SaltPlayer.Native" / "backend.log"
        if log.exists():
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            print("---- backend tail ----")
            print("\n".join(lines[-25:]))
        if ui_audit:
            done = ui_dir.exists() and any(ui_dir.glob("*.png"))
            return 0 if ready and done else 1
        return 0 if ready and shot.exists() else 1
    finally:
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True)
        if thread:
            thread.Close()
        if proc:
            proc.Close()
        user32.CloseDesktop(hdesk)


if __name__ == "__main__":
    raise SystemExit(main())
