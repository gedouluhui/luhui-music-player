"""Import shim for PyQtWebEngine.

PyQt5 builds moved QWebEnginePage / QWebEngineSettings / QWebEngineProfile between
``PyQt5.QtWebEngineWidgets`` (<= 5.15.6 here) and ``PyQt5.QtWebEngineCore`` (newer
builds). A hard import of either module alone breaks on the other, so resolve each
name from whichever module actually exposes it.
"""
from __future__ import annotations

_MODULES = ("PyQt5.QtWebEngineCore", "PyQt5.QtWebEngineWidgets")


def _pick(name: str):
    last_err = None
    for mod in _MODULES:
        try:
            m = __import__(mod, fromlist=[name], level=0)
        except Exception as e:  # module missing entirely on a minimal install
            last_err = e
            continue
        if hasattr(m, name):
            return getattr(m, name)
    raise ImportError(f"PyQt5 exposes no {name} ({last_err})")


QWebEngineView = _pick("QWebEngineView")
QWebEnginePage = _pick("QWebEnginePage")
QWebEngineSettings = _pick("QWebEngineSettings")
QWebEngineProfile = _pick("QWebEngineProfile")

__all__ = ["QWebEngineView", "QWebEnginePage", "QWebEngineSettings", "QWebEngineProfile"]
