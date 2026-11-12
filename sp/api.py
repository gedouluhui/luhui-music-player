"""Local HTTP API + static host + byte-range audio streaming.

Bound to 127.0.0.1 only. The Qt shell and the browser both talk to this; there is
no other network surface.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import db, lyrics as lrc, metadata, playback, scanner
from . import audioplay, downmix
from .textidx import pinyin_initial
from .config import (APP_NAME, LYRIC_EXTS, data_root, normalize_path, port_path,
                     settings_path, web_root)

MIME_BY_EXT = {
    ".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
    ".aac": "audio/aac", ".wav": "audio/wav", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/ogg", ".aif": "audio/aiff", ".aiff": "audio/aiff", ".wma": "audio/x-ms-wma",
}
DEFAULT_SETTINGS: Dict[str, Any] = {
    "theme": "dark",
    "accent_mode": "cover",          # cover | preset
    "preset_accent": "#e8894b",
    "play_mode": "list_loop",        # list_loop | one_loop | shuffle | normal
    "volume": 0.8,
    "muted": False,
    "rate": 1.0,
    "lyric_size": 1.0,
    "lyric_mode": "center",          # center | top
    "lyric_offset": 0.0,             # 秒；正数 = 歌词整体往后推（设置里 ±0.5 一格）
    "show_translation": True,
    "np_open": True,
    "close_to_tray": True,
    "resume_on_start": True,
    "include_unsupported": False,
    "window": {"w": 1320, "h": 830, "x": None, "y": None, "maximized": False},
    "last_track": None,
    "last_position": 0.0,
    "last_view": "library/songs",
    # ---- 无缝衔接 / 交叉淡化（浏览器内核那条双元素链路；原生引擎换曲仍重开一条流）
    #   gapless=False 时待机元素不再预载；crossfade_sec=0 只做 80 ms 防爆音衔接。
    "gapless": True,
    "crossfade_sec": 0.0,
    # ---- 均衡器（WebAudio 十段 BiquadFilter，gain 单位 dB，-12…+12）
    "eq_enabled": False,
    "eq_gains": [0.0] * 10,
    "eq_preset": "flat",
    # ---- 歌词滚动：一次换行给多少毫秒的缓动；0 = 直接跳过去（不做动画）
    "lyric_scroll_ms": 460,
    # ---- 播放条上跑当前这句歌词
    "playbar_lyric": True,
    # ---- 背景动态流光（CSS 动画，暂停/隐藏时由前端把 play-state 掐掉）
    "ambient": True,
    # ---- 音质 / 播放引擎（椒盐那批「会员音质」项的落点）
    #   engine: chromium(默认，浏览器 <audio>+WebAudio) | native(服务端 ffmpeg 浮点解码→WASAPI)
    #   native 档才有：独占/高解析直通、服务端均衡器/响度/平衡、目标采样率档位。
    "audio_engine": "chromium",
    "audio_device": None,            # WASAPI 输出设备 index；None=系统默认
    # 独占 = 绕过系统混音器，直接把样本交给声卡。开不开由用户点（它会锁住整机声音），
    # 而「是不是真 bit-perfect」由引擎逐条算，见 sp/audioplay.py:_bit_perfect_locked。
    "audio_exclusive": False,
    "audio_target_rate": 0,          # -1=跟随源(逐位直通) 0=固定为设备混音率 否则重采样到该 Hz
    # 抖动的适用面**只有**「源比设备容器深」那一档（24 位源落进 16 位独占容器）：
    # 那条路必然量化，不加 TPDF 就是把量化误差锁成与信号相关的失真（听感上是「发闷/谐波硬」）。
    # 设备给 f32、或源位深本来就放得下时，引擎自己会算出 quantizes=False ⇒ 这个开关不产生任何作用，
    # 所以「默认开」不会往一条无损流里注噪声——这一点是 tools/test_audioplay.py 里那三条已答对照量的。
    "audio_dither": True,
    "audio_balance": 0.0,            # -1..1 等功率声像
    "audio_loudness": False,         # 响度均衡（把峰值归一到 0.95 的软 AGC）
    "audio_fade_in": 0.0,            # 淡入秒数
    # 虚拟扬声器（耳机交叉馈电）：对侧声道延迟 0.3 ms、700 Hz 低通后混入，模拟前方音箱。
    # 不是多声道 HRTF/卷积环绕；amount=0 时 DSP 分支不执行（开关开着也不动样本）。
    "audio_speakers": False,
    "audio_speakers_amount": 0.35,
    # ---- 文件夹页：False = 树状，True = 平铺（一个目录一行，按曲目数排序用不着一路点开）
    "folder_flat": False,
    # ---- 桌面歌词：屏幕上一条置顶的药丸（原生小窗，主窗收进托盘时它还在走）。
    #      on_top 关掉时它就在别的窗口后面；mini_window 是用户拖动之后落盘的位置。
    "desktop_lyric": False,
    "desktop_lyric_on_top": True,
    "desktop_lyric_scale": 1.0,      # 原生药丸 A-/A+ 的字号；夹在 0.6…2.0
    "mini_window": {"x": None, "y": None, "w": 460, "h": 68},
}

# injected by the Qt shell so handlers can raise a native dialog on the GUI thread
UI_HOOKS: Dict[str, Callable[..., Any]] = {"pick_folder": None, "about": None}
# injected window controls; absent (no-op) in headless/browser mode
WINDOW_HOOKS: Dict[str, Callable[..., Any]] = {}

# 歌词文件的上限：正常 LRC 几十 KB，1 MB 已经足够宽松；同时挡住「挂一个几百 MB
# 的文本文件进来」这种读取放大。lrc.read_text 自己也只读 2 MB，两层一致。
_LYRIC_MAX_BYTES = 1_000_000
# 桌面文件选择器返回的路径只在一小段时间内算「用户授权过」。attach 用掉即作废；
# 这样 API 不能凭一个路径字符串读走任意文件，而用户点选歌词的正常流程不受影响。
_PICKED_TTL = 300.0
_PICKED_FILES: Dict[str, float] = {}
_PICKED_LOCK = threading.Lock()


def _remember_picked_file(path: str) -> None:
    """记下「本进程的文件选择器刚刚返回了这份文件」这件事（仅歌词扩展名）。"""
    p = normalize_path(path or "")
    if not p or Path(p).suffix.lower() not in LYRIC_EXTS:
        return
    key = os.path.normcase(p)
    now = time.time()
    with _PICKED_LOCK:
        for k, ts in list(_PICKED_FILES.items()):
            if now - ts > _PICKED_TTL:
                _PICKED_FILES.pop(k, None)
        _PICKED_FILES[key] = now


def _take_picked_file(path: str) -> bool:
    """一次性消费选择器票据；返回 True 才允许读这个路径。"""
    key = os.path.normcase(normalize_path(path or ""))
    if not key:
        return False
    now = time.time()
    with _PICKED_LOCK:
        ts = _PICKED_FILES.pop(key, None)
    return ts is not None and (now - ts) <= _PICKED_TTL


def _same_path(a: str, b: str) -> bool:
    """Windows 卷大小写不敏感，但目录必须一起比 —— basename 不是身份。"""
    if not a or not b:
        return False
    try:
        return os.path.normcase(normalize_path(a)) == os.path.normcase(normalize_path(b))
    except Exception:
        return False


def _lyric_file_ok(path: str) -> bool:
    """扩展名/大小/存在性三重门；路径本身已经被 normalize_path 展开成真实路径。"""
    try:
        if not path or not os.path.isfile(path):
            return False
        if Path(path).suffix.lower() not in LYRIC_EXTS:
            return False
        return os.path.getsize(path) <= _LYRIC_MAX_BYTES
    except OSError:
        return False


def _path_in_roots(path: str, roots: List[str]) -> bool:
    """realpath 之后的目录前缀校验（commonpath 不会把 C:\\x 和 C:\\xyz 混为一谈）。"""
    np = os.path.normcase(normalize_path(path or ""))
    for root in roots:
        try:
            if np and root and os.path.commonpath([np, root]) == root:
                return True
        except ValueError:
            continue        # 不同盘符
    return False


def _window_hook(name: str, *args: Any) -> Dict[str, Any]:
    fn = WINDOW_HOOKS.get(name)
    if not callable(fn):
        return {"ok": False, "native": False}
    try:
        return {"ok": True, "native": True, "result": fn(*args)}
    except Exception as e:
        return {"ok": False, "native": True, "error": str(e)}


def reveal_in_explorer(path: str) -> Dict[str, Any]:
    """Open Explorer with the file selected. Only paths inside a registered
    library folder are honoured, so this can't be pointed at arbitrary disk.

    自己的导出目录（<data>/playlists/）也在白名单里：导出的 m3u 就落在那儿，
    「在文件夹中显示」是导出流程的一部分，不该被这条守卫挡回「不在音乐文件夹内」。
    """
    path = normalize_path(path or "")
    if not path or not (os.path.isfile(path) or os.path.isdir(path)):
        return {"ok": False, "error": "路径不存在"}
    allowed = [l["path"] for l in db.list_libraries()] + [str(Path(data_root()) / "playlists")]
    low = path.lower()
    if not any(low == a.lower() or low.startswith(a.lower() + os.sep) for a in allowed):
        return {"ok": False, "error": "该文件不在已登记的音乐文件夹内"}
    try:
        import subprocess
        args = ["explorer", f"/select,{path}"] if os.path.isfile(path) else ["explorer", path]
        subprocess.Popen(args, shell=False)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# 起播时的「真实流事实」缓存：key=(path,size,mtime)，value=probe_stream_facts 的结果。
# 为什么要缓存：ffmpeg 读一次文件头约 25 ms，同一首反复播/切歌不该每次都付。
# 为什么 key 带 size/mtime：文件被替换后旧结论必须作废——缓存不能冒充身份。
_STREAM_FACTS_LOCK = threading.Lock()
_STREAM_FACTS: Dict[tuple, Dict[str, Any]] = {}


def load_settings() -> Dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        raw = json.loads(settings_path().read_text("utf-8"))
    except Exception:
        raw = {}
    for k, v in raw.items():
        if k == "window" and isinstance(v, dict):
            merged["window"].update({kk: vv for kk, vv in v.items() if kk in merged["window"]})
        else:
            merged[k] = v
    return merged


def save_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = load_settings()
    for k, v in (patch or {}).items():
        if k not in DEFAULT_SETTINGS:
            continue
        if k == "window" and isinstance(v, dict):
            cur["window"].update(v)
        else:
            cur[k] = v
    p = settings_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, p)
    return cur


# ------------------------------------------------------------------ helpers

def _like(s: str) -> str:
    return "%" + re.sub(r"[%_]", r"\\\g<0>", s or "") + "%"


def _truthy(v: Any) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on")


def _paging(q: Dict[str, Any], default: int = 0, cap: int = 5000) -> tuple:
    """(limit, offset)。没给 limit 就返回 (0, 0)＝不分页，保持老前端的用法可用。"""
    try:
        limit = int(q.get("limit") or 0)
    except ValueError:
        limit = 0
    try:
        offset = int(q.get("offset") or 0)
    except ValueError:
        offset = 0
    if not limit and default:
        limit = default
    return max(0, min(limit, cap)), max(0, offset)


# 列表默认排序：索引 idx_tracks_default_order 是 (missing, playable, artist, album,
# disc_no, track_no)，所以这里去掉尾部 ", id"，让 ORDER BY 的列正好被索引覆盖，
# SQLite 就不必再建 TEMP B-TREE 排序（深分页 0.5s → 见 tools/scale_after.log）。
DEFAULT_ORDER_LIST = "artist, album, disc_no, track_no"

# 聚合视图缓存：albums / artists / genres / folders 四个页面每次进来都要把整张 tracks
# 重跑一遍 GROUP BY。20k 首夹具上实测（tools/scale_after.log）分别是 44 / 34 / 21 / 48 ms，
# 且 EXPLAIN 里全是 USE TEMP B-TREE —— 分组键是 lower(album)、COALESCE(...) 这类表达式，
# SQLite 的表达式索引又匹配不上带 COALESCE 的键，所以建索引救不了，只能不重跑。
# 这些结果只在曲库内容变化时才会变，而 db 已经维护了写版本号 _WRITE_GEN，所以拿它当
# 版本：号没变 ⇒ 同一份数据，直接复用。收藏、播放、扫描、改词任何一次写都会让它失效。
_AGG_CACHE: Dict[str, tuple] = {}


def _agg(key: str, build: Callable[[], Any]) -> Any:
    """按写版本号缓存一段聚合结果。

    列表类型的值取出来时给一层浅拷贝（免得调用方 append 污染缓存），dict 类型的原样
    返回 —— 现在唯一的 dict 用法是 albums，它拿 `agg["rows"]` 之后只做切片，不改里面
    的 dict。总原则：这里出来的东西只许读；要加工就新建 dict，四个调用方都是这么写的。
    这条原则由 tools/test_agg_cache.py 的 payload-stable-* 和 mutation 反证守着。
    """
    gen = db.write_gen()
    hit = _AGG_CACHE.get(key)
    if hit is not None and hit[0] == gen:
        val = hit[1]
        return list(val) if isinstance(val, list) else val
    val = build()
    if len(_AGG_CACHE) > 64:        # key 里带用户输入的筛选词，会无限造新 key
        _AGG_CACHE.clear()
    _AGG_CACHE[key] = (gen, val)
    return list(val) if isinstance(val, list) else val


def _agg_reset() -> None:
    """测试/探针用：清掉聚合缓存。"""
    _AGG_CACHE.clear()


def _art_url(cover_track: Any, album: str = "", artist: str = "", size: str = "thumb") -> str:
    """一行/一张卡片的封面地址；没有封面时也给出地址（后端生成首字母色块）。

    以前「没封面」用空串表示，前端就得自己挂占位块，而占位块和真封面尺寸不完全
    一致，列表加载时行高会跳一下。这里保证永远有地址：有图走 track_id，没图走
    album/artist 哈希生成的色块，两者都能被浏览器长期缓存。
    """
    try:
        tid = int(cover_track or 0)
    except (TypeError, ValueError):
        tid = 0
    if tid:
        return f"/api/cover?track_id={tid}&size={size}"
    return ("/api/cover?size=" + urllib.parse.quote(size)
            + "&album=" + urllib.parse.quote(album or "")
            + "&artist=" + urllib.parse.quote(artist or ""))


def _where_from_args(args: Dict[str, Any]) -> Tuple[List[str], List[Any]]:
    w: List[str] = []
    v: List[Any] = []
    if args.get("q"):
        q = str(args["q"]).strip()
        w.append("(title LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\' OR album LIKE ? ESCAPE '\\' "
                 "OR album_artist LIKE ? ESCAPE '\\' OR filename LIKE ? ESCAPE '\\' OR genre LIKE ? ESCAPE '\\')")
        v += [_like(q)] * 6
    for key, col in (("artist", "artist"), ("album", "album"), ("album_artist", "album_artist"),
                     ("genre", "genre"), ("folder", "folder"), ("library", "library_id"),
                     ("year", "year"), ("fmt", "fmt")):
        if args.get(key) not in (None, "", "all"):
            w.append(f"{col} = ?")
            v.append(args[key])
    # 专辑的「身份」是 (album, COALESCE(album_artist, artist))，不是专辑名：曲库里
    # 「单曲」这类名字被十几位艺人共用，只按 album 过滤会把不同人的歌混成一页。
    # 这里复用 _albums 的同一个分组表达式，两边口径永远一致。
    if args.get("group_artist") not in (None, "", "all"):
        w.append("COALESCE(NULLIF(album_artist,''), artist) = ?")
        v.append(args["group_artist"])
    if args.get("ids"):
        ids = [int(x) for x in re.findall(r"\d+", str(args["ids"]))][:2000]
        if ids:
            w.append(f"id IN ({','.join('?' * len(ids))})")
            v += ids
    return w, v


_ORDER_MAP = {
    "default": "artist, album, disc_no, track_no, id",
    "title": "title COLLATE NOCASE",
    "artist": "artist COLLATE NOCASE, album",
    "album": "album COLLATE NOCASE, disc_no, track_no",
    "added": "added_at DESC",
    "played": "mtime DESC",
    "duration": "duration DESC",
    "year": "year DESC, album",
    "size": "size DESC",
    "bitrate": "bitrate DESC",
}

# A–Z 目录用：首字母分组的规范顺序（I/U/V 没有汉字取它们，# 兜底数字/符号/生僻字）。
_AZ_LETTERS = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + ["#"]


def _az_order(w: List[str], v: List[Any], desc: bool = False) -> Dict[str, Any]:
    """把当前筛选下的曲目排成「拼音首字母分组、组内按标题码点」的一条 id 串，
    并给出每个字母块的 {letter, offset, count}——前端据此放分区表头、点字母快跳。

    首字母只认 sp/textidx（pypinyin 离线导出的 GBK 查表，见 tools/gen_pinyin_table.py），
    产品运行时不 import pypinyin，也不在前端再算一遍——两处各算必歪，字母表头和快跳
    就会跟实际落点不一致，正是用户点名要修的那类 bug。

    只跑窄投影（id+title）+ 带索引的 ORDER BY title COLLATE NOCASE，20k 首也就是一条
    走 temp-b-tree 的小 SELECT；整串按写版本号进 _agg 缓存，翻页、点字母、要块表都命中
    同一份。desc 把整串镜像反转并重算块偏移，保证倒序看每个字母块仍各自连续。
    缓存里放的是 dict；调用方只读，切片/反转都在这份之外新建列表，不改缓存（见
    tools/test_agg_cache.py 的 payload-stable 约束）。
    """
    def build() -> Dict[str, Any]:
        sql = (f"SELECT id, title FROM tracks WHERE {' AND '.join(w + ['missing = 0'])}"
               f" ORDER BY title COLLATE NOCASE")
        buckets: Dict[str, List[int]] = {}
        for r in db.query(sql, v):
            buckets.setdefault(pinyin_initial(r["title"] or ""), []).append(int(r["id"]))
        ids: List[int] = []
        blocks: List[Dict[str, Any]] = []
        for L in _AZ_LETTERS:
            b = buckets.get(L)
            if b:
                blocks.append({"letter": L, "offset": len(ids), "count": len(b)})
                ids.extend(b)
        return {"ids": ids, "blocks": blocks}

    m = _agg(f"az|{tuple(w)}|{tuple(v)}", build)
    if not desc:
        return m
    blocks: List[Dict[str, Any]] = []
    off = 0
    for blk in reversed(m["blocks"]):
        blocks.append({"letter": blk["letter"], "offset": off, "count": blk["count"]})
        off += blk["count"]
    return {"ids": list(reversed(m["ids"])), "blocks": blocks}



class Handler(BaseHTTPRequestHandler):
    # Server 头只能按 latin-1 编码；中文品牌名写进去会让 send_response 直接抛
    # UnicodeEncodeError，连 500 都发不出去。这里固定用 ASCII 产品标识。
    server_version = "LuHuiMusicPlayer/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- plumbing
    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        if os.environ.get("SALTPLAYER_VERBOSE"):
            print("[api]", fmt % args)

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: Optional[Dict[str, str]] = None, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        # 这里**不再**回 Access-Control-Allow-Origin: *。页面和后端同源
        # （127.0.0.1:<port>），CORS 对它没有用处；通配符只让任意网页拿到了
        # 「读响应」的授权。跨站预检/请求由 _guard() 直接 403。
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _guard(self) -> bool:
        """Host/Origin 白名单：挡住 DNS rebinding 与浏览器跨站调用。

        威胁模型是「用户浏览器里的任意网页」：同用户的本机进程本来就能直接读曲库，
        不是这一层能解决的；这里要关的是网页把 127.0.0.1 当成自家源来读写的那条路。
        - Host 必须是本机回环 + 本服务端口。DNS rebinding 请求带的是攻击者域名，被挡。
        - 带 Origin 的请求必须是本应用自己的页面；没带 Origin 的（curl / 原生壳 /
          同源导航）不是浏览器跨站请求，放行。
        """
        port = int(getattr(self.server, "server_port", 0) or 0)
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in allowed_hosts:
            self._json({"error": "invalid host"}, 403)
            return False
        origin = (self.headers.get("Origin") or "").strip().lower().rstrip("/")
        if origin:
            allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
            if origin not in allowed_origins:
                self._json({"error": "forbidden origin"}, 403)
                return False
        return True

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def _static(self, rel: str) -> None:
        root = web_root().resolve()
        rel = (rel or "/").lstrip("/") or "index.html"
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except Exception:
            return self._json({"error": "forbidden"}, 403)
        fallback = False
        if not target.is_file():
            # 带扩展名的请求是资源，不是 SPA 路由：以前一律回 index.html，
            # 浏览器把 HTML 当 CSS/JS 收下，控制台只报「Unexpected token '<'」，
            # 真正的 404 被藏起来了（实测 /static/app.css 曾拿到 200 + 13 KB HTML）。
            # 无扩展名的路径才回首页（给 hash 路由之外的直接访问留一条路）。
            if Path(rel).suffix:
                return self._json({"error": "not found", "path": rel}, 404)
            target = root / "index.html"
            if not target.is_file():
                return self._json({"error": "not found"}, 404)
            fallback = True
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        # 回退到首页时它不是 /static/ 下的资源：缓存策略按首页的 no-cache 走，
        # 否则一次 /static/typo.css 会被 max-age 缓存 5 分钟的 HTML。
        cache = "max-age=300" if (self.path.startswith("/static/") and not fallback) else "no-cache"
        try:
            st = target.stat()
        except OSError:
            return self._json({"error": "not found"}, 404)
        # 强 ETag：mtime_ns + 字节数。每次装载页面时 index/app.js/app.css 都要重新验证
        # （index 是 no-cache），没有 ETag 就只能整份重发 —— 实测冷启动 27 个资源里
        # 这三份合计 ~260 KB。现在命中就 304，正文 0 字节。
        etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
        last_mod = formatdate(st.st_mtime, usegmt=True)
        inm = (self.headers.get("If-None-Match") or "").strip()
        if inm and (inm == "*" or etag in [x.strip() for x in inm.split(",")]):
            return self._send(304, b"", ctype, cache=cache,
                              extra={"ETag": etag, "Last-Modified": last_mod})
        data = target.read_bytes()
        self._send(200, data, ctype, cache=cache,
                   extra={"ETag": etag, "Last-Modified": last_mod,
                          "X-Content-Type-Options": "nosniff"})

    def _body(self) -> Dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._guard():
            return
        # 不给跨站预检发许可头：同源页面本来就不需要预检。
        self._send(204, b"", "text/plain")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    # ---------------------------------------------------------------- routes
    def do_GET(self) -> None:  # noqa: N802
        if not self._guard():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            if path.startswith("/api/"):
                return self._get_api(path, q)
            if path.startswith("/assets/"):
                return self._serve_asset(path[len("/assets/"):])
            return self._static(path)
        except BrokenPipeError:
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._json({"error": f"{type(e).__name__}: {e}", "path": path}, 500)

    def _get_api(self, path: str, q: Dict[str, str]) -> None:
        if path == "/api/bootstrap":
            return self._json({"ok": True, **self._bootstrap()})
        if path == "/api/scan":
            return self._json(scanner.state_snapshot())
        if path == "/api/settings":
            return self._json({"ok": True, "settings": load_settings()})
        if path == "/api/rtss":
            # 只读状态：设置页据此说明「为什么现在是兼容模式」，以及排除项装没装。
            from . import rtss as rtss_profile
            from . import app as app_mod
            st = rtss_profile.status()
            flags = (os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS") or "").split()
            return self._json({"ok": True, **st,
                               "running": app_mod.rtss_running(),
                               "gpu": any(f in flags for f in app_mod.GPU_CHROMIUM_FLAGS)})
        if path == "/api/tracks":
            return self._json(self._tracks(q))
        if path == "/api/index":
            return self._json(self._index(q))
        if path == "/api/albums":
            return self._json(self._albums(q))
        if path == "/api/artists":
            return self._json(self._artists(q))
        if path == "/api/folders":
            return self._json({"ok": True,
                               "items": self._folders(flat=_truthy(q.get("flat"))),
                               "excluded": db.blacklist_dirs()})
        if path == "/api/excludes":
            return self._json({"ok": True, "items": db.blacklist_dirs()})
        if path == "/api/quality":
            return self._json({"ok": True, **_agg("quality", db.quality)})
        if path == "/api/genres":
            return self._json({"ok": True, "items": self._genres()})
        if path == "/api/playlists":
            return self._json({"ok": True, "items": db.list_playlists()})
        if path == "/api/favorites":
            limit, offset = _paging(q)
            rows = db.favorite_rows(limit, offset, db.list_cols())
            cnt = db.query_one("SELECT COUNT(*) AS n FROM favorites")
            return self._json({"ok": True, "items": rows, "total": int(cnt["n"]) if cnt else 0})
        if path == "/api/history":
            n = int(q.get("limit") or 60)
            return self._json({"ok": True, "items": db.recent_rows(n, db.list_cols())})
        if path == "/api/queue":
            return self._json({"ok": True, **self._queue()})
        if path.startswith("/api/playlist/"):
            pid = int(re.findall(r"\d+", path)[-1])
            limit, offset = _paging(q)
            rows = db.playlist_tracks(pid, limit, offset, db.list_cols("t."))
            # total 是给前端分页用的：没有它它就没法判断「翻到底了没」，
            # 只能靠「这页返回空」才知道结束 —— 那样最后一屏总要多点一次。
            return self._json({"ok": True, "items": rows, "total": db.playlist_count(pid)})
        if path.startswith("/api/track/"):
            tid = int(re.findall(r"\d+", path)[-1])
            t = db.get_track(tid)
            return self._json({"ok": bool(t), "track": t}, 200 if t else 404)
        if path == "/api/lyrics":
            return self._json(self._lyrics(int(q.get("track_id") or 0)))
        if path == "/api/playback":
            return self._json(self._playback())
        if path.startswith("/api/cover"):
            return self._cover(q)
        if path.startswith("/api/stream/"):
            return self._stream(int(re.findall(r"\d+", path)[-1]))
        if path == "/api/native/state":
            return self._json(audioplay.get().state())
        if path == "/api/native/spectrum":
            return self._json({"ok": True, "bands": audioplay.get().spectrum(
                int(q.get("bands") or 24))})
        if path == "/api/native/devices":
            return self._json({"ok": True, "available": audioplay.NativeEngine.available(),
                               "items": audioplay.get().devices()})
        if path == "/api/native/exclusive_caps":
            # 只读：问驱动「这台能独占开什么」，一次 Initialize 都不调（不锁声卡）。
            # ?device= 不给就跟随盘上那台（None=系统默认端点），界面因此不需要自己传 id。
            d = q.get("device")
            idx = None if d in (None, "", "default") else int(d or 0)
            try:
                r = audioplay.get().exclusive_caps(idx if idx is not None
                                                    else load_settings().get("audio_device"))
            except Exception as e:
                r = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            return self._json({"engine": "native", **r})
        if path == "/api/mini/state":
            # 原生 MiniPill 没有网页可供 CDP 读：读数面由 Qt 壳注册进来的函数交出。
            # 形状 = {"ok":bool, "native":bool, "result":{track_id,playing,pos,...}}；
            # 无头/浏览器模式返回 ok:false，探针据此记「这条通道不存在」而不是产品红。
            return self._json(_window_hook("mini_state"))
        if path == "/api/health":
            return self._json({"ok": True, "ts": time.time(), "pid": os.getpid()})
        return self._json({"error": "unknown endpoint", "path": path}, 404)

    def _post_api(self, path: str, body: Dict[str, Any]) -> None:
        if path.startswith("/api/window/"):
            name = path.rsplit("/", 1)[-1]
            if name == "theme":
                return self._json(_window_hook("theme", bool(body.get("dark", True))))
            if name == "mini":
                # 桌面歌词那条药丸：开关和置顶一起报过来，服务端不自己猜默认值 ——
                # 猜错了就是「界面说开着，屏幕上没有」。
                return self._json(_window_hook("mini", bool(body.get("show", True)),
                                               body.get("on_top")))
            if name == "reveal":
                return self._json(reveal_in_explorer(body.get("path") or ""))
            return self._json(_window_hook(name))
        if path == "/api/playback":
            playback.report(body.get("playback") or body)
            return self._json({"reported": True, **self._playback()})
        if path == "/api/native":
            return self._json(self._native_post(body))
        if path == "/api/settings":
            return self._json({"ok": True, "settings": save_settings(body.get("settings") or body)})
        if path == "/api/library/pick":
            want_file = bool(body.get("file"))
            fn = UI_HOOKS.get("pick_file" if want_file else "pick_folder")
            if not callable(fn):
                return self._json({"ok": False, "need_manual": True,
                                   "error": "当前为浏览器模式，请手动输入文件夹路径"})
            try:
                picked = fn(body.get("path") or "")
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
            if not picked:
                return self._json({"ok": False, "cancelled": True})
            if want_file:
                # 「用户刚在本进程的对话框里选了这份文件」——歌词 attach 认这张一次性票据。
                _remember_picked_file(str(picked))
            return self._json({"ok": True, "path": picked})
        if path == "/api/library/add":
            p = (body.get("path") or "").strip().strip('"')
            if not p:
                return self._json({"ok": False, "error": "路径为空"}, 400)
            np = normalize_path(p)
            if not os.path.isdir(np):
                return self._json({"ok": False, "error": f"文件夹不存在：{np}"}, 400)
            lid = db.add_library(np, body.get("name") or "")
            if body.get("scan", True):
                scanner.scan_async(lid, load_settings().get("include_unsupported", False))
            return self._json({"ok": True, "library_id": lid, "path": np})
        if path == "/api/library/remove":
            lid = body.get("id")
            if not str(lid).isdigit():
                return self._json({"ok": False, "error": "缺少有效的文件夹 id"}, 400)
            db.remove_library(int(lid), bool(body.get("keep_tracks")))
            return self._json({"ok": True})
        if path == "/api/library/rename":
            lid = body.get("id")
            name = str(body.get("name") or "").strip()[:80]
            if not str(lid).isdigit() or not name:
                return self._json({"ok": False, "error": "需要 id 和新的名字"}, 400)
            db.write("UPDATE libraries SET name = ? WHERE id = ?", (name, int(lid)))
            return self._json({"ok": True})
        if path == "/api/scan":
            return self._json(scanner.scan_async(
                body.get("id"),
                bool(body.get("include_unsupported", load_settings().get("include_unsupported"))),
                bool(body.get("force")),
            ))
        if path == "/api/purge":
            return self._json({"ok": True, "removed": db.purge_missing()})
        if path == "/api/excludes/add":
            r = db.add_blacklist(str(body.get("path") or "").strip().strip('"'))
            return self._json(r, 200 if r.get("ok") else 400)
        if path == "/api/excludes/remove":
            return self._json(db.remove_blacklist(str(body.get("path") or "").strip()))
        if path == "/api/playlists":
            try:
                pid = db.create_playlist(body.get("name") or "")
            except ValueError as e:
                return self._json({"ok": False, "error": "歌单名称为空或已存在"}, 409)
            return self._json({"ok": True, "id": pid, "items": db.list_playlists()})
        if path == "/api/playlists/rename":
            db.rename_playlist(int(body["id"]), body.get("name") or "")
            return self._json({"ok": True})
        if path == "/api/playlists/reorder":
            raw = body.get("ids")
            if not isinstance(raw, list):
                return self._json({"ok": False, "error": "ids 得是个数组"}, 400)
            # 和 _save_queue 同一套收口：非数字直接丢掉而不是抛 ValueError 变成 500
            n = db.reorder_playlists([int(x) for x in raw if str(x).isdigit()])
            return self._json({"ok": True, "moved": n, "items": db.list_playlists()})
        if path == "/api/playlists/pin":
            db.set_playlist_pinned(int(body["id"]), bool(body.get("pinned")))
            return self._json({"ok": True, "items": db.list_playlists()})
        if path == "/api/playlists/delete":
            db.delete_playlist(int(body["id"]))
            return self._json({"ok": True})
        if path == "/api/playlists/restore":
            # 撤销「删除歌单」：整份快照（名字/顺序/置顶/条目）交回 db 层写回
            try:
                rid = db.restore_playlist(body or {})
            except (ValueError, TypeError) as e:
                return self._json({"ok": False, "error": f"恢复失败：{e}"}, 400)
            return self._json({"ok": True, "id": rid, "items": db.list_playlists()})
        if path == "/api/playlists/export":
            return self._json(self._export_m3u(int(body.get("id") or 0)))
        m = re.match(r"^/api/playlists/(\d+)/add$", path)
        if m:
            ids = [int(x) for x in body.get("track_ids") or ([int(body["track_id"])] if body.get("track_id") else [])]
            added = db.add_to_playlist(int(m.group(1)), ids, int(body.get("position", -1)))
            return self._json({"ok": True, "added": added})
        m = re.match(r"^/api/playlists/(\d+)/remove$", path)
        if m:
            db.remove_from_playlist(int(m.group(1)), int(body["track_id"]))
            return self._json({"ok": True})
        m = re.match(r"^/api/playlists/(\d+)/move$", path)
        if m:
            db.move_in_playlist(int(m.group(1)), int(body["track_id"]), int(body["position"]))
            return self._json({"ok": True})
        if path == "/api/favorites/toggle":
            on = db.toggle_favorite(int(body["track_id"]))
            return self._json({"ok": True, "favorite": on, "count": len(db.favorite_ids())})
        if path == "/api/play":
            tid = int(body.get("track_id") or 0)
            if tid:
                db.record_play(tid, int(body.get("ms") or 0))
            return self._json({"ok": True})
        if path == "/api/queue":
            return self._json({"ok": True, **self._save_queue(body)})
        if path == "/api/lyrics/attach":
            return self._json(self._attach_lyric(int(body["track_id"]), body.get("path") or ""))
        if path == "/api/lyrics/save":
            return self._json(self._save_lyric_text(int(body.get("track_id") or 0),
                                                    str(body.get("text") or "")))
        if path == "/api/lyrics/save_bytes":
            # 原生壳的 WinUI 文件选择器读不到服务端的票据；它把用户选中的**字节**
            # 传上来，由服务端按同一套歌词逻辑解码/落盘。这样既不开放任意路径读取，
            # 也不会把 GBK 歌词按 UTF-8 解码成乱码（C# 只负责读文件字节）。
            return self._json(self._save_lyric_bytes(int(body.get("track_id") or 0),
                                                     str(body.get("data_b64") or "")))
        return self._json({"error": "unknown endpoint", "path": path}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        parsed = urllib.parse.urlparse(self.path)
        try:
            return self._post_api(parsed.path, self._body())
        except BrokenPipeError:
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    do_PUT = do_PATCH = do_POST

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._guard():
            return
        parsed = urllib.parse.urlparse(self.path)
        body = dict(self._body())
        if parsed.path.startswith("/api/playlists/"):
            body["id"] = int(re.findall(r"\d+", parsed.path)[-1])
            return self._post_api("/api/playlists/delete", body)
        if parsed.path.startswith("/api/library/"):
            body["id"] = int(re.findall(r"\d+", parsed.path)[-1])
            return self._post_api("/api/library/remove", body)
        return self._json({"error": "unknown endpoint"}, 404)

    # ------------------------------------------------------------- data ops
    def _bootstrap(self) -> Dict[str, Any]:
        s = load_settings()
        return {
            "app": {"name": APP_NAME, "version": _version(),
                    "native": bool(WINDOW_HOOKS), "pick_folder": callable(UI_HOOKS.get("pick_folder"))},
            "settings": s,
            # 这两个都带「每个库/每个歌单再数一遍曲目」的子查询，20k 首时合起来要
            # 十几毫秒，而首页每次冷启动都要问一遍 —— 交给 _agg：写一次库才重算一次。
            "libraries": _agg("libraries", lambda: [dict(r) for r in db.list_libraries()]),
            "playlists": _agg("playlists", db.list_playlists),
            "stats": db.stats(),
            "scan": scanner.state_snapshot(),
        }

    def _tracks(self, q: Dict[str, str]) -> Dict[str, Any]:
        w, v = _where_from_args(q)
        if not load_settings().get("include_unsupported") and q.get("all_formats") != "1":
            w.append("playable = 1")
        if (q.get("order") or "") == "az":
            return self._tracks_az(q, w, v)
        order = q.get("order") or "default"
        full = _truthy(q.get("full"))
        if order == "random":
            sql_order = "RANDOM()"
        elif order == "default" and not full:
            # 列表投影（没有 path/embedded_lyrics）时把默认排序换成索引覆盖的那条，
            # 深分页就不必把 20k 行大字段整个排一遍。取单条/要完整行时仍用原排序。
            sql_order = DEFAULT_ORDER_LIST
        else:
            sql_order = _ORDER_MAP.get(order, _ORDER_MAP["default"])
        if q.get("dir", "asc").lower() == "desc" and sql_order not in (DEFAULT_ORDER_LIST,
                                                                       _ORDER_MAP["default"]):
            sql_order += " DESC"
        limit = min(int(q.get("limit") or 0), 5000)
        cols = db.TRACK_COLUMNS if full else db.list_cols()
        rows = db.filter_tracks(w, v, limit, int(q.get("offset") or 0), sql_order, cols)
        if _truthy(q.get("with_cover")):
            # 有图没图都给地址：没图时后端会按这首歌的专辑/艺人生成色块。
            # 前端因此不必再挂占位块，行高也不会因为图片到没到而跳。
            for r in rows:
                r["art_url"] = f"/api/cover?track_id={r['id']}&size=thumb"
        total = db.track_count(w, v)
        return {"ok": True, "items": rows, "total": total}

    def _tracks_az(self, q: Dict[str, str], w: List[str], v: List[Any]) -> Dict[str, Any]:
        """A–Z 目录那一页：按拼音首字母分组排好的整串，翻 offset/limit，或过滤到某个字母。

        走的是内存里那条 id 串（_az_order 算好并缓存），不在 SQL 里 LIMIT/OFFSET——因为
        首字母是 Python 算出来的，SQL 够不着；而整串的「行」只在这页 slice 出来才查
        （tracks_by_ids 固定 3 条 SQL），20k 库点一下 R 也只回一页行。快跳落点和分区表头
        都以同一条 id 串为准，所以点 R 一定停在 R 那一块。
        """
        desc = q.get("dir", "asc").lower() == "desc"
        m = _az_order(w, v, desc)
        ids = m["ids"]
        letter = (q.get("letter") or "").strip().upper()
        if letter:
            blk = next((b for b in m["blocks"] if b["letter"] == letter), None)
            ids = ids[blk["offset"]:blk["offset"] + blk["count"]] if blk else []
        limit = min(int(q.get("limit") or 0), 5000)
        offset = int(q.get("offset") or 0)
        page = ids[offset:offset + limit] if limit else ids[offset:]
        cols = db.TRACK_COLUMNS if _truthy(q.get("full")) else db.list_cols()
        rows = db.tracks_by_ids(page, cols)
        if _truthy(q.get("with_cover")):
            for r in rows:
                r["art_url"] = f"/api/cover?track_id={r['id']}&size=thumb"
        return {"ok": True, "items": rows, "total": len(ids)}

    def _index(self, q: Dict[str, str]) -> Dict[str, Any]:
        """A–Z 目录的骨架：当前筛选下每个字母块的 {letter, offset, count} + 总数。
        前端据此画右侧字母轨（哪些字母有料）和分区表头；点字母快跳的落点就是这里的 offset，
        与 /api/tracks?order=az 翻的是同一条 _az_order 串，不会错位。
        """
        w, v = _where_from_args(q)
        if not load_settings().get("include_unsupported") and q.get("all_formats") != "1":
            w.append("playable = 1")
        m = _az_order(w, v, q.get("dir", "asc").lower() == "desc")
        return {"ok": True, "total": len(m["ids"]), "blocks": [dict(b) for b in m["blocks"]]}

    def _by_ids(self, ids: List[int], cols: Optional[str] = None) -> List[Dict[str, Any]]:
        # 原来这里是 for tid in ids: db.get_track(tid)，每首 3 条 SQL：500 首收藏
        # = 1501 条（实测）。批量以后固定 3 条，顺序仍然按传入的 ids 保持。
        return db.tracks_by_ids(ids, cols or db.TRACK_COLUMNS)

    def _albums(self, q: Dict[str, str]) -> Dict[str, Any]:
        # 专辑页搜索只认「专辑名 / 组艺人」：走 _where_from_args 的全局 q 会把
        # 「某张专辑里有一首标题带关键词的歌」也算命中，搜「周」时冒出一堆
        # 与周无关的专辑（它们的歌里碰巧有「周」）。全库模糊搜索在 /api/tracks。
        qq = dict(q)
        term = str(qq.pop("q", "") or "").strip()
        w, v = _where_from_args(qq)
        if term:
            w.append("(album LIKE ? ESCAPE '\\' OR COALESCE(NULLIF(album_artist,''), artist) LIKE ? ESCAPE '\\')")
            v += [_like(term)] * 2
        base = " AND ".join(w + ["missing = 0", "album IS NOT NULL", "album <> ''"])
        sql = (f"""SELECT album, COALESCE(NULLIF(album_artist,''), artist) AS artist,
                       COUNT(*) AS tracks, SUM(duration) AS duration, MAX(year) AS year,
                       MIN(disc_no) AS disc, SUM(has_cover) AS covers, MIN(id) AS first_id,
                       MIN(CASE WHEN has_cover = 1 THEN id END) AS cover_id,
                       GROUP_CONCAT(id) AS ids
                FROM tracks WHERE {base}
                GROUP BY lower(album), lower(COALESCE(NULLIF(album_artist,''), artist))
                ORDER BY tracks * 1.0 + covers * 0.5 DESC, album""")
        def build() -> Dict[str, Any]:
            # 聚合行 + genre 批量补列一起缓：这两样都只随曲库内容变，分开缓就还得在
            # 热路径上多发一条 SQL（scalar_map 那一跳实测 1 ms，但 0 条才是能验收的数）。
            rows = [dict(x) for x in db.query(sql, v)]
            return {"rows": rows,
                    "genre_of": db.scalar_map([int(r["first_id"]) for r in rows], "genre")}

        agg = _agg(f"albums|{base}|{tuple(v)}", build)
        rows = agg["rows"]
        # 以前这里是「每张专辑再 db.get_track(first_id) 一次」：640 张专辑 = 1921 条 SQL
        # （实测），而那一次查询顺手还把 favorite/plays 也算了，专辑行根本用不到。
        genre_of = agg["genre_of"]
        limit, offset = _paging(q)
        total = len(rows)
        if limit:
            rows = rows[offset:offset + limit]
        items = []
        for r in rows:
            tid = int(r["first_id"])
            item = {
                "album": r["album"], "artist": r["artist"], "tracks": int(r["tracks"]),
                "duration": float(r["duration"] or 0), "year": int(r["year"] or 0),
                # 封面只能取「真的有封面」的那首：MIN(id) 可能恰好是组里没封面的那行，
                # 于是 /api/cover 404、卡片/详情页出现破图（本机 8 张专辑踩到过）。
                "cover_track": int(r["cover_id"] or 0),
                "genre": genre_of.get(tid) or "",
                "idx": pinyin_initial(r["album"] or ""),
            }
            if _truthy(q.get("with_cover")):
                # 列表页只用到封面地址；ids 那串（实测占 albums 响应 19%）留给
                # 真正要点开专辑时再取（前端会改用 ?album= 分页查询）。
                item["art_url"] = _art_url(item["cover_track"], r["album"], r["artist"])
            else:
                item["ids"] = [int(x) for x in str(r["ids"]).split(",")][:400]
            items.append(item)
        return {"ok": True, "items": items, "total": total}

    def _artists(self, q: Dict[str, str]) -> Dict[str, Any]:
        # 同 _albums：艺术家搜索只按「组艺人」名字命中。否则一首歌的歌名/文件名
        # 里有「周」，也会把完全无关的艺人（王力宏、某个上传者）拱进艺术家区。
        qq = dict(q)
        term = str(qq.pop("q", "") or "").strip()
        w, v = _where_from_args(qq)
        if term:
            w.append("COALESCE(NULLIF(album_artist,''), artist) LIKE ? ESCAPE '\\'")
            v.append(_like(term))
        base = " AND ".join(w + ["missing = 0"])
        sql = (f"""SELECT COALESCE(NULLIF(album_artist,''), artist) AS name,
                       COUNT(*) AS tracks, COUNT(DISTINCT lower(album)) AS albums,
                       SUM(duration) AS duration, MAX(has_cover) AS covers, MIN(id) AS first_id,
                       MIN(CASE WHEN has_cover = 1 THEN id END) AS cover_id
                FROM tracks WHERE {base} GROUP BY lower(COALESCE(NULLIF(album_artist,''), artist))
                ORDER BY tracks DESC, name""")
        rows = _agg(f"artists|{base}|{tuple(v)}", lambda: [dict(x) for x in db.query(sql, v)])
        limit, offset = _paging(q)
        total = len(rows)
        if limit:
            rows = rows[offset:offset + limit]
        items = []
        for r in rows:
            item = {
                "artist": r["name"], "tracks": int(r["tracks"]), "albums": int(r["albums"]),
                "duration": float(r["duration"] or 0),
                "cover_track": int(r["cover_id"] or 0),
                "idx": pinyin_initial(r["name"] or ""),
            }
            if _truthy(q.get("with_cover")):
                item["art_url"] = _art_url(item["cover_track"], "", r["name"])
            items.append(item)
        return {"ok": True, "items": items, "total": total}

    def _genres(self) -> List[Dict[str, Any]]:
        def build() -> List[Dict[str, Any]]:
            rows = db.query(
                "SELECT genre AS name, COUNT(*) AS tracks, SUM(duration) AS duration FROM tracks "
                "WHERE missing = 0 AND genre IS NOT NULL AND genre <> '' GROUP BY lower(genre) ORDER BY tracks DESC"
            )
            return [{"name": r["name"], "tracks": int(r["tracks"]), "duration": float(r["duration"] or 0)}
                    for r in rows]
        return _agg("genres", build)

    def _folders(self, flat: bool = False) -> List[Dict[str, Any]]:
        """文件夹视图的数据：flat=True 给出「一个目录一行」的平铺列表，False 给树。

        树是原来唯一的形态：它把每个目录的曲目数往上累加，看总量方便，但想直接跳到
        「放了 200 首的那一层」就要一路点开。椒盐音乐两种都有（目录浏览 + 文件夹平铺），
        所以这里共用同一条 GROUP BY folder，平铺只是不建树、按目录名排序而已。

        建树的历史账：原来每建一个节点都要 normalize_path()，而它内部是 os.path.realpath()
        —— 一次文件系统调用。实测 20k 首（960 个叶子目录）＝1129 次 realpath，接口
        1081 ms，其中 SQL 只占 118 ms。库里存的本来就是规范化过的路径，建树只需要比较
        大小写，不需要再问一次操作系统（顺带修掉 parts.index(p) 在同名子目录下的错位）。
        两个形态各自进 _agg 缓存（key 不同），切平铺不会把树的结果顶掉。
        """
        def build_tree() -> List[Dict[str, Any]]:
            libs = db.list_libraries()
            rows = db.query(
                "SELECT folder, library_id, COUNT(*) AS tracks, SUM(duration) AS duration,"
                " SUM(has_cover) AS covers, MIN(CASE WHEN has_cover = 1 THEN id END) AS cover_track"
                " FROM tracks WHERE missing = 0 GROUP BY folder ORDER BY folder"
            )
            nodes: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                root, parts = _split_path(r["folder"] or "")
                key = root
                parent: Optional[Dict[str, Any]] = None
                for i, name in enumerate(parts):
                    key += name.lower() + "\\"
                    node = nodes.get(key)
                    if node is None:
                        node = {"name": name, "path": os.path.join(root, *parts[:i + 1]),
                                "children": {}, "tracks": 0, "duration": 0.0, "covers": 0,
                                "cover_track": 0}
                        nodes[key] = node
                        if parent is not None:
                            parent["children"][key] = node
                    node["tracks"] += int(r["tracks"])
                    node["duration"] += float(r["duration"] or 0)
                    node["covers"] += int(r["covers"] or 0)
                    if not node["cover_track"]:
                        node["cover_track"] = int(r["cover_track"] or 0)
                    parent = node
            roots = []
            for lib in libs:
                lroot, lparts = _split_path(lib["path"])
                lkey = lroot + "".join(p.lower() + "\\" for p in lparts)
                roots.append({
                    "path": lib["path"], "name": lib["name"] or lib["path"], "lib_id": lib["id"],
                    "tracks": lib["track_count"], "duration": 0.0, "covers": 0, "cover_track": 0,
                    "children": _folder_children(nodes, lkey),
                })
            return roots

        def build_flat() -> List[Dict[str, Any]]:
            rows = db.query(
                "SELECT folder, COUNT(*) AS tracks, SUM(duration) AS duration,"
                " SUM(has_cover) AS covers, MIN(CASE WHEN has_cover = 1 THEN id END) AS cover_track"
                " FROM tracks WHERE missing = 0 AND folder IS NOT NULL AND folder <> ''"
                " GROUP BY folder"
            )
            bl = db.blacklist_dirs()
            out = []
            for r in rows:
                f = r["folder"] or ""
                out.append({
                    "name": os.path.basename(f.rstrip("\\/")) or f,
                    "path": f,
                    "tracks": int(r["tracks"]),
                    "duration": float(r["duration"] or 0),
                    "covers": int(r["covers"] or 0),
                    "cover_track": int(r["cover_track"] or 0),
                    # 黑名单是在扫描时生效的，已经入库的行不会自己消失；标记出来让界面
                    # 能说「这条还在库里，是因为排除之后还没重扫」。
                    "excluded": any(db.covers(b, f) for b in bl),
                })
            out.sort(key=lambda d: d["path"].lower())
            return out

        # 两个形态各自一份缓存：key 不同，切平铺不会把树的结果覆盖掉。
        return _agg("folders|flat", build_flat) if flat else _agg("folders", build_tree)

    def _export_m3u(self, pid: int) -> Dict[str, Any]:
        """把歌单导成 .m3u8（UTF-8 + BOM，Windows 播放器认得中文曲名）。

        写进 <data>/playlists/，不弹保存框：弹框要走 Qt 主线程（UI_HOOKS），而这里
        是个后台 HTTP 线程；导出完把路径报给前端，前端再给一个「在资源管理器中显示」。
        路径写绝对路径而不是相对：相对路径要在播放器里调基准目录，导出到别处就散架。
        """
        pl = db.playlist_items(pid)
        if not pl:
            return {"ok": False, "error": "这个歌单是空的，没有可导出的条目"}
        ids = [int(r["track_id"]) for r in pl]
        tracks = db.tracks_by_ids(ids, "id, path, title, artist, duration")
        by_id = {int(t["id"]): t for t in tracks}
        names = db.query_one("SELECT name FROM playlists WHERE id = ?", (pid,))
        title = (names["name"] if names else f"playlist-{pid}") or ""
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title)[:80].strip() or f"playlist-{pid}"
        d = Path(data_root()) / "playlists"
        try:
            d.mkdir(parents=True, exist_ok=True)
            out = d / (safe + ".m3u8")
            lines = ["#EXTM3U"]
            missing = 0
            for tid in ids:
                t = by_id.get(tid)
                if not t:
                    continue
                dur = int(round(float(t.get("duration") or 0)))
                label = f"{t.get('artist') or '未知艺人'} - {t.get('title') or '未知曲目'}"
                lines.append(f"#EXTINF:{dur},{label}")
                lines.append(t["path"])
                if not os.path.isfile(t["path"]):
                    missing += 1
            # BOM 不是可选项：不带它，部分 Windows 播放器按本地代码页读，中文曲名直接乱码。
            # newline="" 也不是可选项：默认文本模式在 Windows 上会把每个 \n 再翻成 \r\n，
            # 而我们写的本来就是 \r\n —— 落盘变成 \r\r\n，Notepad 里看着正常，potplayer
            # 之类的按行读就每条都带一个野 \r（这条是闸门实测抓到的）。
            with open(out, "w", encoding="utf-8", newline="") as fh:
                fh.write("\ufeff" + "\r\n".join(lines) + "\r\n")
        except OSError as e:
            return {"ok": False, "error": f"写入失败：{e}"}
        return {"ok": True, "path": str(out), "count": len(by_id), "missing": missing,
                "bytes": out.stat().st_size}

    def _queue(self) -> Dict[str, Any]:
        data = db.get_meta("queue", {"items": [], "index": 0, "context": "", "track": None, "position": 0})
        # 队列只画行、只按 id 起播：path/歌词正文这些整列都不用传。
        # 实测 300 首从 551,744 B 降到约 178 B/行（约 53 KB）。
        items = [t for t in self._by_ids([int(x) for x in data.get("items", []) if str(x).isdigit()],
                                         db.list_cols())]
        return {"queue": items, "index": data.get("index", 0), "context": data.get("context", ""),
                "track_id": data.get("track"), "position": data.get("position", 0)}

    def _save_queue(self, body: Dict[str, Any]) -> Dict[str, Any]:
        ids = [int(x) for x in body.get("items", []) if str(x).isdigit()][:5000]
        db.set_meta("queue", {
            "items": ids,
            "index": int(body.get("index", 0) or 0),
            "context": str(body.get("context", ""))[:120],
            "track": body.get("track_id"),
            "position": float(body.get("position", 0) or 0),
            "saved_at": time.time(),
        })
        return {"saved": len(ids)}

    @staticmethod
    def _lyric_roots(t: Dict[str, Any]) -> List[str]:
        """一首歌允许读歌词的两个根：歌曲所在目录 + 应用数据目录。"""
        roots: List[str] = []
        folder = t.get("folder") or (
            os.path.dirname(t.get("path") or "") if t.get("path") else "")
        if folder:
            roots.append(os.path.normcase(normalize_path(str(folder))))
        roots.append(os.path.normcase(normalize_path(str(data_root()))))
        return [r for r in roots if r]

    def _attach_lyric(self, tid: int, path: str) -> Dict[str, Any]:
        t = db.get_track(tid)
        if not t:
            return {"ok": False, "error": "曲目不存在"}
        p = normalize_path(path)
        if not p:
            return {"ok": False, "error": "歌词文件不存在"}
        if Path(p).suffix.lower() not in LYRIC_EXTS:
            return {"ok": False, "error": "歌词文件必须是 .lrc 或 .txt"}
        try:
            size = os.path.getsize(p) if os.path.isfile(p) else -1
        except OSError:
            size = -1
        if size < 0:
            return {"ok": False, "error": "歌词文件不存在"}
        if size > _LYRIC_MAX_BYTES:
            return {"ok": False, "error": "歌词文件太大（上限 1 MB）"}

        text: Optional[str] = None
        store_path = p
        if not _path_in_roots(p, self._lyric_roots(t)):
            if not _take_picked_file(p):
                return {"ok": False,
                        "error": "歌词文件必须在歌曲所在文件夹或应用数据目录内"}
            # 用户刚通过本进程的文件对话框选过它：读一次并复制进数据目录。
            # 票据一次性，之后 _lyrics 只从歌曲目录/数据目录读，不再认任意外部路径。
            try:
                text = lrc.read_text(p)
                dest = Path(data_root()) / "lyrics" / ("%d.lrc" % int(tid))
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text, encoding="utf-8")
            except OSError as e:
                return {"ok": False, "error": f"读取失败: {e}"}
            store_path = p = str(dest)
        if text is None:
            try:
                text = lrc.read_text(p)
            except Exception as e:
                return {"ok": False, "error": f"读取失败: {e}"}
        parsed = lrc.parse(text)
        if not parsed["lines"] and not parsed.get("plain"):
            return {"ok": False, "error": "该文件没有可用的歌词内容"}
        db.write("UPDATE tracks SET lyric_path = ? WHERE id = ?", (store_path, tid))
        return {"ok": True, "path": store_path, "lines": len(parsed["lines"])}

    def _save_lyric_bytes(self, tid: int, data_b64: str) -> Dict[str, Any]:
        """原生壳专用：接收选中歌词文件的**字节**（base64），在服务端解码。

        为什么不是让原生壳把路径递进来：那等于把 A2 的任意文件读取重新打开。
        为什么不是让 C# 先解码成字符串：LRC 常见 GBK/Big5，C# 默认 UTF-8 会把它们
        读成乱码；sp/lyrics.decode_bytes 有整套编码探测，应该由它来判。
        """
        if not data_b64:
            return {"ok": False, "error": "歌词内容是空的"}
        import base64
        try:
            raw = base64.b64decode(data_b64.encode("ascii"), validate=True)
        except Exception:
            return {"ok": False, "error": "歌词数据不是合法的 base64"}
        if len(raw) > _LYRIC_MAX_BYTES:
            return {"ok": False, "error": "歌词文件太大（上限 1 MB）"}
        try:
            text = lrc.decode_bytes(raw)
        except Exception as e:
            return {"ok": False, "error": f"读取失败: {e}"}
        return self._save_lyric_text(tid, text)

    def _save_lyric_text(self, tid: int, text: str) -> Dict[str, Any]:
        """把用户粘进来/选进来的歌词正文落成 <data>/lyrics/<track_id>.lrc 并挂载。

        原来前端选完文件只在内存里画一次（applyLyricText），下一首就没了 —— 用户
        的体感是「我明明挂过歌词」。落盘后走 _attach_lyric，等于和「选一个 .lrc
        文件」同一条校验路径：服务端解析，前端再读回来，两边看到的行数是同一个数。
        """
        if not text.strip():
            return {"ok": False, "error": "歌词内容是空的"}
        # 落盘前设个上限：_body() 不限制请求体积，真有人粘 50MB 进来就该报错而不是造一个巨型文件。
        if len(text) > 200000:
            return {"ok": False, "error": "歌词太长了（上限 20 万字符）"}
        parsed = lrc.parse(text.replace("\r\n", "\n").replace("\r", "\n"))
        if not parsed["lines"] and not parsed.get("plain"):
            return {"ok": False, "error": "这些文字里没有可用的歌词"}
        d = Path(data_root()) / "lyrics"
        try:
            d.mkdir(parents=True, exist_ok=True)
            p = d / ("%d.lrc" % int(tid))
            p.write_text(text, encoding="utf-8")
        except OSError as e:
            return {"ok": False, "error": f"保存失败：{e}"}
        out = self._attach_lyric(tid, str(p))
        if not out.get("ok"):
            try:
                p.unlink()      # 挂载没成就别留一堆没人认的孤儿文件
            except OSError:
                pass
            return out
        out["source"] = str(p)
        return out

    def _playback(self) -> Dict[str, Any]:
        """桌面歌词小窗的那一份读数。

        秒数不在这里算：sp/playback.py 按服务端自己的单调时钟外推（主窗口最小化时它的
        rAF 会被 Chromium 冻住，那条药丸恰恰是给「主窗收进托盘」用的）。
        歌名/艺人是这里从库里补的：小窗只认这一个接口，不该自己去翻曲库。
        duration 也以库里那份为准 —— 页面在 loadedmetadata 之前给出来的是 NaN，
        而外推的终点要用它，宁可取库里扫出来的那个数。
        """
        d = playback.snapshot()
        tid = d.get("track_id")
        t = db.get_track(tid) if tid else None
        d["title"] = (t or {}).get("title") or ""
        d["artist"] = (t or {}).get("artist") or ""
        dur = (t or {}).get("duration")
        if dur:
            try:
                d["duration"] = float(dur)
            except (TypeError, ValueError):
                pass
        return d

    # ---------------------------------------------------------------- native
    def _source_facts(self, t: Dict[str, Any]) -> Dict[str, int]:
        """独占协商要的三个源侧量：位深 / 采样率 / 声道。**量不到就回 0，一个都不猜**。

        这三个数是 bit-perfect 那句结论的输入（sp/audioplay.py:_bit_perfect_locked），
        而 0 在这里不是「小的那个值」，是「不知道」——不知道就只能说「不经混音器、不加处理」，
        不能说「bit-perfect」。所以宁可回 0 也不许回一个看起来合理的默认（以前代码里那句
        `source_bits or 24` 就是这类猜测：它让每一首量不到位深的歌都被算成「24 位源要量化」）。

        bit_depth 这列是后加的，老库整列是 0。起播时顺手补**这一行**：一次 mutagen 单文件探针，
        本机实测冷 7.4ms、热 0.2–0.4ms（tools/probe_metadata_speed.py 那趟），相对 ffmpeg 冷启动
        的几十毫秒不算一笔；补完写库，同一首下次就不再探。为什么值得为它写盘：这个结论会显示在
        界面上，同一首歌「这次说 bit-perfect、下次不说」是不可接受的（那等于结论由运气决定）。
        """
        bits = int(t.get("bit_depth") or 0)
        rate = int(t.get("sample_rate") or 0)
        ch = int(t.get("channels") or 0)
        if bits <= 0:
            try:
                bits = int(metadata.probe_bit_depth(str(t.get("path") or "")) or 0)
            except Exception:
                bits = 0
            if bits > 0:
                try:
                    db.write("UPDATE tracks SET bit_depth = ? WHERE id = ?", (bits, int(t["id"])))
                except Exception:
                    pass          # 写不回去只影响下次还要探一遍，不影响这一次的判定
        # 容器里的声道数会撒谎：E-AC3/Atmos 的 mp4 sample entry 常把 channelcount 写成 2，
        # 本机 143 个 m4a 里 127 个真实布局是 5.1(side)。库里的 2 会让下混误以为不用留
        # 余量，整轨峰 1.7 的素材直接进 clip（1115：368872 个样点 >1）。只对「可能会撒谎
        # 且当前读数 ≤2」的容器探一次 ffmpeg 的文件头，量到就和库里对账写回；量不到保持
        # 不知道（0），绝不拿 2 兜底。缓存 key 带 size/mtime，换文件即作废旧结论。
        path = str(t.get("path") or "")
        ext = os.path.splitext(path)[1].lower()
        if ch <= 2 and ext in (".m4a", ".mp4", ".aac"):
            key = (path, int(t.get("size") or 0), int(t.get("mtime") or 0))
            with _STREAM_FACTS_LOCK:
                facts = _STREAM_FACTS.get(key)
            if facts is None:
                try:
                    facts = metadata.probe_stream_facts(path)
                except Exception:
                    facts = {}
                with _STREAM_FACTS_LOCK:
                    if len(_STREAM_FACTS) > 512:
                        _STREAM_FACTS.clear()
                    _STREAM_FACTS[key] = facts
            if facts:
                pch = int(facts.get("channels") or 0)
                prate = int(facts.get("sample_rate") or 0)
                if pch > 0 and pch != ch:
                    ch = pch
                    try:
                        db.write("UPDATE tracks SET channels = ? WHERE id = ?",
                                 (ch, int(t["id"])))
                    except Exception:
                        pass
                if rate <= 0 and prate > 0:
                    rate = prate
                    try:
                        db.write("UPDATE tracks SET sample_rate = ? WHERE id = ?",
                                 (rate, int(t["id"])))
                    except Exception:
                        pass
        return {"source_bits": max(0, bits), "source_rate": max(0, rate),
                "source_ch": max(0, ch)}

    def _native_out_format(self) -> Dict[str, Any]:
        """决定输出流的（率 / 声道 / 设备）。

        两条通道各有硬约束，不能共用一个「目标率」：
          · 共享档（默认）走 WASAPI shared，**不做重采样**，只能开设备混音率。本机默认
            端点 EDIFIER N500 混音是 96 kHz，拿 44.1/48 去开会直接回
            `[Errno -9997] Invalid sample rate`（2026-10-24 实测）。所以共享档的率
            永远取选中设备的 rate，audio_target_rate 不参与。
          · 独占档才谈得上目标率（协商会让驱动确认）。
        设备也必须落成一条**明确的 WASAPI 端点**：以前跟随系统时把 PortAudio 的
        output_device_index 传成 None，它会去用自己那套全局默认（本机是 MME 6 号），
        而率又是从 WASAPI 默认端点读的 96 kHz —— 两个宿主 API 错配后回调只按
        ~26k 帧/秒供数（0.7 s 墙钟走 0.192 s 进度），听感就是坏掉/没声。
        所以这里把共享端点索引单独算出来，独占仍用设置里的原值（None=系统默认 COM 端点）。
        """
        s = load_settings()
        dev_idx = s.get("audio_device")
        try:
            devs = audioplay.get().devices()
        except Exception:
            devs = []
        chosen = None
        if dev_idx is not None:
            chosen = next((d for d in devs if d["index"] == int(dev_idx)), None)
        if chosen is None:
            chosen = next((d for d in devs if d.get("is_default")),
                          devs[0] if devs else None)
        mix_rate = int(chosen["rate"]) if chosen and chosen.get("rate") else 44100
        target = int(s.get("audio_target_rate") or 0)
        # 三档语义（界面上就是那三个选项）：
        #   -1 跟随源（逐位直通）：源率设备支持就按源率开，不重采样；换不同采样率的曲子
        #      必须重开设备，部分声卡在换率那一下有一声很轻的咔嗒（DAC 重锁，抹不掉）。
        #    0 跟随设备（默认）：固定到设备混音率。所有曲子同一份独占施工图，切歌只复用、
        #      永不 Release→Initialize —— 「切歌一声」从源头消失；不同源率交给 aresample。
        #   >0 固定到指定率：同上，只是率由用户点名（设备不支持时协商退到最近档并如实报）。
        follow_source = target < 0
        exclusive_rate = mix_rate if target <= 0 else target
        return {"rate": mix_rate, "ch": 2,
                "device_index": dev_idx,
                "shared_device_index": None if chosen is None else int(chosen["index"]),
                "exclusive": bool(s.get("audio_exclusive")),
                "exclusive_rate": exclusive_rate,
                "exclusive_follow_source": follow_source,
                "device_name": (chosen or {}).get("name", ""),
                "target_rate": target}

    def _native_dsp(self, s: Dict[str, Any]) -> Dict[str, Any]:
        """把设置里的音质参数摊成引擎 configure() 的关键字（均衡器关＝十段全 0＝不滤波）。"""
        gains = [float(x) for x in (s.get("eq_gains") or [0.0] * 10)][:10]
        if not s.get("eq_enabled"):
            gains = [0.0] * 10
        # 静音旗标（SALTPLAYER_SILENCE）只在这里落地一次：它把**交给引擎的增益**钉成 0，
        # 不去改 s["muted"]/s["volume"] —— 盘上那份 settings.json 是和用户共用的，
        # 写它等于替用户重设偏好（2026-10-13 那起外放事故的成因，见 功能对照表.md §10）。
        # 这一处是不是真的生效，不靠注释自证：tools/silence_gate.py 那把尺在开任何会出声
        # 的断言之前，从**这个实例**读 state().gain（原生档）或元素 volume（浏览器档）判。
        silent = playback.forced_silence() or bool(s.get("muted"))
        return {
            "volume": 0.0 if silent else float(s.get("volume", 0.8)),
            "balance": float(s.get("audio_balance", 0.0) or 0.0),
            "gains_db": gains,
            "loudness": bool(s.get("audio_loudness")),
            "fade_in_sec": float(s.get("audio_fade_in", 0.0) or 0.0),
            # 虚拟扬声器是 DSP 参数（configure 当场换，不断流），和抖动/开流参数不是一类。
            "speakers": bool(s.get("audio_speakers")),
            "speakers_amount": float(s.get("audio_speakers_amount", 0.35) or 0.0),
            # 抖动是**开流参数**（协商那一次算进施工图，泵只照抄），所以这里改它不会打断
            # 正在放的那首——下一首才生效。前端那行的提示语要说清这一点，不然就是
            # 「改了没反应、再改一次才生效」那种解释不清的延迟。
            "dither": bool(s.get("audio_dither")),
        }

    def _native_post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        eng = audioplay.get()
        action = body.get("action")
        s = load_settings()
        if not eng.available():
            return {"ok": False, "error": "原生引擎依赖未安装（ffmpeg / pyaudiowpatch）"}
        if action in ("pause", "resume", "seek"):
            # 控制类动作前先对「服务端载的是哪一首」。引擎是全进程单例，它只知道自己的 path，
            # 而前端的身份是 track_id；这一句把两边对上，对不上就一个字都不动。
            # 实测事故（57922，2026-10-13）：页面挂着 Smile(296.7s)，引擎里还是上一趟真机
            # 测试留下的 RAT CITY(177.9s、已 ended)，一次拖进度条 seek 的是**那首别人的歌**
            # （而且它已播完，seek 早退），响应里的 state 又把前端锚点拽回 177.93 ——
            # 症状是「进度条拖不动」，而服务端什么都没做错，错在把控制发给了不相干的一首。
            # 按 basename 大小写不敏感比对，和前端 _natSameFile 同一把尺（不拿 duration 当身份）。
            tid = int(body.get("track_id") or 0)
            t = db.get_track(tid) if tid else None
            cur = str((eng.state() or {}).get("path") or "")
            # 身份是完整路径，不是 basename：两个目录里各有一首 same.flac 时，
            # 按文件名比会把控制动作发给错误的那一首（seek/暂停被吞或作用错对象）。
            if t is not None and (not cur or not _same_path(str(t["path"]), cur)):
                return {"ok": True, "ignored": "track_mismatch", **eng.state()}
        if action == "play":
            tid = int(body.get("track_id") or 0)
            t = db.get_track(tid)
            if not t or t.get("missing"):
                return {"ok": False, "error": "曲目不存在"}
            if not os.path.isfile(t["path"]):
                return {"ok": False, "error": "文件不在：" + t["path"]}
            of = self._native_out_format()
            eng.configure(rate=of["rate"], **self._native_dsp(s))
            # speed 必须过服务端那把夹取（sp/playback.clamp_rate，和小窗外推同一把）：
            # 前端 applySettings 的 clamp 只管**元素**和**界面**，原生档出声的是这条 ffmpeg 流，
            # 以前这里直接把 settings.rate 原样递进去 —— 绕过界面塞 9 就真跑 9 倍，
            # 而界面上写着 4.00×。夹不住的东西不许叫上限。
            sf = self._source_facts(t)
            # 下混预留：按这一首**下混后的实测真峰**算，不再一律 −6 dB。真峰要整轨
            # 解码才量得准（4 分钟歌约 2.7 s），所以只在缓存里查；没查到就用兜底
            # 起播，并把这首丢进后台队列 —— 下一次播放就是准的。绝不在起播路径上
            # 卡几秒钟等一个电平读数（那会变成「每首 5.1 开头都顿一下」）。
            peak = None
            if int(sf.get("source_ch") or 0) > int(of["ch"]):
                peak = downmix.peak_for(t["path"])
                if peak is None:
                    downmix.enqueue(t["path"])
            r = eng.play(t["path"], pos=float(body.get("pos", 0) or 0.0),
                         duration=float(t.get("duration") or 0.0),
                         rate=of["rate"], ch=of["ch"], device_index=of["device_index"],
                         shared_device_index=of["shared_device_index"],
                         exclusive=of["exclusive"], exclusive_rate=of["exclusive_rate"],
                         exclusive_follow_source=of["exclusive_follow_source"],
                         speed=playback.clamp_rate(s.get("rate")),
                         source_peak=peak,
                         **sf)
            if r.get("ok") is False:
                # 「点了播放没声音」必须留下一条可查的原因。以前这条错误只回到前端，
                # 服务端日志里什么都没有，事后只能靠猜宿主 API/采样率/设备哪一环坏了。
                print("[native-play] 失败 rate=%s shared_device=%s exclusive=%s: %s"
                      % (of["rate"], of["shared_device_index"],
                         of["exclusive"], r.get("error")), flush=True)
            if r.get("ok") is not False:
                # eng.play 成功时只回 {ok, mode, note, rate, channels}，而前端 nativeCtl 是在
                # **同一个响应**里重定锚的（web/app.js 要求 r.engine==='native' 且 r.position 是数），
                # 少了这两个键页面就「服务端在播、界面冻在 0:00」，轮询表也永远不会起（本机实测：
                # 服务端 2.9 秒走了 2.92s，前端 posNow() 一直是 0）。pause/resume/seek 本来就带
                # state()，play 是这条契约里唯一的漏网 —— 在这里补齐，而不是让前端多发一次 GET。
                r = {**eng.state(), **r}
            return r
        if action == "pause":
            eng.pause(); return {"ok": True, **eng.state()}
        if action == "resume":
            eng.resume(); return {"ok": True, **eng.state()}
        if action == "stop":
            # 和 pause/resume/seek 一样把 state() 带回去（前端 nativeCtl 只认「响应里有 engine」
            # 才刷音质面板那行，缺了它就得为「停」这个动作多发一次 GET）。
            # 注意 position 这里仍是「停在哪儿」而不是 0（stop 不清 _base_pos/_consumed），
            # 所以前端对 stop 只采纳 playing/ended，不拿它重定锚 —— 见 web/app.js nativeCtl。
            eng.stop(); return {"ok": True, **eng.state()}
        if action == "seek":
            eng.seek(float(body.get("pos", 0) or 0.0)); return {"ok": True, **eng.state()}
        if action == "configure":
            of = self._native_out_format()
            eng.configure(rate=of["rate"], **self._native_dsp(s))
            # 同上：configure 改的是 DSP（响度/音量/声像/淡入/均衡曲线），音质面板那行正好要报
            # 这些（响度均衡开、格式、倍速）。带 state 回来，前端就不用为「动了一个开关」再发 GET。
            return {"ok": True, **eng.state()}
        return {"ok": False, "error": "unknown action: " + str(action)}

    def _lyrics(self, tid: int) -> Dict[str, Any]:
        t = db.get_track(tid)
        if not t:
            return {"ok": False, "error": "not found", "lines": []}
        src = ""
        text = ""
        lp = t.get("lyric_path") or ""
        if lp and os.path.isfile(lp):
            # 读侧同一条闸：老版本可能存过任意外部路径，这样的行只清掉、绝不读。
            # 文件暂时不在（移动盘拔了）时不清，保留原关联。
            if not (_lyric_file_ok(lp) and _path_in_roots(lp, self._lyric_roots(t))):
                db.write("UPDATE tracks SET lyric_path = '' WHERE id = ?", (tid,))
                lp = ""
        if lp and os.path.isfile(lp):
            try:
                text = lrc.read_text(lp)
                src = lp
            except Exception as e:
                text = ""
                src = f"error:{e}"
        if not text and t.get("embedded_lyrics"):
            text = str(t["embedded_lyrics"])
            src = "embedded"
        if not text:
            guess = self._guess_lrc_sidecar(t)
            if guess and _lyric_file_ok(guess) and _path_in_roots(
                    guess, self._lyric_roots(t)):
                try:
                    text = lrc.read_text(guess)
                    src = guess
                    db.write("UPDATE tracks SET lyric_path = ? WHERE id = ?", (guess, tid))
                except Exception:
                    pass
        parsed = lrc.parse(text) if text else {"lines": [], "meta": {}, "plain": False}
        return {
            "ok": True, "track_id": tid, "source": src, "plain": parsed.get("plain", False),
            "lines": parsed.get("lines", []), "meta": parsed.get("meta", {}),
            "text": parsed.get("text", "") if parsed.get("plain") else "",
            "title": t.get("title"), "artist": t.get("artist"), "album": t.get("album"),
        }

    @staticmethod
    def _guess_lrc_sidecar(t: Dict[str, Any]) -> str:
        folder = t.get("folder") or ""
        stem = Path(t.get("filename") or "").stem
        if not folder:
            return ""
        for ext in (".lrc", ".txt"):
            cand = os.path.join(folder, stem + ext)
            if os.path.isfile(cand):
                return cand
        return ""

    # ------------------------------------------------------------- binaries
    def _cover(self, q: Dict[str, str]) -> None:
        """封面/色块图：按用途出缩略图、给强 ETag、认 If-None-Match。

        三条实测依据（tools/scale_before.log）：单张 1027 KB、一屏 24 张 = 24.6 MB、
        带 If-None-Match 请求仍然回 200 + 整图。所以这里做三件事：
          1) size=thumb|art|full 决定实际发的字节（列表 240，主图 640）；
          2) 没内嵌封面时按专辑名生成首字母色块（前端就不用给每行挂一个尺寸不一致的
             占位块，行高不再跳）；
          3) 强 ETag + Cache-Control: immutable，命中 If-None-Match 回 304（几十字节）。
        """
        tid = int(q.get("track_id") or 0)
        variant = q.get("size") or "art"
        # 没给 size 的老调用（比如直接拼 track_id 的旧代码）退回原尺寸，行为不变
        if variant not in metadata.COVER_VARIANTS:
            variant = "full" if variant == "full" else "art"
        size_param = q.get("size")
        t: Dict[str, Any] = {}
        if tid:
            t = db.cover_info(tid) or {}
            if not t:
                return self._json({"error": "track not found"}, 404)
        src = t.get("cover_path") or ""
        if (not src or not os.path.isfile(src)) and size_param and (q.get("album") or q.get("artist") or tid):
            # 只有明确带了 size（＝新前端，它会把这个 URL 当 img src 用）才生成色块：
            # 裸 track_id 的老调用要继续拿到 204，那是既有断言里的契约。
            # 「单曲」是没有专辑标签时的桶名，用它做首字会让首页一屏出现五块相同的字；
            # 这类占位专辑改用曲目名生成，每张卡片因此各有各的识别度。
            album = str(t.get("album") or q.get("album") or "")
            if album.strip() in ("单曲", "未知", "未知专辑", "Unknown", "Unknown Album"):
                album = ""
            gen = metadata.text_art_png(str(t.get("title") or q.get("title") or ""),
                                         album,
                                         str(t.get("artist") or q.get("artist") or ""),
                                         px=metadata.COVER_VARIANTS.get(variant) or 240)
            if gen:
                src = gen
        if not src or not os.path.isfile(src):
            if size_param:
                return self._json({"error": "no artwork"}, 404)
            # 不带 size 的裸请求保持原来的 204 契约（verify_app 里就有这条断言）
            return self._send(204, b"", "image/png", cache="no-store")
        try:
            path, ctype = metadata.cover_variant(src, variant)
            st = os.stat(path)
        except Exception:
            return self._json({"error": "no artwork"}, 404)
        # ETag 只用「同尺寸 + 同 mtime + 同体积」三元组：够唯一、比整图哈希便宜。
        # 文件是不可变命名（v240_xxxx.img / gen_xxxx.png），所以 immutable 也成立。
        etag = f'"{variant}-{int(st.st_mtime)}-{st.st_size}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            return
        try:
            data = Path(path).read_bytes()
        except Exception:
            return self._json({"error": "unreadable artwork"}, 404)
        if not data:
            return self._send(204, b"", "image/png", cache="no-store")
        return self._send(200, data, ctype, cache="public, max-age=31536000, immutable",
                          extra={"ETag": etag, "Vary": "Accept-Encoding"})

    def _serve_asset(self, rel: str) -> None:
        from .config import data_root
        target = (data_root() / "assets" / rel).resolve()
        try:
            target.relative_to((data_root() / "assets").resolve())
        except Exception:
            return self._json({"error": "forbidden"}, 403)
        if not target.is_file():
            return self._json({"error": "not found"}, 404)
        self._send(200, target.read_bytes(),
                   mimetypes.guess_type(str(target))[0] or "application/octet-stream",
                   cache="private, max-age=3600")

    def _stream(self, tid: int) -> None:
        # 音频流是延迟最敏感的一条路：起播/拖动进度条都在这里。以前用 get_track()，
        # 它为了一条路径顺手再查收藏和播放次数（3 条 SQL），而且和封面、列表请求
        # 排同一把锁。现在只问「这个 id 的文件在哪」，一条。
        r = db.query_one("SELECT path, fmt FROM tracks WHERE id = ?", (tid,))
        if not r:
            return self._json({"error": "track not found"}, 404)
        path = r["path"] or ""
        if not os.path.isfile(path):
            return self._json({"error": "文件已移动或删除", "path": path}, 404)
        try:
            fsize = os.path.getsize(path)
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        start, end = 0, fsize - 1
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if not m:
                return self._send(416, b"", extra={"Content-Range": f"bytes */{fsize}"})
            s, e = m.group(1), m.group(2)
            if s == "" and e:
                start = max(0, fsize - int(e))
            else:
                start = int(s or 0)
                if e:
                    end = min(fsize - 1, int(e))
            if start > end or start >= fsize:
                return self._send(416, b"", extra={"Content-Range": f"bytes */{fsize}"})
        length = end - start + 1
        code = 206 if rng else 200
        ext = Path(path).suffix.lower()
        ctype = MIME_BY_EXT.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Last-Modified", _http_date(os.path.getmtime(path)))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{fsize}")
        self.send_header("ETag", f'W/"{tid}-{int(os.path.getmtime(path))}-{fsize}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        chunk = 256 * 1024
        try:
            with open(path, "rb", buffering=0) as f:
                f.seek(start)
                left = length
                while left > 0:
                    data = f.read(min(chunk, left))
                    if not data:
                        break
                    self.wfile.write(data)
                    left -= len(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def _split_path(p: str) -> Tuple[str, List[str]]:
    """(盘符或根, [段, ...])，只做字符串拆分，绝不碰文件系统。

    Windows: 'C:\\Users\\x\\music' → ('C:\\', ['Users','x','music'])
    POSIX:   '/data/music'       → ('/', ['data','music'])
    """
    drv, rest = os.path.splitdrive(p or "")
    root = (drv.upper() + "\\") if drv else "/"
    return root, [s for s in rest.replace("\\", "/").split("/") if s]


def _folder_key(root: str, parts: List[str]) -> str:
    return root + "".join(s.lower() + "\\" for s in parts)


def _folder_children(nodes: Dict[str, Any], lkey: str) -> List[Dict[str, Any]]:
    """库根下面的第一层节点。键里已经带了盘符+全部段，比较只是字符串活。"""
    depth = lkey.count("\\")
    kids = [n for k, n in nodes.items()
            if k != lkey and k.startswith(lkey) and k.count("\\") == depth + 1]
    return sorted((_folder_out(n) for n in kids), key=lambda x: x["name"].lower())


def _folder_out(node: Dict[str, Any]) -> Dict[str, Any]:
    children = [_folder_out(c) for c in sorted(node["children"].values(), key=lambda x: x["name"].lower())]
    return {"name": node["name"], "path": node["path"], "tracks": node["tracks"],
            "duration": node["duration"], "children": children,
            # 目录自己的封面＝里面第一首有封面的歌；一首都没有就用目录名生成色块，
            # 这样侧栏/文件夹视图不会因为「有没有图」而长短不齐。
            "art_url": _art_url(node.get("cover_track", 0), node["name"], "")}


def _http_date(ts: float) -> str:
    import email.utils
    return email.utils.formatdate(ts, usegmt=True)


def _version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:
        return "1.0"


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server(ThreadingHTTPServer):
    allow_reuse_address = False   # Windows: SO_REUSEADDR would let 2 processes share a port
    daemon_threads = True

    def handle_error(self, request, client_address):  # noqa: N802
        """<audio> aborts in-flight range requests constantly (scrubbing). Not an error."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Server:
    """Binds 127.0.0.1 on a free port.

    allow_reuse_address is deliberately off: on Windows SO_REUSEADDR lets a second
    process bind the *same* port and silently split incoming connections.
    """

    def __init__(self, port: Optional[int] = None) -> None:
        last_err: Optional[Exception] = None
        for _ in range(20):
            p = port or find_free_port()
            try:
                self.httpd = _Server(("127.0.0.1", p), Handler)
                self.port = p
                break
            except OSError as e:
                last_err = e
                if port:
                    raise
                continue
        else:  # pragma: no cover
            raise OSError(f"无法绑定本地端口: {last_err}")
        self.thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> str:
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="salt-api")
        self.thread.start()
        try:
            port_path().write_text(str(self.port), "utf-8")
        except Exception:
            pass
        return self.url

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
