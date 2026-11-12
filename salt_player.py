#!/usr/bin/env python3
"""LuHui音乐播放器 launcher.

    python salt_player.py          -> legacy desktop app (Qt window + tray)
    python salt_player.py --server -> headless API, open the printed URL in a browser
    python salt_player.py --icon   -> (re)generate app.ico next to this file
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Console on Windows is GBK by default; Chinese log lines would crash it.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _frozen_env() -> None:
    """Point imageio-ffmpeg at the copy we ship; give the GUI build somewhere to print.

    windowed PyInstaller sets sys.stdout/stderr to None and the shell logs a dozen
    lines at startup -- without a sink the first print would raise inside
    PlayerApp.__init__ and the window would never appear.
    """
    if not getattr(sys, "frozen", False):
        return
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    candidates = [base / "ffmpeg.exe"]
    ffdir = base / "ffmpeg"
    if ffdir.is_dir():
        candidates += sorted(ffdir.glob("*.exe"))
    for cand in candidates:
        if cand.is_file():
            os.environ.setdefault("IMAGEIO_FFMPEG_EXE", str(cand))
            break
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


_frozen_env()


def _install_frozen_crash_log() -> None:
    """A windowed build has no console; leave a readable trace next to the library."""
    if not getattr(sys, "frozen", False):
        return
    import traceback

    def _hook(exc_type, exc, tb):
        try:
            from sp.config import data_root
            with (data_root() / "crash.log").open("a", encoding="utf-8") as f:
                f.write("\n===== launcher =====\n"
                        + "".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass

    sys.excepthook = _hook


_install_frozen_crash_log()


def _ascii_only(text: str) -> bool:
    return all(ord(ch) < 128 for ch in text)


def _ascii_alias(pkg_dir: Path):
    """An all-ASCII path that resolves to the same folder (NTFS junction).

    Qt 5.15 derives its install prefix from the DLL path through 8-bit strings:
    a non-ASCII install path degrades to '?' and QtWebEngine aborts before the
    window exists. A junction under %ProgramData% gives the same files an ASCII
    path, so a user who moves the folder into e.g. a Chinese-named directory
    still gets a working player.
    """
    import hashlib

    base = Path(os.environ.get("PROGRAMDATA") or "C:/ProgramData") / "SaltPlayerPro"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    tag = hashlib.sha1(str(pkg_dir).casefold().encode("utf-8")).hexdigest()[:12]
    link = base / ("run-" + tag)
    try:
        if link.is_junction() or link.exists():
            try:
                if Path(os.path.realpath(link)).samefile(pkg_dir):
                    return link
            except OSError:
                pass
            os.rmdir(link)  # junction only; never recurses into the target
    except OSError:
        return None
    try:
        res = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(pkg_dir)],
            capture_output=True, creationflags=0x08000000)
    except OSError:
        return None
    return link if res.returncode == 0 and link.is_dir() else None


def _relaunch_from_ascii_path() -> None:
    if not getattr(sys, "frozen", False) or _ascii_only(sys.executable):
        return
    exe = Path(sys.executable)
    alias = _ascii_alias(exe.parent)
    if alias is not None:
        try:
            subprocess.Popen([str(alias / exe.name), *sys.argv[1:]], close_fds=True)
            raise SystemExit(0)
        except OSError:
            pass
    import ctypes

    ctypes.windll.user32.MessageBoxW(
        None,
        "当前程序所在文件夹的路径包含非 ASCII 字符（例如中文），\n"
        "Qt 5.15 启动窗口时无法解析这类路径。\n\n"
        "请把整个 LuHuiMusic 文件夹移动到纯英文路径后重新打开，\n"
        "例如：D:\\LuHuiMusic",
        "LuHui音乐播放器", 0x10)
    raise SystemExit(1)


_relaunch_from_ascii_path()


def main() -> int:
    raw = sys.argv[1:]
    args = {a.lower() for a in raw}
    # Qt parses its own argv; a leftover "--icon"/"--server" makes QApplication
    # abort the whole process silently (exit 127). Keep only the program name.
    sys.argv = sys.argv[:1]
    from sp import app as spapp

    port = next((int(a) for a in raw if a.isdigit()), None)
    if "--server" in args:
        return spapp.run_server_only(port=port)
    if "--install-rtss-exclusion" in args:
        from sp import rtss
        return rtss.install_cli()
    if "--rtss-status" in args:
        from sp import rtss
        import json
        print(json.dumps(rtss.status(), ensure_ascii=False))
        return 0
    if "--icon" in args:
        from pathlib import Path
        out = spapp.write_icon_files(Path(__file__).resolve().parent)
        print(f"图标已生成: {out}")
        return 0
    if "--port" in args and port:
        from sp import api, db
        db.conn()
        srv = api.Server(port)
        print(srv.start(), flush=True)
        import time
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            srv.stop()
        return 0
    return spapp.run()


if __name__ == "__main__":
    raise SystemExit(main())
