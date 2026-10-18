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
        self.store.perform(submit, spec["binding"], lambda p: {"response": {"accepted": True}, "call_closed": True})
        self.assertEqual(self.store.get_run("selected")["counters"]["attempts"], 1)

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
