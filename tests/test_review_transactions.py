"""Transaction/scan-lock regressions using only disposable SQLite databases."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _isolated_env(root):
    return {
        "SALTPLAYER_DATA": str(root / "data"),
        "LOCALAPPDATA": str(root),
        "XDG_DATA_HOME": str(root),
        "SALTPLAYER_ENGINE_TRACE": "",
    }


# config/db/metadata were inspected: paths are lazy; no services are imported.
# Override, never setdefault: an inherited user-library path must not be used.
with tempfile.TemporaryDirectory(prefix="salt-foundation-import-") as _import_dir:
    with mock.patch.dict(os.environ, _isolated_env(Path(_import_dir))):
        from sp import config, db, scanner


TABLES = ("tracks", "favorites", "playlist_items", "plays")


def snapshot(connection):
    return {
        table: [tuple(row) for row in connection.execute(
            f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()]
        for table in TABLES
    }


class _DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(
            tempfile.TemporaryDirectory(prefix="salt-foundation-test-")))
        self.stack.enter_context(mock.patch.dict(os.environ, _isolated_env(self.root)))
        # Do not inspect, close or reuse a connection belonging to another suite.
        for name, value in (("_CONN", None), ("_LOCK", threading.RLock()),
                            ("_COUNT_CACHE", {}), ("_WRITE_GEN", 0)):
            self.stack.enter_context(mock.patch.object(db, name, value))
        self.stack.enter_context(mock.patch.object(scanner, "STATE", copy.deepcopy(scanner.STATE)))
        self.connection = db.conn()  # real production connection + schema/migrations
        self.stack.callback(self.connection.close)
        self.observer = sqlite3.connect(str(config.db_path()), timeout=0)
        self.stack.callback(self.observer.close)
        self.assertEqual(Path(self.connection.execute("PRAGMA database_list").fetchone()[2]),
                         self.root / "data" / "library.sqlite3")

    def seed(self, missing_count, alive_count=0):
        folder = self.root / "music"
        lid = db.add_library(str(folder), "fixture")
        self.lib = {"id": lid, "path": str(folder), "name": "fixture"}
        pid = db.create_playlist("fixture")
        stmts = []
        for i in range(missing_count + alive_count):
            stmts.extend(db.upsert_statements({
                "path": str(folder / f"song-{i:05d}.mp3"),
                "title": f"Song {i}", "library_id": lid,
            }, now=1.0))
        db.batch(stmts)
        rows = db.query("SELECT id, path FROM tracks ORDER BY id")
        self.ids = [int(r["id"]) for r in rows]
        self.paths = [r["path"] for r in rows]
        stmts = []
        for position, tid in enumerate(self.ids):
            stmts.extend([
                ("UPDATE tracks SET missing = ? WHERE id = ?",
                 (int(position < missing_count), tid)),
                ("INSERT INTO favorites(track_id, added_at) VALUES(?,?)", (tid, 2.0)),
                ("INSERT INTO playlist_items(playlist_id, track_id, position, added_at)"
                 " VALUES(?,?,?,?)", (pid, tid, position, 2.0)),
                ("INSERT INTO plays(track_id, played_at, ms) VALUES(?,?,?)", (tid, 2.0, 100)),
            ])
        db.batch(stmts)

    def trace(self, callback):
        self.connection.set_trace_callback(callback)
        self.stack.callback(self.connection.set_trace_callback, None)

    def reconcile(self):
        return scanner._reconcile(self.lib, self.paths, {"removed": 0})


class TestPurgeTransactions(_DatabaseCase):
    def test_empty_purge_returns_zero_and_leaves_no_transaction(self):
        self.assertEqual(db.purge_missing(), 0)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(snapshot(self.connection), snapshot(self.observer))

    def test_chunks_preserve_live_tracks_and_invalidate_cached_stats(self):
        count = db._SQL_CHUNK * 2 + 1
        self.seed(count, alive_count=1)
        before = snapshot(self.connection)
        self.assertEqual(db.stats()["favorites"], count + 1)
        generation = db.write_gen()

        self.assertEqual(db.purge_missing(), count)

        after = snapshot(self.connection)
        for table in TABLES:
            self.assertEqual(after[table], before[table][-1:], table)
        self.assertEqual(after, snapshot(self.observer))
        self.assertEqual(db.stats()["favorites"], 1)
        self.assertGreater(db.write_gen(), generation)
        self.assertFalse(self.connection.in_transaction)

    def test_scanner_restored_tracks_and_all_refs_are_preserved(self):
        self.seed(1)
        self.reconcile()  # production revival, no filesystem walk or audio
        before = snapshot(self.connection)
        self.assertEqual(db.query_one("SELECT missing FROM tracks")["missing"], 0)
        self.assertEqual(db.purge_missing(), 0)
        self.assertEqual(snapshot(self.connection), before)

    def test_scanner_handoff_after_id_selection_never_loses_live_refs(self):
        self.seed(1)
        before = snapshot(self.connection)
        selected = threading.Event()
        reconciled = threading.Event()
        errors, scanned_views = [], []
        owner = threading.get_ident()

        class HandoffLock:
            """Pause purge at the first *outer* lock release after its SELECT."""
            def __init__(self):
                self.lock = threading.RLock()
                self.local = threading.local()
                self.armed = False

            def __enter__(self):
                self.lock.acquire()
                self.local.depth = getattr(self.local, "depth", 0) + 1
                return self

            def __exit__(self, *exc):
                self.local.depth -= 1
                handoff = (threading.get_ident() == owner and self.armed
                           and self.local.depth == 0)
                if handoff:
                    self.armed = False
                self.lock.release()
                if handoff:
                    selected.set()
                    if not reconciled.wait(5):
                        raise AssertionError("scanner did not finish after purge released its lock")

        gate = HandoffLock()

        def trace(sql):
            if threading.get_ident() == owner and sql.startswith("SELECT id FROM tracks WHERE missing = 1"):
                gate.armed = True

        def rescan():
            try:
                if not selected.wait(5):
                    raise AssertionError("purge never selected missing IDs")
                self.reconcile()
                scanned_views.append(snapshot(self.connection))
            except BaseException as exc:
                errors.append(exc)
            finally:
                reconciled.set()

        self.trace(trace)
        worker = threading.Thread(target=rescan, daemon=True)
        with mock.patch.object(db, "_LOCK", gate):
            worker.start()
            try:
                removed = db.purge_missing()
            finally:
                selected.set()
                worker.join(6)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
        after = snapshot(self.connection)
        self.assertEqual(len(scanned_views), 1)
        if scanned_views[0]["tracks"]:
            self.assertEqual(after["tracks"], scanned_views[0]["tracks"],
                             "purge deleted a track that the scanner had already restored")
            self.assertEqual(db.query_one("SELECT missing FROM tracks")["missing"], 0)
            for table in TABLES[1:]:
                self.assertEqual(after[table], before[table], f"live track lost {table}")
            self.assertEqual(removed, 0)
        else:
            self.assertEqual(after, {table: [] for table in TABLES})
            self.assertEqual(removed, 1)

    def test_independent_reader_cannot_see_refs_deleted_before_tracks(self):
        self.seed(db._SQL_CHUNK + 1)
        before = snapshot(self.observer)
        observed = []
        self.trace(lambda sql: observed.append(snapshot(self.observer))
                   if sql.startswith("DELETE FROM tracks") else None)

        self.assertEqual(db.purge_missing(), db._SQL_CHUNK + 1)

        self.assertTrue(observed, "track DELETE was not observed")
        for view in observed:
            self.assertEqual(view, before, "another connection saw a partially committed purge")
        self.assertEqual(snapshot(self.observer), {table: [] for table in TABLES})

    def test_writer_reservation_is_held_before_selecting_ids(self):
        self.seed(1)
        reserved = []

        def trace(sql):
            if sql.startswith("SELECT id FROM tracks WHERE missing = 1"):
                try:
                    self.observer.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    reserved.append("locked" in str(exc))
                else:
                    reserved.append(False)
                finally:
                    self.observer.rollback()

        self.trace(trace)
        self.assertEqual(db.purge_missing(), 1)
        self.assertEqual(reserved, [True], "missing IDs were read without reserving the writer")

    def test_track_delete_failure_rolls_back_refs_and_earlier_chunks(self):
        self.seed(db._SQL_CHUNK + 1, alive_count=1)
        # Fail only after an earlier chunk can have been deleted. Real SQLite
        # raises DatabaseError; the refs and any earlier tracks must roll back.
        failing_id = self.ids[db._SQL_CHUNK]
        self.connection.execute(
            f"CREATE TRIGGER fail_purge BEFORE DELETE ON tracks WHEN OLD.id = {failing_id} "
            "BEGIN SELECT RAISE(ABORT, 'injected track delete failure'); END")
        self.connection.commit()
        before = snapshot(self.connection)
        cached = db.stats()
        generation = db.write_gen()

        with self.assertRaisesRegex(sqlite3.DatabaseError, "injected track delete failure"):
            db.purge_missing()

        self.assertEqual(snapshot(self.connection), before)
        self.assertEqual(snapshot(self.observer), before)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(db.stats(), cached)
        self.assertEqual(db.write_gen(), generation)
        self.connection.execute("DROP TRIGGER fail_purge")
        self.connection.commit()
        self.assertEqual(db.purge_missing(), db._SQL_CHUNK + 1)

    def test_returns_direct_delete_rowcount_not_selected_id_count(self):
        self.seed(2)
        # SQLite's direct rowcount excludes a row deleted by a trigger. Both
        # tracks disappear, but the DELETE itself affects only one row.
        first, second = self.ids
        self.connection.execute(
            f"CREATE TRIGGER paired_delete BEFORE DELETE ON tracks WHEN OLD.id = {first} "
            f"BEGIN DELETE FROM tracks WHERE id = {second}; END")
        self.connection.commit()

        self.assertEqual(db.purge_missing(), 1)
        self.assertEqual(snapshot(self.connection), {table: [] for table in TABLES})


class TestScanAsyncLocks(_DatabaseCase):
    def setUp(self):
        super().setUp()
        for name, value in (("_STATE_LOCK", threading.Lock()),
                            ("_SCAN_LOCK", threading.Lock()), ("_scan_thread", None)):
            self.stack.enter_context(mock.patch.object(scanner, name, value))
        scanner._set(running=False, phase="idle", errors=[])
        folder = self.root / "empty-library"
        folder.mkdir()
        self.library_id = db.add_library(str(folder), "empty fixture")

    def test_running_scan_returns_busy_in_bounded_child(self):
        try:
            child = subprocess.run(
                [sys.executable, "-B", str(Path(__file__).resolve()), "--busy-child"],
                cwd=str(ROOT), env=dict(os.environ), capture_output=True, text=True,
                encoding="utf-8", timeout=5,
            )
        except subprocess.TimeoutExpired:
            # subprocess.run kills and waits for this child only, never other
            # Python processes (including the unittest parent).
            self.fail("scan_async busy branch self-deadlocked: test child exceeded 5 seconds")
        self.assertEqual(child.returncode, 0, child.stderr)
        result = json.loads(child.stdout)
        self.assertFalse(result["ok"])
        self.assertTrue(result["busy"])
        self.assertTrue(result["running"])
        self.assertEqual(result["phase"], "walking")
        self.assertEqual(result["errors"], [str(i) for i in range(5, 25)])

    def test_idle_concurrent_requests_start_only_one_scan(self):
        barrier = threading.Barrier(3)
        entered = threading.Event()
        release = threading.Event()
        calls, results, errors = [], [], []
        scan_library = scanner.scan_library

        def gated_scan(*args):
            calls.append(args)
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release the scan worker")
            return scan_library(*args)  # real scan of an empty, temporary directory

        def request():
            try:
                barrier.wait(5)
                results.append(scanner.scan_async(self.library_id, True, True))
            except BaseException as exc:
                errors.append(exc)

        callers = [threading.Thread(target=request, daemon=True) for _ in range(2)]
        with mock.patch.object(scanner, "scan_library", side_effect=gated_scan):
            for caller in callers:
                caller.start()
            try:
                barrier.wait(5)
                self.assertTrue(entered.wait(5))
                for caller in callers:
                    caller.join(5)
                    self.assertFalse(caller.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(sum(bool(r.get("started")) for r in results), 1)
                self.assertEqual(sum(bool(r.get("busy")) for r in results), 1)
                self.assertEqual(calls, [(self.library_id, True, True)])
            finally:
                release.set()
                for caller in callers:
                    caller.join(5)
                worker = scanner._scan_thread
                if worker is not None:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
        self.assertFalse(scanner.state_snapshot()["running"])
        self.assertEqual(scanner.state_snapshot()["phase"], "done")
        self.assertIsNotNone(db.get_meta("last_scan_at"))
        self.assert_scan_lock_available()

    def assert_scan_lock_available(self):
        acquired = scanner._SCAN_LOCK.acquire(blocking=False)
        if acquired:
            scanner._SCAN_LOCK.release()
        self.assertTrue(acquired, "scan admission lock was leaked")

    def assert_retry_starts_real_scan(self):
        result = scanner.scan_async(self.library_id)
        self.assertEqual(result, {"ok": True, "started": True})
        worker = scanner._scan_thread
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(scanner.state_snapshot()["phase"], "done")
        self.assert_scan_lock_available()

    def test_thread_start_failure_releases_lock_for_retry(self):
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("start failed")):
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                scanner.scan_async(self.library_id)
        self.assert_scan_lock_available()
        self.assertFalse(scanner.state_snapshot()["running"])
        self.assert_retry_starts_real_scan()

    def test_thread_constructor_failure_releases_lock_for_retry(self):
        with mock.patch.object(threading, "Thread", side_effect=RuntimeError("constructor failed")):
            with self.assertRaisesRegex(RuntimeError, "constructor failed"):
                scanner.scan_async(self.library_id)
        self.assert_scan_lock_available()
        self.assert_retry_starts_real_scan()


def _busy_child():
    previous = copy.deepcopy(scanner.STATE)
    try:
        scanner._set(running=True, phase="walking", errors=[str(i) for i in range(25)])
        result = scanner.scan_async()
        # Verify the response is an independent snapshot and the lock is free.
        result["errors"].append("response-only")
        assert scanner.state_snapshot()["errors"] == [str(i) for i in range(5, 25)]
        result["errors"].pop()
        print(json.dumps(result))
    finally:
        with scanner._STATE_LOCK:
            scanner.STATE.clear()
            scanner.STATE.update(previous)


if __name__ == "__main__":
    if sys.argv[1:] == ["--busy-child"]:
        _busy_child()
    else:
        unittest.main(verbosity=2)
