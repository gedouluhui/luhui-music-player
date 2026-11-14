"""Regression tests for WORLD_CLASS_AUDIT_MASTERPLAN Part 4 (music player).

Batch 2: scanner completeness + SQLite batching.

Safety rules for this module:
  * every test uses a throw-away SALTPLAYER_DATA root (set before ``sp`` import);
  * no audio device is opened and no real library/settings are touched;
  * fake track rows are plain DB rows -- no file is ever played.
"""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="salt-masterplan-fixes-"))
os.environ["SALTPLAYER_DATA"] = str(_TMP_ROOT / "data")
os.environ.pop("SALTPLAYER_ENGINE_TRACE", None)

from sp import api, db, metadata, scanner  # noqa: E402


def _wipe() -> None:
    """Each test starts from an empty DB; scanner/purge assertions are global."""
    db.script(
        "DELETE FROM playlist_items; DELETE FROM favorites; DELETE FROM plays;"
        "DELETE FROM tracks; DELETE FROM libraries; DELETE FROM playlists;"
    )


def _mk_lib(name: str) -> tuple[int, str]:
    root = _TMP_ROOT / name
    root.mkdir(parents=True, exist_ok=True)
    lid = db.add_library(str(root), name)
    return int(lid), str(root)


def _seed_tracks(lib_id: int, root: str, count: int, prefix: str = "t",
                 missing: int = 0) -> list[int]:
    stmts = []
    now = time.time()
    for i in range(count):
        path = os.path.join(root, f"{prefix}{i:05d}.mp3")
        stmts.extend(db.upsert_statements({
            "path": path, "title": f"{prefix}{i}", "artist": "a", "album": "b",
            "duration": 1.0, "size": 1, "mtime": 1.0, "fmt": "mp3",
            "filename": os.path.basename(path), "library_id": lib_id,
        }, now))
    db.batch(stmts)
    if missing:
        db.write("UPDATE tracks SET missing = 1 WHERE library_id = ?", (lib_id,))
    return [int(r["id"]) for r in db.query("SELECT id FROM tracks WHERE library_id = ?", (lib_id,))]


class TestScannerCompleteness(unittest.TestCase):
    def test_walk_audio_reports_incomplete_on_truncation(self):
        _wipe()
        root = _TMP_ROOT / "walk"
        root.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (root / f"{i}.mp3").write_bytes(b"x")

        files, complete = scanner.walk_audio(str(root), max_files=2)
        self.assertEqual(len(files), 2, "上限截断时仍要交出找到的那部分")
        self.assertFalse(complete, "截断必须报告不完整，否则调用方会把没走到的曲目标成失效")

        files, complete = scanner.walk_audio(str(root), max_files=100)
        self.assertEqual(len(files), 3)
        self.assertTrue(complete)

    def test_incomplete_walk_does_not_mark_anything_missing(self):
        _wipe()
        lid, root = _mk_lib("inc")
        _seed_tracks(lid, root, 2)

        with mock.patch.object(scanner, "walk_audio", return_value=([], False)):
            st = scanner.scan_library(lid)

        self.assertTrue(st["ok"], st)
        missing = db.query_one(
            "SELECT COUNT(*) AS c FROM tracks WHERE library_id = ? AND missing = 1", (lid,)
        )["c"]
        self.assertEqual(missing, 0, "遍历不完整时绝不能用残缺清单做失效判定")

        # 同一份空清单，声明为完整（并 force）以后才允许落 missing。
        with mock.patch.object(scanner, "walk_audio", return_value=([], True)):
            scanner.scan_library(lid, force=True)
        missing = db.query_one(
            "SELECT COUNT(*) AS c FROM tracks WHERE library_id = ? AND missing = 1", (lid,)
        )["c"]
        self.assertEqual(missing, 2)

    def test_zero_file_scan_keeps_a_large_library(self):
        _wipe()
        lid, root = _mk_lib("zero")
        _seed_tracks(lid, root, 10)

        with mock.patch.object(scanner, "walk_audio", return_value=([], True)):
            st = scanner.scan_library(lid)

        self.assertTrue(st["ok"], st)
        missing = db.query_one(
            "SELECT COUNT(*) AS c FROM tracks WHERE library_id = ? AND missing = 1", (lid,)
        )["c"]
        self.assertEqual(missing, 0,
                         "盘没挂载/路径临时不可见时扫到 0 个文件，不能把整库打成失效")

        # force 是显式覆盖：用户确实清空了文件夹时仍可落 missing。
        with mock.patch.object(scanner, "walk_audio", return_value=([], True)):
            scanner.scan_library(lid, force=True)
        missing = db.query_one(
            "SELECT COUNT(*) AS c FROM tracks WHERE library_id = ? AND missing = 1", (lid,)
        )["c"]
        self.assertEqual(missing, 10)


class TestSqliteBatching(unittest.TestCase):
    def test_mark_missing_not_in_chunks_sql_parameters(self):
        _wipe()
        lid, root = _mk_lib("chunk")
        _seed_tracks(lid, root, 1201)

        with mock.patch.object(db, "batch", wraps=db.batch) as spy:
            gone = db.mark_missing_not_in([], lid)

        self.assertEqual(len(gone), 1201)
        stmts = [s for call in spy.call_args_list for s in call.args[0]]
        self.assertGreaterEqual(len(stmts), 3, "1201 个 id 至少拆成 3 条 UPDATE")
        for _sql, args in stmts:
            self.assertLessEqual(len(args), 500, "单条 SQL 的参数个数要有上限")

    def test_remove_library_does_not_delete_refs_one_track_at_a_time(self):
        _wipe()
        lid, root = _mk_lib("rm")
        ids = _seed_tracks(lid, root, 300)
        db.many("INSERT INTO favorites(track_id, added_at) VALUES(?,?)",
                [(tid, time.time()) for tid in ids])

        with mock.patch.object(db, "script", wraps=db.script) as spy:
            db.remove_library(lid)

        self.assertEqual(spy.call_count, 0, "引用清理必须批量，不能每首一条 executescript")
        self.assertEqual(db.query_one("SELECT COUNT(*) AS c FROM favorites")["c"], 0)
        self.assertEqual(
            db.query_one("SELECT COUNT(*) AS c FROM tracks WHERE library_id = ?", (lid,))["c"], 0
        )

    def test_purge_missing_does_not_delete_refs_one_track_at_a_time(self):
        _wipe()
        lid, root = _mk_lib("purge")
        ids = _seed_tracks(lid, root, 300, missing=1)
        db.many("INSERT INTO plays(track_id, played_at, ms) VALUES(?,?,?)",
                [(tid, time.time(), 1000) for tid in ids])

        with mock.patch.object(db, "script", wraps=db.script) as spy:
            n = db.purge_missing()

        self.assertEqual(n, 300)
        self.assertEqual(spy.call_count, 0, "引用清理必须批量，不能每首一条 executescript")
        self.assertEqual(db.query_one("SELECT COUNT(*) AS c FROM plays")["c"], 0)


def _http(port: int, method: str, path: str, body=None):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    status = resp.status
    conn.close()
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        obj = None
    return status, obj


class TestApiHardening(unittest.TestCase):
    """P1: oversized bodies are refused before read; 500s never leak internals."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.srv = api.Server()
        cls.srv.start()
        cls.port = cls.srv.port

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.stop()

    def test_oversized_body_is_rejected_before_read(self):
        limit = getattr(api, "MAX_BODY_BYTES", 4 * 1024 * 1024)
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            # Declare a body far above the limit and send none of it: if the server
            # tried to read first, this would time out instead of answering.
            s.sendall((
                f"POST /api/playlists HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {limit + 1}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("ascii"))
            data = s.recv(4096)
        finally:
            s.close()
        head = data.split(b"\r\n", 1)[0]
        self.assertIn(b" 413 ", head, head)

    def test_post_500_does_not_echo_the_exception(self):
        with mock.patch.object(api.Handler, "_post_api",
                               side_effect=RuntimeError("secret-marker-C:/Users/example")):
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                status, obj = _http(self.port, "POST", "/api/playlists", {})
        self.assertEqual(status, 500)
        dumped = json.dumps(obj, ensure_ascii=False)
        self.assertNotIn("secret-marker", dumped)
        self.assertNotIn("RuntimeError", dumped)

    def test_get_500_does_not_echo_the_exception(self):
        with mock.patch.object(api.Handler, "_get_api",
                               side_effect=RuntimeError("secret-marker-D:/private")):
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                status, obj = _http(self.port, "GET", "/api/bootstrap")
        self.assertEqual(status, 500)
        dumped = json.dumps(obj, ensure_ascii=False)
        self.assertNotIn("secret-marker", dumped)
        self.assertNotIn("RuntimeError", dumped)


class TestCoverCacheGovernance(unittest.TestCase):
    def setUp(self) -> None:
        _wipe()
        d = metadata.cover_cache_dir()
        for p in d.iterdir():
            try:
                p.unlink()
            except OSError:
                pass

    def test_pillow_pixel_bomb_limit_is_lowered(self):
        from PIL import Image
        self.assertEqual(Image.MAX_IMAGE_PIXELS, 25_000_000,
                         "默认 89MP 解码一张大图就能吃掉数百 MB")

    def test_trim_evicts_oldest_files_until_under_quota(self):
        d = metadata.cover_cache_dir()
        paths = []
        for i in range(5):
            p = d / f"v96_{i}.img"
            p.write_bytes(b"x" * 100)
            os.utime(p, (1000 + i, 1000 + i))
            paths.append(p)

        info = metadata.cover_cache_trim(max_bytes=250)

        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[1].exists())
        self.assertFalse(paths[2].exists())
        self.assertTrue(paths[3].exists())
        self.assertTrue(paths[4].exists())
        self.assertEqual(info["removed"], 3)
        self.assertEqual(info["bytes"], 200)
        self.assertEqual(info["freed"], 300)

    def test_trim_keeps_everything_under_quota(self):
        d = metadata.cover_cache_dir()
        p = d / "v96_keep.img"
        p.write_bytes(b"y" * 50)
        info = metadata.cover_cache_trim(max_bytes=1000)
        self.assertTrue(p.exists())
        self.assertEqual(info["removed"], 0)

    def test_scan_finishes_with_a_cover_cache_trim(self):
        lid, root = _mk_lib("trim")
        with mock.patch.object(scanner, "walk_audio", return_value=([], False)), \
             mock.patch.object(scanner, "cover_cache_trim") as trim:
            st = scanner.scan_library(lid)
        self.assertTrue(st["ok"], st)
        trim.assert_called_once()


class TestPortHandshake(unittest.TestCase):
    """The native shell passes --port 0 and reads the real port from stdout."""

    def test_server_reports_an_ephemeral_port_on_stdout(self):
        import subprocess
        import threading

        env = dict(os.environ)
        env.pop("SALTPLAYER_API_TOKEN", None)
        proc = subprocess.Popen(
            [sys.executable, "salt_player.py", "--server", "--port", "0"],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=env,
        )
        lines: list[str] = []

        def _read() -> None:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                lines.append(line)
                if line.startswith("SALTPLAYER_PORT="):
                    return

        try:
            t = threading.Thread(target=_read, daemon=True)
            t.start()
            t.join(60)
            marker = next((ln.strip() for ln in lines if ln.startswith("SALTPLAYER_PORT=")), None)
            self.assertIsNotNone(marker, f"后端没有报告端口；已读到 {lines!r}")
            port = int(marker.split("=", 1)[1])
            self.assertGreater(port, 0)
            status, obj = _http(port, "GET", "/api/health")
            self.assertEqual(status, 200)
            self.assertTrue(obj and obj.get("ok"), obj)
        finally:
            proc.kill()
            proc.wait(timeout=10)
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)