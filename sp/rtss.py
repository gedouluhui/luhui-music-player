# -*- coding: utf-8 -*-
"""RTSS / 微星小飞机 OSD 的按进程排除项。

RTSS 通过 RTSSHooks*.dll 注入每一个创建 D3D/OpenGL/Vulkan 设备的进程来画 OSD。
QtWebEngine 5.15 的 GPU 线程跑在浏览器进程里（打包后就是 LuHuiMusic.exe；历史探针
的 ``inProcessGpu=true`` 与 nvidia-smi 都指向主进程），渲染器 QtWebEngineProcess.exe
也可能被个别 RTSS 版本注入。RTSS 的官方按程序开关是
``<RTSS>\\Profiles\\<exe>.cfg`` 里的 ``[Hooking] EnableHooking = 0``（安装目录自带的
chrome.exe / msedge.exe / spotify.exe 模板就是这么写的）。两份都写齐，播放器才保留
--use-angle=d3d11 的硬件栅格化，而桌面角落不会冒出帧率条；缺任一份就整组退回软件光栅。

读永远安全；写只发生在用户显式执行 ``--install-rtss-exclusion`` 时，而且 Profiles
位于 Program Files，需要一次管理员权限。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, Optional, Tuple

GPU_EXE = "QtWebEngineProcess.exe"
APP_EXE = "LuHuiMusic.exe"
PROFILE_EXES = (GPU_EXE, APP_EXE)
DIR_ENV = "RTSS_PROFILE_DIR"


def profile_dirs() -> list:
    """候选的 RTSS Profiles 目录（环境变量优先，便于离线自检）。"""
    override = (os.environ.get(DIR_ENV) or "").strip()
    if override:
        # 自检/验收把这里指向临时目录时必须**完全替换**候选：否则真实机上的
        # profile 会先命中，用例想验的「只有一份」「EnableHooking=1」全被掩蔽。
        return [Path(override)]
    out = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        base = (os.environ.get(env) or "").strip()
        if base:
            out.append(Path(base) / "RivaTuner Statistics Server" / "Profiles")
    if not out:
        out.append(Path(r"C:\Program Files (x86)\RivaTuner Statistics Server\Profiles"))
    seen, uniq = set(), []
    for d in out:
        key = str(d).casefold()
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq


def read_enable_hooking(text: str) -> Optional[bool]:
    """``[Hooking] EnableHooking`` 的值；没有这一段/这一项时返回 None。"""
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section != "hooking" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().lower() != "enablehooking":
            continue
        v = value.strip().strip('"').lower()
        if v in ("0", "false", "no", "off"):
            return False
        if v in ("1", "true", "yes", "on"):
            return True
        return None
    return None


def _cfg(directory: Path, exe: str = GPU_EXE) -> Path:
    return Path(directory) / (exe + ".cfg")


def is_excluded(exe: str = GPU_EXE, dirs: Optional[Iterable] = None) -> bool:
    """是否已有一份把该 exe 的注入关掉的 profile。"""
    for d in (dirs if dirs is not None else profile_dirs()):
        try:
            text = _cfg(Path(d), exe).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if read_enable_hooking(text) is False:
            return True
    return False


def hooking_excluded_for_gpu() -> bool:
    """主进程（GPU 宿主）与渲染器两份都排除才算完成；缺一份就退回软件光栅。"""
    dirs = profile_dirs()
    return all(is_excluded(exe, dirs) for exe in PROFILE_EXES)


def set_hooking_disabled(text: str) -> str:
    """保留 profile 里其它键，只把 [Hooking] EnableHooking 改成 0。"""
    lines = text.splitlines() if text else []
    out, section, done = [], "", False
    for raw in lines:
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            if section == "hooking" and not done:
                out.append("EnableHooking\t\t= 0")
                done = True
            section = line[1:-1].strip().lower()
            out.append(raw)
            continue
        if section == "hooking" and "=" in line:
            key = line.split("=", 1)[0].strip().lower()
            if key == "enablehooking":
                out.append("EnableHooking\t\t= 0")
                done = True
                continue
        out.append(raw)
    if section == "hooking" and not done:
        out.append("EnableHooking\t\t= 0")
        done = True
    if not done:
        if out and out[-1].strip():
            out.append("")
        out.extend(["[Hooking]", "EnableHooking\t\t= 0"])
    return "\r\n".join(out).rstrip("\r\n") + "\r\n"


def status() -> dict:
    dirs = profile_dirs()
    existing = next((d for d in dirs if d.is_dir()), None)
    return {
        "excluded": hooking_excluded_for_gpu(),
        "dir": str(existing or ""),
        "candidates": [str(d) for d in dirs],
        "gpu_exe": GPU_EXE,
        "app_exe": APP_EXE,
    }


def install(exe: Optional[str] = None, dirs: Optional[Iterable] = None) -> Tuple[bool, str]:
    """写入（或补上）排除项。返回 (成功, 给用户看的说明)。

    默认同时覆盖主进程与渲染器：QtWebEngine 5.15 的 D3D 设备建在主进程里，
    只排除 QtWebEngineProcess.exe 挡不住 OSD。``exe`` 仅供离线用例单独写一份。
    """
    dirs = list(dirs if dirs is not None else profile_dirs())
    targets = (exe,) if exe else PROFILE_EXES
    if all(is_excluded(t, dirs) for t in targets):
        paths = []
        for t in targets:
            d = next(d for d in dirs if _cfg(d, t).is_file())
            paths.append(str(_cfg(d, t)))
        return True, "RTSS 排除项已存在：\n" + "\n".join(paths)
    target_dir = next((Path(d) for d in dirs if Path(d).is_dir()), None)
    if target_dir is None:
        return False, ("找不到 RivaTuner Statistics Server 的 Profiles 目录。\n"
                       "如果没装 RTSS / 微星小飞机，本程序不会显示 OSD，无需设置。")
    for t in targets:
        if is_excluded(t, [target_dir]):
            continue
        path = _cfg(target_dir, t)
        try:
            old = path.read_text(encoding="utf-8", errors="ignore") if path.is_file() else ""
        except OSError:
            old = ""
        try:
            path.write_text(set_hooking_disabled(old), encoding="utf-8", newline="")
        except PermissionError:
            return False, ("没有写入权限。请右键本程序（或同目录的“启用显卡加速.cmd”）\n"
                           "选择“以管理员身份运行”，再执行一次。")
        except OSError as e:
            return False, "写入 %s 失败：%s" % (path, e)
        if not is_excluded(t, [target_dir]):
            return False, "写入后复核未通过：%s" % path
    if not all(is_excluded(t, [target_dir]) for t in targets):
        return False, "写入后复核未通过：%s" % target_dir
    paths = [str(_cfg(target_dir, t)) for t in targets]
    return True, ("已写入 RTSS 排除项：\n%s\n\n下次启动将启用硬件栅格化，"
                  "微星小飞机的 OSD 不会出现在播放器里。" % "\n".join(paths))


def _message_box(title: str, text: str, error: bool = False) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)
    except Exception:
        pass


def install_cli() -> int:
    ok, msg = install()
    print(("RTSS 排除项：" if ok else "RTSS 排除项失败：") + msg)
    if getattr(sys, "frozen", False):
        _message_box("LuHui音乐播放器 · 显卡加速", msg, not ok)
    return 0 if ok else 1
