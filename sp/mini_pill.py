"""桌面歌词小窗的**原生**实现：一块真空心透明的圆角药丸。

为什么要推倒 WebEngine 那版重来：椒盐那条药丸要的是「圆角外面彻底透明」——
同一块屏幕区域，红色背景和绿色背景各抓一次，四个角必须跟着背景走（差分≥阈值），
才叫真的透明；只要外面套了一层不透明白底，两次都读成白、差分=0，判据立刻翻红。
Qt5 + Chromium 87 这条路把 `WA_TranslucentBackground` 装到外层、focusProxy、
`findChildren` 里每一个渲染子件，再把页面底色设成全透明，四角仍读到 (255,255,255)
（本仓 `功能对照表.md` §9 记的实测：control 有效、`mw-transparent-corners` 判红）。
那不是 Qt 画不出 alpha，而是 **Chromium 的合成器把一块不透明表面提交给了窗口** ——
`testAttribute` 返回 True 并不会让那块 native surface 重新分配成 ARGB，
hide+show 重建、disable-gpu 都没能翻案（都试过了）。
原生 QWidget 用 `QPainter` 直接往窗口自己的 backing store 画，绕过那一层合成器：
圆角路径之外不画任何东西 ⇒ 那些像素 alpha=0 ⇒ 背景透得过来。这条能不能成，
不靠断言、靠 `tools/probe_mini_window.py` 的两背景差分实测；本模块只提供「该透明」
的画法，判词由量具给。

线程模型：小窗在主窗 GUI 线程上是个 QWidget，但它要的进度读数不能问主窗
（主窗最小化时它的 rAF 会被 Chromium 冻住，见 sp/playback.py 顶部那段）。所以这里
按墙钟自己外推，取数走一次 HTTP GET 到本进程自带的服务端（`/api/playback` 与
`/api/lyrics`）——和当初 mini.js 用的是同一对接口，数据源保持一模一样，
差别只在「谁来显示」。取数放在后台线程，结果用信号回 GUI 线程，绝不在 paint
里做 IO。

兼容：PyQt5。本模块所有槽函数、paintEvent、后台回调都包了 try/except ——
按仓里的老账（memory：PyQt 槽异常＝静默 abort rc=127），GUI 线程里一个没接住的
异常会把整个 App 带走且不留 stderr，那是最难查的一类崩。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from PyQt5.QtCore import (QEvent, QObject, QPoint, QPointF, QRectF, Qt, QTimer, pyqtSignal)
from PyQt5.QtGui import (QColor, QFont, QFontMetrics, QLinearGradient, QPainter,
                         QPainterPath, QPen)
from PyQt5.QtWidgets import QWidget

POLL_MS = 700            # 和主窗口报进度的节奏同量级：再快也只是空转
STALE_MS = 6000          # 超过这个年龄还「在播」＝主窗没在报了（崩溃/被杀），别再外推
SCALE_MIN, SCALE_MAX, SCALE_STEP = 0.6, 2.0, 0.1

# 药丸里那四个可点的小件，从右往左排：× 关、钉 置顶、A- 缩、A+ 放。
# 命中区宽度按字号缩放；顺序和网页 mini.html 一致，用户不该因为换了实现就找不到。
CHIPS = ("close", "top", "minus", "plus")
CHIP_LABEL = {"close": "×", "top": "钉", "minus": "A-", "plus": "A+"}


def clamp_scale(v: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 1.0
    if f != f:
        return 1.0
    return min(SCALE_MAX, max(SCALE_MIN, f))


class _Signals(QObject):
    """后台取数线程 → GUI 线程的回程通道（pyqtSignal 跨线程是排队的，槽在 GUI 线程跑）。"""
    playback = pyqtSignal(object)
    lyric = pyqtSignal(int, object)


class MiniPill(QWidget):
    """原生透明桌面歌词药丸。

    对外契约（app.py 与验证脚本都只认这些）：
      - 构造参数把「取数」和「副作用」都注入进来，模块自己不 import api/playback，
        这样它能在没有 App 进程的隔离脚本里单开（先验证透明这件事）。
      - state() 交出的键，和当初 window.Mini.state() 一字不差：验证脚本换实现不换判据。
      - on_top / close / persist_* 由调用方给；缺省就什么都不做（隔离脚本用得上）。
    """

    def __init__(self,
                 base_url: str = "",
                 fetch_state: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
                 fetch_lyric: Optional[Callable[[str, int], Optional[Dict[str, Any]]]] = None,
                 on_top_changed: Optional[Callable[[bool], None]] = None,
                 on_close: Optional[Callable[[], None]] = None,
                 on_geometry: Optional[Callable[["MiniPill"], None]] = None,
                 on_scale: Optional[Callable[[float], None]] = None,
                 scale: float = 1.0,
                 reader: bool = True,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # 顶层透明窗：无边框 + 不抢焦点 + 不吃任务栏。真正的 alpha 来自
        # WA_TranslucentBackground + paintEvent 里圆角外不画东西这两件事，缺一不可。
        self.setWindowFlags(self._flags(True))
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setWindowTitle("桌面歌词")
        self.setMouseTracking(True)

        self.base_url = (base_url or "").rstrip("/")
        self._fetch_state = fetch_state
        self._fetch_lyric = fetch_lyric
        self._on_top_changed = on_top_changed
        self._on_close = on_close
        self._on_geometry = on_geometry
        self._on_scale = on_scale
        self._reader = bool(reader)
        self._scale = clamp_scale(scale)

        self._sig = _Signals()
        self._sig.playback.connect(self._apply_playback)
        self._sig.lyric.connect(self._apply_lyric)

        # st：这一页的全部可见状态，和 mini.js 那份同名同义，方便逐条对照。
        self.st: Dict[str, Any] = {
            "track_id": None, "playing": False, "rate": 1.0,
            "anchor_pos": 0.0, "anchor_at": time.monotonic(),
            "lines": [], "i": -1, "plain": False, "text": "",
            "title": "", "artist": "", "age": None,
            "on_top": True, "fetching": False,
        }
        self._display = "没有播放"
        self._is_title = True
        self._chip_rects: Dict[str, Any] = {}
        self._text_rect = None              # 最近一帧文字带的窗口内坐标（量具定位内容用）
        self._canvas = (0, 0)               # 最近一帧的画布尺寸（读数面从 HTTP 线程拿，不能碰 QWidget）

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._interp = QTimer(self)          # 两次轮询之间让换行别晚 0.7s
        self._interp.timeout.connect(self._repaint_line)
        self._drag_off = None
        self._dragging = False
        self._drag_origin = None
        self._drag_moved = False
        if self._reader:
            self._timer.start(POLL_MS)
            self._interp.start(120)
            self._poll()

    # ------------------------------------------------------------------ 窗口标记
    def _flags(self, on_top: bool):
        f = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus
        if on_top:
            f |= Qt.WindowStaysOnTopHint
        return f

    def set_on_top(self, on_top: bool) -> None:
        """改置顶：Windows 上改 windowFlags 等于重建窗口，得重画一次并重新 show。"""
        on_top = bool(on_top)
        if on_top == self.st["on_top"] and int(self.windowFlags()) & int(Qt.WindowStaysOnTopHint) == (int(Qt.WindowStaysOnTopHint) if on_top else 0):
            return
        was_visible = self.isVisible()
        self.setWindowFlags(self._flags(on_top))
        self.st["on_top"] = on_top
        if was_visible:
            self.show()
        if on_top:
            self.raise_()
        self.update()

    def on_top_actual(self) -> bool:
        return bool(int(self.windowFlags()) & int(Qt.WindowStaysOnTopHint))

    # ---------------------------------------------------------------------- 取数
    def _default_http(self, path: str) -> Optional[Dict[str, Any]]:
        if not self.base_url:
            return None
        try:
            with urllib.request.urlopen(self.base_url + path, timeout=2.5) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    def _get_state(self) -> Optional[Dict[str, Any]]:
        if self._fetch_state is not None:
            return self._fetch_state(self.base_url)
        return self._default_http("/api/playback")

    def _get_lyric(self, tid: int) -> Optional[Dict[str, Any]]:
        if self._fetch_lyric is not None:
            return self._fetch_lyric(self.base_url, tid)
        return self._default_http("/api/lyrics?track_id=%d" % tid)

    def _poll(self) -> None:
        if self.st["fetching"]:
            return
        self.st["fetching"] = True
        # 取数在后台线程：urllib 阻塞几十毫秒，绝不能压在 GUI 线程的定时器槽里，
        # 否则那一帧药丸就卡住不画了。结果用信号送回 GUI 线程处理。
        threading.Thread(target=self._poll_worker, daemon=True).start()

    def _poll_worker(self) -> None:
        d = None
        try:
            d = self._get_state()
        except Exception:
            d = None
        try:
            self._sig.playback.emit(d)
        except Exception:
            self.st["fetching"] = False

    def _lyric_worker(self, tid: int) -> None:
        d = None
        try:
            d = self._get_lyric(tid)
        except Exception:
            d = None
        try:
            self._sig.lyric.emit(tid, d)
        except Exception:
            pass

    def _apply_playback(self, d: Optional[Dict[str, Any]]) -> None:
        self.st["fetching"] = False
        try:
            if not d or not d.get("ok"):
                # 服务端自己都说这份数不作数：停在「没在播」，别拿旧位置继续走。
                self.st["playing"] = False
                self._resolve_line()
                self.update()
                return
            age = d.get("age")
            try:
                age = float(age) if age is not None else None
            except (TypeError, ValueError):
                age = None
            stale = (age is not None and age * 1000.0 > STALE_MS)
            self.st["playing"] = bool(d.get("playing")) and not stale
            rate = d.get("rate")
            try:
                self.st["rate"] = float(rate) if rate else 1.0
            except (TypeError, ValueError):
                self.st["rate"] = 1.0
            if not stale:                        # 只采纳「还信得住」的那份进度做锚点
                pos = d.get("position")
                try:
                    self.st["anchor_pos"] = float(pos) if pos is not None else 0.0
                except (TypeError, ValueError):
                    self.st["anchor_pos"] = 0.0
                self.st["anchor_at"] = time.monotonic()
            self.st["age"] = age
            self.st["title"] = d.get("title") or ""
            self.st["artist"] = d.get("artist") or ""
            # on_top 不从这里抄：它是窗口 flags 的事实，由 set_on_top() 独占维护。
            # 从服务端抄回来会在「刚点了钉、POST 还在路上」的那一帧把读数面写回旧值。
            tid = d.get("track_id")
            tid = int(tid) if tid is not None else None
            if tid != self.st["track_id"]:
                self.st["track_id"] = tid
                self.st["lines"] = []
                self.st["plain"] = False
                self.st["text"] = ""
                # reader=False（隔离脚本 / 离线自测）时不自动取歌词：那时状态只由 feed() 喂，
                # 后台取数线程会把喂进去的 lines 覆盖成空，读数面当场说谎。
                if self._reader and tid is not None:
                    threading.Thread(target=self._lyric_worker, args=(tid,), daemon=True).start()
            self._resolve_line()
            self.update()
        except Exception:
            pass

    def _apply_lyric(self, tid: int, d: Optional[Dict[str, Any]]) -> None:
        try:
            if self.st["track_id"] != tid:        # 已经换歌了，这份回来得太晚
                return
            self.st["lines"] = (d or {}).get("lines") or []
            self.st["plain"] = bool((d or {}).get("plain"))
            self.st["text"] = (d or {}).get("text") or ""
            self._resolve_line()
            self.update()
        except Exception:
            pass

    # -------------------------------------------------------------------- 读数逻辑
    def play_pos(self) -> float:
        """当前秒数：在播就按本地单调钟从锚点外推，暂停就停在锚点。和 mini.js 的 pos() 同式。

        名字不叫 pos()：QWidget 自己有一个 pos()（窗左上角），覆盖它会让拖动那一段
        `np != self.pos()` 变成「QPoint 比 float」——异常被 except 吞掉，窗照动、落盘永不发生。
        （2026-10-21 由 tools/test_mini_wiring.py 的「真拖动后要落盘一次」当场抓住。）
        """
        if not self.st["playing"]:
            return self.st["anchor_pos"]
        dt = time.monotonic() - self.st["anchor_at"]
        if dt < 0:
            dt = 0.0
        return self.st["anchor_pos"] + dt * (self.st["rate"] or 1.0)

    def _pick(self):
        p = self.play_pos()
        L = self.st["lines"]
        i = -1
        for k in range(len(L)):
            t = L[k].get("t")
            try:
                t = float(t) if t is not None else 0.0
            except (TypeError, ValueError):
                t = 0.0
            if t <= p:
                i = k
            else:
                break
        return i, (L[i].get("text", "") if i >= 0 else "")

    def _resolve_line(self) -> None:
        """算出这一帧该显示什么（四支，和 mini.js 的 paint() 一一对应），并同步 st['i']。

        st['i'] 是「这句是第几句」的唯一读数面，验证脚本只认它。所以每一支都要给它
        当下的值——只在有轴那支赋值，换到没歌词的歌时它会留着上一首的下标，读数面就成了
        会说谎的地方（这条是当初 mini.js 里踩过的实测坑，注释专门留了一遍）。
        is_title 这里每支都给当下值（比 mini.js 只在有轴那支赋值更干净：换歌不留旧态）。
        """
        st = self.st
        art, title = st["artist"], st["title"]
        show = ""
        is_title = False
        try:
            if st["track_id"] is None:
                show = "没有播放"
                is_title = True
                st["i"] = -1
            elif st["lines"]:
                i, text = self._pick()
                if i >= 0:
                    show = text
                elif art:
                    show = art + " — " + title
                else:
                    show = "（前奏）"
                is_title = i < 0
                st["i"] = i
            elif st["plain"] and st["text"]:
                show = str(st["text"]).split("\n")[0]
                is_title = False
                st["i"] = -1
            else:
                show = (art + " — " if art else "") + title
                is_title = True
                st["i"] = -1
        except Exception as e:
            try:
                print("[mini-pill] _resolve_line 出错：%s" % e, flush=True)
            except Exception:
                pass
        self._display = show
        self._is_title = is_title

    # ------------------------------------------------------------------------ 绘制
    def _font(self) -> QFont:
        f = QFont("Microsoft YaHei")
        f.setPointSizeF(14.0 * self._scale)
        f.setWeight(QFont.DemiBold)
        f.setHintingPreference(QFont.PreferFullHinting)
        return f

    def _path(self, rect: QRectF) -> QPainterPath:
        """圆角药丸轮廓（半径=短边一半 ⇒ 两端是标准半圆）。"""
        p = QPainterPath()
        r = min(rect.width(), rect.height()) / 2.0
        p.addRoundedRect(rect, r, r)
        return p

    def paintEvent(self, _ev) -> None:
        # 整窗先按 alpha=0 起底（WA_TranslucentBackground 已保证），只在圆角路径内上色：
        # 圆角外一个像素都不画 ⇒ 那些点 alpha=0 ⇒ 背景透得过来。这就是原生路线相对
        # WebEngine 的全部差别——画布归 Qt 自己管，没有 Chromium 那块不透明合成表面。
        try:
            w, h = self.width(), self.height()
            if w <= 0 or h <= 0:
                return
            self._canvas = (int(w), int(h))
            pad = max(2.0, h * 0.10)             # 四周留一条缝：投影/抗锯齿不贴窗边
            body = QRectF(pad, pad, w - 2 * pad, h - 2 * pad)
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.TextAntialiasing, True)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

            # 哑面玻璃条：上浅下深的一条单调明度斜坡（iOS 质感＝边缘受光哑面，不是纯色块、
            # 也不是高光斑）。半透明让底下的桌面能透出来一点，但角（圆角外）仍是全透明。
            grad = QLinearGradient(body.topLeft(), body.bottomLeft())
            grad.setColorAt(0.0, QColor(46, 52, 66, 150))
            grad.setColorAt(0.5, QColor(28, 33, 45, 168))
            grad.setColorAt(1.0, QColor(16, 19, 27, 182))
            painter.fillPath(self._path(body), grad)

            # 1px 上沿补光（≤10% 漫射），别做成 2px 白沿那种塑料感。
            highlight = QPen(QColor(255, 255, 255, 26))
            highlight.setWidthF(1.0)
            painter.setPen(highlight)
            painter.drawArc(QRectF(body.x() + 0.5, body.y() + 0.5,
                                   body.width() - 1.0, body.height() - 1.0),
                            20 * 16, 140 * 16)

            self._paint_text(painter, body)
            self._paint_chips(painter, body)
            painter.end()
        except Exception as e:
            try:
                print("[mini-pill] paint 出错（吞掉不让 GUI 线程崩）：%s" % e, flush=True)
            except Exception:
                pass

    def _paint_text(self, painter: QPainter, body: QRectF) -> None:
        font = self._font()
        painter.setFont(font)
        fm = QFontMetrics(font)
        chip_w = self._chips_width(fm)
        # 文字带：左右各让开内边距，右边再多让出四个小件的位置。
        left = body.x() + fm.horizontalAdvance("A") + 6
        right = body.right() - chip_w - 8
        band = QRectF(left, body.y(), max(10.0, right - left), body.height())
        self._text_rect = band
        text = self._display or ""
        elided = fm.elidedText(text, Qt.ElideMiddle, int(band.width()))
        painter.setPen(QPen(QColor(240, 244, 250)))
        # 标题态略淡、句歌词亮一点，和网页那条 is-title 的层级一致。
        if self._is_title:
            painter.setPen(QPen(QColor(210, 216, 226, 235)))
        painter.drawText(band, int(Qt.AlignLeft | Qt.AlignVCenter), elided)

    def _chips_width(self, fm: QFontMetrics) -> float:
        # 四个小件的总宽：随字号缩放，留出手点空间。
        unit = fm.horizontalAdvance("A") + 10
        return unit * len(CHIPS) + 6

    def _paint_chips(self, painter: QPainter, body: QRectF) -> None:
        font = self._font()
        font.setPointSizeF(max(8.0, 10.5 * self._scale))
        painter.setFont(font)
        fm = QFontMetrics(font)
        unit = fm.horizontalAdvance("A") + 10
        cy = body.center().y()
        x = body.right() - 6
        self._chip_rects = {}
        for name in CHIPS:                        # 从右往左：× 钉 A- A+
            label = CHIP_LABEL[name]
            r = QRectF(x - unit, cy - unit * 0.5, unit, unit)
            self._chip_rects[name] = r
            painter.setPen(QPen(QColor(198, 206, 218, 210)))
            painter.drawText(r, int(Qt.AlignCenter), label)
            x -= unit

    # ------------------------------------------------------------------ 鼠标：拖动 + 点击
    def mousePressEvent(self, ev) -> None:
        try:
            if ev.button() == Qt.LeftButton:
                gp = ev.globalPos()
                for name, r in self._chip_rects.items():
                    if r.contains(QPointF(float(gp.x() - self.x()), float(gp.y() - self.y()))):
                        self._click(name)
                        return
                self._drag_off = gp - self.frameGeometry().topLeft()
                self._drag_origin = QPoint(self.x(), self.y())
                self._drag_moved = False
                self._dragging = True
        except Exception:
            self._dragging = False

    def mouseMoveEvent(self, ev) -> None:
        try:
            if getattr(self, "_dragging", False) and (ev.buttons() & Qt.LeftButton):
                np = ev.globalPos() - getattr(self, "_drag_off", QPointF(0, 0))
                here = QPoint(self.x(), self.y())      # QWidget.pos() 不再被读数方法遮住
                if np != here:
                    self.move(np)
                    if self._drag_origin is not None:
                        # #52：按下-放开不算「用户挪过」。只有真的动了几像素才把矩形落盘；
                        # 否则一次误触就会把设置里那份「开跑前的位置」改写成窗当时的临时位。
                        dx = abs(np.x() - self._drag_origin.x())
                        dy = abs(np.y() - self._drag_origin.y())
                        if dx + dy > 2:
                            self._drag_moved = True
        except Exception:
            pass

    def mouseReleaseEvent(self, ev) -> None:
        try:
            if getattr(self, "_dragging", False):
                moved = bool(self._drag_moved)
                self._dragging = False
                self._drag_origin = None
                self._drag_moved = False
                if moved and callable(self._on_geometry):
                    self._on_geometry(self)
        except Exception:
            self._dragging = False
            self._drag_origin = None
            self._drag_moved = False

    def _click(self, name: str) -> None:
        try:
            if name == "close":
                if callable(self._on_close):
                    self._on_close()
                else:
                    self.hide()
            elif name == "top":
                new = not self.on_top_actual()
                self.set_on_top(new)
                if callable(self._on_top_changed):
                    self._on_top_changed(new)
            elif name == "minus":
                self._set_scale(clamp_scale(self._scale - SCALE_STEP))
            elif name == "plus":
                self._set_scale(clamp_scale(self._scale + SCALE_STEP))
        except Exception as e:
            try:
                print("[mini-pill] 点击 %s 出错：%s" % (name, e), flush=True)
            except Exception:
                pass

    def _set_scale(self, v: float) -> None:
        self._scale = clamp_scale(v)
        self.update()
        if callable(self._on_scale):
            try:
                self._on_scale(self._scale)
            except Exception:
                pass

    def set_scale(self, v: float) -> None:
        self._set_scale(clamp_scale(v))

    # ------------------------------------------------------------------------ 定时器
    def _tick(self) -> None:
        try:
            self._poll()
        except Exception:
            pass

    def _repaint_line(self) -> None:
        try:
            if self.st["lines"]:
                old = self._display
                self._resolve_line()
                if self._display != old:
                    self.update()
        except Exception:
            pass

    # ------------------------------------------------------------------------ 读数面
    def state(self) -> Dict[str, Any]:
        """交给验证脚本的读数面，键和当初 window.Mini.state() 一字不差。"""
        return {
            "track_id": self.st["track_id"], "playing": self.st["playing"],
            "rate": self.st["rate"],
            "pos": round(self.play_pos() * 10) / 10,
            "i": self.st["i"], "lines": len(self.st["lines"]),
            "text": self._display, "is_title": bool(self._is_title),
            "state_attr": "playing" if self.st["playing"] else "idle",
            "scale": self._scale, "on_top": bool(self.st["on_top"]),
            "age": self.st["age"],
        }

    def canvas(self):
        """最近一帧的画布尺寸 [w, h]（纯 Python 缓存）。HTTP 线程读状态时不碰 QWidget。"""
        return [int(self._canvas[0]), int(self._canvas[1])]

    def paint_box(self):
        """最近一帧文字带的窗口内坐标 [x,y,w,h]；还没画过就 None。
        读数面 state() 的键不许动（它要与旧 window.Mini.state() 逐字一致），
        所以量具要的绘制盒单独从这里拿。"""
        r = self._text_rect
        try:
            return [round(r.x(), 1), round(r.y(), 1), round(r.width(), 1), round(r.height(), 1)]
        except Exception:
            return None

    def chips(self):
        """四个小件的窗口内矩形 {name: [x,y,w,h]}（最近一帧画出来的那份）；没画过就 {}。
        原生窗的按钮没有 DOM 可查，量具只能靠这份几何把「按哪儿」换算成屏幕坐标。"""
        out = {}
        for name, r in (self._chip_rects or {}).items():
            try:
                out[name] = [round(r.x(), 1), round(r.y(), 1),
                             round(r.width(), 1), round(r.height(), 1)]
            except Exception:
                continue
        return out

    # 隔离/自测用：不连服务端，直接把一份读数喂进来，走和线上一样的解析与绘制。
    def feed(self, playback: Optional[Dict[str, Any]] = None,
             lyric: Optional[Dict[str, Any]] = None) -> None:
        if playback is not None:
            self._apply_playback(playback)
        if lyric is not None:
            tid = self.st["track_id"]
            if tid is not None:
                self._apply_lyric(tid, lyric)

    def start_reader(self) -> None:
        """恢复后台取数（关掉药丸时停表，再打开时接着走）。reader=False 的隔离自测不启表。"""
        if not self._reader:
            return
        try:
            if not self._timer.isActive():
                self._timer.start(POLL_MS)
            if not self._interp.isActive():
                self._interp.start(120)
            self._poll()
        except Exception:
            pass

    def stop_reader(self) -> None:
        try:
            self._timer.stop()
            self._interp.stop()
        except Exception:
            pass
