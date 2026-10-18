"""Optional public K adapter; adoption/feedback intents live in the P journal.

One reviewed version per attempt. No P transaction is held across K calls.
The caller is trusted: this journal records adoption, never grants authority.
"""
import hashlib
import json
import re
from pathlib import Path

from . import model as m


def encoded(value):
    # K's public canonical request fingerprint uses UTF-8, unlike P's ASCII.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def context_value(value):
    """Validate the public K context contract before persisting an intent."""
    m.validate_tree(value)
    m.require(type(value) is dict and len(value) <= 16, "K context must have at most 16 fields")
    for name, item in value.items():
        m.require(re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name) is not None, "Invalid K context key")
        m.text(item, 256)
        m.require("\x00" not in item, "K context values cannot contain NUL")
    return json.loads(encoded(value))


def condition_scope(conditions):
    text = json.dumps(conditions, ensure_ascii=False, sort_keys=True)
    # K permits 16 * 256-byte values; P's human-readable scope is 2048 bytes.
    if len(text.encode()) > 2048:
        return "conditions-sha256:" + hashlib.sha256(encoded(conditions)).hexdigest()
    return text


def key(store_id, state, operation):
    return "pk:" + m.digest([store_id, state["run_id"], state["attempt"]["attempt_id"], operation])


def refs(store_id, state):
    return "p:" + store_id, "p-task:" + state["attempt"]["operation_id"]


def require_adopted(state):
    call = state.get("knowledge_calls", {}).get(state["attempt"].get("knowledge_use"))
    items = state["plan"]["knowledge"]["items"]
    m.require(call is not None and call["state"] == "COMMITTED" and len(items) == 1
              and call["attempt_id"] == state["attempt"]["attempt_id"]
              and call["request"]["body"]["version_id"] == items[0]["version_id"],
              "Selected knowledge requires its original K adoption receipt", "NOT_READY")
    return call


def historical(store, db, state, revision):
    if revision == state["revision"]:
        return state
    row = db.execute("SELECT document FROM revisions WHERE run_id=? AND revision=?", (state["run_id"], revision)).fetchone()
    m.require(row is not None, "Original plan revision missing", "NOT_FOUND")
    return m.decode(row[0].encode())


def feedback_material(store_id, state, usage, notes):
    m.text(notes, 4096)
    attempt = state["attempt"]
    observed = attempt["observation"]
    # An unresolved result is still reportable; never upgrade UNKNOWN to success.
    outcome = observed["outcome"] if observed else "UNKNOWN"
    facts = dict(run_id=state["run_id"], revision=state["revision"], attempt_id=attempt["attempt_id"],
                 operation_id=attempt["operation_id"], goal_digest=state["goal_digest"],
                 plan_sha256=m.digest(state["plan"]), submission_recorded=attempt["submission_recorded"],
                 observation=observed, candidate=attempt.get("candidate"), decision=attempt.get("decision"),
                 cancel=state["cancel"], maintenance_stop=attempt.get("maintenance_stop"), notes=notes)
    raw = encoded(facts)
    if len(raw) > 65536:
        # Preserve exact references to the immutable P candidate/decision; do
        # not send a valid P record larger than K's public material text bound.
        facts["snapshot_format"] = "bounded-references-v1"
        facts["full_snapshot_sha256"] = hashlib.sha256(raw).hexdigest()
        if facts["candidate"] is not None:
            candidate = facts["candidate"]
            facts["candidate"] = {k: candidate[k] for k in (
                "run_id", "attempt_id", "artifact_id", "version_id", "readback_ref", "receipt_sha256", "goal_digest", "revision")}
            facts["candidate"].update(document_sha256=m.digest(candidate), source_sha256=m.digest(candidate["source"]))
        if facts["decision"] is not None:
            decision = facts["decision"]
            facts["decision"] = dict(decision, document_sha256=m.digest(decision), checks=[
                dict(condition_sha256=hashlib.sha256(c["condition"].encode()).hexdigest(),
                     passed=c["passed"], evidence_ref=c["evidence_ref"]) for c in decision["checks"]])
        raw = encoded(facts)
    m.require(len(raw) <= 65536, "Feedback exceeds K material text limit", "LIMIT")
    return dict(material_id=key(store_id, state, "feedback-material"), source_ref=usage["consumer_ref"],
                task_ref=usage["task_ref"], context=usage["context"], claimed_outcome=outcome,
                gaps=["Acceptance is recorded separately from execution outcome; inspect the retained P facts."],
                text=raw.decode())


def record_intent(store, db, body):
    m.fields(body, "run_id revision knowledge_store_id request notes")
    m.identity(body["knowledge_store_id"])
    m.natural(body["revision"], high=100)
    state = store._load(db, body["run_id"])
    original = historical(store, db, state, body["revision"])
    req = body["request"]
    m.fields(req, "schema scope operation request_id body")
    m.require(req["schema"] == "operational-knowledge-v1", "Unsupported K schema")
    m.identity(req["scope"])
    op = req["operation"]
    m.require(op in ("start_use", "complete_use"), "Unsupported K operation")
    cid = key(store.identity["store_id"], original, op)
    m.require(req["request_id"] == cid, "K request must retain original attempt identity", "CONFLICT")
    calls = state.setdefault("knowledge_calls", {})
    record = dict(call_id=cid, revision=body["revision"], attempt_id=original["attempt"]["attempt_id"],
                  knowledge_store_id=body["knowledge_store_id"], request=req, notes=body["notes"], state="PENDING", receipt=None)
    if cid in calls:
        m.require(all(calls[cid][k] == v for k, v in record.items() if k not in ("state", "receipt")),
                  "Original K call binds different content", "CONFLICT")
        return dict(run_id=state["run_id"], call_id=cid)
    consumer, task = refs(store.identity["store_id"], original)
    value = req["body"]
    if op == "start_use":
        m.require(original["revision"] == state["revision"] and not state["cancel"]
                  and not original["attempt"]["submission_recorded"] and not original["attempt"].get("maintenance_stop"),
                  "Cannot retrospectively register knowledge for executed or replaced work", "NOT_READY")
        m.require(body["notes"] == "", "Adoption rationale belongs in the plan")
        k = original["plan"]["knowledge"]
        m.require(k["status"] == "SELECTED" and len(k["items"]) == 1, "Select one verified version before adoption")
        m.fields(value, "usage_id consumer_ref task_ref version_id context")
        m.require(value["usage_id"] == cid and value["consumer_ref"] == consumer and value["task_ref"] == task
                  and value["version_id"] == k["items"][0]["version_id"], "Knowledge use differs from original plan", "CONFLICT")
        context_value(value["context"])
        state["attempt"]["knowledge_use"] = cid
    else:
        use = calls.get(key(store.identity["store_id"], original, "start_use"))
        m.require(use is not None and use["state"] == "COMMITTED" and use["knowledge_store_id"] == body["knowledge_store_id"]
                  and use["request"]["scope"] == req["scope"], "Original committed knowledge use required", "NOT_READY")
        expected = dict(usage_id=use["request"]["body"]["usage_id"],
                        material=feedback_material(store.identity["store_id"], original, use["request"]["body"], body["notes"]))
        m.require(value == expected, "Feedback must preserve the observed original attempt", "CONFLICT")
    calls[cid] = record
    store._save(db, state)
    return dict(run_id=state["run_id"], call_id=cid)


def record_ack(store, db, body):
    m.fields(body, "run_id call_id receipt")
    state = store._load(db, body["run_id"])
    call = state.get("knowledge_calls", {}).get(body["call_id"])
    m.require(call is not None, "K intent missing", "NOT_FOUND")
    receipt, req = body["receipt"], call["request"]
    m.fields(receipt, "store_id scope operation request_id fingerprint recorded_at result")
    m.require(receipt["store_id"] == call["knowledge_store_id"]
              and all(receipt[k] == req[k] for k in ("scope", "operation", "request_id"))
              and receipt["fingerprint"] == hashlib.sha256(encoded(req)).hexdigest(), "K receipt does not bind original request", "IDENTITY_CONFLICT")
    result = receipt["result"]
    if req["operation"] == "start_use":
        m.require(result.get("usage_id") == req["body"]["usage_id"], "K usage identity differs", "IDENTITY_CONFLICT")
    else:
        m.require(result.get("material_id") == req["body"]["material"]["material_id"], "K material identity differs", "IDENTITY_CONFLICT")
        m.identity(result.get("feedback_id"))
    m.require(call["receipt"] is None or call["receipt"] == receipt, "Original K receipt changed", "CONFLICT")
    call.update(state="COMMITTED", receipt=receipt)
    store._save(db, state)
    return dict(run_id=state["run_id"], call_id=body["call_id"], state="COMMITTED")


class KnowledgeAdapter:
    def __init__(self, store, config):
        m.validate_tree(config)
        m.fields(config, "store_path store_id anchor scope")
        m.fields(config["anchor"], "scope operation request_id fingerprint")
        m.require(type(config["store_path"]) is str and Path(config["store_path"]).is_absolute(), "K path must be absolute")
        for field in ("store_id", "scope"):
            m.identity(config[field])
        m.sha(config["anchor"]["fingerprint"])
        self.store, self.config = store, json.loads(m.canonical(config))

    def _open(self):
        try:
            from domain_operational_knowledge_core import open_store
        except ImportError as exc:
            raise m.ControlError("DEPENDENCY_MISSING", "Install the configured public K package") from exc
        k = open_store(self.config["store_path"])
        a = self.config["anchor"]
        r = k.get_operation(a["scope"], a["operation"], a["request_id"])["receipt"]
        m.require(r["store_id"] == self.config["store_id"] and r["fingerprint"] == a["fingerprint"],
                  "Configured K store differs from its retained public receipt", "IDENTITY_CONFLICT")
        return k

    def query(self, kind, context, *, after="", limit=10):
        return self._open().query(kind, context_value(context), after=after, limit=limit)

    def selection(self, version_id, context):
        context = context_value(context)
        value = self._open().get("version", version_id)
        v = value["record"]
        m.require(v["state"] == "ACTIVE" and all(context.get(k) == x for k, x in v["conditions"].items())
                  and all(x["status"] == "VERIFIED" for x in value["materials"]), "Knowledge unavailable for new adoption", "NOT_READY")
        item = dict(knowledge_id=v["lineage_id"], version_id=v["version_id"], content_sha256=hashlib.sha256(v["content"].encode()).hexdigest(),
                    admission_ref=v["review_id"], scope=condition_scope(v["conditions"]))
        return dict(item=item, content=v["content"], conditions=v["conditions"])

    def _command(self, op, body, suffix):
        return dict(schema="production-plane-v1", request_id="pk:" + m.digest([op, body, suffix]), operation=op, body=body)

    def _intent(self, original, operation, value, notes, lease):
        req = dict(schema="operational-knowledge-v1", scope=self.config["scope"], operation=operation,
                   request_id=key(self.store.identity["store_id"], original, operation), body=value)
        body = dict(run_id=original["run_id"], revision=original["revision"], knowledge_store_id=self.config["store_id"], request=req, notes=notes)
        self.store.execute(self._command("knowledge_intent", body, "intent"), lease=lease)
        return req["request_id"]

    def adopt(self, run_id, context):
        context = context_value(context)
        with self.store.executor() as lease:
            state = self.store.get_run(run_id)
            cid = key(self.store.identity["store_id"], state, "start_use")
            old = state.get("knowledge_calls", {}).get(cid)
            if old:
                m.require(old["request"]["body"]["context"] == context, "Original use has another context", "CONFLICT")
                return self._resume(run_id, cid)
            k = state["plan"]["knowledge"]
            m.require(k["status"] == "SELECTED" and len(k["items"]) == 1, "Select one knowledge version")
            selected = self.selection(k["items"][0]["version_id"], context)
            m.require(selected["item"] == k["items"][0], "Plan knowledge differs from verified public version", "CONFLICT")
            consumer, task = refs(self.store.identity["store_id"], state)
            cid = self._intent(state, "start_use", dict(usage_id=cid, consumer_ref=consumer, task_ref=task,
                                version_id=k["items"][0]["version_id"], context=context), "", lease)
            return self._resume(run_id, cid)

    def feedback(self, run_id, revision, notes):
        with self.store.executor() as lease:
            state = self.store.get_revision(run_id, revision)
            current = self.store.get_run(run_id)
            use = current.get("knowledge_calls", {}).get(key(self.store.identity["store_id"], state, "start_use"))
            m.require(use is not None and use["state"] == "COMMITTED", "Original committed use required", "NOT_READY")
            cid = key(self.store.identity["store_id"], state, "complete_use")
            old = current.get("knowledge_calls", {}).get(cid)
            if old:
                m.require(old["notes"] == notes, "Original feedback differs; resume it before any correction", "CONFLICT")
                return self._resume(run_id, cid)
            value = dict(usage_id=use["request"]["body"]["usage_id"], material=feedback_material(self.store.identity["store_id"], state, use["request"]["body"], notes))
            cid = self._intent(state, "complete_use", value, notes, lease)
            return self._resume(run_id, cid)

    def resume(self, run_id, call_id):
        with self.store.executor():
            return self._resume(run_id, call_id)

    def _resume(self, run_id, call_id):
        from domain_operational_knowledge_core import KnowledgeError
        state = self.store.get_run(run_id)
        call = state.get("knowledge_calls", {}).get(call_id)
        m.require(call is not None, "Original K call missing", "NOT_FOUND")
        m.require(call["knowledge_store_id"] == self.config["store_id"] and call["request"]["scope"] == self.config["scope"],
                  "Original K destination changed", "IDENTITY_CONFLICT")
        k, req = self._open(), call["request"]
        try:
            receipt = k.get_operation(req["scope"], req["operation"], req["request_id"])["receipt"]
        except KnowledgeError as exc:
            if exc.code != "NOT_FOUND":
                raise
            # The read above may block while another caller cancels/revises P.
            # Re-read after it, before considering any new K write. A later
            # cancellation can still race with the K call; P submission keeps
            # its own transactional cancellation barrier.
            current = self.store.get_run(run_id)
            m.require(current.get("knowledge_calls", {}).get(call_id) == call,
                      "Original K call changed during lookup; inspect it again", "CONFLICT")
            state = current
            # A lost K store must never silently recreate a committed call.
            m.require(call["state"] != "COMMITTED", "Committed K receipt is missing", "IDENTITY_CONFLICT")
            if req["operation"] == "start_use":
                m.require(call["attempt_id"] == state["attempt"]["attempt_id"] and not state["cancel"]
                          and not state["attempt"]["submission_recorded"], "No new adoption for replaced or cancelled work", "NOT_READY")
            receipt = k.execute(encoded(req))["receipt"]
        body = dict(run_id=run_id, call_id=call_id, receipt=receipt)
        return self.store.execute(self._command("knowledge_ack", body, "ack"))
