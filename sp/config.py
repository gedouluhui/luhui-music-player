"""Runtime paths and constants.

Data lives in %LOCALAPPDATA%\\SaltPlayerPro so the app folder can stay read-only
and the library survives the project directory being moved.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "LuHui音乐播放器"
APP_ID = "SaltPlayerPro"

AUDIO_EXTS = {
    ".mp3", ".flac", ".m4a", ".mp4", ".aac", ".wav", ".ogg", ".oga",
    ".opus", ".wma", ".aiff", ".aif", ".ape", ".ttk", ".tak", ".dsf", ".wv",
}
COVER_NAMES = ("cover", "folder", "album", "front", "thumbnail", "artwork")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
LYRIC_EXTS = (".lrc", ".txt")


def data_root() -> Path:
    override = os.environ.get("SALTPLAYER_DATA")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / APP_ID
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")) / APP_ID
    base.mkdir(parents=True, exist_ok=True)
    return base


def web_root() -> Path:
    # PyInstaller 把 web/ 放进 _internal（onedir）或临时解包目录（onefile）；
    # 源码运行仍是项目根下的 web/。
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "web"
    return Path(__file__).resolve().parent.parent / "web"


DB_PATH = None  # lazily set below to keep import side-effect free


def db_path() -> Path:
    return data_root() / "library.sqlite3"


def settings_path() -> Path:
    return data_root() / "settings.json"


def port_path() -> Path:
    return data_root() / "server.port"


_NORM_CACHE: dict = {}
_NORM_CACHE_MAX = 60000


def normalize_path(p: str) -> str:
    """Windows-canonical path used as the join key everywhere.

    里面是 os.path.realpath —— 真的文件系统调用：路径存在时是一次 API 查询，不存在时
    Windows 会逐段解析到失败为止（实测 1129 次 ≈1.1 s，folders 接口原先就栽在这）。
    同一段路径在一个进程里会被反复规范（扫描、建树、按 id 取行都要），所以按输入做
    有界记忆。
    代价说清楚：进程运行期间如果有人改了目录联接/重映射盘符，本进程会继续用旧规范
    结果。对曲库管理器来说，这个窗口远没有「每次重问操作系统」值钱；真要重来，
    重新扫描时先调 reset_path_cache()。
    """
    if not p:
        return ""
    hit = _NORM_CACHE.get(p)
    if hit is not None:
        return hit
    r = _normalize_path_slow(p)
    if len(_NORM_CACHE) >= _NORM_CACHE_MAX:
        _NORM_CACHE.clear()        # 路径是一批一批来的，整体清空比 LRU 记账便宜
    _NORM_CACHE[p] = r
    return r


def reset_path_cache() -> None:
    """让下一次规范重新问操作系统（重新扫描、或换了挂载点时调用）。"""
    _NORM_CACHE.clear()


def _normalize_path_slow(p: str) -> str:
    if not p:
        return ""
    try:
        ap = os.path.abspath(p)
    except Exception:
        return p.strip().strip('"')
    if sys.platform == "win32":
        ap = os.path.normpath(ap)
        try:
            ap = os.path.realpath(ap)
        except Exception:
            pass
        drive, rest = os.path.splitdrive(ap)
        ap = drive.upper() + rest
    return ap
