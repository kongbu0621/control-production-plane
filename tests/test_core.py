"""Public API, real SQLite transactions and separate-process recovery checks.

Adapter responses below are synthetic. They are not Local Hand field evidence.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from control_production_plane import CallNotStarted, ControlError, decode, initialize, open_store


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def sample(run_id="example"):
    return {"run_id": run_id, "goal": {"text": "Fix the synthetic greeting", "acceptance": ["Returns hello"]},
            "binding": {"executor_id": "demo:executor", "authority_id": "demo:authority", "principal_id": "demo:caller",
                        "profile_ref": "demo:profile", "tool_schema_digest": "1" * 64,
                        "archive_id": "demo:archive", "producer": "demo:producer"},
            "plan": {"source_ref": "demo:source", "source_commit": "2" * 40, "relative_path": "greeting.py",
                     "expected_sha256": "3" * 64, "content": "print('hello')\n", "validation_ref": "demo:validation",
                     "knowledge": {"required": False, "status": "UNCONFIGURED", "reason": "No knowledge adapter configured", "items": []}},
            "budget": {"max_attempts": 3, "max_actions": 20, "max_queries": 20, "max_cancellations": 20,
                       "deadline_utc": int(time.time()) + 3600, "max_response_bytes": 32768}}


def command(operation, body, key=None):
    return {"schema": "production-plane-v1", "request_id": key or str(uuid.uuid4()), "operation": operation, "body": body}


def ready(payload):
    request = dict(payload, expires_at=int(time.time()) + 600, expected={}, schema_version="synthetic-example-v1")
    request["request_digest"] = hashlib.sha256(canonical(request)).hexdigest()
    return {"response": {"state": "READY", "request": request}, "call_closed": True}


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "control"
        self.store = initialize(self.path)
        self.spec = sample()
        self.store.execute(command("create", self.spec, "create-original"))

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(ControlError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def queue(self, kind, run_id="example"):
        state = self.store.get_run(run_id)
        action_id = str(uuid.uuid4())
        self.store.execute(command("queue", {"run_id": run_id, "attempt_id": state["attempt"]["attempt_id"],
                                             "action_id": action_id, "kind": kind}))
        return action_id

    def prepare(self):
        action = self.queue("prepare")
        self.store.perform(action, self.spec["binding"], ready)
        return action

    def submit(self):
        self.prepare()
        action = self.queue("submit")
        self.store.perform(action, self.spec["binding"], lambda payload: {"response": {"accepted": True}, "call_closed": True})
        return action

    def observation(self, outcome="FAILED", exited=True, remote_state="TERMINAL"):
        state = self.store.get_run("example")
        attempt = state["attempt"]
        return {"run_id": "example", "attempt_id": attempt["attempt_id"], "operation_id": attempt["operation_id"],
                "request_digest": attempt["prepared_request"]["request_digest"], "remote_state": remote_state,
                "outcome": outcome, "exited": exited, "evidence_state": "STAGING", "evidence_ref": "demo:status-observation"}

    def test_create_replay_conflict_and_mutable_input_detached(self):
        before = self.store.get_run("example")
        receipt = self.store.execute(command("create", self.spec, "create-original"))
        self.assertTrue(receipt["replayed"])
        self.assertEqual(len(self.store.events("example")), 1)
        self.spec["plan"]["content"] = "changed"
        self.error("CONFLICT", self.store.execute, command("create", self.spec, "create-original"))
        self.assertEqual(self.store.get_run("example"), before)

    def test_missing_store_is_not_initialized_and_collision_retains_files(self):
        self.error("ALREADY_EXISTS", initialize, self.path)
        missing = self.root / "missing"
        self.error("STORE_INVALID", open_store, missing)
        self.assertFalse(missing.exists())
        (self.path / "runs.sqlite").unlink()
        self.error("STORE_INVALID", open_store, self.path)
        self.assertFalse((self.path / "runs.sqlite").exists())

    def test_replaced_database_and_wrong_store_identity_rejected(self):
        other = initialize(self.root / "other")
        (self.path / "identity.json").write_bytes((other.root / "identity.json").read_bytes())
        self.error("STORE_INVALID", open_store, self.path)

    def test_strict_json_and_fields_do_not_write(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1.0}', b'{"x":"\\ud800"}', b'{"x":9007199254740992}'):
            self.error("INVALID_INPUT", decode, raw)
        for path in ("../target.py", "/tmp/target.py", "a/./b.py", "a//b.py", "a\\b.py"):
            spec = sample("bad")
            spec["plan"]["relative_path"] = path
            self.error("INVALID_INPUT", self.store.execute, command("create", spec))
        self.assertEqual(len(self.store.events("example")), 1)

    def test_unicode_content_limits_boolean_budgets_and_wrong_action_kind(self):
        spec = sample("unicode")
        spec["plan"]["content"] = "中" * 5462
        self.error("LIMIT", self.store.execute, command("create", spec))
        spec["plan"]["content"] = ""
        spec["budget"]["max_attempts"] = True
        self.error("INVALID_INPUT", self.store.execute, command("create", spec))
        self.error("INVALID_INPUT", self.store.execute, command("queue", {"run_id": "example",
            "attempt_id": self.store.get_run("example")["attempt"]["attempt_id"], "action_id": str(uuid.uuid4()), "kind": []}))

    def test_pending_intent_is_not_execution_and_abandon_costs_no_budget(self):
        action = self.queue("prepare")
        state = open_store(self.path).recover("example")
        self.assertEqual(state["next_step"], "CONTINUE_UNSENT_PLAN")
        self.assertEqual(state["queued_actions"], [action])
        self.assertEqual(state["run"]["counters"]["actions"], 0)
        self.store.execute(command("abandon", {"action_id": action, "reason": "Correct local input"}))
        self.assertEqual(self.store.get_action(action)["state"], "NOT_SENT")

    def test_prepare_polling_uses_same_operation_and_inputs(self):
        first = self.queue("prepare")
        self.store.perform(first, self.spec["binding"], lambda payload: {"response": {"state": "PREPARING"}, "call_closed": True})
        second = self.queue("prepare")
        self.assertEqual(self.store.get_action(first)["payload"], self.store.get_action(second)["payload"])
        self.store.perform(second, self.spec["binding"], ready)
        self.assertIsNotNone(self.store.get_run("example")["attempt"]["prepared_request"])

    def test_ready_payload_binding_is_validated(self):
        action = self.queue("prepare")
        def wrong(payload):
            payload["inputs"]["content"] = "different"
            return ready(payload)
        self.error("IDENTITY_CONFLICT", self.store.perform, action, self.spec["binding"], wrong)
        self.assertIsNone(self.store.get_run("example")["attempt"]["prepared_request"])
        self.assertEqual(self.store.get_action(action)["state"], "MAY_HAVE_RUN")

    def test_invalid_response_after_external_effect_reports_recorded_dispatch(self):
        self.prepare()
        action = self.queue("submit")
        effects = []
        def malformed(payload):
            effects.append(payload["operation_id"])
            return {"response": [], "call_closed": True}
        error = self.error("INVALID_INPUT", self.store.perform, action, self.spec["binding"], malformed)
        self.assertEqual(error.commit_state, "not_committed")
        self.assertTrue(error.as_dict()["dispatch_recorded"])
        self.assertEqual(error.as_dict()["action_id"], action)
        self.assertEqual(self.store.get_action(action)["state"], "MAY_HAVE_RUN")
        self.assertEqual(self.store.get_run("example")["counters"]["attempts"], 1)
        self.error("PENDING", self.store.perform, action, self.spec["binding"], malformed)
        self.assertEqual(len(effects), 1)

    def test_response_recording_failure_keeps_dispatch_and_single_effect(self):
        self.prepare()
        action = self.queue("submit")
        effects = []
        original = sqlite3.connect
        class Connection(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("synthetic response commit failure")
        def invoke(payload):
            effects.append(payload["operation_id"])
            fault = patch("control_production_plane.store.sqlite3.connect",
                          side_effect=lambda *a, **k: original(*a, **k, factory=Connection))
            fault.start()
            self.addCleanup(fault.stop)
            return {"response": {"accepted": True}, "call_closed": True}
        error = self.error("STORAGE_ERROR", self.store.perform, action, self.spec["binding"], invoke)
        self.assertEqual(error.commit_state, "unknown")
        self.assertTrue(error.as_dict()["dispatch_recorded"])
        self.assertEqual(error.as_dict()["action_id"], action)
        self.assertEqual(self.store.get_action(action)["state"], "MAY_HAVE_RUN")
        self.error("PENDING", self.store.perform, action, self.spec["binding"], invoke)
        self.assertEqual(len(effects), 1)

    def test_refresh_that_is_not_ready_invalidates_prior_request(self):
        for reply in ("PREPARING", "FAILED"):
            with self.subTest(reply=reply):
                self.store = initialize(self.root / reply)
                self.store.execute(command("create", self.spec))
                first = self.prepare()
                original = self.store.get_run("example")["attempt"]
                queued = self.queue("submit")
                refresh = self.queue("prepare")
                self.store.perform(refresh, self.spec["binding"],
                                   lambda p: {"response": {"state": reply}, "call_closed": True})
                self.assertIsNone(self.store.get_run("example")["attempt"]["prepared_request"])
                self.assertEqual(self.store.get_action(first)["response"]["request"], original["prepared_request"])
                called = []
                self.error("CONFLICT", self.store.perform, queued, self.spec["binding"], called.append)
                self.error("NOT_READY", self.queue, "submit")
                self.assertEqual(called, [])
                self.assertEqual(self.store.get_run("example")["counters"]["attempts"], 0)
                self.prepare()
                fresh = self.queue("submit")
                self.store.perform(fresh, self.spec["binding"],
                                   lambda p: {"response": {"accepted": True}, "call_closed": True})
                self.assertEqual(self.store.get_run("example")["attempt"]["operation_id"], original["operation_id"])

    def test_ready_refresh_preflight_failure_retains_original_request(self):
        self.prepare()
        original = self.store.get_run("example")["attempt"]["prepared_request"]
        refresh = self.queue("prepare")
        def reject(payload):
            raise ControlError("LOCAL_CHECK", "Synthetic local preflight failure")
        self.error("LOCAL_CHECK", self.store.perform, refresh, self.spec["binding"], ready, preflight=reject)
        self.assertEqual(self.store.get_run("example")["attempt"]["prepared_request"], original)

    def test_ready_refresh_budget_rejection_rolls_back_invalidation(self):
        spec = sample("limited-refresh")
        spec["budget"]["max_actions"] = 1
        self.store.execute(command("create", spec))
        first = self.queue("prepare", spec["run_id"])
        self.store.perform(first, spec["binding"], ready)
        original = self.store.get_run(spec["run_id"])
        refresh = self.queue("prepare", spec["run_id"])
        self.error("LIMIT", self.store.perform, refresh, spec["binding"], ready)
        self.assertEqual(self.store.get_run(spec["run_id"]), original)
        self.assertEqual(self.store.get_action(refresh)["state"], "QUEUED")

    def test_legacy_ready_cache_does_not_authorize_submit(self):
        for reply in ("PREPARING", "FAILED"):
            for boundary in ("queue", "dispatch"):
                with self.subTest(reply=reply, boundary=boundary):
                    self.path = self.root / (reply + boundary)
                    self.store = initialize(self.path)
                    self.store.execute(command("create", self.spec))
                    first = self.prepare()
                    original = self.store.get_run("example")["attempt"]["prepared_request"]
                    queued = self.queue("submit")
                    refresh = self.queue("prepare")
                    self.store.perform(refresh, self.spec["binding"],
                                       lambda p: {"response": {"state": reply}, "call_closed": True})
                    # The first release kept this READY cache after a later
                    # non-ready preparation. Preserve its actual on-disk shape.
                    legacy = self.store.get_run("example")
                    legacy["attempt"]["prepared_request"] = original
                    with sqlite3.connect(self.path / "runs.sqlite") as db:
                        db.execute("UPDATE runs SET document=? WHERE run_id='example'", (canonical(legacy).decode(),))
                    self.store = open_store(self.path)
                    effects = []
                    def submit(payload):
                        effects.append(payload)
                        return {"response": {"accepted": True}, "call_closed": True}
                    if boundary == "queue":
                        self.error("NOT_READY", self.queue, "submit")
                    else:
                        self.error("NOT_READY", self.store.perform, queued, self.spec["binding"], submit)
                    self.assertEqual(effects, [])
                    self.assertEqual(self.store.get_run("example"), legacy)
                    self.assertEqual(self.store.get_action(first)["response"]["request"], original)
                    # A fresh READY for the same operation resolves the gap.
                    self.prepare()
                    fresh = self.queue("submit")
                    self.store.perform(fresh, self.spec["binding"], submit)
                    self.assertEqual(len(effects), 1)
                    self.assertEqual(effects[0]["operation_id"], original["operation_id"])

    def test_nonstarted_refresh_preserves_ready_and_submission_budget(self):
        spec = sample("bounded-refresh")
        spec["budget"]["max_actions"] = 2
        self.store.execute(command("create", spec))
        first = self.queue("prepare", spec["run_id"])
        self.store.perform(first, spec["binding"], ready)
        original = self.store.get_run(spec["run_id"])["attempt"]["prepared_request"]
        refresh = self.queue("prepare", spec["run_id"])
        def cannot_start(payload):
            raise CallNotStarted("demo:no-process-or-transport-started")
        self.error("CALL_NOT_STARTED", self.store.perform, refresh, spec["binding"], cannot_start)
        self.store = open_store(self.path)
        state = self.store.get_run(spec["run_id"])
        self.assertEqual(state["attempt"]["prepared_request"], original)
        self.assertEqual(state["counters"]["actions"], 1)
        self.assertEqual(self.store.get_action(refresh)["state"], "NOT_SENT")
        submit = self.queue("submit", spec["run_id"])
        seen = []
        def invoke(payload):
            seen.append(payload)
            return {"response": {"accepted": True}, "call_closed": True}
        self.store.perform(submit, spec["binding"], invoke)
        self.assertEqual(seen, [original])
        self.assertEqual(self.store.get_run(spec["run_id"])["counters"]["actions"], 2)

    def test_nonstarted_refresh_does_not_restore_superseded_ready(self):
        first = self.prepare()
        refresh = self.queue("prepare")
        self.store.perform(refresh, self.spec["binding"],
                           lambda p: {"response": {"state": "FAILED"}, "call_closed": True})
        for _ in range(2):
            retry = self.queue("prepare")
            def cannot_start(payload):
                raise CallNotStarted("demo:local-start-rejected")
            self.error("CALL_NOT_STARTED", self.store.perform, retry, self.spec["binding"], cannot_start)
        self.assertIsNone(self.store.get_run("example")["attempt"]["prepared_request"])
        self.error("NOT_READY", self.queue, "submit")
        self.assertEqual(self.store.get_action(first)["response"]["state"], "READY")

    def test_preparation_history_uses_dispatch_order_not_queue_order(self):
        queued_first = self.queue("prepare")
        queued_second = self.queue("prepare")
        self.store.perform(queued_second, self.spec["binding"],
                           lambda p: {"response": {"state": "FAILED"}, "call_closed": True})
        self.store.perform(queued_first, self.spec["binding"], ready)
        submit = self.queue("submit")
        self.store.perform(submit, self.spec["binding"],
                           lambda p: {"response": {"accepted": True}, "call_closed": True})
        self.assertEqual(self.store.get_run("example")["counters"]["attempts"], 1)

    def test_incomplete_preparation_history_cannot_restore_old_ready(self):
        for fault in ("missing", "wrong_run", "wrong_action", "wrong_attempt", "wrong_kind",
                      "invalid_response", "invalid_json", "invalid_kind", "missing_queue"):
            with self.subTest(fault=fault):
                path = self.root / fault
                self.store = initialize(path)
                self.store.execute(command("create", self.spec))
                self.store.execute(command("create", sample("other")))
                first = self.prepare()
                failed = self.queue("prepare")
                self.store.perform(failed, self.spec["binding"],
                                   lambda p: {"response": {"state": "FAILED"}, "call_closed": True})
                broken = self.store.get_action(failed)
                retry = self.queue("prepare")
                with self.store.executor() as lease:
                    self.store.execute(command("dispatch", {"action_id": retry, "observed_binding": self.spec["binding"]}), lease=lease)
                # Inject damaged history while retaining its dispatch event.
                # quick_check cannot detect these broken logical links.
                with sqlite3.connect(path / "runs.sqlite") as db:
                    if fault == "missing":
                        db.execute("DELETE FROM actions WHERE action_id=?", (failed,))
                    elif fault == "wrong_run":
                        db.execute("UPDATE actions SET run_id='other' WHERE action_id=?", (failed,))
                    elif fault == "missing_queue":
                        db.execute("DELETE FROM events WHERE operation='queue' AND json_extract(body,'$.action_id')=?", (failed,))
                    else:
                        if fault == "wrong_action":
                            broken["action_id"] = str(uuid.uuid4())
                        elif fault == "wrong_attempt":
                            broken["attempt_id"] = str(uuid.uuid4())
                        elif fault == "wrong_kind":
                            broken["kind"] = "query"
                        elif fault == "invalid_response":
                            broken["response"] = None
                        elif fault == "invalid_kind":
                            broken["kind"] = []
                        document = "[" if fault == "invalid_json" else canonical(broken).decode()
                        db.execute("UPDATE actions SET document=? WHERE action_id=?", (document, failed))
                    self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.store = open_store(path)
                before = (path / "runs.sqlite").read_bytes()
                proof = command("not_started", {"action_id": retry, "evidence_ref": "demo:local-start-rejected"})
                self.error("STORE_INVALID", self.store.execute, proof)
                self.assertEqual((path / "runs.sqlite").read_bytes(), before)
                self.assertIsNone(self.store.get_run("example")["attempt"]["prepared_request"])
                self.assertEqual(self.store.get_action(retry)["state"], "MAY_HAVE_RUN")
                self.assertEqual(self.store.get_action(first)["response"]["state"], "READY")
                self.error("NOT_READY", self.queue, "submit")

    def test_missing_ready_history_blocks_queue_and_dispatch_without_effect(self):
        first = self.prepare()
        # Losing the newest READY cannot silently fall back to an older one.
        newer = self.prepare()
        queued = self.queue("submit")
        with sqlite3.connect(self.path / "runs.sqlite") as db:
            db.execute("DELETE FROM actions WHERE action_id=?", (newer,))
        before = (self.path / "runs.sqlite").read_bytes()
        self.error("STORE_INVALID", self.queue, "submit")
        self.assertEqual((self.path / "runs.sqlite").read_bytes(), before)
        # A request queued before the loss must also be checked at dispatch.
        effects = []
        self.error("STORE_INVALID", self.store.perform, queued, self.spec["binding"], effects.append)
        self.assertEqual(effects, [])
        self.assertEqual((self.path / "runs.sqlite").read_bytes(), before)
        self.assertEqual(self.store.get_action(first)["response"]["state"], "READY")

    def test_no_start_proof_cannot_restore_ready_from_prior_attempt(self):
        first = self.prepare()
        old_attempt = self.store.get_run("example")["attempt"]
        self.store.execute(command("revise", {"run_id": "example", "expected_revision": 1,
                                            "plan": dict(self.spec["plan"], content="print('revised')\n")}))
        action = self.queue("prepare")
        def cannot_start(payload):
            raise CallNotStarted("demo:new-attempt-did-not-start")
        self.error("CALL_NOT_STARTED", self.store.perform, action, self.spec["binding"], cannot_start)
        current = self.store.get_run("example")["attempt"]
        self.assertNotEqual(current["operation_id"], old_attempt["operation_id"])
        self.assertIsNone(current["prepared_request"])
        self.error("NOT_READY", self.queue, "submit")
        self.assertEqual(self.store.get_action(first)["response"]["request"], old_attempt["prepared_request"])

    def test_refresh_process_exit_preserves_old_ready_only_as_history(self):
        first = self.prepare()
        queued = self.queue("submit")
        refresh = self.queue("prepare")
        original = self.store.get_run("example")["attempt"]
        code = '''import os,sys
from control_production_plane import open_store
s=open_store(sys.argv[1])
s.perform(sys.argv[2],s.get_run('example')['binding'],lambda payload:os._exit(72))
'''
        result = subprocess.run([sys.executable, "-c", code, str(self.path), refresh], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 72, result.stderr)
        self.store = open_store(self.path)
        self.assertIsNone(self.store.get_run("example")["attempt"]["prepared_request"])
        self.assertEqual(self.store.get_action(first)["response"]["request"], original["prepared_request"])
        called = []
        self.error("PENDING", self.store.perform, queued, self.spec["binding"], called.append)
        self.assertEqual(called, [])
        self.assertEqual(self.store.recover("example")["next_step"], "RECONCILE_ORIGINAL_PREPARATION")

    def test_cancel_payload_matches_executor_public_contract(self):
        self.submit()
        self.store.execute(command("cancel", {"run_id": "example", "reason": "Stop"}))
        action = self.queue("cancel")
        attempt = self.store.get_run("example")["attempt"]
        expected = {"operation_id": attempt["operation_id"],
                    "expected_request_digest": attempt["prepared_request"]["request_digest"],
                    "target": {"kind": "job"}}
        seen = []
        def cancel(payload):
            seen.append(payload)
            return {"response": {"requested": True}, "call_closed": True}
        self.store.perform(action, self.spec["binding"], cancel)
        self.assertEqual(seen, [expected])

    def test_legacy_queued_cancel_is_retained_and_requires_new_action(self):
        self.submit()
        self.store.execute(command("cancel", {"run_id": "example", "reason": "Stop"}))
        action = self.queue("cancel")
        legacy = self.store.get_action(action)
        legacy["payload"]["request_digest"] = legacy["payload"].pop("expected_request_digest")
        # Fixture models the exact queued record written by the first version.
        with sqlite3.connect(self.path / "runs.sqlite") as db:
            db.execute("UPDATE actions SET document=? WHERE action_id=?", (canonical(legacy).decode(), action))
        called = []
        self.error("CONFLICT", self.store.perform, action, self.spec["binding"], called.append)
        self.assertEqual(called, [])
        self.assertEqual(self.store.get_action(action), legacy)
        self.assertEqual(self.store.get_run("example")["counters"]["cancellations"], 0)
        self.store.execute(command("abandon", {"action_id": action, "reason": "Replace unsent legacy cancellation"}))
        new = self.queue("cancel")
        self.assertEqual(self.store.get_action(new)["payload"]["operation_id"], legacy["payload"]["operation_id"])
        self.store.perform(new, self.spec["binding"], lambda p: {"response": {"requested": True}, "call_closed": True})

    def test_cancel_queued_during_submit_cannot_dispatch_after_no_start_proof(self):
        self.prepare()
        submit = self.queue("submit")
        cancellations = []
        def cannot_start(payload):
            self.store.execute(command("cancel", {"run_id": "example", "reason": "Stop"}))
            cancellations.append(self.queue("cancel"))
            raise CallNotStarted("demo:spawn-did-not-start")
        self.error("CALL_NOT_STARTED", self.store.perform, submit, self.spec["binding"], cannot_start)
        called = []
        self.error("NOT_READY", self.store.perform, cancellations[0], self.spec["binding"], called.append)
        self.assertEqual(called, [])
        self.assertEqual(self.store.get_run("example")["counters"]["cancellations"], 0)
        self.assertTrue(self.store.get_run("example")["cancel"])

    def test_preflight_failure_keeps_original_action_retryable_and_quota(self):
        action = self.queue("prepare")
        called = []
        def reject(payload):
            raise ControlError("LOCAL_CHECK", "Local input unavailable")
        self.error("LOCAL_CHECK", self.store.perform, action, self.spec["binding"], called.append, preflight=reject)
        self.assertEqual(called, [])
        self.assertEqual(self.store.get_run("example")["counters"]["actions"], 0)
        self.store.perform(action, self.spec["binding"], ready)
        self.assertEqual(self.store.get_run("example")["counters"]["actions"], 1)

    def test_proved_process_start_failure_refunds_attempt_and_retries_same_task(self):
        self.prepare()
        original = self.store.get_run("example")["attempt"]
        action = self.queue("submit")
        def cannot_start(payload):
            raise CallNotStarted("demo:spawn-failed-before-process-creation")
        self.error("CALL_NOT_STARTED", self.store.perform, action, self.spec["binding"], cannot_start)
        self.assertEqual(self.store.get_run("example")["counters"]["attempts"], 0)
        next_action = self.queue("submit")
        self.assertEqual(self.store.get_action(action)["payload"], self.store.get_action(next_action)["payload"])
        self.store.perform(next_action, self.spec["binding"], lambda p: {"response": {"accepted": True}, "call_closed": True})
        self.assertEqual(self.store.get_run("example")["attempt"]["operation_id"], original["operation_id"])

    def test_cancel_between_queue_and_dispatch_blocks_callback_and_budget(self):
        self.prepare()
        action = self.queue("submit")
        self.store.execute(command("cancel", {"run_id": "example", "reason": "Stop before submit"}))
        called = []
        self.error("NOT_READY", self.store.perform, action, self.spec["binding"], called.append)
        self.assertEqual(called, [])
        self.assertEqual(self.store.get_run("example")["counters"]["attempts"], 0)
        self.assertEqual(self.store.recover("example")["next_step"], "NO_NEW_SUBMISSION")

    def test_concurrent_not_found_does_not_block_proved_nonstart_retry(self):
        spec = sample("one-attempt")
        spec["budget"]["max_attempts"] = 1
        spec["budget"]["max_actions"] = 2
        self.store.execute(command("create", spec))
        prepare = self.queue("prepare", spec["run_id"])
        self.store.perform(prepare, spec["binding"], ready)
        original = self.store.get_run(spec["run_id"])["attempt"]
        action = self.queue("submit", spec["run_id"])
        started, release = self.root / "waiting", self.root / "release"
        code = """
import json, sys, time
from pathlib import Path
from control_production_plane import CallNotStarted, ControlError, open_store
store = open_store(sys.argv[1])
def invoke(payload):
 Path(sys.argv[3]).touch()
 deadline = time.monotonic() + 10
 while not Path(sys.argv[4]).exists() and time.monotonic() < deadline: time.sleep(.02)
 if not Path(sys.argv[4]).exists(): raise RuntimeError('Parent did not release callback')
 raise CallNotStarted('demo:spawn-failed-before-transport')
try:
 store.perform(sys.argv[2], store.get_run('one-attempt')['binding'], invoke)
except ControlError as exc:
 print(json.dumps(exc.as_dict()))
 sys.exit(0 if exc.code == 'CALL_NOT_STARTED' else 71)
sys.exit(72)
"""
        child = subprocess.Popen([sys.executable, "-c", code, str(self.path), action, str(started), str(release)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not started.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(started.exists())
            query = self.queue("query", spec["run_id"])
            self.store.perform(query, spec["binding"],
                               lambda p: {"response": {"state": "NOT_FOUND"}, "call_closed": True})
            observation = {"run_id": spec["run_id"], "attempt_id": original["attempt_id"],
                           "operation_id": original["operation_id"], "request_digest": original["prepared_request"]["request_digest"],
                           "remote_state": "NOT_FOUND", "outcome": "UNKNOWN", "exited": False,
                           "evidence_state": "UNKNOWN", "evidence_ref": "demo:concurrent-query"}
            self.store.execute(command("observe", observation))
            # NOT_FOUND alone does not refund or allow another submission.
            self.assertEqual(self.store.get_run(spec["run_id"])["counters"]["attempts"], 1)
            self.error("NOT_READY", self.queue, "submit", spec["run_id"])
        finally:
            release.touch()
            stdout, stderr = child.communicate(timeout=15)
        self.assertEqual(child.returncode, 0, (stdout, stderr))
        self.assertTrue(json.loads(stdout)["dispatch_recorded"])
        self.store = open_store(self.path)
        state = self.store.get_run(spec["run_id"])
        self.assertFalse(state["attempt"]["submission_recorded"])
        self.assertEqual(state["attempt"]["observation"], observation)
        self.assertEqual(state["attempt"]["operation_id"], original["operation_id"])
        self.assertEqual(state["counters"], {"actions": 1, "attempts": 0, "queries": 1, "cancellations": 0})
        effects = []
        def submit(payload):
            effects.append(payload)
            return {"response": {"accepted": True}, "call_closed": True}
        retry = self.queue("submit", spec["run_id"])
        self.store.perform(retry, spec["binding"], submit)
        self.assertEqual(effects, [original["prepared_request"]])
        self.assertEqual(self.store.get_run(spec["run_id"])["counters"]["attempts"], 1)
        replay = self.store.execute(command("not_started", {"action_id": action,
                                  "evidence_ref": "demo:spawn-failed-before-transport"}, "not-started:" + action))
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.store.get_run(spec["run_id"])["counters"]["attempts"], 1)
        self.assertTrue(self.store.get_run(spec["run_id"])["attempt"]["submission_recorded"])
        self.error("PENDING", self.store.perform, action, spec["binding"], submit)
        self.assertEqual(len(effects), 1)

    def test_positive_observation_history_blocks_nonstart_even_after_not_found(self):
        for remote_state, later_missing in (("RUNNING", False), ("TERMINAL", False), ("RUNNING", True), ("NOT_FOUND", True)):
            with self.subTest(remote_state=remote_state, later_missing=later_missing):
                path = self.root / (remote_state + str(later_missing))
                self.store = initialize(path)
                self.store.execute(command("create", self.spec))
                self.prepare()
                action = self.queue("submit")
                with self.store.executor() as lease:
                    self.store.execute(command("dispatch", {"action_id": action, "observed_binding": self.spec["binding"]}), lease=lease)
                observed = self.observation("UNKNOWN", False, remote_state)
                if remote_state == "NOT_FOUND":
                    observed["evidence_state"] = "SEALED"
                self.store.execute(command("observe", observed))
                if later_missing:
                    self.store.execute(command("observe", self.observation("UNKNOWN", False, "NOT_FOUND")))
                before = (path / "runs.sqlite").read_bytes()
                self.error("CONFLICT", self.store.execute,
                           command("not_started", {"action_id": action, "evidence_ref": "demo:contradictory-proof"}))
                self.assertEqual((path / "runs.sqlite").read_bytes(), before)
                self.assertTrue(self.store.get_run("example")["attempt"]["submission_recorded"])
                self.assertEqual(self.store.get_run("example")["counters"]["attempts"], 1)

    def test_nonstart_refund_commit_failure_reconciles_atomically(self):
        real_connect = sqlite3.connect
        for committed in (False, True):
            with self.subTest(committed=committed):
                path = self.root / str(committed)
                self.store = initialize(path)
                self.store.execute(command("create", self.spec))
                self.prepare()
                action = self.queue("submit")
                with self.store.executor() as lease:
                    self.store.execute(command("dispatch", {"action_id": action, "observed_binding": self.spec["binding"]}), lease=lease)
                self.store.execute(command("observe", self.observation("UNKNOWN", False, "NOT_FOUND")))
                before = self.store.get_run("example")
                proof = command("not_started", {"action_id": action, "evidence_ref": "demo:nonstart-proof"})
                class Connection(sqlite3.Connection):
                    def commit(self):
                        if committed:
                            super().commit()
                        raise sqlite3.OperationalError("synthetic proof commit failure")
                with patch("control_production_plane.store.sqlite3.connect",
                           side_effect=lambda *a, **k: real_connect(*a, **k, factory=Connection)):
                    error = self.error("STORAGE_ERROR", self.store.execute, proof)
                self.assertEqual(error.commit_state, "unknown")
                self.store = open_store(path)
                state = self.store.get_run("example")
                if committed:
                    self.assertFalse(state["attempt"]["submission_recorded"])
                    self.assertEqual(state["counters"]["attempts"], 0)
                    self.assertEqual(self.store.get_action(action)["state"], "NOT_SENT")
                else:
                    self.assertEqual(state, before)
                    self.assertEqual(self.store.get_action(action)["state"], "MAY_HAVE_RUN")
                result = self.store.execute(proof)
                self.assertEqual(result["replayed"], committed)
                self.assertEqual(self.store.get_run("example")["counters"]["attempts"], 0)
                self.assertEqual(self.store.get_run("example")["attempt"]["observation"], before["attempt"]["observation"])
                self.assertTrue(self.store.execute(proof)["replayed"])
                self.assertEqual(self.store.get_run("example")["counters"]["attempts"], 0)

    def test_cancel_ack_and_not_found_do_not_become_exit_proof(self):
        self.submit()
        self.store.execute(command("cancel", {"run_id": "example", "reason": "Stop"}))
        action = self.queue("cancel")
        self.store.perform(action, self.spec["binding"], lambda p: {"response": {"error": "NOT_FOUND"}, "call_closed": True})
        self.store.execute(command("observe", self.observation("UNKNOWN", False, "NOT_FOUND")))
        state = open_store(self.path).recover("example")
        self.assertTrue(state["run"]["cancel"])
        self.assertFalse(state["run"]["attempt"]["observation"]["exited"])
        self.assertEqual(state["next_step"], "QUERY_ORIGINAL_TASK")
        self.assertEqual(state["run"]["counters"]["attempts"], 1)
        self.queue("cancel")  # original cancellation may need another bounded request

    def test_success_before_cancel_remains_success_not_acceptance(self):
        self.submit()
        self.store.execute(command("observe", self.observation("SUCCEEDED")))
        self.store.execute(command("cancel", {"run_id": "example", "reason": "Late cancellation"}))
        state = self.store.recover("example")
        self.assertEqual(state["run"]["attempt"]["observation"]["outcome"], "SUCCEEDED")
        self.assertNotIn("accepted", state["run"])
        self.error("CONFLICT", self.store.execute, command("observe", self.observation("CANCELLED")))

    def test_bound_identity_mismatch_refuses_callback_and_spending(self):
        action = self.queue("prepare")
        observed = dict(self.spec["binding"], principal_id="other:caller")
        self.error("IDENTITY_CONFLICT", self.store.perform, action, observed, ready)
        self.assertEqual(self.store.get_action(action)["state"], "QUEUED")
        self.assertEqual(self.store.get_run("example")["counters"]["actions"], 0)

    def test_failed_attempt_revision_preserves_goal_history_and_budget(self):
        self.submit()
        old = self.store.get_run("example")
        self.store.execute(command("observe", self.observation()))
        new_plan = dict(self.spec["plan"], content="print('corrected')\n")
        self.store.execute(command("revise", {"run_id": "example", "expected_revision": 1, "plan": new_plan}))
        new = self.store.get_run("example")
        self.assertEqual(new["goal_digest"], old["goal_digest"])
        self.assertNotEqual(new["attempt"]["operation_id"], old["attempt"]["operation_id"])
        self.assertEqual(new["counters"]["attempts"], 1)
        historical = self.store.get_revision("example", 1)
        self.assertEqual(historical["attempt"]["operation_id"], old["attempt"]["operation_id"])
        self.assertEqual(historical["attempt"]["observation"]["outcome"], "FAILED")
        self.assertEqual(self.store.events("example")[0]["body"]["plan"], self.spec["plan"])
        self.error("CONFLICT", self.store.execute, command("revise", {"run_id": "example", "expected_revision": 1, "plan": new_plan}))

    def test_unknown_and_unclosed_calls_cannot_be_replaced_but_query_works(self):
        self.prepare()
        action = self.queue("submit")
        def lost(payload):
            raise TimeoutError()
        self.error("CALL_UNKNOWN", self.store.perform, action, self.spec["binding"], lost)
        query = self.queue("query")
        self.store.perform(query, self.spec["binding"], lambda p: {"response": {"state": "RUNNING"}, "call_closed": True})
        self.store.execute(command("observe", self.observation()))
        self.error("PENDING", self.store.execute, command("revise", {"run_id": "example", "expected_revision": 1, "plan": self.spec["plan"]}))
        self.store.execute(command("close_call", {"action_id": action, "evidence_ref": "demo:verified-exit-and-eof"}))
        self.store.execute(command("revise", {"run_id": "example", "expected_revision": 1, "plan": self.spec["plan"]}))

    def test_transport_failure_can_receive_verified_ready_without_reinvoking(self):
        action = self.queue("prepare")
        calls = []
        def lost(payload):
            calls.append(payload)
            raise TimeoutError()
        self.error("CALL_UNKNOWN", self.store.perform, action, self.spec["binding"], lost)
        original = self.store.get_action(action)
        reply = ready(original["payload"])
        reply["call_closed"] = False
        reconcile = command("respond", dict(reply, action_id=action))
        self.store.execute(reconcile)
        request = self.store.get_run("example")["attempt"]["prepared_request"]
        self.assertEqual(request, reply["response"]["request"])
        submit = self.queue("submit")
        effects = []
        def invoke(payload):
            effects.append(payload)
            return {"response": {"accepted": True}, "call_closed": True}
        self.error("PENDING", self.store.perform, submit, self.spec["binding"], invoke)
        self.store.execute(command("close_call", {"action_id": action, "evidence_ref": "demo:old-call-closed"}))
        self.store.perform(submit, self.spec["binding"], invoke)
        self.assertEqual(effects, [request])
        self.error("PENDING", self.store.perform, action, self.spec["binding"], lost)
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.store.execute(reconcile)["replayed"])
        # The old failure receipt remains historical and cannot overwrite READY.
        failure = command("respond", {"action_id": action, "response": original["response"],
                                      "call_closed": False}, "response:" + action)
        self.assertTrue(self.store.execute(failure)["replayed"])
        self.assertEqual(self.store.get_action(action)["response"], reply["response"])
        with sqlite3.connect(self.path / "runs.sqlite") as db:
            responses = [json.loads(row[0])["response"] for row in db.execute(
                "SELECT body FROM events WHERE operation='respond' ORDER BY seq")]
        self.assertEqual(responses[:2], [original["response"], reply["response"]])

    def test_delayed_nonstart_after_transport_failure_refunds_only_original_call(self):
        for kind in ("prepare", "submit", "query", "cancel"):
            with self.subTest(kind=kind):
                path = self.root / ("late-proof-" + kind)
                self.store = initialize(path)
                self.store.execute(command("create", self.spec))
                self.prepare()
                if kind == "cancel":
                    submit = self.queue("submit")
                    self.store.perform(submit, self.spec["binding"],
                                       lambda p: {"response": {"accepted": True}, "call_closed": True})
                    self.store.execute(command("cancel", {"run_id": "example", "reason": "Synthetic stop"}))
                before = self.store.get_run("example")
                action = self.queue(kind)
                def lost(payload):
                    raise OSError("Synthetic adapter failure before classifying startup")
                self.error("CALL_UNKNOWN", self.store.perform, action, self.spec["binding"], lost)
                failure = self.store.get_action(action)
                if kind == "submit":
                    self.store.execute(command("observe", self.observation("UNKNOWN", False, "NOT_FOUND")))
                proof = command("not_started", {"action_id": action, "evidence_ref": "demo:later-verified-no-process"})
                self.store.execute(proof)
                self.store = open_store(path)
                current = self.store.get_run("example")
                self.assertEqual(current["counters"], before["counters"])
                self.assertEqual(current["attempt"]["prepared_request"], before["attempt"]["prepared_request"])
                self.assertEqual(current["attempt"]["submission_recorded"], before["attempt"]["submission_recorded"])
                self.assertEqual(current["cancel"], before["cancel"])
                self.assertEqual(self.store.get_action(action)["state"], "NOT_SENT")
                self.assertTrue(self.store.execute(proof)["replayed"])
                self.assertEqual(self.store.get_run("example")["counters"], before["counters"])
                with sqlite3.connect(path / "runs.sqlite") as db:
                    retained = db.execute("SELECT body FROM events WHERE operation='respond' AND json_extract(body,'$.action_id')=?", (action,)).fetchone()
                self.assertEqual(json.loads(retained[0])["response"], failure["response"])
                self.error("PENDING", self.store.perform, action, self.spec["binding"], lost)

    def test_late_recovered_ready_cannot_supersede_newer_preparation(self):
        for next_state in ("READY", "FAILED", "PREPARING"):
            with self.subTest(next_state=next_state):
                path = self.root / ("late-ready-" + next_state)
                self.store = initialize(path)
                self.store.execute(command("create", self.spec))
                old = self.queue("prepare")
                def lost(payload):
                    raise TimeoutError()
                self.error("CALL_UNKNOWN", self.store.perform, old, self.spec["binding"], lost)
                self.store.execute(command("close_call", {"action_id": old, "evidence_ref": "demo:old-call-exited"}))
                newer = self.queue("prepare")
                if next_state == "READY":
                    def next_ready(payload):
                        result = ready(payload)
                        request = result["response"]["request"]
                        request["expires_at"] += 60
                        request["request_digest"] = hashlib.sha256(canonical({k:v for k,v in request.items() if k != "request_digest"})).hexdigest()
                        return result
                    self.store.perform(newer, self.spec["binding"], next_ready)
                else:
                    self.store.perform(newer, self.spec["binding"],
                                       lambda p: {"response": {"state": next_state}, "call_closed": True})
                current = self.store.get_run("example")["attempt"]["prepared_request"]
                reply = ready(self.store.get_action(old)["payload"])
                self.error("CONFLICT", self.store.execute,
                           command("respond", dict(reply, action_id=old, call_closed=False)))
                self.assertEqual(self.store.get_run("example")["attempt"]["prepared_request"], current)
                self.store.execute(command("respond", dict(reply, action_id=old)))
                self.assertEqual(self.store.get_run("example")["attempt"]["prepared_request"], current)
                self.assertEqual(self.store.get_action(old)["response"], reply["response"])
                if next_state == "READY":
                    queued = self.queue("submit")
                    self.assertEqual(self.store.get_action(queued)["payload"], current)
                else:
                    self.error("NOT_READY", self.queue, "submit")

    def test_confirmed_responses_cannot_be_replaced_or_refunded(self):
        for response in ({"accepted": True}, {"state": "FAILED"}, {"state": "PREPARING"},
                         {"transport_error": "TimeoutError", "remote_detail": "A real response"}):
            with self.subTest(response=response):
                action = self.queue("query")
                self.store.perform(action, self.spec["binding"], lambda p: {"response": response, "call_closed": True})
                before = (self.path / "runs.sqlite").read_bytes()
                self.error("CONFLICT", self.store.execute, command("not_started", {"action_id": action, "evidence_ref": "demo:contradictory-proof"}))
                self.error("CONFLICT", self.store.execute, command("respond", {"action_id": action, "response": {}, "call_closed": True}))
                self.assertEqual((self.path / "runs.sqlite").read_bytes(), before)

    def test_delayed_nonstart_cannot_erase_observed_execution(self):
        self.prepare()
        action = self.queue("submit")
        def lost(payload):
            raise TimeoutError()
        self.error("CALL_UNKNOWN", self.store.perform, action, self.spec["binding"], lost)
        self.store.execute(command("observe", self.observation("UNKNOWN", False, "RUNNING")))
        self.store.execute(command("observe", self.observation("UNKNOWN", False, "NOT_FOUND")))
        before = (self.path / "runs.sqlite").read_bytes()
        self.error("CONFLICT", self.store.execute, command("not_started", {"action_id": action,
                                                         "evidence_ref": "demo:contradictory-late-proof"}))
        self.assertEqual((self.path / "runs.sqlite").read_bytes(), before)
        self.assertEqual(self.store.get_run("example")["counters"]["attempts"], 1)
        self.assertTrue(self.store.get_run("example")["attempt"]["submission_recorded"])

    def test_old_preparation_or_query_nonstart_refunds_without_changing_new_attempt(self):
        for kind in ("prepare", "query"):
            with self.subTest(kind=kind):
                path = self.root / ("revised-proof-" + kind)
                self.store = initialize(path)
                spec = sample()
                spec["budget"].update(max_attempts=1, max_actions=2, max_queries=1)
                self.store.execute(command("create", spec))
                action = self.queue(kind)
                def lost(payload):
                    raise TimeoutError()
                self.error("CALL_UNKNOWN", self.store.perform, action, spec["binding"], lost)
                self.store.execute(command("close_call", {"action_id": action, "evidence_ref": "demo:old-call-closed"}))
                self.store.execute(command("revise", {"run_id": "example", "expected_revision": 1,
                    "plan": dict(spec["plan"], content="print('revised')\n")}))
                old_revision = self.store.get_revision("example", 1)
                self.prepare()
                current = self.store.get_run("example")
                next_action = self.queue("submit" if kind == "prepare" else "query")
                effects = []
                def invoke(payload):
                    effects.append(payload)
                    return {"response": {"accepted": True}, "call_closed": True}
                self.error("LIMIT", self.store.perform, next_action, spec["binding"], invoke)
                self.assertEqual(effects, [])
                proof = command("not_started", {"action_id": action, "evidence_ref": "demo:late-old-call-nonstart"})
                self.store.execute(proof)
                self.store = open_store(path)
                expected = copy.deepcopy(current)
                counter = "actions" if kind == "prepare" else "queries"
                expected["counters"][counter] -= 1
                self.assertEqual(self.store.get_run("example"), expected)
                self.assertEqual(self.store.get_revision("example", 1), old_revision)
                self.assertEqual(self.store.get_action(action)["state"], "NOT_SENT")
                self.store.perform(next_action, spec["binding"], invoke)
                self.assertEqual(len(effects), 1)
                self.assertEqual(effects[0]["operation_id"], current["attempt"]["operation_id"])
                after = self.store.get_run("example")
                self.assertTrue(self.store.execute(proof)["replayed"])
                self.error("CONFLICT", self.store.execute, command("not_started", proof["body"]))
                self.assertEqual(self.store.get_run("example"), after)
                self.assertEqual(self.store.get_revision("example", 1), old_revision)

    def test_old_submit_nonstart_cannot_refund_or_clear_a_new_submission(self):
        self.prepare()
        old_submit = self.queue("submit")
        def lost(payload):
            raise TimeoutError()
        self.error("CALL_UNKNOWN", self.store.perform, old_submit, self.spec["binding"], lost)
        self.store.execute(command("observe", self.observation("FAILED")))
        self.store.execute(command("close_call", {"action_id": old_submit, "evidence_ref": "demo:old-submit-closed"}))
        self.store.execute(command("revise", {"run_id": "example", "expected_revision": 1,
            "plan": dict(self.spec["plan"], content="print('new attempt')\n")}))
        self.submit()
        self.store.execute(command("cancel", {"run_id": "example", "reason": "Stop the current attempt"}))
        before = (self.path / "runs.sqlite").read_bytes()
        self.error("CONFLICT", self.store.execute, command("not_started", {"action_id": old_submit,
            "evidence_ref": "demo:contradictory-old-submit-proof"}))
        self.assertEqual((self.path / "runs.sqlite").read_bytes(), before)
        current = self.store.get_run("example")
        self.assertTrue(current["attempt"]["submission_recorded"])
        self.assertEqual(current["counters"]["attempts"], 2)
        self.assertIsNotNone(current["cancel"])

    def test_old_query_refund_commit_failure_preserves_revision_and_replays_once(self):
        real_connect = sqlite3.connect
        for committed in (False, True):
            with self.subTest(committed=committed):
                path = self.root / ("old-proof-commit-" + str(committed))
                self.store = initialize(path)
                self.store.execute(command("create", self.spec))
                action = self.queue("query")
                def lost(payload):
                    raise TimeoutError()
                self.error("CALL_UNKNOWN", self.store.perform, action, self.spec["binding"], lost)
                self.store.execute(command("close_call", {"action_id": action, "evidence_ref": "demo:closed-query"}))
                self.store.execute(command("revise", {"run_id": "example", "expected_revision": 1, "plan": self.spec["plan"]}))
                before = self.store.get_run("example")
                history = self.store.get_revision("example", 1)
                proof = command("not_started", {"action_id": action, "evidence_ref": "demo:old-query-nonstart"})
                class Connection(sqlite3.Connection):
                    def commit(self):
                        if committed:
                            super().commit()
                        raise sqlite3.OperationalError("synthetic old query proof commit failure")
                with patch("control_production_plane.store.sqlite3.connect",
                           side_effect=lambda *a, **k: real_connect(*a, **k, factory=Connection)):
                    error = self.error("STORAGE_ERROR", self.store.execute, proof)
                self.assertEqual(error.commit_state, "unknown")
                self.store = open_store(path)
                current = self.store.get_run("example")
                self.assertEqual(current["counters"]["queries"], 0 if committed else 1)
                self.assertEqual(current["attempt"], before["attempt"])
                self.assertEqual(self.store.get_action(action)["state"], "NOT_SENT" if committed else "RESPONDED")
                self.assertEqual(self.store.execute(proof)["replayed"], committed)
                self.assertTrue(self.store.execute(proof)["replayed"])
                self.assertEqual(self.store.get_run("example")["counters"]["queries"], 0)
                self.assertEqual(self.store.get_revision("example", 1), history)

    def test_recovered_response_commit_failure_retains_atomic_history_and_readiness(self):
        real_connect = sqlite3.connect
        for committed in (False, True):
            with self.subTest(committed=committed):
                path = self.root / ("response-commit-" + str(committed))
                self.store = initialize(path)
                self.store.execute(command("create", self.spec))
                action = self.queue("prepare")
                def lost(payload):
                    raise TimeoutError()
                self.error("CALL_UNKNOWN", self.store.perform, action, self.spec["binding"], lost)
                old_action = self.store.get_action(action)
                old_run = self.store.get_run("example")
                reply = ready(old_action["payload"])
                request = command("respond", dict(reply, action_id=action))
                class Connection(sqlite3.Connection):
                    def commit(self):
                        if committed:
                            super().commit()
                        raise sqlite3.OperationalError("synthetic recovered response commit failure")
                with patch("control_production_plane.store.sqlite3.connect",
                           side_effect=lambda *a, **k: real_connect(*a, **k, factory=Connection)):
                    error = self.error("STORAGE_ERROR", self.store.execute, request)
                self.assertEqual(error.commit_state, "unknown")
                self.store = open_store(path)
                if committed:
                    self.assertEqual(self.store.get_run("example")["attempt"]["prepared_request"], reply["response"]["request"])
                    self.assertEqual(self.store.get_action(action)["response"], reply["response"])
                else:
                    self.assertEqual(self.store.get_run("example"), old_run)
                    self.assertEqual(self.store.get_action(action), old_action)
                result = self.store.execute(request)
                self.assertEqual(result["replayed"], committed)
                self.assertTrue(self.store.execute(request)["replayed"])
                self.assertEqual(self.store.get_run("example")["counters"], old_run["counters"])
                self.assertEqual(self.store.get_action(action)["response"], reply["response"])

    def test_duplicate_perform_and_forked_or_expired_lease_cannot_dispatch(self):
        action = self.prepare()
        calls = []
        self.error("PENDING", self.store.perform, action, self.spec["binding"], calls.append)
        queued = self.queue("submit")
        with self.store.executor() as lease:
            other = open_store(self.path)
            with self.assertRaises(ControlError) as cm:
                with other.executor():
                    pass
            self.assertEqual(cm.exception.code, "BUSY")
            child = os.fork()
            if child == 0:
                try:
                    self.store.execute(command("dispatch", {"action_id": queued, "observed_binding": self.spec["binding"]}), lease=lease)
                except ControlError as exc:
                    os._exit(0 if exc.code == "LEASE_REQUIRED" else 72)
                os._exit(73)
            _, status = os.waitpid(child, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.error("LEASE_REQUIRED", self.store.execute, command("dispatch", {"action_id": queued, "observed_binding": self.spec["binding"]}), lease=lease)
        self.assertEqual(calls, [])

    def test_other_run_waits_for_outstanding_execution(self):
        self.submit()
        self.store.execute(command("create", sample("second")))
        action = self.queue("prepare", "second")
        self.error("PENDING", self.store.perform, action, self.spec["binding"], ready)
        self.store.execute(command("observe", self.observation()))
        self.store.perform(action, self.spec["binding"], ready)

    def test_expired_prepared_request_and_wrong_observation_retain_original(self):
        action = self.queue("prepare")
        def expired(payload):
            result = ready(payload)
            request = result["response"]["request"]
            request["expires_at"] = int(time.time()) - 1
            request["request_digest"] = hashlib.sha256(canonical({k:v for k,v in request.items() if k != "request_digest"})).hexdigest()
            return result
        self.store.perform(action, self.spec["binding"], expired)
        submit = self.queue("submit")
        self.error("NOT_READY", self.store.perform, submit, self.spec["binding"], lambda p: {})
        self.prepare()  # same task may receive a new READY request before submission
        self.error("CONFLICT", self.store.perform, submit, self.spec["binding"], lambda p: {})
        submit = self.queue("submit")
        self.store.perform(submit, self.spec["binding"], lambda p: {"response": {"accepted": True}, "call_closed": True})
        observation = dict(self.observation(), operation_id=str(uuid.uuid4()))
        self.error("IDENTITY_CONFLICT", self.store.execute, command("observe", observation))
        self.assertIsNone(self.store.get_run("example")["attempt"]["observation"])

    def test_required_selected_knowledge_keeps_exact_version_and_admission(self):
        spec = sample("selected")
        selection = {"knowledge_id": "demo:knowledge", "version_id": "demo:v2", "content_sha256": "a"*64,
                     "admission_ref": "demo:review-v2", "scope": "Synthetic project only"}
        spec["plan"]["knowledge"] = {"required": True, "status": "SELECTED", "reason": "Explicit synthetic selection", "items": [selection]}
        self.store.execute(command("create", spec))
        self.assertEqual(self.store.get_run("selected")["plan"]["knowledge"]["items"], [selection])
        prepare = self.queue("prepare", "selected")
        self.store.perform(prepare, spec["binding"], ready)
        submit = self.queue("submit", "selected")
        self.error("NOT_READY", self.store.perform, submit, spec["binding"], lambda p: self.fail("Must not dispatch without K receipt"))
        self.assertEqual(self.store.get_run("selected")["counters"]["attempts"], 0)

    def test_late_missing_files_and_read_failure_are_structured(self):
        (self.path / "runs.sqlite").unlink()
        self.error("STORE_INVALID", self.store.get_run, "example")
        self.assertFalse((self.path / "runs.sqlite").exists())

    def test_required_knowledge_blocks_submit_but_optional_absence_does_not(self):
        spec = sample("required")
        spec["plan"]["knowledge"]["required"] = True
        self.store.execute(command("create", spec))
        action = self.queue("prepare", "required")
        self.store.perform(action, spec["binding"], ready)
        action = self.queue("submit", "required")
        self.error("NOT_READY", self.store.perform, action, spec["binding"], lambda p: {})
        self.assertEqual(self.store.get_run("required")["counters"]["attempts"], 0)
        self.submit()  # independent optional-knowledge run can proceed

    def test_knowledge_binding_and_learning_candidate_do_not_self_admit(self):
        state = self.store.get_run("example")
        body = {"run_id": "example", "attempt_id": state["attempt"]["attempt_id"], "candidate_id": "learning:one",
                "explanation": "Synthetic hypothesis", "evidence_refs": ["demo:original-observation"],
                "artifact_versions": [{"artifact_id": "demo:report", "version_id": "demo:report-v1"}]}
        self.store.execute(command("learning_candidate", body))
        item = self.store.get_candidate("learning:one")
        self.assertEqual(item["delivery"], "UNCONFIGURED")
        self.assertEqual(item["admission"], "NOT_REQUESTED")
        self.assertEqual(item["goal_digest"], state["goal_digest"])
        self.store.execute(command("learning_candidate", body))
        self.error("CONFLICT", self.store.execute, command("learning_candidate", dict(body, explanation="changed")))

    def test_pre_submission_plan_correction_does_not_spend_business_attempt(self):
        spec = sample("one-attempt")
        spec["budget"]["max_attempts"] = 1
        self.store.execute(command("create", spec))
        for revision in range(1, 4):
            previous = self.store.get_run("one-attempt")
            plan = dict(spec["plan"], content="print(" + str(revision) + ")\n")
            self.store.execute(command("revise", {"run_id": "one-attempt", "expected_revision": revision, "plan": plan}))
            self.assertEqual(self.store.get_revision("one-attempt", revision), previous)
        self.assertEqual(self.store.get_run("one-attempt")["counters"]["attempts"], 0)
        action = self.queue("prepare", "one-attempt")
        self.store.perform(action, spec["binding"], ready)
        action = self.queue("submit", "one-attempt")
        self.store.perform(action, spec["binding"], lambda p: {"response": {"accepted": True}, "call_closed": True})
        self.assertEqual(self.store.get_run("one-attempt")["counters"]["attempts"], 1)

    def test_transaction_failure_after_commit_returns_unknown_and_replay_recovers(self):
        real = sqlite3.connect
        class Connection(sqlite3.Connection):
            def commit(self):
                super().commit()
                raise sqlite3.OperationalError("simulated lost commit acknowledgement")
        req = command("cancel", {"run_id": "example", "reason": "atomic cancel"}, "cancel-atomic")
        with patch("control_production_plane.store.sqlite3.connect", side_effect=lambda *a, **k: real(*a, **k, factory=Connection)):
            error = self.error("STORAGE_ERROR", self.store.execute, req)
        self.assertEqual(error.commit_state, "unknown")
        self.assertTrue(self.store.execute(req)["replayed"])
        self.assertEqual(len(self.store.events("example")), 2)

    def test_limits_persist_across_reopen_and_queries_remain_independent(self):
        spec = sample("limited")
        spec["budget"].update(max_actions=1, max_queries=1)
        self.store.execute(command("create", spec))
        action = self.queue("prepare", "limited")
        self.store.perform(action, spec["binding"], ready)
        self.store = open_store(self.path)
        action = self.queue("prepare", "limited")
        self.error("LIMIT", self.store.perform, action, spec["binding"], ready)
        query = self.queue("query", "limited")
        self.store.perform(query, spec["binding"], lambda p: {"response": {"error": "NOT_FOUND"}, "call_closed": True})
        query = self.queue("query", "limited")
        self.error("LIMIT", self.store.perform, query, spec["binding"], lambda p: {})
        self.assertEqual(self.store.get_run("limited")["counters"]["queries"], 1)

    def test_process_crash_after_effect_keeps_one_effect_and_original_identity(self):
        self.prepare()
        action = self.queue("submit")
        marker = self.root / "one-external-effect"
        code = '''import json,os,sys
from pathlib import Path
from control_production_plane import open_store
s=open_store(sys.argv[1]);binding=s.get_run('example')['binding']
def invoke(payload):
 with Path(sys.argv[3]).open('x') as f:f.write(payload['operation_id'])
 os._exit(71)
s.perform(sys.argv[2],binding,invoke)
'''
        result = subprocess.run([sys.executable, "-c", code, str(self.path), action, str(marker)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 71, result.stderr)
        original = self.store.get_run("example")["attempt"]["operation_id"]
        self.assertEqual(marker.read_text(), original)
        restored = open_store(self.path)
        self.assertEqual(restored.recover("example")["next_step"], "QUERY_ORIGINAL_TASK")
        self.error("PENDING", restored.perform, action, self.spec["binding"], lambda p: marker.write_text("duplicate"))
        self.assertEqual(marker.read_text(), original)
        self.assertEqual(restored.get_run("example")["counters"]["attempts"], 1)

    def test_cancel_and_query_reachable_while_separate_executor_hangs(self):
        self.prepare()
        action = self.queue("submit")
        started, release = self.root / "started", self.root / "release"
        code = '''import sys,time
from pathlib import Path
from control_production_plane import open_store
s=open_store(sys.argv[1])
def invoke(payload):
 Path(sys.argv[3]).touch()
 deadline=time.monotonic()+10
 while not Path(sys.argv[4]).exists() and time.monotonic()<deadline:time.sleep(.02)
 return {'response':{'accepted':True},'call_closed':True}
s.perform(sys.argv[2],s.get_run('example')['binding'],invoke)
'''
        child = subprocess.Popen([sys.executable, "-c", code, str(self.path), action, str(started), str(release)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(started.exists())
            begin = time.monotonic()
            other = open_store(self.path)
            other.execute(command("cancel", {"run_id": "example", "reason": "Cancel while waiting"}))
            self.assertLess(time.monotonic() - begin, 2)
            with self.assertRaises(ControlError) as cm:
                with other.executor():
                    pass
            self.assertEqual(cm.exception.code, "BUSY")
            query = self.queue("query")
            other.perform(query, self.spec["binding"], lambda p: {"response": {"state": "RUNNING"}, "call_closed": True})
            cancel = self.queue("cancel")
            other.perform(cancel, self.spec["binding"], lambda p: {"response": {"requested": True}, "call_closed": True})
            self.assertTrue(other.get_run("example")["cancel"])
        finally:
            release.touch()
            stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, stderr)


if __name__ == "__main__":
    unittest.main()
