import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_core import command, sample


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="production cli ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / "private store"
        self.argv = [sys.executable, "-m", "control_production_plane.cli", "--store", str(self.store)]

    def call(self, *args):
        return subprocess.run(self.argv + list(args), capture_output=True, text=True, timeout=10)

    def request_file(self, request):
        path = self.root / "request.json"
        path.write_text(json.dumps(request))
        return str(path)

    def test_independent_process_create_read_replay_cancel_recover(self):
        self.assertEqual(self.call("init").returncode, 0)
        request = self.request_file(command("create", sample(), "create-one"))
        result = self.call("write", "--request", request)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["data"]["replayed"])
        duplicate = self.call("write", "--request", request)
        self.assertTrue(json.loads(duplicate.stdout)["data"]["replayed"])
        first = json.loads(self.call("show", "--run-id", "example").stdout)["data"]
        request = self.request_file(command("cancel", {"run_id": "example", "reason": "No execution requested"}))
        self.assertEqual(self.call("write", "--request", request).returncode, 0)
        recovered = json.loads(self.call("recover", "--run-id", "example").stdout)["data"]
        self.assertEqual(recovered["next_step"], "NO_NEW_SUBMISSION")
        self.assertEqual(first["attempt"], recovered["run"]["attempt"])
        self.assertEqual(recovered["external_calls_made"], 0)
        events = json.loads(self.call("events", "--run-id", "example").stdout)["data"]
        self.assertEqual([event["operation"] for event in events], ["create", "cancel"])

    def test_missing_store_and_invalid_json_emit_structured_errors(self):
        result = self.call("show", "--run-id", "example")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "STORE_INVALID")
        self.assertFalse(self.store.exists())
        self.assertEqual(self.call("init").returncode, 0)
        path = self.root / "duplicate.json"
        path.write_bytes(b'{"operation":"create","operation":"cancel"}')
        result = self.call("write", "--request", str(path))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "INVALID_INPUT")

    def test_receipt_output_failure_does_not_undo_commit_or_duplicate_run(self):
        self.assertEqual(self.call("init").returncode, 0)
        request = self.request_file(command("create", sample(), "lost-response"))
        with open("/dev/full", "wb") as failing:
            result = subprocess.run(self.argv + ["write", "--request", request], stdout=failing,
                                    stderr=subprocess.PIPE, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error"]["commit_state"], "committed")
        retry = self.call("write", "--request", request)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertTrue(json.loads(retry.stdout)["data"]["replayed"])
        self.assertEqual(len(json.loads(self.call("events", "--run-id", "example").stdout)["data"]), 1)

    def test_cli_short_writes_return_complete_json(self):
        code = '''import os,sys
from control_production_plane.cli import main
class Short:
 def write(self,data):return os.write(1,data[:7])
 def flush(self):pass
class Output:
 buffer=Short()
 def flush(self):pass
sys.stdout=Output()
raise SystemExit(main(sys.argv[1:]))
'''
        result = subprocess.run([sys.executable, "-c", code, "--store", str(self.store), "init"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "OK")
        self.assertTrue(result.stdout.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
