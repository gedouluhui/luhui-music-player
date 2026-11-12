"""Qt shell: native window, tray, media keys, folder picker, drag-drop import.

Run:  python salt_player.py             -> window + tray (the app)
      python salt_player.py --server    -> headless API only (browser dev mode)
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import queue
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import api, db, playback, scanner
from .config import APP_NAME, data_root, normalize_path, port_path

MUTEX_PREFIX = "Global\\SaltPlayerPro_SingleInstance_"
ERROR_ALREADY_EXISTS = 183


# --------------------------------------------------------------- single instance

def _mutex_name() -> str:
    """One lock per data root: two profiles / an isolated audit instance may coexist,
    while a second launch on the same library is still refused."""
    root = str(data_root()).casefold().encode("utf-8")
    return MUTEX_PREFIX + hashlib.sha1(root).hexdigest()[:12]


def _mutex_acquire():
    """(True, handle) = 锁归我们；(False, None) = 别的进程还持有（包括正在退出的那个）。"""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, _mutex_name())
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        if handle:
            kernel32.CloseHandle(handle)
        return False, None
    return True, handle


def running_instance_alive() -> bool:
    """端口文件里那个实例是否真的还活着（只有 /api/health 应答才算）。"""
    try:
        port = int(port_path().read_text("utf-8").strip())
    except Exception:
        return False
    if not port:
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            return json.loads(r.read() or b"{}").get("ok") is True
    except Exception:
        return False


def acquire_single_instance(wait: float = 10.0) -> tuple[bool, Any]:
    """拿单实例锁。返回 False 只应该发生在「确实有另一个活着的实例」时。

    为什么不能「CreateMutex 说已存在就立刻退出」：旧实例退出时要等 Chromium 子进程
    和它的句柄一起释放，这段窗口里锁还被占着，但 /api/health 已经不应答了。实测撞过
    一次：quit 之后 0.3 s 内 health 就没了，紧接着起来的实例看到锁被占、focus 又打不通，
    于是打印「已有一个实例在运行，但无法激活它的窗口」然后自己退出——用户看到的就是
    「刚关掉，再点图标没反应」。
    所以锁被占时分真假：health 还在应答才是真对手；不应答就等它把锁放掉（最多 wait 秒）。
    """
    if sys.platform != "win32":
        return True, None
    deadline = time.time() + max(0.0, wait)
    while True:
        ok, handle = _mutex_acquire()
        if ok:
            return True, handle
        if running_instance_alive():
            return False, None          # 真有一个活实例在跑，交给调用方去唤醒它
        if time.time() >= deadline:
            return False, None          # 等不到锁释放：仍按「已有实例」处理，不双开
        time.sleep(0.4)


def focus_running_instance() -> bool:
    """Second launch -> bring the already-open window forward (never hide it)."""
    try:
        port = int(port_path().read_text("utf-8").strip())
    except Exception:
        return False
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/window/show", data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=3).read()
        return True
    except Exception:
        return False


# --------------------------------------------------------------- chromium flags

# "covered by another window" must not freeze the UI: the lyric/spectrum/seek-bar
# loop runs on requestAnimationFrame, and Chromium throttles that to a halt for
# occluded or backgrounded pages -- audio would keep playing behind a frozen UI.
REQUIRED_CHROMIUM_FLAGS = (
    "--autoplay-policy=no-user-gesture-required",
    "--disable-logging",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
)
REQUIRED_CHROMIUM_FEATURES = ("CalculateNativeWinOcclusion",)

# #54：显式要硬件栅格化。Chromium 87 / QtWebEngine 5.15 在这台机器上默认只给软件光栅
# （10-20 的证据：QtWebEngineProcess 的子进程里只有 renderer、没有 gpu-process），
# 而界面有 11 处 backdrop-filter —— 每一帧都要在 CPU 上重做模糊。
# 这三条是**产品必需项**：restart_dev 的 extra_flags 不许把它们顶掉（A/B 时用下面的
# SALT_NO_GPU=1 关掉整组，而不是逐条拆）。
GPU_CHROMIUM_FLAGS = (
    "--ignore-gpu-blocklist",
    "--enable-gpu-rasterization",
    "--enable-zero-copy",
)
# ANGLE 后端单独一类：它必须能被 restart_dev 的 --use-angle=… 顶掉，所以**不进** REQUIRED。
# 出厂默认点在 D3D11（这台是 RTX 3070 Ti Laptop）；env 里已有 --use-angle 时按「键已存在」
# 的规则保留用户那份，A/B 才做得出对照。
DEFAULT_GPU_FLAGS = ("--use-angle=d3d11",)
# 逃生门：驱动把 GPU 进程带崩时，一句环境变量就能整组退回软件光栅（不必改代码）。
GPU_OFF_ENV = "SALT_NO_GPU"
_GPU_OFF_TRUTHY = {"1", "true", "yes", "on"}

# 微星小飞机（MSI Afterburner）的 OSD 由 RTSS 通过 DLL 注入 D3D/OpenGL/Vulkan 进程来画。
# Chromium 一旦真的走 D3D11，RTSSHooksLoader 就把本程序当 3D 游戏注入，桌面角落冒出帧率条；
# 别的音乐播放器不冒，是因为它们本来就用软件合成。这里只在探测到注入器时整组退回软件光栅：
# 宁可让 backdrop-filter 回到 CPU，也不往用户屏幕上贴 OSD。
# 治本的办法是建 RTSS 按程序排除项（Profiles\LuHuiMusic.exe.cfg 与
# QtWebEngineProcess.exe.cfg，[Hooking] EnableHooking = 0）——QtWebEngine 5.15 的
# GPU 线程在主进程里，渲染器也可能被注入，两份都写齐才放行。文件在 Program Files 下，
# 只能由用户显式执行 --install-rtss-exclusion（管理员）写入；两份齐全时探测到
# RTSS 也照常开 GPU。
# 也可以设 SALT_ALLOW_GPU_WITH_RTSS=1 手动放行（OSD 会重新出现）。
RTSS_GUARD_ENV = "SALT_ALLOW_GPU_WITH_RTSS"
RTSS_PROCESS_NAMES = frozenset(("rtss.exe", "rtsshooksloader64.exe", "rtsshooksloader.exe"))

# 隔离审计开关：探针/验收进程不建托盘、不抢焦点、不注册全局媒体键、不弹气泡。
# 它们仍然 needs 一个已 show 的 QWebEngineView 才出帧，所以窗口本身不隐藏，
# 由探针放到私有桌面上；这条只保证进程不再往用户的桌面留下可见痕迹。
AUDIT_ENV = "SALT_AUDIT_HIDDEN"


def audit_hidden() -> bool:
    return (os.environ.get(AUDIT_ENV) or "").strip().lower() in _GPU_OFF_TRUTHY


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def rtss_running() -> bool:
    """RTSS 的注入器是否在跑（只按进程名探测，不 hook、不读写目标进程）。

    查 RTSSHooksLoader* 而不只是 RTSS.exe：主界面退出后 loader 仍在，而正是 loader
    把 OSD 注进每一个新建的 D3D 进程。
    """
    if sys.platform != "win32":
        return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.CreateToolhelp32Snapshot.argtypes = (ctypes.c_ulong, ctypes.c_ulong)
        k32.Process32FirstW.restype = ctypes.c_int
        k32.Process32FirstW.argtypes = (ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32W))
        k32.Process32NextW.restype = ctypes.c_int
        k32.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32W))
        k32.CloseHandle.argtypes = (ctypes.c_void_p,)
        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == ctypes.c_void_p(-1).value:
            return False
        try:
            entry = _ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                if entry.szExeFile.lower() in RTSS_PROCESS_NAMES:
                    return True
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return False
    return False


def rtss_guard_active(rtss: Optional[bool] = None) -> bool:
    if (os.environ.get(RTSS_GUARD_ENV) or "").strip().lower() in _GPU_OFF_TRUTHY:
        return False
    if rtss is None:
        rtss = rtss_running()
    if not rtss:
        return False
    from . import rtss as rtss_profile
    if rtss_profile.hooking_excluded_for_gpu():
        return False
    return True


def gpu_requested() -> bool:
    return (os.environ.get(GPU_OFF_ENV) or "").strip().lower() not in _GPU_OFF_TRUTHY


def gpu_flags_for(parts, *, rtss: bool = False) -> List[str]:
    """这一进程该追加哪些 GPU 开关（纯函数，给离线自检用）。

    三个前提都当场问：「用户没关」「没人显式 --disable-gpu」「没有 RTSS 在等注入」。
    中间那条是 Chromium 的硬开关，再追三条光栅化只会让日志看着像开了、实际全被它压住。
    ``rtss`` 由调用方传入而不是在这里探测，离线测试才能同时覆盖两种机器。
    """
    keys = {p.split("=", 1)[0] for p in parts}
    if "--disable-gpu" in keys or not gpu_requested():
        return []
    if rtss_guard_active(rtss):
        return ["--disable-gpu"]
    return list(GPU_CHROMIUM_FLAGS) + list(DEFAULT_GPU_FLAGS)


def ensure_chromium_flags(rtss: Optional[bool] = None) -> str:
    """Merge the switches this app needs into QTWEBENGINE_CHROMIUM_FLAGS.

    ``setdefault`` is not enough: the launcher can already export the variable,
    and Chromium honours only the last ``--disable-features``, so their values
    have to be unioned by hand instead of appended.
    """
    if rtss is None:
        rtss = rtss_running()
    parts = [p for p in (os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS") or "").split() if p]
    out: list = []
    names: set = set()
    features: set = set()
    for p in parts:
        key = p.split("=", 1)[0]
        if key == "--disable-features":
            features |= {f for f in p.split("=", 1)[1].split(",") if f and f != "0"}
            continue
        if key in names:
            continue
        names.add(key)
        out.append(p)
    features |= set(REQUIRED_CHROMIUM_FEATURES)
    for flag in list(REQUIRED_CHROMIUM_FLAGS) + gpu_flags_for(parts, rtss=bool(rtss)):
        key = flag.split("=", 1)[0]
        if key not in names:
            names.add(key)
            out.append(flag)
    out.append("--disable-features=" + ",".join(sorted(features)))
    value = " ".join(out)
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = value
    return value


# --------------------------------------------------------------- window cosmetics

def apply_dark_titlebar(hwnd: int, dark: bool = True) -> None:
    """DWM attribute so the native caption matches the dark UI (Win10 1809+/Win11)."""
    if sys.platform != "win32" or not hwnd:
        return
    try:
        dwm = ctypes.windll.dwmapi
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE, build-dependent index
            dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attr, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


SW_SHOW = 5
SW_RESTORE = 9


def ensure_window_visible(hwnd: int) -> bool:
    """If the startup hint hid or minimized our window, bring it back. True = we intervened.

    Windows 把父进程的 STARTUPINFO.wShowWindow 一路交给我们进程「第一次 ShowWindow」：
    WScript.Shell.Run 的 style 0、subprocess 的 CREATE_NO_WINDOW 都会写 SW_HIDE。
    于是静默启动时 Qt 照常调了 show()、事件循环也在跑（HTTP 接口全都应答），但 Win32
    层拿不到 WS_VISIBLE —— 用户看到的是「双击图标，只有托盘图标，窗口不出来」。
    实测对照：同一个 pythonw 启动，默认 STARTUPINFO 下 1.0 s 就有可见窗口；带
    SW_HIDE 时 45 s 内一个都没有。
    Qt 内部认为窗口已经可见（它只是把 hint 转交给 ShowWindow），所以再调一次
    QWidget.show() 是空操作，必须直接用 Win32 补这一刀。
    另外一条实测：对 SW_HIDE 起来的窗口只发 SW_SHOW，结果是 visible=True 但
    minimized=True（rect 变成 -21333,-21333,158x26）——用户同样看不见界面。所以
    先判 IsIconic，最小化态得用 SW_RESTORE。
    """
    if sys.platform != "win32" or not hwnd:
        return False
    user32 = ctypes.windll.user32
    try:
        h = ctypes.c_void_p(hwnd)
        if user32.IsIconic(h):
            user32.ShowWindow(h, SW_RESTORE)
            return True
        if user32.IsWindowVisible(h):
            return False
        user32.ShowWindow(h, SW_SHOW)
        return True
    except Exception:
        return False


def ensure_qt_attributes() -> bool:
    """QtWebEngine 的合成器需要一组共享的 OpenGL 上下文；这条属性只在**第一个 QApplication
    建出来之前**有效，事后再设等于没设（Qt 静默忽略）。

    正常启动时 PlayerApp.__init__ 是第一个建 app 的地方；但 --icon 那条路会先经
    ``_ensure_gui_app()`` 建一个 app 只为画图标 —— 所以两个入口都要先过这里。
    返回值问的是**实际状态**（testAttribute），不是「我刚调用过」。
    """
    try:
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QApplication
    except Exception:
        return False
    try:
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
        return bool(QApplication.testAttribute(Qt.AA_ShareOpenGLContexts))
    except Exception:
        return False


def _ensure_gui_app():
    """QPixmap/QPainter aborts the process without a Q(Gui)Application alive."""
    from PyQt5.QtWidgets import QApplication
    ensure_qt_attributes()
    return QApplication.instance() or QApplication(sys.argv[:1])


def make_icon_pixmap(size: int = 256):
    _ensure_gui_app()
    from PyQt5.QtCore import QPointF, QRectF, Qt
    from PyQt5.QtGui import (QBrush, QColor, QIcon, QLinearGradient, QPainter, QPolygonF)
    from PyQt5.QtGui import QPixmap

    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor("#ff9a5a"))
    grad.setColorAt(0.55, QColor("#f4693f"))
    grad.setColorAt(1.0, QColor("#a3286b"))
    p.setBrush(QBrush(grad))
    p.setPen(Qt.NoPen)
    rad = size * 0.22
    p.drawRoundedRect(QRectF(size * 0.06, size * 0.06, size * 0.88, size * 0.88), rad, rad)
    cx, cy, s = size * 0.45, size * 0.5, size * 0.2
    p.setBrush(QColor(255, 255, 255, 242))
    p.drawPolygon(QPolygonF([
        QPointF(cx - s * 0.5, cy - s), QPointF(cx - s * 0.5, cy + s), QPointF(cx + s, cy)
    ]))
    p.setBrush(QColor(255, 255, 255, 205))
    for gx, gy in [(0.72, 0.26), (0.80, 0.38), (0.73, 0.48), (0.81, 0.58), (0.75, 0.70)]:
        d = size * 0.032
        p.drawEllipse(QPointF(size * gx, size * gy), d, d)
    p.end()
    return pm


def make_icon(size: int = 256):
    from PyQt5.QtGui import QIcon
    return QIcon(make_icon_pixmap(size))


def write_icon_files(out_dir: Path) -> Optional[Path]:
    """app.ico for the shortcut / taskbar identity (PNG fallback if Pillow is absent)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / "app.png"
    if not make_icon_pixmap(256).save(str(png), "PNG"):
        return None
    ico = out_dir / "app.ico"
    try:
        from PIL import Image
        img = Image.open(png).convert("RGBA")
        img.save(ico, format="ICO", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    except Exception:
        ico.write_bytes(png.read_bytes())
    return ico


# --------------------------------------------------------------- GUI bridge

class GuiBridge:
    """Marshals requests from API worker threads onto the Qt GUI thread."""

    def __init__(self) -> None:
        self.requests: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._results: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def call(self, kind: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 300.0) -> Any:
        rid = uuid.uuid4().hex
        self.requests.put({"id": rid, "kind": kind, "payload": payload or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if rid in self._results:
                    return self._results.pop(rid)
            time.sleep(0.03)
        return None

    def put_result(self, rid: str, value: Any) -> None:
        with self._lock:
            self._results[rid] = value


# --------------------------------------------------------------- the app object

class PlayerApp:
    def __init__(self) -> None:
        from PyQt5.QtCore import Qt, QTimer, QUrl
        from PyQt5.QtGui import QKeySequence, QColor
        from PyQt5.QtWidgets import (QApplication, QFileDialog, QMenu, QMessageBox, QShortcut,
                                     QSystemTrayIcon)
        from .qtcompat import QWebEnginePage, QWebEngineSettings, QWebEngineView

        _rtss = rtss_running()
        ensure_chromium_flags(rtss=_rtss)
        print(f"[chromium-flags] {os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')}", flush=True)
        # 这一行是给 tools/probe_gpu.py 在日志里读的：它要能分开「出厂真的追了 GPU 开关」
        # 与「用户/对照趟把它关了」，而不是只从旗标串里猜。判的是**合并后的实际串**：
        # env 里若已有 --disable-gpu，gpu_flags_for 会一条都不加，这里就照实写 off。
        _eff = (os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS") or "").split()
        print("[gpu-switch] " + ("on" if any(f in _eff for f in GPU_CHROMIUM_FLAGS) else "off"),
              flush=True)
        _guard = rtss_guard_active(_rtss)
        print(f"[rtss-guard] {'on' if _guard else 'off'} rtss={int(bool(_rtss))}", flush=True)
        self.Qt, self.QTimer, self.QKeySequence, self.QShortcut = Qt, QTimer, QKeySequence, QShortcut
        self.QFileDialog, self.QMenu, self.QMessageBox = QFileDialog, QMenu, QMessageBox
        self.QSystemTrayIcon, self.QWebEnginePage = QSystemTrayIcon, QWebEnginePage
        # PyQt 5.15 exposes the WebAttribute members as class shortcuts; the
        # nested ``WebAttribute.<Name>`` enum object does not resolve on this build.
        self.Settings = QWebEngineSettings
        shared_ctx = ensure_qt_attributes()
        print(f"[qt-attrs] share-opengl-contexts={shared_ctx}", flush=True)
        self.app = QApplication.instance() or QApplication(sys.argv[:1])
        self.app.setApplicationName(APP_NAME)
        self.app.setApplicationDisplayName(APP_NAME)
        self.app.setQuitOnLastWindowClosed(False)

        self.bridge = GuiBridge()
        self._shortcuts = []
        # 文件系统看护（_watch_files）：目录树的 mtime 快照 + 上次检查时刻
        self._tree_stamp_last = None
        self._tree_check_at = 0.0
        self.tray = None
        # 桌面歌词小窗：第一次真要显示时才建，关掉只藏不删（用户点一下开关不该有空窗）。
        self.mini = None
        self._mini_last_rect = None
        self.server = api.Server()
        self.url = self.server.start()
        api.UI_HOOKS["pick_folder"] = lambda start="": self.bridge.call("pick_folder", {"start": start})
        api.UI_HOOKS["pick_file"] = lambda start="": self.bridge.call("pick_file", {"start": start})
        api.WINDOW_HOOKS.update({
            "focus": lambda: self.bridge.call("focus", {}, timeout=5),
            "show": lambda: self.bridge.call("show", {}, timeout=5),
            "theme": lambda dark: self.bridge.call("theme", {"dark": bool(dark)}, timeout=5),
            "mini": lambda show, on_top=None: self.bridge.call(
                "mini", {"show": bool(show), "on_top": on_top}, timeout=5),
            "about": lambda: self.bridge.call("about", {}, timeout=5),
            "quit": lambda: self.bridge.call("quit", {}, timeout=5),
        })

        self.icon = make_icon(256)
        self.window = QWebEngineView()
        # 右键是 App 自己的事：Chromium 默认菜单（后退/重新加载/另存为…）一律不许出现。
        # 前端在 web/app.js 里按上下文给菜单（曲目/歌单/文件夹/输入框/复制），空地上不弹。
        try:
            self.window.setContextMenuPolicy(Qt.NoContextMenu)
        except Exception as e:
            print(f"[context-menu] 压不住默认菜单: {e}", flush=True)
        self.window.setWindowTitle(APP_NAME)
        self.window.setWindowIcon(self.icon)
        try:
            # no white flash before the CSS paints
            self.window.page().setBackgroundColor(QColor("#0d1016"))
        except Exception:
            pass
        st = self.window.settings()
        wanted = (
            ("JavascriptEnabled", True),
            ("WebGLEnabled", True),
            ("PlaybackRequiresUserGesture", False),
            ("LocalContentCanAccessRemoteUrls", True),
            ("LocalContentCanAccessFileUrls", True),
            ("FocusOnNavigationEnabled", True),
            # 滚动动画器在 Chromium 87 上会给每一帧再插一段合成动画；列表行本来就是
            # content-visibility 分页渲染，关掉它直接跟手（表头也已去掉逐帧 backdrop blur）。
            ("ScrollAnimatorEnabled", False),
            ("FullScreenSupportEnabled", True),
            ("JavascriptCanOpenWindows", True),
            ("JavascriptCanAccessClipboard", True),
        )
        for name, on in wanted:
            attr = getattr(self.Settings, name, None)
            if attr is None:
                print(f"[web-engine] 无此设置项: {name}", flush=True)
                continue
            try:
                st.setAttribute(attr, on)
            except Exception as e:
                print(f"[web-engine] {name} 设置失败: {e}", flush=True)
        self.window.page().titleChanged.connect(self._on_title_changed)
        self.window.loadFinished.connect(self._on_loaded)

        self._restore_geometry()
        self.window.show()
        self._rescue_hidden_window("startup")
        self._audit = audit_hidden()
        if not self._audit:
            self._make_tray()
            self._media_keys()
        self._drag_guard()
        self.pump = QTimer()
        self.pump.timeout.connect(self._drain_bridge)
        self.pump.start(70)
        self.window.load(QUrl(self.url))
        self._auto_scan_if_empty()

    # ------------------------------------------------------------- geometry
    def _restore_geometry(self) -> None:
        from PyQt5.QtCore import QRect
        from PyQt5.QtGui import QGuiApplication
        w = api.load_settings().get("window", {}) or {}
        screen = QGuiApplication.primaryScreen().availableGeometry()
        width = max(880, min(int(w.get("w") or 1320), screen.width() - 40))
        height = max(560, min(int(w.get("h") or 830), screen.height() - 40))
        geo = QRect(int(w.get("x") if w.get("x") is not None else 0),
                    int(w.get("y") if w.get("y") is not None else 0), width, height)
        if w.get("x") is None or w.get("y") is None or not screen.intersects(geo):
            geo.moveCenter(screen.center())
        self.window.setGeometry(geo)
        # 尺寸下限是「界面还能不能用」的下限，不是审美：拖到 532px 宽时 .player 的三列网格
        # 地板（230+230+间距+内边距=536px）已经把中间列挤成 0 宽 —— 进度条不可见不可拖、
        # 频谱画布溢出盖住播放键。760 这一档 CSS 兜住了 560–760，再往下就让窗口系统拦。
        self.window.setMinimumSize(560, 480)
        if w.get("maximized"):
            self.window.showMaximized()

    def _persist_geometry(self) -> None:
        try:
            maximized = bool(self.window.windowState() & self.Qt.WindowMaximized)
            geo = self.window.normalGeometry() if maximized else self.window.geometry()
            api.save_settings({"window": {"x": geo.x(), "y": geo.y(), "w": geo.width(),
                                          "h": geo.height(), "maximized": maximized}})
        except Exception:
            pass

    # ------------------------------------------------------------- 桌面歌词小窗
    # 原生 QWidget 药丸（sp/mini_pill.py）：Chromium 的合成器会把一块不透明表面提交给窗口，
    # 四角永远是白底（功能对照表 §9 的实测），所以这条不再经 QWebEngineView。位置仍落
    # mini_window，开关/置顶仍落 desktop_lyric / desktop_lyric_on_top，网页那边的契约没变。
    def _ensure_mini(self):
        if self.mini is not None:
            return self.mini
        from .mini_pill import MiniPill
        s = api.load_settings() or {}
        try:
            scale = float(s.get("desktop_lyric_scale") or 1.0)
        except (TypeError, ValueError):
            scale = 1.0
        v = MiniPill(base_url=self.url,
                     on_top_changed=self._on_mini_top_changed,
                     on_close=self._on_mini_close,
                     on_geometry=self._on_mini_geometry,
                     on_scale=self._on_mini_scale,
                     scale=scale)
        v.setWindowTitle("桌面歌词")
        v.setWindowIcon(self.icon)
        # 构造时默认会把取数表起起来；真正开不开由 _apply_mini 决定，先停掉。
        v.stop_reader()
        self._restore_mini_geometry(v)
        self.mini = v
        # 原生药丸没有网页可让 CDP 读，读数面改由 GET /api/mini/state 交出去。
        api.WINDOW_HOOKS["mini_state"] = self._mini_state_view
        print("[mini] 原生 MiniPill 已建立（QPainter 直画，无 Chromium 合成表面）", flush=True)
        return v

    def _apply_mini(self, show: bool, on_top: Optional[bool]) -> Dict[str, Any]:
        """把界面上点出来的意图落到真的窗口状态上，并把「落成了什么」报回去。
        报回意图不报回结果，验证脚本就只是在自证 —— 所以这里读的是 isVisible()
        和 windowFlags()，不是传进来的那两个参数。"""
        on_top = True if on_top is None else bool(on_top)
        v = self._ensure_mini()
        flags_before = int(v.windowFlags())
        v.set_on_top(on_top)
        if show:
            if not v.isVisible():
                v.show()
            v.start_reader()
            if on_top:
                v.raise_()
        else:
            v.stop_reader()
            if v.isVisible():
                v.hide()
        playback.set_on_top(on_top)
        # 开关的最终状态以「窗口真的落成什么样」为准写回设置：小窗上那个 ✕ 也是关闭，
        # 网页这边看不见原生窗口，只知道自己发过一次请求。两边都写同一个键，
        # 界面下次聚焦时读回来的就是真值（不会出现「药丸没了但开关还亮着」）。
        try:
            api.save_settings({"desktop_lyric": bool(show), "desktop_lyric_on_top": bool(on_top)})
        except Exception as e:
            print(f"[mini] 开关状态落盘失败：{e}", flush=True)
        g = v.geometry()
        return {"shown": bool(v.isVisible()),
                "on_top": bool(int(v.windowFlags()) & int(self.Qt.WindowStaysOnTopHint)),
                "flags_changed": flags_before != int(v.windowFlags()),
                # 原生画法的透明不是「装了几个属性」：`MiniPill` 自己同时设了
                # WA_TranslucentBackground / WA_NoSystemBackground / WA_ShowWithoutActivating，
                # 圆角外不画像素。这里把实际属性位报回去，别让旧脚本以为它还在补装子件。
                "translucent_set": int(bool(v.testAttribute(self.Qt.WA_TranslucentBackground))),
                "drag_widgets": 1,            # 拖动内建在 MiniPill.mouse*Event 上
                "engine": "native",
                "x": g.x(), "y": g.y(), "w": g.width(), "h": g.height()}

    def _restore_mini_geometry(self, v) -> None:
        from PyQt5.QtCore import QPoint
        from PyQt5.QtGui import QGuiApplication
        w = api.load_settings().get("mini_window") or {}
        screen = QGuiApplication.primaryScreen().availableGeometry()
        width = max(240, min(int(w.get("w") or 460), screen.width() - 20))
        height = max(44, min(int(w.get("h") or 68), screen.height() - 20))
        x, y = w.get("x"), w.get("y")
        if x is None or y is None:
            # 默认落在屏幕下方居中：桌面歌词是给「主窗收进托盘」用的，
            # 贴在任务栏上方一条最不打扰别的窗口。
            x = screen.center().x() - width // 2
            y = screen.bottom() - height - 56
        p = QPoint(int(x), int(y))
        if not screen.contains(p):
            p = QPoint(screen.center().x() - width // 2, screen.center().y() - height // 2)
        v.setGeometry(p.x(), p.y(), width, height)
        # #52：设置与窗矩形是两条通道，只回一条等于没回。落盘的是**夹取之后真正落成的矩形**
        # （屏幕外坐标/越界尺寸会被上面两条改掉），所以下次 boot 的期望值和窗实际位置一致；
        # 探针把设置 POST 回来时也能用同一份值核两处。
        self._persist_mini_geometry(pill=v)

    def _persist_mini_geometry(self, pill=None) -> None:
        v = pill if pill is not None else self.mini
        if v is None:
            return
        try:
            g = v.geometry()
            rect = {"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height()}
            api.save_settings({"mini_window": rect})
            self._mini_last_rect = rect
        except Exception:
            pass

    def _mini_state_view(self) -> Dict[str, Any]:
        """GET /api/mini/state 的读数面：state() 原样 + 画布几何（量具定位内容盒用）。
        state() 的键仍是旧 window.Mini.state() 那一份；box/w/h 只在这条 HTTP 通道上加，
        所以离线契约测试与屏上探针各拿各的，谁也不用迁就谁。"""
        v = self.mini
        if v is None:
            return {}
        st = dict(v.state())
        st["box"] = v.paint_box()
        st["chips"] = v.chips()
        w, h = v.canvas()          # 画布缓存：HTTP 线程只读 Python 值，不碰 QWidget
        st["w"], st["h"] = w, h
        return st

    # ---- 原生药丸上的三颗按钮：置顶 / 关闭 / 字号
    def _on_mini_top_changed(self, on_top: bool) -> None:
        playback.set_on_top(bool(on_top))
        try:
            api.save_settings({"desktop_lyric_on_top": bool(on_top)})
        except Exception as e:
            print(f"[mini] 置顶落盘失败：{e}", flush=True)

    def _on_mini_close(self) -> None:
        try:
            if self.mini is not None:
                self.mini.stop_reader()
                self.mini.hide()
        except Exception:
            pass
        try:
            api.save_settings({"desktop_lyric": False})
        except Exception as e:
            print(f"[mini] 关闭落盘失败：{e}", flush=True)

    def _on_mini_geometry(self, pill) -> None:
        # 只在 MiniPill 确认「真的挪过」之后进来（见 mouseReleaseEvent 的 #52 注释）。
        self._persist_mini_geometry(pill=pill)

    def _on_mini_scale(self, scale: float) -> None:
        try:
            api.save_settings({"desktop_lyric_scale": float(scale)})
        except Exception as e:
            print(f"[mini] 字号落盘失败：{e}", flush=True)

    # ------------------------------------------------------------- callbacks
    def _hwnd(self) -> int:
        if sys.platform != "win32":
            return 0
        try:
            return int(self.window.winId())
        except Exception:
            return 0

    def _window_shown(self) -> bool:
        """Win32 真相：既要有 WS_VISIBLE，也不能是 minimized。

        启动 hint 藏过窗口时 Qt 的 isVisible() 会说「已显示」；被最小化时
        IsWindowVisible 也还是 True。两条都会让托盘图标一点就「隐藏」，
        而屏幕上本来就没有窗口——所以这里一次把两个坑都堵掉。
        """
        hwnd = self._hwnd()
        if not hwnd:
            return self.window.isVisible()
        try:
            user32 = ctypes.windll.user32
            h = ctypes.c_void_p(hwnd)
            return bool(user32.IsWindowVisible(h)) and not bool(user32.IsIconic(h))
        except Exception:
            return self.window.isVisible()

    def _rescue_hidden_window(self, stage: str) -> None:
        if ensure_window_visible(self._hwnd()):
            print(f"[window] {stage}: 启动参数把窗口藏了(SW_HIDE)，已用 ShowWindow 找回", flush=True)

    def _on_title_changed(self, title: str) -> None:
        self.window.setWindowTitle(title or APP_NAME)

    def _on_loaded(self, ok: bool) -> None:
        hwnd = self._hwnd()
        apply_dark_titlebar(hwnd, api.load_settings().get("theme", "dark") != "light")
        self._rescue_hidden_window("loaded")
        if getattr(self, "_audit", False):
            return
        self.window.activateWindow()
        self.window.raise_()

    def _close_event(self, event) -> None:
        if api.load_settings().get("close_to_tray", True) and self.tray.isVisible():
            self.window.hide()
            self._toast(APP_NAME, "已缩到托盘，音乐继续播放。右键托盘图标可退出。")
            event.ignore()
        else:
            self._persist_geometry()
            event.accept()
            self.app.quit()

    def _key_press(self, event) -> None:
        from PyQt5.QtWidgets import QWidget
        QWidget.keyPressEvent(self.window, event)

    def _media_keys(self) -> None:
        """Application-wide shortcuts for the OS media keys (play/pause, next, prev, stop)."""
        # This PyQt 5.15 build has no QKeySequence.StandardKey.Media*, but Qt.Key_*
        # works through the integer QKeySequence constructor.
        pairs = [("Key_MediaPlay", "playpause"), ("Key_MediaPause", "playpause"),
                 ("Key_MediaTogglePlayPause", "playpause"), ("Key_MediaNext", "next"),
                 ("Key_MediaPrevious", "prev"), ("Key_MediaStop", "stop")]
        registered = 0
        for name, action in pairs:
            code = getattr(self.Qt, name, None)
            if code is None:
                continue
            try:
                sc = self.QShortcut(self.QKeySequence(int(code)), self.window)
                sc.setContext(self.Qt.ApplicationShortcut)
                sc.activated.connect(lambda a=action: self._js(f"window.SaltShell&&SaltShell.key('{a}')"))
                self._shortcuts.append(sc)
                registered += 1
            except Exception as e:
                print(f"[media-keys] {name} 注册失败: {e}", flush=True)
        print(f"[media-keys] 注册 {registered} 个", flush=True)

    def _js(self, code: str, callback=None) -> None:
        try:
            self.window.page().runJavaScript(code, callback) if callback else self.window.page().runJavaScript(code)
        except Exception:
            pass

    def _toast(self, title: str, body: str) -> None:
        try:
            if self.tray and self.tray.isVisible():
                self.tray.showMessage(title, body, self.QSystemTrayIcon.Information, 4000)
        except Exception:
            pass

    # ------------------------------------------------------------- tray
    def _make_tray(self) -> None:
        self.tray = self.QSystemTrayIcon(self.icon)
        menu = self.QMenu()
        menu.addAction("显示 / 隐藏主界面", self._toggle)
        for label, act in (("播放 / 暂停", "playpause"), ("下一曲", "next"), ("上一曲", "prev"), ("停止", "stop")):
            menu.addAction(label, lambda a=act: self._js(f"window.SaltShell&&SaltShell.key('{a}')"))
        menu.addSeparator()
        menu.addAction("重新扫描曲库", lambda: scanner.scan_async(
            None, bool(api.load_settings().get("include_unsupported", False))))
        menu.addAction("退出", self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.setToolTip(APP_NAME)
        self.tray.show()

    def _tray_activated(self, reason) -> None:
        if reason == self.QSystemTrayIcon.Trigger:
            self._toggle()

    def _toggle(self) -> None:
        # _window_shown() 已经把「隐藏」和「最小化」都算进去了：最小化时点托盘应该恢复，
        # 不是再 hide 一次（那样屏幕上什么都没有）。
        if self._window_shown():
            self.window.hide()
        else:
            self._show_window()

    def _show_window(self) -> None:
        """Bring the window on screen and activate it (never toggles off)."""
        if not self.window.isVisible():
            self.window.show()
        if self.window.isMinimized():
            self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()
        # Qt 可能压根没调原生 ShowWindow（它以为自己已经显示过了），所以这里补 Win32 一刀。
        ensure_window_visible(self._hwnd())

    def _quit(self) -> None:
        self._js("window.Salt && Salt.flush && Salt.flush()")
        time.sleep(0.25)
        self._persist_geometry()
        self._persist_mini_geometry()   # 小窗位置也要落盘：下次开机那条药丸该在用户放的地方
        try:
            if self.mini is not None:
                self.mini.stop_reader()
                self.mini.hide()
        except Exception:
            pass
        try:
            self.tray.hide()
        except Exception:
            pass
        self.server.stop()
        self.app.quit()

    # ------------------------------------------------------------- drag & drop
    def _drag_guard(self) -> None:
        from PyQt5.QtCore import QEvent, QObject
        outer = self

        class ShellFilter(QObject):
            """窗口级事件看门：拖进来的是文件夹，窗口刚被激活时顺便看一眼磁盘有没有变。"""

            def eventFilter(self, obj, ev):
                t = ev.type()
                if t == QEvent.Drop and ev.mimeData().hasUrls():
                    added = False
                    for url in ev.mimeData().urls():
                        p = url.toLocalFile()
                        if p and os.path.isdir(p):
                            outer.add_folder(p)
                            added = True
                    ev.acceptProposedAction()
                    return not added
                if t == QEvent.DragEnter and ev.mimeData().hasUrls():
                    ev.acceptProposedAction()
                    return True
                if t == QEvent.WindowActivate:
                    # 用户多半是「在资源管理器里塞完歌再切回播放器」，这一刻查最划算：
                    # 平时不烧 CPU，也不需要 ReadDirectoryChangesW 那种长驻监听。
                    outer._watch_files()
                    return False
                return False

        self._filter = ShellFilter(self.window)
        self.window.setAcceptDrops(True)
        self.window.installEventFilter(self._filter)

    # ------------------------------------------------------- 外部改动看护
    def _tree_stamp(self) -> float:
        """曲库里「所有目录（含子目录）最近的 mtime」。

        往某个专辑文件夹里加/删一个文件，会改动那个文件夹自身的 mtime，所以只 stat
        目录就能发现增删，代价是一次目录树遍历（几百个目录 ≈ 几毫秒），不碰文件本身。
        覆盖不到的：原地改标签/封面（文件夹 mtime 不变），以及刚在外部改完就立刻断电
        这种极端情况；前者靠设置里的「重新扫描全部」兜底。
        """
        best = 0.0
        for lib in db.list_libraries():
            stack = [lib["path"]]
            while stack:
                d = stack.pop()
                try:
                    best = max(best, os.stat(d).st_mtime)
                    with os.scandir(d) as it:
                        for e in it:
                            try:
                                if e.is_dir(follow_symlinks=False):
                                    stack.append(e.path)
                            except OSError:
                                continue
                except OSError:
                    continue          # 没权限/正在消失的目录：跳过，不影响其它库
        return best

    def _watch_files(self) -> None:
        now = time.time()
        if now - self._tree_check_at < 45:      # Alt+Tab 来回切不该每次都遍历
            return
        self._tree_check_at = now
        stamp = self._tree_stamp()
        prev = self._tree_stamp_last
        self._tree_stamp_last = stamp
        if prev is None or stamp <= prev:
            return                              # 第一次只记基线；没变就什么都不做
        if scanner.state_snapshot().get("running"):
            return
        # 扫描本身交给前端发起：这样进度条、扫完刷新侧栏/视图都走同一条路径
        # （startScan → watchScan），不会出现「后台扫完了但界面没知道」。
        self._js("window.SaltShell&&SaltShell.onFilesChanged&&SaltShell.onFilesChanged()")

    def _auto_scan_if_empty(self) -> None:
        """Resume housekeeping: catch files added or changed while the app was closed.

        The scan is incremental (mtime + size), so a clean library costs a directory
        walk and no re-parse; an empty one gets a full pass.
        """
        try:
            if not db.list_libraries():
                return                      # no folder yet -> the welcome screen asks for one
            inc = bool(api.load_settings().get("include_unsupported", False))
            scanner.scan_async(None, inc)
        except Exception as e:
            print(f"[auto-scan] 跳过: {e}", flush=True)

    def add_folder(self, path: str) -> None:
        np = normalize_path(path)
        if not os.path.isdir(np):
            return
        db.add_library(np, Path(np).name or np)
        scanner.scan_async(None, bool(api.load_settings().get("include_unsupported", False)))
        # 原来这句打的是 window.Salt.refreshAll —— Salt 是前端的状态对象（S），上面没有
        # refreshAll，于是原生对话框加完文件夹后界面一动不动，要手动重启才看得见。
        # 入口在 window.SaltShell（同文件 454/480 行的媒体键用的就是它）。
        self._js("window.SaltShell&&SaltShell.onFolderAdded&&SaltShell.onFolderAdded()")
        self._toast("已加入曲库", f"{np}\n正在后台扫描…")

    # ------------------------------------------------------------- bridge pump
    def _drain_bridge(self) -> None:
        try:
            while True:
                req = self.bridge.requests.get_nowait()
                rid, kind, payload = req["id"], req["kind"], req["payload"]
                if kind == "pick_folder":
                    start = payload.get("start") or ""
                    if not os.path.isdir(start):
                        start = str(Path.home())
                    path = self.QFileDialog.getExistingDirectory(self.window, "选择音乐文件夹", start)
                    self.bridge.put_result(rid, normalize_path(path) if path else "")
                elif kind == "pick_file":
                    start = payload.get("start") or ""
                    if not os.path.isdir(start):
                        start = str(Path.home())
                    path, _ = self.QFileDialog.getOpenFileName(
                        self.window, "选择歌词文件", start, "歌词文件 (*.lrc *.txt);;所有文件 (*)")
                    self.bridge.put_result(rid, normalize_path(path) if path else "")
                elif kind == "focus":
                    self._toggle()
                    self.bridge.put_result(rid, True)
                elif kind == "show":
                    self._show_window()
                    self.bridge.put_result(rid, True)
                elif kind == "theme":
                    hwnd = int(self.window.winId()) if sys.platform == "win32" else 0
                    apply_dark_titlebar(hwnd, bool(payload.get("dark", True)))
                    self.bridge.put_result(rid, True)
                elif kind == "mini":
                    # 建窗/改 flags/显隐都必须在 GUI 线程上做（bridge 就是为这个存在的）：
                    # 在 HTTP 处理线程里碰 QWidget，轻则崩，重则窗口出来但永远点不动。
                    self.bridge.put_result(rid, self._apply_mini(
                        bool(payload.get("show")), payload.get("on_top")))
                elif kind == "about":
                    self.QMessageBox.about(self.window, APP_NAME, _about_text())
                    self.bridge.put_result(rid, True)
                elif kind == "quit":
                    self.bridge.put_result(rid, True)
                    self._quit()
                else:
                    self.bridge.put_result(rid, None)
        except queue.Empty:
            pass
        except RuntimeError:  # Qt object torn down mid-pump
            pass


def _about_text() -> str:
    return (f"{APP_NAME} 1.0\n\n本地音乐播放器：曲库扫描、歌单、LRC 双语滚动歌词、"
            f"封面自动取色主题、托盘与媒体键。\n\n数据目录：{data_root()}")


# --------------------------------------------------------------- entry points

def _crash_log(text: str) -> None:
    """The GUI can launch with no console, so failures must land on disk."""
    try:
        p = Path(data_root()) / "crash.log"
        with p.open("a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n{text}\n")
    except Exception:
        pass


def run() -> int:
    import traceback as _tb

    def _hook(exc_type, exc, tb):
        _crash_log("".join(_tb.format_exception(exc_type, exc, tb)))
        _tb.print_exception(exc_type, exc, tb)

    sys.excepthook = _hook
    ok, _mutex = acquire_single_instance()
    if not ok:
        if focus_running_instance():
            return 0
        print("已有一个实例在运行，但无法激活它的窗口。")
        return 0
    try:
        app_obj = PlayerApp()
    except BaseException:
        import traceback
        _crash_log(traceback.format_exc())
        raise
    code = app_obj.app.exec_()
    app_obj._persist_geometry()
    return code


def run_server_only(print_url: bool = True, port: Optional[int] = None) -> int:
    """Headless mode: serves the same UI at http://127.0.0.1:<port>/ for browser testing.

    ``port`` is for the native shell: it binds a known port instead of publishing one
    through server.port, so the UI can poll readiness without racing other instances.
    """
    db.conn()
    server = api.Server(port)
    url = server.start()
    api.WINDOW_HOOKS.update({k: (lambda *a, **k: {"ok": False})
                             for k in ("focus", "show", "theme", "about", "quit", "mini")})
    # 浏览器/无头模式没有原生药丸：让 GET /api/mini/state 明确回「这条通道不存在」。
    api.WINDOW_HOOKS.pop("mini_state", None)
    if print_url:
        print(url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
    return 0
