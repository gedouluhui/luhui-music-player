"""Audio tag / artwork / sidecar discovery.

Everything here is best-effort: a broken or exotic file must never abort a scan,
so every public call returns a dict with whatever could be learned plus 'error'.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, Optional
from pathlib import Path

from .config import AUDIO_EXTS, COVER_NAMES, IMAGE_EXTS, LYRIC_EXTS, data_root

# Formats Chromium's <audio> element can actually decode (wma/ape/dsf/tak/wv cannot).
PLAYABLE = {
    ".mp3", ".flac", ".m4a", ".mp4", ".aac", ".wav", ".ogg", ".oga", ".opus",
    ".aiff", ".aif",
}

_TRACK_PREFIX = re.compile(
    r"^\s*(?:\[[\s]*\d{1,3}[\s]*[-_.)]?\s*\]|[\(（]?\d{1,3}[\)）]?[\s]*[-_.)]|[\d]{1,3}\s*[-–—]\s*)"
)
_FEAT = re.compile(r"\s*[\(\[（【]\s*(feat|ft|featuring|with)\.?\s*[:：]?\s*[^)\]）】]+[\)\]）】]", re.I)


def cover_cache_dir() -> Path:
    d = data_root() / "covers"
    d.mkdir(parents=True, exist_ok=True)
    return d


# 封面派生尺寸。列表行里的图只有 44~120 px，却被要求下载整张内嵌封面
# （实测单张 1027 KB，一屏 24 张 = 24.6 MB / 205 ms），所以按用途出小图。
# list 只给 34px 的行内缩略图：240px 的 thumb 在滚动时每行都要解码/上传 6 倍像素，
# 无头实测（1792 首、QtWebEngine 87 软件光栅）快速滚动时这是主要的长任务来源之一。
# 96px 覆盖 2x DPR 的 34px 格；art/full 仍给主封面和歌词背景。
COVER_VARIANTS = {"list": 96, "thumb": 240, "art": 640, "full": 0}

_CJK_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",   # 微软雅黑：中文标题首字母块要用它
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
)
_FONT_CACHE: Dict[str, Any] = {}


def _font(size: int):
    """尽量拿一个能画中文的字体；拿不到就返回 None（字块退化成纯色底）。"""
    key = str(size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    f = None
    try:
        from PIL import ImageFont
        for cand in _CJK_FONT_CANDIDATES:
            if os.path.isfile(cand):
                try:
                    f = ImageFont.truetype(cand, size)
                    break
                except Exception:
                    continue
        if f is None:
            f = ImageFont.load_default()
    except Exception:
        f = None
    _FONT_CACHE[key] = f
    return f


def _initial(text: str) -> str:
    """取封面字块的「首字母」：拉丁取首字母大写，中文取前两三个字本身。"""
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", (text or "").strip())
    if not s:
        return ""
    words = s.split(" ")
    first = words[0]
    # 「2007世界巡回演唱会」这种以年份开头的名字，取数字后面的正文首字；
    # 直接取首位会得到「2」，看起来像占位失败而不是专辑色块。
    core = re.sub(r"^[\W\d_]+", "", first)
    if re.search(r"[\u4e00-\u9fff]", core):
        cores = [re.sub(r"^[\W\d_]+", "", w) for w in words[:3]]
        return "".join(w[:1] for w in cores if w) or core[:2]
    return first[:2].upper() if len(first) <= 2 else first[0].upper()


def _hue_key(*parts: str) -> int:
    return int(hashlib.sha1("|".join(p or "" for p in parts).encode("utf-8", "ignore")).hexdigest()[:6], 16)


def _sibling_tmp(out: Path) -> Path:
    """并发安全的临时名：同一张图可能被两个请求（主页 thumb/full）同时生成。

    以前固定用 <name>.tmp，两个线程会写同一个文件，先完成的 replace 后把另一半
    写了一半的临时文件换上去（或 replace 撞共享冲突直接失败），对应请求就 404，
    客户端 BitmapImage 失败后那张卡片永久空白。
    """
    suffix = f".{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    return out.with_name(out.name + suffix)


def text_art_png(title: str, album: str = "", artist: str = "", px: int = 240) -> Optional[str]:
    """没有内嵌封面时生成一张「首字母色块」图，路径缓存后当文件发。

    浏览器对 204 的处理是：不画任何内容，而且每次重新加载这个 URL 都要再请求一次。
    前端因此只能给每一行挂一个 CSS 占位块（列表是 HTML 画的，占位块和封面尺寸还
    不一致，行高会跳）。改成后端发一张和真封面同尺寸的 PNG 之后：行高稳定、
    浏览器能长期缓存、没有封面的专辑也有颜色区分。
    """
    # art3：色块从高饱和海报色改成低饱和哑光石板色，和深色玻璃 UI 同一明度区间；
    # 首字也收小一档，避免首页一屏出现五块巨大的同色字。
    key = hashlib.sha1(f"art3|{px}|{album}|{artist}|{title}".encode("utf-8", "ignore")).hexdigest()[:24]
    out = cover_cache_dir() / f"gen_{key}.png"
    if out.is_file():
        return str(out)
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    h = _hue_key(album or title, artist)
    hue = h % 360
    import colorsys
    sat = 0.16 + ((h >> 8) % 9) / 100.0      # 0.16–0.24
    val = 0.22 + ((h >> 16) % 8) / 100.0     # 0.22–0.29
    top = tuple(round(c * 255) for c in colorsys.hsv_to_rgb(hue / 360.0, sat, min(0.50, val * 1.34)))
    bottom = tuple(round(c * 255) for c in colorsys.hsv_to_rgb(hue / 360.0, sat * 1.10, val * 0.80))
    light = tuple(round(c * 255) for c in colorsys.hsv_to_rgb(hue / 360.0, 0.18, 0.88))
    img = Image.new("RGB", (px, px))
    dr = ImageDraw.Draw(img)
    span = max(1, px - 1)
    for y in range(px):
        t = y / span
        dr.line([(0, y), (px, y)],
                fill=tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    initial = _initial(album or title or artist)
    f = _font(int(px * 0.30)) if initial else None
    if initial and f is not None:
        try:
            box = dr.textbbox((0, 0), initial, font=f)
            w, hh = box[2] - box[0], box[3] - box[1]
        except Exception:
            w = hh = px // 3
            box = (0, 0, w, hh)
        dr.text(((px - w) / 2 - box[0], (px - hh) / 2 - box[1]), initial, font=f, fill=light)
    elif initial:
        # 没有可用字体（非 Windows/没装 Pillow 字体）时画两个圆点，仍然比纯空白强
        r = px * 0.07
        for i in range(2):
            cx = px / 2 + (i - 0.5) * px * 0.22
            dr.ellipse([cx - r, px / 2 - r, cx + r, px / 2 + r], fill=light)
    tmp = _sibling_tmp(out)
    try:
        img.save(tmp, "PNG")
        os.replace(tmp, out)
        return str(out)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        # 并发同伴可能已经把同一张图写好了，那就直接用它，不再回一个 404
        return str(out) if out.is_file() and out.stat().st_size > 0 else None


def cover_variant(src: str, variant: str = "art") -> tuple:
    """返回 (实际可发的文件路径, mime)。variant 未知或缩放出问题时退回原图。

    派生文件以「源文件绝对路径 + mtime + 尺寸」为键落盘，所以同一张专辑在列表、
    现在播放、歌词背景三处各自只生成一次，之后全是直接读小文件。
    """
    size = COVER_VARIANTS.get(variant)
    if size is None or not src or not os.path.isfile(src):
        return src, _mime_of(src)
    try:
        mtime = int(os.path.getmtime(src))
        w0 = int(os.stat(src).st_size)
    except OSError:
        return src, _mime_of(src)
    key = hashlib.sha1(f"{os.path.abspath(src)}|{mtime}|{w0}|{size}".encode("utf-8", "ignore")).hexdigest()[:28]
    out = cover_cache_dir() / f"v{size}_{key}.img"
    if out.is_file() and out.stat().st_size > 0:
        return str(out), _mime_of(str(out))
    try:
        from PIL import Image
        with Image.open(src) as im:
            if size and min(im.size) <= size:
                return src, _mime_of(src)          # 本来就比目标小：不放大，直接发原图
            has_alpha = im.mode in ("RGBA", "LA", "P") and (
                "transparency" in im.info or im.mode != "P")
            im = im.convert("RGBA" if has_alpha else "RGB")
            if size:
                im.thumbnail((size, size), Image.BILINEAR)
            tmp = _sibling_tmp(out)
            try:
                if has_alpha:
                    im.save(tmp, "PNG", optimize=True)
                else:
                    im.save(tmp, "JPEG", quality=82, optimize=True)
                os.replace(tmp, out)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        return str(out), _mime_of(str(out))
    except Exception:
        # Pillow 缺失/图片解码失败都不能让封面整块打不开；并发同伴写好的派生图
        # 是可以直接发的，别把它当成自己的失败产物删掉。
        if out.is_file() and out.stat().st_size > 0:
            return str(out), _mime_of(str(out))
        return src, _mime_of(src)


def _mime_of(path: str) -> str:
    ext = Path(path).suffix.lower()
    by_ext = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif"}
    if ext in by_ext:
        return by_ext[ext]
    # 内嵌封面统一存成 *.img，扩展名不带信息，所以看魔数；认不出来才当 JPEG 兜底
    # （原来只看后缀，PNG 的 .img 会被标成 image/jpeg，浏览器靠嗅探侥幸能画）。
    try:
        head = Path(path).read_bytes()[:16]
    except OSError:
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    return "image/jpeg"


def is_audio(path: str) -> bool:
    return Path(path).suffix.lower() in AUDIO_EXTS


def is_playable(path: str) -> bool:
    return Path(path).suffix.lower() in PLAYABLE


def clean_title(filename: str) -> str:
    stem = Path(filename).stem
    stem = _TRACK_PREFIX.sub("", stem).strip(" -–—. _")
    return stem or Path(filename).stem


def split_artist_title(stem: str) -> tuple[str, str]:
    """'Artist - Title' or 'Title - Artist' heuristics used when tags are absent."""
    for sep in (" - ", " – ", " — ", " -", "- "):
        if sep in stem:
            a, b = stem.split(sep, 1)
            a, b = a.strip(" -–—."), b.strip(" -–—.")
            if a and b and not re.match(r"^\d+$", a):
                # numeric-first convention: '01 - Title' handled by clean_title already
                return a, b
    return "", stem


def _riff_info(path: str) -> Dict[str, str]:
    """RIFF INFO (WAV) / FORM text chunks (AIFF) - mutagen's WAVE ignores these."""
    out: Dict[str, str] = {}
    key_map = {
        "INAM": "title", "IART": "artist", "IPRD": "album", "IGNR": "genre",
        "ICRD": "year", "IPRT": "tracknumber", "ICMT": "comment", "ILNG": "language",
        "ISFT": "encoder", "ITCH": "keyword", "TRCK": "tracknumber", "TIT1": "grouping",
        "NAME": "title", "AUTH": "artist", "ANNO": "comment", "(c) ": "copyright",
        "APFR": "album",
    }
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[:4] not in (b"RIFF", b"FORM"):
                return out
            form = head[8:12]
            if form not in (b"WAVE", b"AIFF", b"AIFF", b"WAIF", b"wave"):
                return out
            pos, size = 12, int.from_bytes(head[4:8], "little") + 8
            while pos + 8 <= size:
                f.seek(pos)
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                cid = hdr[:4].decode("latin-1")
                clen = int.from_bytes(hdr[4:8], "little")
                pos += 8
                if cid == "LIST":
                    f.seek(pos)
                    ltype = f.read(4)
                    if ltype in (b"INFO", b"infO"):
                        end = pos + clen
                        sub = pos + 4
                        while sub + 8 <= end:
                            f.seek(sub)
                            sh = f.read(8)
                            if len(sh) < 8:
                                break
                            sid = sh[:4].decode("latin-1")
                            slen = int.from_bytes(sh[4:8], "little")
                            sub += 8
                            f.seek(sub)
                            raw = f.read(min(slen, 4096))
                            sub += (slen + 1) & ~1
                            field = key_map.get(sid)
                            if field and field not in out:
                                val = _decode_tag(raw)
                                if val:
                                    out[field] = val
                elif cid.strip() in key_map and clen < 4096:
                    f.seek(pos)
                    val = _decode_tag(f.read(clen))
                    if val:
                        out.setdefault(key_map[cid.strip()] if cid.strip() in key_map else key_map.get(cid, ""), val)
                pos += (clen + 1) & ~1
                if clen < 0 or pos > size + 8192:
                    break
    except Exception:
        return out
    return {k: v for k, v in out.items() if k in key_map.values() and v}


def _decode_tag(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            s = raw.split(b"\x00", 1)[0].decode(enc).strip()
        except Exception:
            continue
        if s and "\ufffd" not in s:
            return s
    return raw.split(b"\x00", 1)[0].decode("latin-1", "replace").strip()


def bit_depth_of(info: "Any") -> int:
    """从一个 mutagen `info` 对象里取**源位深**；量不到就回 0（0＝不知道，不是 16）。

    为什么这条不能含糊：独占档那句「bit-perfect」的判据里有它——设备给 16 位容器而源是
    24 位，就是**确凿的量化**（该加抖动）；源位深没量到却按默认 24 位算，会把一堆真 16 位
    的 CD rip 误判成「量化过」，反过来把 0 当 16 又是把未知说成已知。所以两边都要显式区分：
    0 走「不敢判」那一条，24/16 走实测那一条。

    属性名各家不一样（FLAC 的 StreamInfo、WAVE 的 WaveStreamInfo、MP4 的 MP4Info 都叫
    bits_per_sample；AIFF 有些版本叫 bitdepth；老 lame 对象叫 sample_size），
    所以按名挨个试一遍，取第一个合理值。本机实测（曲库随机抽 70 首）：flac / wav / m4a(ALAC)
    三类都从 bits_per_sample 拿到了值，16 与 24 两档都有，没有一个落到「量不到」。
    """
    for attr in ("bits_per_sample", "bitdepth", "sample_size", "bits"):
        try:
            v = int(getattr(info, attr, 0) or 0)
        except (TypeError, ValueError):
            v = 0
        if 0 < v <= 64:
            return v
    return 0


def probe_bit_depth(path: str) -> int:
    """单独量一首的位深（起播时库里是 0 就用它补一次，量不到回 0）。

    实测耗时：本机 70 首冷读均值 **7.4 ms/首**、同一首第二次 0.2–0.4 ms（OS 文件缓存）。
    这个数字决定了它能不能放在起播路径上——起播本身要起一根 ffmpeg（几十到两百毫秒），
    7 毫秒不是瓶颈，所以答案是能，而且补完就落库，同一首一辈子只付一次。
    """
    try:
        from mutagen import File as _MF
        mf = _MF(path)
        info = getattr(mf, "info", None)
        return bit_depth_of(info) if info is not None else 0
    except Exception:
        return 0


# ffmpeg 的声道布局名 → 真实声道数。只收实际会出现在曲库里的布局；没列的按「不知道」
# 处理（返回 0），**不拿 2 当兜底**——那正是这次要修的病：E-AC3/Atmos 的 mp4 sample
# entry 常写 channelcount=2，mutagen 照抄，ffmpeg 解出来却是 5.1(side)。
_LAYOUT_CHANNELS = {
    "mono": 1, "stereo": 2, "downmix": 2, "binaural": 2,
    "2.1": 3, "3.0": 3, "3.0(back)": 3,
    "3.1": 4, "4.0": 4, "quad": 4, "quad(side)": 4,
    "4.1": 5, "5.0": 5, "5.0(side)": 5,
    "5.1": 6, "5.1(side)": 6, "6.0": 6, "6.0(front)": 6, "hexagonal": 6,
    "6.1": 7, "6.1(back)": 7, "6.1(front)": 7, "7.0": 7, "7.0(front)": 7,
    "7.1": 8, "7.1(wide)": 8, "7.1(wide-side)": 8, "octagonal": 8,
}

_AUDIO_LINE = re.compile(r"Audio:\s*([\w-]+).*?,\s*(\d+)\s*Hz,\s*([^,]+)")


def probe_stream_facts(path: str) -> Dict[str, Any]:
    """用 ffmpeg 的输入摘要读第一条音频流的真实 (channels, sample_rate, codec, layout)。

    为什么不信 mutagen：本机 143 个 m4a 里 **127 个是 5.1(side)**（全部在「环绕」/
    Dolby Atmos 文件夹），而库里的 channels 全是 2 —— E-AC3 的 mp4 sample entry 把
    channelcount 写成 2，真实布局在 dec3 里，mutagen 不解析。这个错值会让 5.1 下混
    以为自己不需要留余量，于是整轨峰 1.7 的素材直接进 clip（1115 实测 368872 个
    样点 >1）。ffmpeg 解的是同一条流，它的输入摘要才是设备真正会收到的声道数。

    只读文件头，不解码；量不到回 {}（调用方保持「不知道」，不猜 2）。
    """
    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return {}
    if not ff:
        return {}
    kw: Dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        p = subprocess.run([ff, "-hide_banner", "-nostdin", "-i", path],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=8, **kw)
    except Exception:
        return {}
    line = None
    for ln in p.stderr.decode("utf-8", "replace").splitlines():
        if "Audio:" in ln:
            line = ln
            break
    if not line:
        return {}
    m = _AUDIO_LINE.search(line)
    if not m:
        return {}
    codec, rate_s, layout = m.group(1), m.group(2), m.group(3).strip()
    key = layout.lower()
    ch = _LAYOUT_CHANNELS.get(key, 0)
    if not ch:
        mm = re.match(r"(\d+)\s*(?:channels?|ch)\b", key)
        if mm:
            ch = int(mm.group(1))
    try:
        rate = int(rate_s)
    except ValueError:
        rate = 0
    return {"channels": ch, "sample_rate": rate, "codec": codec, "layout": layout}



def _read_easy(path: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        import mutagen
    except Exception:
        return out
    ext = os.path.splitext(path)[1].lower()
    if ext in (".wav", ".aiff", ".aif", ".wave"):
        out.update(_riff_info(path))
    try:
        mf = mutagen.File(path, easy=True)
    except Exception:
        try:
            mf = mutagen.File(path)
        except Exception:
            return out
    if mf is None:
        return out
    tags = mf.tags or {}

    def first(key: str) -> Optional[str]:
        try:
            v = tags.get(key)
        except Exception:
            return None
        if not v:
            return None
        val = v[0] if isinstance(v, (list, tuple)) else v
        return str(val).strip() or None

    key_map = {
        "title": ["title", "TIT2", "\xa9nam"],
        "artist": ["artist", "TPE1", "\xa9ART"],
        "album": ["album", "TALB", "\xa9alb"],
        "album_artist": ["albumartist", "TPE2", "\xa9alb", "album artists"],
        "genre": ["genre", "TCON", "\xa9gen"],
        "year": ["date", "TDRC", "\xa9day", "year"],
        "tracknumber": ["tracknumber", "TRCK", "trkn"],
        "discnumber": ["discnumber", "TPOS", "disk"],
    }
    for field, keys in key_map.items():
        for k in keys:
            v = first(k)
            if v:
                out[field] = v
                break
    # embedded lyrics
    for k in ("lyrics", "LYRICS", "UNSYNCEDLYRICS", "uslt", "\xa9lyr"):
        try:
            v = tags.get(k)
        except Exception:
            v = None
        if v:
            val = v[0] if isinstance(v, (list, tuple)) else v
            txt = str(val)
            # mutagen returns ID3 SYLT/USLT objects sometimes
            if hasattr(val, "text"):
                txt = str(val.text)
            if txt.strip():
                out["embedded_lyrics"] = txt
                break
    try:
        info = mf.info
    except Exception:
        info = None
    if info is not None:
        out["duration"] = float(getattr(info, "length", 0) or 0)
        out["bitrate"] = int(getattr(info, "bitrate", 0) or 0)
        out["sample_rate"] = int(getattr(info, "sample_rate", 0) or 0)
        out["channels"] = int(getattr(info, "channels", 0) or 0)
        out["bit_depth"] = bit_depth_of(info)
    return out


def _read_binary_tags(path: str) -> tuple[Optional[bytes], str, Optional[str]]:
    """Returns (embedded_cover_bytes, cover_mime, embedded_lyric_text)."""
    cover: Optional[bytes] = None
    mime = "image/jpeg"
    lyric: Optional[str] = None
    try:
        import mutagen
        from mutagen.id3 import ID3
        from mutagen.flac import FLAC
        from mutagen.mp4 import MP4
        from mutagen.oggvorbis import OggVorbis
        from mutagen.musepack import Musepack  # noqa: F401  (probe availability only)
    except Exception:
        try:
            import mutagen  # noqa: F401
        except Exception:
            return None, mime, None
        mutagen = None  # type: ignore
    ext = Path(path).suffix.lower()

    def try_fn(fn):
        try:
            fn()
        except Exception:
            pass

    if ext == ".mp3":
        def f():
            nonlocal cover, mime, lyric
            id3 = ID3(path)
            for frame in id3.getall("APIC"):
                if frame.desc and "cover" not in frame.desc.lower():
                    continue
                if frame.type in (3, 6, 0) or cover is None:
                    cover = frame.data
                    mime = (frame.mime or "image/jpeg").decode("latin-1") if isinstance(frame.mime, bytes) else (frame.mime or "image/jpeg")
                    break
            for frame in id3.getall("USLT"):
                if frame.lang in ("eng", "zho", "chi", "") or lyric is None:
                    lyric = str(frame.text)
                    break
        try_fn(f)
    elif ext == ".flac":
        def f():
            nonlocal cover, mime, lyric
            flac = FLAC(path)
            if flac.pictures:
                pic = flac.pictures[0]
                cover, mime = pic.data, (pic.mime or "image/jpeg")
            for k in ("LYRICS", "UNSYNCEDLYRICS", "lyrics"):
                if flac.get(k):
                    lyric = str(flac[k][0])
                    break
        try_fn(f)
    elif ext in (".m4a", ".mp4", ".aac", ".m4b"):
        def f():
            nonlocal cover, mime, lyric
            mp4 = MP4(path)
            covr = mp4.get("\xa9covr") or mp4.get("covr")
            if covr:
                cover = bytes(covr[0])
                mime = "image/png" if cover[:4] == b"\x89PNG" else "image/jpeg"
            lyr = mp4.get("\xa9lyr")
            if lyr:
                lyric = str(lyr[0])
        try_fn(f)
    elif ext in (".ogg", ".oga", ".opus", ".spx"):
        def f():
            nonlocal cover, mime, lyric
            import base64
            from mutagen.id3 import PictureType
            ogg = OggVorbis(path)
            blocks = ogg.get("metadata_block_picture") or []
            if blocks:
                from mutagen.flac import Picture
                pic = Picture(base64.b64decode(blocks[0]))
                cover, mime = pic.data, (pic.mime or "image/jpeg")
            for k in ("LYRICS", "lyrics"):
                if ogg.get(k):
                    lyric = str(ogg[k][0])
                    break
        try_fn(f)
    elif mutagen is not None:
        def f():
            nonlocal cover
            mf = mutagen.File(path)
            pics = getattr(mf, "pictures", None)
            if pics:
                cover, mime = pics[0].data, (getattr(pics[0], "mime", "") or "image/jpeg")
        try_fn(f)
    return cover, mime or "image/jpeg", lyric


def sidecar_cover(path: str) -> Optional[str]:
    """Album-artwork file next to the audio file, if any."""
    folder = Path(path).parent
    for name in COVER_NAMES:
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
            cand = folder / f"{name}{ext}"
            if cand.is_file():
                return str(cand)
    # <track name>.<img> beats a generic folder image
    stem = Path(path).stem
    for ext in IMAGE_EXTS:
        cand = folder / f"{stem}{ext}"
        if cand.is_file():
            return str(cand)
    return None


def sidecar_lyric(path: str) -> Optional[str]:
    stem = Path(path).stem
    folder = Path(path).parent
    for ext in LYRIC_EXTS:
        cand = folder / f"{stem}{ext}"
        if cand.is_file() and cand.stat().st_size < 2_000_000:
            return str(cand)
    return None


def store_cover(blob: bytes, key_hint: str) -> Optional[str]:
    if not blob:
        return None
    key = hashlib.sha1(blob[:65536] + key_hint.encode("utf-8", "ignore")).hexdigest()[:24]
    out = cover_cache_dir() / f"{key}.img"
    try:
        if not out.exists() or out.stat().st_size != len(blob):
            tmp = out.with_suffix(".tmp")
            tmp.write_bytes(blob)
            os.replace(tmp, out)
        return str(out)
    except Exception:
        return None


def read_track_metadata(path: str) -> Dict[str, Any]:
    """Full record for one file: tags + duration + artwork + lyric sidecar."""
    p = Path(path)
    st = None
    try:
        st = p.stat()
    except Exception as e:
        return {"path": path, "error": f"stat: {e}"}

    meta: Dict[str, Any] = {
        "path": str(p),
        "filename": p.name,
        "folder": str(p.parent),
        "fmt": p.suffix.lower().lstrip("."),
        "size": int(st.st_size),
        "mtime": st.st_mtime,
        "ctime": st.st_ctime,
    }
    tags: Dict[str, Any] = {}
    try:
        tags = _read_easy(str(p)) or {}
    except Exception as e:
        meta["error"] = f"tags: {e}"

    dur = float(tags.get("duration") or 0)
    if dur <= 0:
        dur = _probe_duration_mutagen(str(p))
    meta["duration"] = round(max(0.0, dur), 3)

    title = (tags.get("title") or "").strip()
    artist = (tags.get("artist") or "").strip()
    album = (tags.get("album") or "").strip()
    album_artist = (tags.get("album_artist") or "").strip() or artist
    if not title or title.lower() in ("unknown", "no title", "标题"):
        stem = clean_title(p.name)
        t_artist, t_title = split_artist_title(stem)
        title = title or t_title or stem
        artist = artist or t_artist
    meta["title"] = title[:200]
    meta["artist"] = (artist or "未知艺术家")[:200]
    meta["album"] = (album or ("单曲" if not album else album))[:200]
    meta["album_artist"] = (album_artist or meta["artist"])[:200]
    meta["genre"] = (tags.get("genre") or "")[:120]
    meta["year"] = _parse_year(tags.get("year"))
    meta["track_no"], meta["disc_no"] = _parse_numbers(tags.get("tracknumber"), tags.get("discnumber"))
    meta["bitrate"] = int(tags.get("bitrate") or 0)
    meta["sample_rate"] = int(tags.get("sample_rate") or 0)
    meta["channels"] = int(tags.get("channels") or 0)
    # 位深：独占档那句 bit-perfect 的输入（0＝没量到，见 bit_depth_of 的说明）。
    # 老库里已入库的行是 0，由 sp/api.py 起播时那一次 lazy probe 补，不逼用户重扫。
    meta["bit_depth"] = int(tags.get("bit_depth") or 0)

    # ---- artwork: embedded wins, then track-named image, then folder cover
    cover_file: Optional[str] = None
    try:
        blob, mime, emb_lyric = _read_binary_tags(str(p))
    except Exception:
        blob, mime, emb_lyric = None, "image/jpeg", None
    if blob:
        key_hint = f"{p.name}:{int(st.st_mtime)}"
        cover_file = store_cover(blob, key_hint)
        if emb_lyric and emb_lyric.strip():
            meta["embedded_lyrics"] = emb_lyric
    if not cover_file:
        sc_track = None
        for ext in IMAGE_EXTS:
            cand = p.with_suffix(ext)
            if cand != p and cand.is_file():
                sc_track = str(cand)
                break
        cover_file = sc_track or sidecar_cover(str(p))
    meta["cover_path"] = cover_file
    meta["has_cover"] = 1 if cover_file else 0

    lyr = sidecar_lyric(str(p))
    if lyr:
        meta["lyric_path"] = lyr
    elif meta.get("embedded_lyrics"):
        meta["lyric_path"] = ""  # embedded text is stashed separately
    meta["has_lyrics"] = 1 if (lyr or meta.get("embedded_lyrics")) else 0
    meta["playable"] = 1 if is_playable(str(p)) else 0
    meta["scanned_at"] = time.time()
    return meta


def _probe_duration_mutagen(path: str) -> float:
    try:
        import mutagen
        for kwargs in ({"easy": False}, {}):
            try:
                mf = mutagen.File(path, **kwargs)
            except Exception:
                continue
            length = getattr(getattr(mf, "info", None), "length", None)
            if length:
                return float(length)
    except Exception:
        pass
    return 0.0


def _parse_year(raw: Any) -> int:
    if not raw:
        return 0
    m = re.search(r"(1[89]\d{2}|20\d{2}|21\d{2})", str(raw))
    return int(m.group(1)) if m else 0


def _parse_numbers(trk: Any, disc: Any) -> tuple[int, int]:
    def one(v: Any) -> int:
        if not v:
            return 0
        s = str(v).split("/")[0].strip()
        m = re.match(r"^\d{1,4}", s)
        return int(m.group(0)) if m else 0
    return one(trk), one(disc)
