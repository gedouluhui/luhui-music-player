"""Regression tests for the externally reviewed findings A1-A5.

Run with ``python -m unittest tests.test_review_fixes -v`` from the repo root.

Safety rules for this module:
  * every test uses a throw-away SALTPLAYER_DATA root (set before ``sp`` import);
  * no audio device is ever opened -- the engine reader is invoked directly;
  * the user's library/settings under %LOCALAPPDATA% are never touched.
"""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import base64
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="salt-review-fixes-"))
os.environ["SALTPLAYER_DATA"] = str(_TMP_ROOT / "data")
os.environ.pop("SALTPLAYER_ENGINE_TRACE", None)

from sp import api, audioplay, config, db  # noqa: E402


def _new_dir(name: str) -> Path:
    p = _TMP_ROOT / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def _insert_track(folder: Path, name: str = "song.flac", title: str = "Song") -> int:
    p = folder / name
    if not p.exists():
        p.write_bytes(b"\x00" * 64)
    np = api.normalize_path(str(p))
    now = time.time()
    db.write(
        "INSERT INTO tracks(path,title,artist,album,duration,size,folder,filename,"
        "added_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (np, title, "Artist", "Album", 10.0, 64, str(folder), name, now, now),
    )
    row = db.query_one("SELECT id FROM tracks WHERE path = ?", (np,))
    return int(row["id"])


def _request(port: int, method: str, path: str, body=None, headers=None,
             host=None):
    """Send one request; returns (status, lower-case headers, parsed JSON)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    if host is None:
        conn.request(method, path, body=data, headers=hdrs)
    else:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host)
        for k, v in hdrs.items():
            conn.putheader(k, v)
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders()
        if data is not None:
            conn.send(data)
    resp = conn.getresponse()
    raw = resp.read()
    status = resp.status
    hs = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        obj = None
    return status, hs, obj


class _ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.srv = api.Server()
        cls.srv.start()
        cls.port = cls.srv.port
        cls.origin = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.stop()


class TestA1RequestOrigin(_ServerCase):
    """A1: no wildcard CORS, foreign Host/Origin must not reach the API."""

    def test_health_without_origin_still_works(self) -> None:
        status, hs, obj = _request(self.port, "GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(obj and obj.get("ok"))
        self.assertNotIn("access-control-allow-origin", hs)

    def test_same_origin_allowed_but_no_wildcard(self) -> None:
        status, hs, _ = _request(self.port, "GET", "/api/health",
                                 headers={"Origin": self.origin})
        self.assertEqual(status, 200)
        self.assertNotIn("access-control-allow-origin", hs)

    def test_foreign_origin_is_rejected(self) -> None:
        status, hs, _ = _request(self.port, "GET", "/api/health",
                                 headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertNotIn("access-control-allow-origin", hs)

    def test_forged_host_is_rejected(self) -> None:
        status, hs, _ = _request(self.port, "GET", "/api/health",
                                 host="evil.example")
        self.assertEqual(status, 403)
        self.assertNotIn("access-control-allow-origin", hs)

    def test_localhost_host_is_accepted(self) -> None:
        status, _, obj = _request(self.port, "GET", "/api/health",
                                  host=f"localhost:{self.port}")
        self.assertEqual(status, 200)
        self.assertTrue(obj and obj.get("ok"))

    def test_foreign_preflight_is_rejected(self) -> None:
        status, hs, _ = _request(
            self.port, "OPTIONS", "/api/playlists",
            headers={"Origin": "https://evil.example",
                     "Access-Control-Request-Method": "POST"})
        self.assertEqual(status, 403)
        self.assertNotIn("access-control-allow-origin", hs)

    def test_foreign_post_cannot_mutate_library(self) -> None:
        name = "REVIEW_PWN"
        status, _, _ = _request(self.port, "POST", "/api/playlists",
                                body={"name": name},
                                headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        row = db.query_one("SELECT id FROM playlists WHERE name = ?", (name,))
        self.assertIsNone(row)


class TestA2LyricPaths(_ServerCase):
    """A2: lyric attach/read is confined to the song folder and the data root."""

    def setUp(self) -> None:
        self.folder = _new_dir("a2-" + self._testMethodName)
        self.tid = _insert_track(self.folder, "a2.flac")
        self._saved_hook = api.UI_HOOKS.get("pick_file")
        api.UI_HOOKS["pick_file"] = None

    def tearDown(self) -> None:
        api.UI_HOOKS["pick_file"] = self._saved_hook

    def _attach(self, path: Path) -> dict:
        status, _, obj = _request(self.port, "POST", "/api/lyrics/attach",
                                  body={"track_id": self.tid, "path": str(path)})
        self.assertEqual(status, 200)
        return obj or {}

    def _lyrics(self) -> dict:
        status, _, obj = _request(
            self.port, "GET", f"/api/lyrics?track_id={self.tid}")
        self.assertEqual(status, 200)
        return obj or {}

    def test_sidecar_lyric_still_works(self) -> None:
        p = self.folder / "a2.lrc"
        p.write_text("[00:01.00]sidecar line\n", encoding="utf-8")
        self.assertTrue(self._attach(p).get("ok"))
        self.assertIn("sidecar line", json.dumps(self._lyrics(), ensure_ascii=False))

    def test_outside_file_is_rejected(self) -> None:
        outside = _new_dir("a2-outside-" + self._testMethodName) / "secret.lrc"
        marker = "REVIEW-MARKER-outside"
        outside.write_text(f"[00:01.00]{marker}\n", encoding="utf-8")
        self.assertFalse(self._attach(outside).get("ok"))
        self.assertNotIn(marker, json.dumps(self._lyrics(), ensure_ascii=False))

    def test_wrong_extension_is_rejected(self) -> None:
        p = self.folder / "lyrics.ini"
        p.write_text("[00:01.00]looks like a lyric\n", encoding="utf-8")
        self.assertFalse(self._attach(p).get("ok"))

    def test_oversized_lyric_is_rejected(self) -> None:
        p = self.folder / "huge.lrc"
        p.write_text("[00:01.00]head\n" + ("x" * 1_200_000), encoding="utf-8")
        self.assertFalse(self._attach(p).get("ok"))

    def test_windows_ini_cannot_be_read(self) -> None:
        win_ini = Path(os.environ.get("WINDIR", r"C:\Windows")) / "win.ini"
        if not win_ini.is_file():
            self.skipTest("win.ini not present on this host")
        out = self._attach(win_ini)
        self.assertFalse(out.get("ok"))
        body = json.dumps(self._lyrics(), ensure_ascii=False)
        self.assertNotIn("[fonts]", body)
        self.assertNotIn("16-bit app support", body)

    def test_picker_ticket_allows_one_outside_file_and_copies_it(self) -> None:
        outside = _new_dir("a2-picked-" + self._testMethodName) / "picked.lrc"
        marker = "REVIEW-MARKER-picked"
        outside.write_text(f"[00:01.00]{marker}\n", encoding="utf-8")
        api.UI_HOOKS["pick_file"] = lambda start="": str(outside)
        status, _, picked = _request(self.port, "POST", "/api/library/pick",
                                     body={"file": True, "path": str(self.folder)})
        self.assertEqual(status, 200)
        self.assertTrue(picked and picked.get("ok"))
        self.assertEqual(api.normalize_path(picked["path"]),
                         api.normalize_path(str(outside)))

        out = self._attach(outside)
        self.assertTrue(out.get("ok"), out)
        stored = api.normalize_path(str(out.get("path") or ""))
        lyrics_root = api.normalize_path(str(Path(config.data_root()) / "lyrics"))
        self.assertTrue(stored.lower().startswith(lyrics_root.lower() + os.sep),
                        f"picked lyric was not copied into the data root: {stored}")
        self.assertIn(marker, json.dumps(self._lyrics(), ensure_ascii=False))

        # the ticket is one-shot: the same outside path must not be attachable again
        self.assertFalse(self._attach(outside).get("ok"))

    def test_legacy_outside_lyric_path_is_not_read(self) -> None:
        outside = _new_dir("a2-legacy-" + self._testMethodName) / "legacy.txt"
        marker = "REVIEW-MARKER-legacy"
        outside.write_text(marker + "\n", encoding="utf-8")
        db.write("UPDATE tracks SET lyric_path = ? WHERE id = ?",
                 (str(outside), self.tid))
        body = json.dumps(self._lyrics(), ensure_ascii=False)
        self.assertNotIn(marker, body)

    def test_save_bytes_decodes_non_utf8_lyrics(self) -> None:
        text = "[00:01.00]中文歌词\n[00:02.00]第二行\n"
        raw = text.encode("gb18030")
        status, _, obj = _request(
            self.port, "POST", "/api/lyrics/save_bytes",
            body={"track_id": self.tid,
                  "data_b64": base64.b64encode(raw).decode("ascii")})
        self.assertEqual(status, 200)
        self.assertTrue(obj and obj.get("ok"), obj)
        self.assertIn("中文歌词", json.dumps(self._lyrics(), ensure_ascii=False))

    def test_save_bytes_rejects_oversized_payload(self) -> None:
        raw = b"x" * 1_100_000
        status, _, obj = _request(
            self.port, "POST", "/api/lyrics/save_bytes",
            body={"track_id": self.tid,
                  "data_b64": base64.b64encode(raw).decode("ascii")})
        self.assertEqual(status, 200)
        self.assertFalse(obj and obj.get("ok"), obj)


class _FakeEngine:
    def __init__(self, path: str) -> None:
        self.path = path
        self.paused = 0
        self.resumed = 0
        self.seeked = []

    def available(self) -> bool:
        return True

    def state(self) -> dict:
        return {
            "ok": True, "engine": "native", "path": self.path,
            "playing": True, "ended": False, "position": 1.0, "duration": 10.0,
            "rate": 44100, "channels": 2, "mode": "shared", "speed": 1.0,
            "underruns": 0, "gain": 1.0, "balance": 0.0, "loudness": False,
        }

    def pause(self) -> None:
        self.paused += 1

    def resume(self) -> None:
        self.resumed += 1

    def seek(self, pos: float) -> None:
        self.seeked.append(pos)


class TestA3TrackIdentity(_ServerCase):
    """A3: control actions identify the loaded track by full path, not basename."""

    def setUp(self) -> None:
        self.dir_a = _new_dir("a3-a-" + self._testMethodName)
        self.dir_b = _new_dir("a3-b-" + self._testMethodName)
        self.tid_a = _insert_track(self.dir_a, "same.flac", "A")
        self.tid_b = _insert_track(self.dir_b, "same.flac", "B")
        self.fake = _FakeEngine(api.normalize_path(str(self.dir_a / "same.flac")))
        self._orig_get = api.audioplay.get

    def tearDown(self) -> None:
        api.audioplay.get = self._orig_get

    def _pause(self, tid: int) -> dict:
        api.audioplay.get = lambda: self.fake
        status, _, obj = _request(self.port, "POST", "/api/native",
                                  body={"action": "pause", "track_id": tid})
        self.assertEqual(status, 200)
        return obj or {}

    def test_same_basename_other_folder_is_ignored(self) -> None:
        out = self._pause(self.tid_b)
        self.assertEqual(out.get("ignored"), "track_mismatch")
        self.assertEqual(self.fake.paused, 0)

    def test_exact_path_is_accepted(self) -> None:
        out = self._pause(self.tid_a)
        self.assertNotEqual(out.get("ignored"), "track_mismatch")
        self.assertEqual(self.fake.paused, 1)


# ---------------------------------------------------------------- 前端（web/app.js）
# 这两只函数是纯逻辑，用 Qt 的 QJSEngine 跑 app.js 里的**原文**（和
# tools/test_dual_path_js.py 同一手法）：不启浏览器、不开窗、不需要服务端。
# Qt 5.15 的 QJSEngine 不支持 async/await，所以只切不含 await 的同步函数。
_APP_JS = ROOT / "web" / "app.js"


def _extract_js_function(src: str, header: str) -> str:
    """从 header 那行起切到第一条顶格的 `}`（这几只函数内部没有顶格的 `}`）。"""
    import re
    m = re.search(r"^%s" % re.escape(header), src, re.M)
    if not m:
        raise AssertionError("app.js 里找不到函数：%r" % header)
    lines = src[m.start():].splitlines(keepends=True)
    for i, line in enumerate(lines[1:], 1):
        if line.rstrip("\r\n") == "}":
            return "".join(lines[:i + 1])
    raise AssertionError("函数 %r 找不到结尾" % header)


def _run_js(body: str):
    """在 QJSEngine 里跑一段脚本，经 JSON.stringify 回传结果。"""
    from PyQt5.QtCore import QCoreApplication
    from PyQt5.QtQml import QJSEngine
    app = QCoreApplication.instance() or QCoreApplication([])
    eng = QJSEngine()
    wrapper = ("(function(){ %s\n; return JSON.stringify(typeof __out !== 'undefined'"
               " ? __out : null); })()" % body)
    res = eng.evaluate(wrapper)
    if res.isError():
        raise AssertionError("JS 执行失败: " + res.property("message").toString())
    obj = json.loads(res.toString())
    del eng                      # 先解掉对 engine 的引用再让 app 被回收（Qt 包装对象生命周期）
    del app
    return obj


def _js_with_function(header: str, prelude: str, call: str):
    src = _APP_JS.read_text(encoding="utf-8")
    fn = _extract_js_function(src, header)
    return _run_js(prelude + "\n" + fn + "\n" + call)


class TestA3FrontendTrackIdentity(unittest.TestCase):
    """A3 的前端一半：界面拿服务端 path 和自己这一行对身份，必须比完整路径。"""

    def test_full_path_separator_and_case_insensitive(self) -> None:
        got = _js_with_function(
            "function _natSameFile(serverPath, t) {", "var __out;", """
            __out = [
              _natSameFile('C:\\\\Music\\\\A\\\\same.flac', {path: 'c:/music/a/SAME.flac'}),
              _natSameFile('C:\\\\Music\\\\A\\\\same.flac', {path: 'C:\\\\Music\\\\B\\\\same.flac',
                                                             filename: 'same.flac'}),
              _natSameFile('C:\\\\Music\\\\A\\\\same.flac', {filename: 'same.flac'}),
              _natSameFile('', {path: 'C:\\\\Music\\\\A\\\\same.flac'}),
              _natSameFile('C:\\\\Music\\\\A\\\\same.flac', null)
            ];""")
        self.assertEqual(got, [True, False, True, False, False])


class TestA4FrontendErrorNotice(unittest.TestCase):
    """A4/A5 的前端一半：错误要提示一次、并撤掉「服务端已载本曲」（防 resume 空转）。"""

    def _run(self, call: str) -> dict:
        return _js_with_function(
            "function natErrorNotice(st) {",
            "var S = {_natErrShown: '', _natLoadedId: 7}; var toasts = [];"
            "function toast(msg, kind, ttl){ toasts.push([String(msg), String(kind)]); }"
            "var __out;",
            call)

    def test_error_toasts_once_and_clears_loaded_track(self) -> None:
        got = self._run("""
            var err = {error: {kind: 'decode_failed', rc: 1, detail: 'bad header'}};
            natErrorNotice(err);
            var afterFirst = {n: toasts.length, loaded: S._natLoadedId};
            natErrorNotice(err);                    // 同一份错误：不许再弹
            var afterSecond = {n: toasts.length, loaded: S._natLoadedId};
            natErrorNotice({error: null});          // 恢复后清掉去重标记
            natErrorNotice(err);                    // 再坏一次：应该重新提示
            __out = {first: afterFirst, second: afterSecond, third: toasts.length,
                     kind: toasts[0] && toasts[0][1]};""")
        self.assertEqual(got["first"], {"n": 1, "loaded": None})
        self.assertEqual(got["second"], {"n": 1, "loaded": None})
        self.assertEqual(got["third"], 2)
        self.assertEqual(got["kind"], "err")


def _reader_engine(path: Path, duration: float = 10.0) -> audioplay.NativeEngine:
    eng = audioplay.NativeEngine()
    eng._gen = 0
    eng._stop_evt.clear()
    eng._eof = False
    eng._ended = False
    eng._paused = False
    eng._rate = 44100
    eng._ch = 2
    eng._path = str(path)
    eng._duration = duration
    eng._consumed = 0
    return eng


class TestA4DecodeFailure(unittest.TestCase):
    """A4: a decoder that dies is an error, not a natural end-of-track."""

    def test_corrupt_file_reports_error_and_does_not_end(self) -> None:
        folder = _new_dir("a4-corrupt")
        bad = folder / "corrupt.mp3"
        bad.write_bytes(b"this is not an audio file at all\n" * 4)
        eng = _reader_engine(bad)
        eng._reader_loop(str(bad), 0.0, 44100, 2, 1.0, 0)
        st = eng.state()
        self.assertFalse(st.get("playing"), st)
        self.assertFalse(st.get("ended"), st)
        err = st.get("error")
        self.assertIsInstance(err, dict, st)
        self.assertEqual(err.get("kind"), "decode_failed")
        self.assertNotEqual(int(err.get("rc") or 0), 0)
        self.assertFalse(eng._eof)

    def test_natural_eof_is_not_an_error(self) -> None:
        folder = _new_dir("a4-good")
        good = folder / "good.wav"
        ff = audioplay.NativeEngine.ffmpeg_exe()
        subprocess.run(
            [ff, "-v", "error", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=0.3", "-ac", "2", "-ar", "44100",
             str(good)],
            check=True, capture_output=True)
        eng = _reader_engine(good)
        eng._reader_loop(str(good), 0.0, 44100, 2, 1.0, 0)
        self.assertIsNone(eng._error)
        self.assertTrue(eng._eof)
        with eng._lock:
            eng._chunks.clear()
            eng._frames_avail = 0
        st = eng.state()
        self.assertTrue(st.get("ended"), st)
        self.assertFalse(st.get("playing"), st)
        self.assertIsNone(st.get("error"))

    def test_stopped_reader_is_not_an_error(self) -> None:
        folder = _new_dir("a4-stop")
        bad = folder / "stop.mp3"
        bad.write_bytes(b"not audio\n" * 8)
        eng = _reader_engine(bad)
        eng._stop_evt.set()
        eng._reader_loop(str(bad), 0.0, 44100, 2, 1.0, 0)
        self.assertIsNone(eng._error)

    def test_stale_generation_is_not_an_error(self) -> None:
        folder = _new_dir("a4-gen")
        bad = folder / "stale.mp3"
        bad.write_bytes(b"not audio\n" * 8)
        eng = _reader_engine(bad)
        eng._gen = 7
        eng._reader_loop(str(bad), 0.0, 44100, 2, 1.0, 6)
        self.assertIsNone(eng._error)

    def test_stop_clears_a_previous_error(self) -> None:
        folder = _new_dir("a4-clear")
        bad = folder / "clear.mp3"
        bad.write_bytes(b"not audio\n" * 8)
        eng = _reader_engine(bad)
        eng._reader_loop(str(bad), 0.0, 44100, 2, 1.0, 0)
        self.assertIsNotNone(eng._error)
        eng.stop()
        self.assertIsNone(eng._error)
        self.assertFalse(eng.state().get("playing"))


class TestA5SpawnFailure(unittest.TestCase):
    """A5: when the decoder cannot be started, playing must become false."""

    def test_missing_ffmpeg_reports_decoder_unavailable(self) -> None:
        folder = _new_dir("a5-spawn")
        track = folder / "song.flac"
        track.write_bytes(b"\x00" * 64)
        eng = _reader_engine(track)
        eng.ffmpeg_exe = lambda: r"Z:\definitely-missing\ffmpeg.exe"
        # Must not raise: in the app this runs on a daemon thread whose death
        # used to leave the engine reporting playing=true forever.
        eng._reader_loop(str(track), 0.0, 44100, 2, 1.0, 0)
        st = eng.state()
        self.assertFalse(st.get("playing"), st)
        self.assertFalse(st.get("ended"), st)
        err = st.get("error")
        self.assertIsInstance(err, dict, st)
        self.assertEqual(err.get("kind"), "decoder_unavailable")
        self.assertFalse(eng._eof)


class TestP0LimiterNaN(unittest.TestCase):
    """P0: one NaN sample must not poison the limiter gain for the rest of the track."""

    def test_nan_and_inf_are_sanitised_before_the_gain_loop(self) -> None:
        from sp.limiter import TruePeakLimiter

        lim = TruePeakLimiter(48000, 2)
        bad = np.zeros((512, 2), dtype=np.float32)
        bad[10, 0] = np.nan
        bad[20, 1] = np.inf
        bad[30, 0] = -np.inf
        bad[40, :] = 4.0
        out = lim.process(bad)
        self.assertTrue(np.all(np.isfinite(out)), "limiter emitted non-finite samples")
        self.assertTrue(np.isfinite(lim.g), "limiter gain became NaN/Inf")
        self.assertLessEqual(float(np.max(np.abs(out))), 1.0)

        t = np.arange(2048, dtype=np.float32) / 48000.0
        good = (0.25 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        good = np.repeat(good[:, None], 2, axis=1)
        out2 = lim.process(good)
        self.assertTrue(np.all(np.isfinite(out2)))
        self.assertTrue(np.isfinite(lim.g))
        self.assertGreater(float(np.max(np.abs(out2[600:]))), 0.1,
                           "later clean audio was silenced by the poisoned gain")

    def test_reader_rejects_non_finite_pcm_before_the_device(self) -> None:
        """The exclusive path bypasses the limiter, so reader-loop sanitising is required."""
        import inspect

        src = inspect.getsource(audioplay.NativeEngine._reader_loop)
        self.assertIn("nan_to_num", src,
                      "reader loop must sanitise float32 PCM before feeding the device")


class TestP1ApiToken(_ServerCase):
    """P1: the native shell's token must gate /api/*, while static pages stay readable."""

    TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef"

    @classmethod
    def setUpClass(cls) -> None:
        os.environ["SALTPLAYER_API_TOKEN"] = cls.TOKEN
        cls.addClassCleanup(lambda: os.environ.pop("SALTPLAYER_API_TOKEN", None))
        super().setUpClass()

    def test_api_without_token_is_forbidden(self) -> None:
        status, _, obj = _request(self.port, "GET", "/api/native/state")
        self.assertEqual(status, 403, obj)
        status, _, obj = _request(self.port, "POST", "/api/native", body={"action": "stop"})
        self.assertEqual(status, 403, obj)

    def test_wrong_token_is_forbidden(self) -> None:
        status, _, _ = _request(self.port, "GET", "/api/bootstrap",
                                headers={"X-Salt-Token": self.TOKEN[:-1] + "0"})
        self.assertEqual(status, 403)

    def test_correct_token_reaches_the_api(self) -> None:
        status, _, obj = _request(self.port, "GET", "/api/bootstrap",
                                  headers={"X-Salt-Token": self.TOKEN})
        self.assertEqual(status, 200, obj)
        self.assertTrue(obj and obj.get("ok"))

    def test_cover_may_use_query_token_but_other_get_may_not(self) -> None:
        # <img>/<audio> in the WinUI shell cannot attach headers; the shell appends
        # the token to exactly these read-only URLs. Everything else stays header-only.
        status, hs, _ = _request(self.port, "GET", "/api/cover?album=A&artist=B&size=thumb"
                                 "&token=" + self.TOKEN)
        self.assertEqual(status, 200)
        self.assertTrue((hs.get("content-type") or "").startswith("image/"))
        self.assertEqual(_request(self.port, "GET", "/api/bootstrap?token=" + self.TOKEN)[0], 403)

    def test_static_page_needs_no_token(self) -> None:
        status, _, _ = _request(self.port, "GET", "/")
        self.assertEqual(status, 200)


class TestP1WasapiDeviceIdentity(unittest.TestCase):
    """Simultaneous identical USB DACs must not be mapped onto each other by name."""

    def test_duplicate_friendly_names_are_left_unmapped(self) -> None:
        old = audioplay._wasapi_endpoints
        audioplay._wasapi_endpoints = lambda: [
            {"name": "USB DAC", "id": "first"},
            {"name": "USB DAC", "id": "second"},
            {"name": "Speakers", "id": "spk"},
        ]
        try:
            rows = [{"index": 1, "name": "USB DAC"},
                    {"index": 2, "name": "USB DAC"},
                    {"index": 3, "name": "Speakers"}]
            audioplay.NativeEngine._attach_wasapi_ids(rows)
        finally:
            audioplay._wasapi_endpoints = old
        self.assertIsNone(rows[0]["wasapi_id"])
        self.assertIsNone(rows[1]["wasapi_id"])
        self.assertEqual(rows[2]["wasapi_id"], "spk")


class TestP1WasapiAlignment(unittest.TestCase):
    """P1: AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED must trigger the documented retry."""

    def test_open_retries_with_aligned_period_and_closes_failed_client(self) -> None:
        from sp import wasapi

        state = {"init_calls": 0, "clients": 0, "closed": 0, "init_packets": []}

        class FakeClient:
            def __init__(self, device_id: str):
                state["clients"] += 1
                self.p = 0x1000 + state["clients"]
                self.device_id = device_id

            def close(self) -> None:
                state["closed"] += 1
                self.p = 0

        class FakeKernel:
            @staticmethod
            def CreateEventExW(*_a):
                return 0xBEEF

            @staticmethod
            def CloseHandle(*_a):
                return True

        def fake_call(this, name, restype, argtypes, args):
            if name == "ac.initialize":
                packet = int(args[2])
                state["init_packets"].append(packet)
                state["init_calls"] += 1
                if state["init_calls"] == 1:
                    return wasapi.AUDCLNT_E_BUFFER_SIZE_NOT_ALIGNED
                return wasapi.S_OK
            if name == "ac.get_buffer_size":
                args[0]._obj.value = 448
                return wasapi.S_OK
            if name == "ac.set_event_handle":
                return wasapi.S_OK
            if name == "ac.get_service":
                args[1]._obj.value = 0xCAFE
                return wasapi.S_OK
            if name == "unk.release":
                return 0
            raise AssertionError("unexpected COM call: " + name)

        old_call, old_client, old_libs, old_init = (
            wasapi._call, wasapi.AudioClient, wasapi._libs, wasapi.com_init)
        wasapi._call = fake_call
        wasapi.AudioClient = FakeClient
        wasapi._libs = lambda: (None, FakeKernel)
        wasapi.com_init = lambda mta=True: "already"
        try:
            st = wasapi.ExclusiveStream(
                "fake-device", wasapi.make_wf(44100, 2, 32), buffer_frames=448,
                period_hns=100000)
            rec = st.open()
        finally:
            wasapi._call, wasapi.AudioClient, wasapi._libs, wasapi.com_init = (
                old_call, old_client, old_libs, old_init)

        self.assertEqual(state["init_calls"], 2)
        self.assertEqual(state["clients"], 2)
        self.assertGreaterEqual(state["closed"], 1)
        aligned = int(round(10_000_000.0 * 448 / 44100))
        self.assertEqual(state["init_packets"][-1], aligned)
        self.assertEqual(st.period_hns, aligned)
        self.assertEqual(rec["period_frames"], 448)
        self.assertEqual(rec["period_frames_calc"], 448)


if __name__ == "__main__":
    unittest.main(verbosity=2)
