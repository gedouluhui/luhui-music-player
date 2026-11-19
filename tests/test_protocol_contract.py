"""Read-only bootstrap contracts on a disposable SQLite database; no server/audio."""
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="music-protocol-")))
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "SALTPLAYER_DATA": str(self.directory / "data"), "SALTPLAYER_ENGINE_TRACE": "",
        }))
        from sp import api, db, scanner
        self.api, self.db = api, db
        for name, value in (("_CONN", None), ("_LOCK", threading.RLock()), ("_COUNT_CACHE", {}), ("_WRITE_GEN", 0)):
            self.stack.enter_context(mock.patch.object(db, name, value))
        self.stack.enter_context(mock.patch.object(api, "_AGG_CACHE", {}))
        self.stack.enter_context(mock.patch.object(scanner, "STATE", copy.deepcopy(scanner.STATE)))
        self.stack.enter_context(mock.patch.object(api.audioplay, "get", side_effect=AssertionError("bootstrap must not open engine")))
        self.connection = db.conn()
        self.stack.callback(self.connection.close)
        self.handler = object.__new__(api.Handler)

    def test_bootstrap_declares_independent_protocol(self):
        result = self.handler._bootstrap()
        self.assertEqual(result.get("protocol"), {
            "major": 1, "minor": 0, "capabilities": ["native-error-object-v1"],
        })
        self.assertIn("version", result["app"])
        self.assertEqual(json.loads(json.dumps(result))["protocol"], result["protocol"])

    def test_callers_cannot_mutate_future_protocol_responses(self):
        first = self.handler._bootstrap()
        self.assertIn("protocol", first)
        first["protocol"]["major"] = 99
        first["protocol"]["capabilities"].clear()
        self.assertEqual(self.handler._bootstrap()["protocol"], {
            "major": 1, "minor": 0, "capabilities": ["native-error-object-v1"],
        })


if __name__ == "__main__":
    unittest.main(verbosity=2)
