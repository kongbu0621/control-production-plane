"""Transactional control records and a process lease, separate from external I/O."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid

from . import model as m
from .model import ControlError

APP_ID = 0x43505031
SCHEMA = "production-plane-store-v1"
SQL = """
CREATE TABLE metadata (store_id TEXT PRIMARY KEY, schema TEXT NOT NULL);
CREATE TABLE runs (run_id TEXT PRIMARY KEY, document TEXT NOT NULL);
CREATE TABLE revisions (run_id TEXT NOT NULL, revision INTEGER NOT NULL, document TEXT NOT NULL,
 PRIMARY KEY(run_id,revision), FOREIGN KEY(run_id) REFERENCES runs(run_id));
CREATE TABLE actions (action_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, document TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(run_id));
CREATE TABLE receipts (request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
 operation TEXT NOT NULL, body TEXT NOT NULL, recorded_at INTEGER NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(run_id));
CREATE TABLE candidates (candidate_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, document TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(run_id));
CREATE INDEX action_run ON actions(run_id);
CREATE INDEX event_run ON events(run_id, seq);
"""


def _private(path, directory=False):
    info = path.lstat()
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    m.require(valid and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
              "Control files must be private and owned by the caller", "STORE_INVALID")
    if not directory:
        m.require(info.st_nlink == 1, "Control file has multiple links", "STORE_INVALID")
    return info.st_dev, info.st_ino


def _fsync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def initialize(directory):
    """Create a new private store. Existing and incomplete directories are retained."""
    root = Path(directory).absolute()
    try:
        root.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ControlError("ALREADY_EXISTS", "Initialization requires an absent directory") from exc
    store_id = str(uuid.uuid4())
    try:
        for name in ("runs.sqlite", "executor.lock"):
            fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        with sqlite3.connect(root / "runs.sqlite") as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute(f"PRAGMA application_id={APP_ID}")
            db.execute("PRAGMA user_version=1")
            db.executescript(SQL)
            db.execute("INSERT INTO metadata VALUES (?, ?)", (store_id, SCHEMA))
        raw = m.canonical({"schema": SCHEMA, "store_id": store_id})
        fd = os.open(root / "identity.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync(root)
        _fsync(root.parent)
    except (OSError, sqlite3.Error) as exc:
        raise ControlError("STORAGE_ERROR", "Initialization incomplete; retain directory and inspect",
                           commit_state="unknown") from exc
    return open_store(root)


def open_store(directory):
    return ControlStore(directory)


class _Lease:
    def __init__(self, store, fd):
        self.store, self.fd, self.pid, self.active = store, fd, os.getpid(), True

    def verify(self, store):
        m.require(self.active and self.pid == os.getpid() and self.store is store,
                  "An active executor lease from this handle is required", "LEASE_REQUIRED")
        m.require((os.fstat(self.fd).st_dev, os.fstat(self.fd).st_ino) == store.identities["executor.lock"],
                  "Executor lock identity changed", "STORE_INVALID")


class ControlStore:
    def __init__(self, directory):
        self.root = Path(directory).absolute()
        self.identities = {}
        try:
            self.identities["."] = _private(self.root, directory=True)
            for name in ("runs.sqlite", "identity.json", "executor.lock"):
                self.identities[name] = _private(self.root / name)
            with (self.root / "identity.json").open("rb") as stream:
                self.identity = m.decode(stream.read(m.MAX_JSON + 1))
            m.fields(self.identity, "schema store_id")
            m.require(self.identity["schema"] == SCHEMA, "Unsupported store schema", "STORE_INVALID")
            m.uuid4(self.identity["store_id"])
            with self._connection() as db:
                m.require(db.execute("PRAGMA application_id").fetchone()[0] == APP_ID
                          and db.execute("PRAGMA user_version").fetchone()[0] == 1,
                          "Unsupported database format", "STORE_INVALID")
                m.require(db.execute("SELECT store_id, schema FROM metadata").fetchall()
                          == [(self.identity["store_id"], SCHEMA)], "Store identity mismatch", "STORE_INVALID")
                m.require(db.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok",
                          "Database integrity check failed", "STORE_INVALID")
        except (OSError, sqlite3.Error) as exc:
            raise ControlError("STORE_INVALID", "Store missing, inaccessible or damaged; not initialized automatically") from exc

    def _check_identity(self):
        try:
            for name, expected in self.identities.items():
                m.require(_private(self.root / name, directory=name == ".") == expected,
                          "Open store files were replaced", "STORE_INVALID")
        except OSError as exc:
            raise ControlError("STORE_INVALID", "Open store files are no longer accessible") from exc

    @contextmanager
    def _connection(self):
        self._check_identity()
        db = None
        try:
            db = sqlite3.connect((self.root / "runs.sqlite").as_uri() + "?mode=rw", uri=True,
                                 timeout=1, isolation_level=None)
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            if db is not None:
                db.close()

    @contextmanager
    def executor(self):
        """One executor per store. Does not hold a SQLite transaction across I/O."""
        self._check_identity()
        fd = os.open(self.root / "executor.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        lease = _Lease(self, fd)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ControlError("BUSY", "Another executor holds this store") from exc
            lease.verify(self)
            yield lease
        finally:
            lease.active = False
            os.close(fd)

    @staticmethod
    def _load(db, run_id):
        m.identity(run_id)
        row = db.execute("SELECT document FROM runs WHERE run_id=?", (run_id,)).fetchone()
        m.require(row is not None, "Run not found", "NOT_FOUND")
        return m.decode(row[0].encode("ascii"))

    @staticmethod
    def _action(db, action_id):
        m.uuid4(action_id)
        row = db.execute("SELECT document FROM actions WHERE action_id=?", (action_id,)).fetchone()
        m.require(row is not None, "Action not found", "NOT_FOUND")
        return m.decode(row[0].encode("ascii"))

    @staticmethod
    def _save(db, state):
        from .knowledge import validate_receipt_capacity
        m.validate_tree(state)
        validate_receipt_capacity(state)
        db.execute("UPDATE runs SET document=? WHERE run_id=?", (m.canonical(state).decode(), state["run_id"]))

    @staticmethod
    def _save_action(db, action):
        m.validate_tree(action)
        db.execute("UPDATE actions SET document=? WHERE action_id=?", (m.canonical(action).decode(), action["action_id"]))

    @staticmethod
    def _pending(db, run_id):
        result = []
        for row in db.execute("SELECT document FROM actions WHERE run_id=?", (run_id,)):
            action = m.decode(row[0].encode("ascii"))
            if action["state"] == "MAY_HAVE_RUN" or (action["state"] == "RESPONDED" and not action["call_closed"]):
                result.append(action)
        return result

    @staticmethod
    def _prepared_from_history(db, state):
        """Read the latest effective preparation in dispatch order, not queue order.

        Legacy stores can retain a READY cache after a later non-ready response.
        Only proved non-starts may be skipped; unknown calls remain barriers.
        """
        rows = db.execute("""SELECT json_extract(e.body,'$.action_id'), a.run_id, a.document
            FROM events e LEFT JOIN actions a
            ON a.action_id=json_extract(e.body,'$.action_id')
            WHERE e.run_id=? AND e.operation='dispatch' ORDER BY e.seq DESC""",
            (state["run_id"],))
        for action_id, run_id, document in rows:
            # An inner join or filtering by action fields can hide a broken
            # link and make an older READY appear current. Check before filtering.
            m.require(document is not None and run_id == state["run_id"],
                      "Preparation history has a missing or conflicting action", "STORE_INVALID")
            try:
                action = m.decode(document.encode("ascii"))
            except (ControlError, UnicodeError, AttributeError) as exc:
                raise ControlError("STORE_INVALID", "Preparation history action cannot be decoded") from exc
            m.require(isinstance(action, dict) and action.get("action_id") == action_id
                      and action.get("run_id") == run_id
                      and isinstance(action.get("attempt_id"), str)
                      and isinstance(action.get("kind"), str) and action["kind"] in m.KINDS,
                      "Preparation history action identity is inconsistent", "STORE_INVALID")
            queued = db.execute("""SELECT body FROM events WHERE run_id=? AND operation='queue'
                AND json_extract(body,'$.action_id')=? LIMIT 2""", (run_id, action_id)).fetchall()
            original = {key: action[key] for key in ("run_id", "attempt_id", "action_id", "kind")}
            if "retry_failed" in action:
                original["retry_failed"] = action["retry_failed"]
            m.require(len(queued) == 1 and json.loads(queued[0][0]) == original,
                      "Preparation history differs from original queued identity", "STORE_INVALID")
            if action["attempt_id"] != state["attempt"]["attempt_id"] or action["kind"] != "prepare":
                continue
            response = action.get("response")
            m.require(action.get("state") in ("MAY_HAVE_RUN", "RESPONDED", "NOT_SENT")
                      and type(action.get("call_closed")) is bool,
                      "Preparation history dispatch state is invalid", "STORE_INVALID")
            m.require(action["state"] != "RESPONDED" or isinstance(response, dict),
                      "Preparation history response is invalid", "STORE_INVALID")
            if (action["state"] == "NOT_SENT" and action["call_closed"]
                    and isinstance(response, dict) and response.get("local_failure") == "CALL_NOT_STARTED"):
                m.identity(response.get("evidence_ref"))
                continue
            if action["state"] == "RESPONDED" and response.get("state") == "READY":
                request = response.get("request")
                m.prepared(request, state)
                return request
            return None
        return None

    def _current_prepared(self, db, state):
        request = state["attempt"]["prepared_request"]
        m.require(request is not None, "No prepared request", "NOT_READY")
        m.require(self._prepared_from_history(db, state) == request,
                  "Prepared request is not backed by current preparation history", "NOT_READY")
        return request

    @contextmanager
    def _reading(self):
        try:
            with self._connection() as db:
                yield db
        except sqlite3.Error as exc:
            raise ControlError("STORE_INVALID", "Persistent record cannot be read",
                               commit_state="not_applicable") from exc

    def get_run(self, run_id):
        with self._reading() as db:
            return self._load(db, run_id)

    def get_action(self, action_id):
        with self._reading() as db:
            return self._action(db, action_id)

    def get_revision(self, run_id, revision):
        m.natural(revision, high=100)
        with self._reading() as db:
            db.execute("BEGIN")
            state = self._load(db, run_id)
            if state["revision"] == revision:
                return state
            row = db.execute("SELECT document FROM revisions WHERE run_id=? AND revision=?", (run_id, revision)).fetchone()
            m.require(row is not None, "Plan revision not found", "NOT_FOUND")
            return m.decode(row[0].encode("ascii"))

    def get_candidate(self, candidate_id):
        m.identity(candidate_id)
        with self._reading() as db:
            row = db.execute("SELECT document FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            m.require(row is not None, "Candidate not found", "NOT_FOUND")
            return m.decode(row[0].encode("ascii"))

    def perform(self, action_id, observed_binding, invoke, *, preflight=None, knowledge=None):
        """Call a trusted adapter once, after durable dispatch; never auto-redeliver.

        invoke(payload) returns {response: object, call_closed: bool}. The adapter
        must prove closure of any child process and both streams before reporting
        call_closed. It must enforce its own bounded I/O and full remote schema.
        """
        action = self.get_action(action_id)
        if action["kind"] in ("query", "cancel"):
            return self._perform(action_id, observed_binding, invoke, preflight, None, knowledge)
        with self.executor() as lease:
            return self._perform(action_id, observed_binding, invoke, preflight, lease, knowledge)

    def _perform(self, action_id, observed_binding, invoke, preflight, lease, knowledge):
        action = self.get_action(action_id)
        m.require(action["state"] == "QUEUED", "Original action already passed the dispatch boundary", "PENDING")
        check = None
        try:
            if preflight is not None:
                preflight(json.loads(m.canonical(action["payload"])))
            if action['kind'] == 'submit':
                state = self.get_run(action['run_id'])
                if state['plan']['knowledge']['status'] == 'SELECTED':
                    from .knowledge import KnowledgeAdapter, _ExecutionCheck
                    m.require(isinstance(knowledge, KnowledgeAdapter) and knowledge.store is self,
                              'Selected knowledge requires a configured current K check', 'NOT_READY')
                    check = _ExecutionCheck(self, action_id, state, knowledge.check_execution(state))
        except ControlError as exc:
            self.execute({"schema": "production-plane-v1", "request_id": str(uuid.uuid4()),
                          "operation": "preflight_failure", "body": {"action_id": action_id, "code": exc.code}})
            raise
        receipt = self.execute({"schema": "production-plane-v1", "request_id": "dispatch:" + action_id,
                                "operation": "dispatch", "body": {"action_id": action_id, "observed_binding": observed_binding}},
                               lease=lease, _knowledge_check=check)
        m.require(not receipt["replayed"], "Dispatch receipt is historical, not permission to invoke", "PENDING")
        try:
            try:
                result = invoke(json.loads(m.canonical(action["payload"])))
            except m.CallNotStarted as exc:
                self.execute({"schema": "production-plane-v1", "request_id": "not-started:" + action_id,
                              "operation": "not_started", "body": {"action_id": action_id, "evidence_ref": exc.evidence_ref}})
                raise
            except Exception as exc:
                self.execute({"schema": "production-plane-v1", "request_id": "response:" + action_id,
                              "operation": "respond", "body": {"action_id": action_id,
                              "response": {"transport_error": type(exc).__name__}, "call_closed": False}})
                raise ControlError("CALL_UNKNOWN", "Adapter failed; query original task and retain call gap",
                                   commit_state="committed") from exc
            m.validate_tree(result)
            m.fields(result, "response call_closed")
            return self.execute({"schema": "production-plane-v1", "request_id": "response:" + action_id,
                                 "operation": "respond", "body": dict(result, action_id=action_id)})
        except ControlError as exc:
            # This step may fail before/after its own commit, but dispatch has
            # already committed. Preserve that distinction on the public error.
            exc.action_id = action_id
            exc.dispatch_recorded = True
            raise

    def events(self, run_id, *, after=0, limit=10):
        m.natural(after, low=0, high=9007199254740991)
        m.natural(limit, high=10)
        with self._reading() as db:
            self._load(db, run_id)
            rows = db.execute("SELECT seq,operation,body,recorded_at FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                              (run_id, after, limit)).fetchall()
            return [{"sequence": r[0], "operation": r[1], "body": json.loads(r[2]), "recorded_at": r[3]} for r in rows]

    def recover(self, run_id):
        """Read-only recovery advice. Never invokes or replays an external operation."""
        with self._reading() as db:
            db.execute("BEGIN")
            state = self._load(db, run_id)
            pending = self._pending(db, run_id)
            queued = [r[0] for r in db.execute("SELECT action_id FROM actions WHERE run_id=? AND json_extract(document,'$.state')='QUEUED'", (run_id,))]
            attempt = state["attempt"]
            if attempt.get("maintenance_stop"):
                next_step = "REVIEW_STOPPED_RESULT_GAPS"
            elif attempt["observation"] and attempt["observation"]["exited"]:
                next_step = "REVIEW_OBSERVED_RESULT"
            elif attempt["submission_recorded"]:
                next_step = "QUERY_ORIGINAL_TASK"
            elif state["cancel"]:
                next_step = "NO_NEW_SUBMISSION"
            elif pending:
                next_step = "RECONCILE_ORIGINAL_PREPARATION"
            else:
                next_step = "CONTINUE_UNSENT_PLAN"
            return {"run": state, "next_step": next_step,
                    "pending_calls": [{"action_id": a["action_id"], "kind": a["kind"], "state": a["state"]} for a in pending],
                    "queued_actions": queued, "pending_knowledge_calls": [k for k, v in state.get("knowledge_calls", {}).items() if v["state"] == "PENDING"],
                    "external_calls_made": 0}

    def execute(self, request, *, lease=None, _knowledge_check=None):
        """Commit a bounded local command; receipts and domain changes are atomic."""
        if type(request) is bytes:
            request = m.decode(request)
        m.validate_tree(request)
        request = json.loads(m.canonical(request))  # detach caller-owned mutable objects
        m.fields(request, "schema request_id operation body")
        m.require(request["schema"] == "production-plane-v1", "Unsupported command schema")
        m.identity(request["request_id"])
        operation = request["operation"]
        handlers = {"create": self._create, "cancel": self._cancel, "revise": self._revise,
                    "queue": self._queue, "dispatch": self._dispatch, "respond": self._respond,
                    "close_call": self._close_call, "observe": self._observe, "abandon": self._abandon,
                    "learning_candidate": self._learning, "preflight_failure": self._preflight_failure,
                    "not_started": self._not_started, "candidate": self._candidate,
                    "decide": self._decide, "confirm_not_accepted": self._confirm_not_accepted,
                    "confirm_stopped": self._confirm_stopped,
                    "knowledge_intent": self._knowledge_intent, "knowledge_ack": self._knowledge_ack}
        m.require(type(operation) is str and operation in handlers, "Unknown operation")
        fingerprint = m.digest(request)
        committing = False
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute("SELECT fingerprint,result FROM receipts WHERE request_id=?", (request["request_id"],)).fetchone()
                if old:
                    m.require(old[0] == fingerprint, "Request ID already binds different content", "CONFLICT")
                    db.rollback()
                    return {"replayed": True, "receipt": json.loads(old[1])}
                size = db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
                m.require(size + 8 * m.MAX_JSON <= m.MAX_STORE, "Control store limit reached", "LIMIT")
                result = (self._dispatch(db, request['body'], lease, _knowledge_check) if operation == 'dispatch'
                          else handlers[operation](db, request["body"], lease))
                run_id = result["run_id"]
                now = int(time.time())
                seq = db.execute("INSERT INTO events(run_id,operation,body,recorded_at) VALUES (?,?,?,?)",
                                 (run_id, operation, m.canonical(request["body"]).decode(), now)).lastrowid
                receipt = {"store_id": self.identity["store_id"], "request_id": request["request_id"],
                           "operation": operation, "recorded_at": now, "event_sequence": seq, "result": result}
                db.execute("INSERT INTO receipts VALUES (?,?,?)", (request["request_id"], fingerprint, m.canonical(receipt).decode()))
                committing = True
                db.commit()
                return {"replayed": False, "receipt": receipt}
        except sqlite3.OperationalError as exc:
            code = "BUSY" if "locked" in str(exc).lower() else "STORAGE_ERROR"
            raise ControlError(code, "Local transaction could not be confirmed; retain request ID",
                               commit_state="unknown" if committing else "not_committed") from exc
        except sqlite3.Error as exc:
            raise ControlError("STORE_INVALID", "Invalid persistent state",
                               commit_state="unknown" if committing else "not_committed") from exc

    @staticmethod
    def _new_attempt(revision):
        return {"attempt_id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "revision": revision,
                "prepared_request": None, "submission_recorded": False, "observation": None}

    def _knowledge_intent(self, db, body, lease):
        from .knowledge import record_intent
        m.require(isinstance(lease, _Lease), "Executor lease required", "LEASE_REQUIRED")
        lease.verify(self)
        return record_intent(self, db, body)

    def _knowledge_ack(self, db, body, lease):
        from .knowledge import record_ack
        return record_ack(self, db, body)

    def _create(self, db, body, lease):
        m.specification(body)
        m.require(db.execute("SELECT 1 FROM runs WHERE run_id=?", (body["run_id"],)).fetchone() is None,
                  "Run already exists", "CONFLICT")
        state = dict(body, revision=1, goal_digest=m.digest(body["goal"]), cancel=None,
                     attempt=self._new_attempt(1), counters={"attempts": 0, "actions": 0, "queries": 0, "cancellations": 0})
        m.validate_tree(state)
        db.execute("INSERT INTO runs VALUES (?,?)", (body["run_id"], m.canonical(state).decode()))
        return {"run_id": state["run_id"], "attempt_id": state["attempt"]["attempt_id"]}

    def _cancel(self, db, body, lease):
        m.fields(body, "run_id reason")
        m.text(body["reason"], 2048)
        state = self._load(db, body["run_id"])
        if not state["cancel"]:
            state["cancel"] = {"reason": body["reason"], "recorded_at": int(time.time())}
            self._save(db, state)
        return {"run_id": body["run_id"], "cancel_requested": True}

    def _revise(self, db, body, lease):
        m.fields(body, "run_id expected_revision plan")
        m.plan(body["plan"])
        state = self._load(db, body["run_id"])
        m.natural(body["expected_revision"], high=100)
        m.require(body["expected_revision"] == state["revision"], "Plan revision changed", "CONFLICT")
        observation = state["attempt"]["observation"]
        revisable = (not state["attempt"]["submission_recorded"]
                     and not state["attempt"].get("maintenance_stop")) or (
            observation and observation["exited"] and (observation["outcome"] == "FAILED" or (observation["outcome"] == "SUCCEEDED"
            and state["attempt"].get("decision", {}).get("decision") == "REJECT")))
        m.require(not state["cancel"] and revisable,
                  "Revise only before submission or after exited failure or rejected candidate", "NOT_READY")
        m.require(not self._pending(db, body["run_id"]), "Old calls remain unclosed", "PENDING")
        m.require(state["revision"] < 100, "Plan revision limit reached", "LIMIT")
        m.require(state["counters"]["attempts"] < state["budget"]["max_attempts"], "Business attempt budget exhausted", "LIMIT")
        db.execute("INSERT INTO revisions VALUES (?,?,?)", (body["run_id"], state["revision"], m.canonical(state).decode()))
        state["revision"] += 1
        state["plan"] = body["plan"]
        state["attempt"] = self._new_attempt(state["revision"])
        self._save(db, state)
        return {"run_id": body["run_id"], "revision": state["revision"], "attempt_id": state["attempt"]["attempt_id"]}

    def _queue(self, db, body, lease):
        fields = "run_id attempt_id action_id kind"
        m.fields(body, fields + (" retry_failed" if isinstance(body, dict) and "retry_failed" in body else ""))
        if "retry_failed" in body:
            m.require(body["kind"] == "prepare" and body["retry_failed"] is True,
                      "retry_failed is only valid for preparation")
        m.uuid4(body["action_id"])
        m.require(type(body["kind"]) is str and body["kind"] in m.KINDS, "Unknown action kind")
        state = self._load(db, body["run_id"])
        attempt = state["attempt"]
        m.require(body["attempt_id"] == attempt["attempt_id"], "Attempt changed", "CONFLICT")
        m.require(db.execute("SELECT 1 FROM actions WHERE action_id=?", (body["action_id"],)).fetchone() is None,
                  "Action already exists", "CONFLICT")
        count = db.execute("SELECT count(*) FROM actions WHERE run_id=?", (body["run_id"],)).fetchone()[0]
        m.require(count < 3000, "Action record limit reached", "LIMIT")
        kind = body["kind"]
        if kind in ("prepare", "submit"):
            m.require(not state["cancel"] and not attempt["submission_recorded"] and not attempt.get("maintenance_stop"),
                      "New submission prohibited", "NOT_READY")
        if kind == "prepare":
            payload = {"operation_id": attempt["operation_id"], "kind": "workspace.edit",
                       "profile_ref": state["binding"]["profile_ref"], "inputs": m.inputs(state["plan"])}
            if "retry_failed" in body:
                payload["retry_failed"] = body["retry_failed"]
        elif kind == "submit":
            payload = self._current_prepared(db, state)
        elif kind == "cancel":
            m.require(state["cancel"] and attempt["submission_recorded"], "Cancellation requires a submitted attempt and local intent", "NOT_READY")
            payload = {"operation_id": attempt["operation_id"], "expected_request_digest": attempt["prepared_request"]["request_digest"], "target": {"kind": "job"}}
        else:
            payload = {"operation_id": attempt["operation_id"]}
        action = dict(body, payload=payload, state="QUEUED", call_closed=False, response=None)
        db.execute("INSERT INTO actions VALUES (?,?,?)", (body["action_id"], body["run_id"], m.canonical(action).decode()))
        return {"run_id": body["run_id"], "action_id": body["action_id"]}

    def _dispatch(self, db, body, lease, knowledge_check=None):
        m.fields(body, "action_id observed_binding")
        action = self._action(db, body["action_id"])
        state = self._load(db, action["run_id"])
        m.binding(body["observed_binding"])
        m.require(body["observed_binding"] == state["binding"], "Executor or archive binding differs", "IDENTITY_CONFLICT")
        m.require(action["state"] == "QUEUED", "Action cannot be redelivered", "CONFLICT")
        m.require(action["attempt_id"] == state["attempt"]["attempt_id"], "Action belongs to an older attempt", "CONFLICT")
        kind = action["kind"]
        if kind in ("prepare", "submit"):
            m.require(isinstance(lease, _Lease), "Executor lease required", "LEASE_REQUIRED")
            lease.verify(self)
        if kind in ("prepare", "submit"):
            m.require(not state["cancel"] and not state["attempt"]["submission_recorded"]
                      and not state["attempt"].get("maintenance_stop"), "New submission prohibited", "NOT_READY")
            m.require(time.time() < state["budget"]["deadline_utc"], "New execution deadline expired", "LIMIT")
            m.require(not [a for a in self._pending(db, state["run_id"]) if a["kind"] != "query"], "Original call is not closed", "PENDING")
            for other_id, document in db.execute("SELECT run_id,document FROM runs WHERE run_id!=?", (state["run_id"],)):
                other = json.loads(document)
                observed = other["attempt"]["observation"]
                m.require(not other["attempt"]["submission_recorded"] or (observed and observed["exited"])
                          or self._stopped_binding(other),
                          "Another run has an unresolved execution", "PENDING")
                m.require(not [a for a in self._pending(db, other_id) if a["kind"] != "query"],
                          "Another run has an unresolved call", "PENDING")
        if kind == "prepare":
            # A new preparation must establish readiness again. Prior READY
            # requests remain in their action records, never as current grants.
            state["attempt"]["prepared_request"] = None
        if kind == "cancel":
            attempt = state["attempt"]
            m.require(state["cancel"] and attempt["submission_recorded"],
                      "No original submission remains to cancel", "NOT_READY")
            expected = {"operation_id": attempt["operation_id"],
                        "expected_request_digest": attempt["prepared_request"]["request_digest"],
                        "target": {"kind": "job"}}
            m.require(action["payload"] == expected, "Cancellation no longer matches original submission", "CONFLICT")
        if kind == "submit":
            m.require(action["payload"] == state["attempt"]["prepared_request"], "Prepared revision changed", "CONFLICT")
            self._current_prepared(db, state)
            m.require(time.time() < action["payload"]["expires_at"], "Prepared request expired", "NOT_READY")
            k = state["plan"]["knowledge"]
            m.require(not k["required"] or k["status"] == "SELECTED", "Required knowledge unavailable", "NOT_READY")
            if k["status"] == "SELECTED":
                from .knowledge import require_adopted, _ExecutionCheck
                require_adopted(state)
                m.require(isinstance(knowledge_check, _ExecutionCheck),
                          'Fresh knowledge check required; dispatch through perform()', 'NOT_READY')
                action['knowledge_check'] = knowledge_check.consume(self, action['action_id'], state)
            self._spend(state, "attempts", "max_attempts")
            state["attempt"]["submission_recorded"] = True
        counter, bound = {"query": ("queries", "max_queries"), "cancel": ("cancellations", "max_cancellations")}.get(kind, ("actions", "max_actions"))
        self._spend(state, counter, bound)
        action["state"] = "MAY_HAVE_RUN"
        self._save_action(db, action)
        self._save(db, state)
        return {"run_id": state["run_id"], "action_id": action["action_id"], "state": action["state"]}

    @staticmethod
    def _spend(state, counter, bound):
        m.require(state["counters"][counter] < state["budget"][bound], "Persistent call budget exhausted", "LIMIT")
        state["counters"][counter] += 1

    @staticmethod
    def _awaiting_response(action):
        # perform records an adapter exception in this reserved envelope. It
        # preserves a transport failure, not a confirmed remote response.
        response = action["response"]
        return action["state"] == "MAY_HAVE_RUN" or (
            action["state"] == "RESPONDED" and isinstance(response, dict)
            and set(response) == {"transport_error"} and type(response["transport_error"]) is str)

    def _respond(self, db, body, lease):
        m.fields(body, "action_id response call_closed")
        m.require(type(body["response"]) is dict and type(body["call_closed"]) is bool, "Invalid response observation")
        action = self._action(db, body["action_id"])
        state = self._load(db, action["run_id"])
        m.require(self._awaiting_response(action), "Action already has a confirmed response", "CONFLICT")
        m.require(not action["call_closed"] or body["call_closed"], "Retained call closure cannot be undone", "CONFLICT")
        m.require(len(m.canonical(body["response"])) <= state["budget"]["max_response_bytes"], "Response exceeds budget", "LIMIT")
        is_ready = action["kind"] == "prepare" and body["response"].get("state") == "READY"
        if is_ready:
            m.require(action["attempt_id"] == state["attempt"]["attempt_id"], "Prepared response belongs to older attempt", "CONFLICT")
            m.require(not state["attempt"]["submission_recorded"], "Cannot replace a submitted request", "CONFLICT")
            request = body["response"].get("request")
            m.prepared(request, state)
        action.update(state="RESPONDED", response=body["response"], call_closed=body["call_closed"])
        self._save_action(db, action)
        if is_ready:
            # A recovered response can arrive after a later preparation. Retain
            # it on its original action without replacing that later authority.
            state["attempt"]["prepared_request"] = self._prepared_from_history(db, state)
            self._save(db, state)
        return {"run_id": action["run_id"], "action_id": action["action_id"]}

    def _close_call(self, db, body, lease):
        m.fields(body, "action_id evidence_ref")
        m.identity(body["evidence_ref"])
        action = self._action(db, body["action_id"])
        m.require(action["state"] == "RESPONDED", "Missing response; reconcile original call first", "PENDING")
        action["call_closed"] = True
        self._save_action(db, action)
        return {"run_id": action["run_id"], "action_id": action["action_id"]}

    def _abandon(self, db, body, lease):
        m.fields(body, "action_id reason")
        m.text(body["reason"], 2048)
        action = self._action(db, body["action_id"])
        m.require(action["state"] == "QUEUED", "Potentially sent action cannot be abandoned", "PENDING")
        action.update(state="NOT_SENT", call_closed=True)
        self._save_action(db, action)
        return {"run_id": action["run_id"], "action_id": action["action_id"]}

    def _preflight_failure(self, db, body, lease):
        m.fields(body, "action_id code")
        m.identity(body["code"])
        action = self._action(db, body["action_id"])
        m.require(action["state"] == "QUEUED", "Preflight failure must precede dispatch", "CONFLICT")
        return {"run_id": action["run_id"], "action_id": action["action_id"], "state": "NOT_DISPATCHED"}

    @staticmethod
    def _require_no_execution_observed(db, state):
        m.require(state['attempt'].get('maintenance_stop') is None,
                  'Maintenance stop proof cannot release the original submission', 'CONFLICT')
        def compatible(observation):
            return isinstance(observation, dict) and (
                observation.get('remote_state') == 'NOT_FOUND' and observation.get('outcome') == 'UNKNOWN'
                and observation.get('exited') is False and observation.get('evidence_state') in ('UNKNOWN', 'STAGING'))
        # The adapter records respond and observe in separate transactions. A
        # crash between them must not hide execution already present in a reply.
        for (raw,) in db.execute("""SELECT json_extract(document,'$.response') FROM actions
                WHERE run_id=? AND json_extract(document,'$.attempt_id')=?
                AND json_type(document,'$.response.observation') IS NOT NULL""",
                (state['run_id'], state['attempt']['attempt_id'])):
            m.require(compatible(json.loads(raw)['observation']),
                      'Retained response conflicts with non-acceptance proof', 'CONFLICT')
        positive = db.execute("""SELECT 1 FROM events WHERE run_id=? AND operation='observe'
            AND json_extract(body,'$.attempt_id')=? AND
            (json_extract(body,'$.remote_state')!='NOT_FOUND'
             OR json_extract(body,'$.outcome')!='UNKNOWN'
             OR json_extract(body,'$.exited')!=0
             OR json_extract(body,'$.evidence_state')='SEALED') LIMIT 1""",
            (state['run_id'], state['attempt']['attempt_id'])).fetchone()
        observation = state['attempt']['observation']
        m.require((observation is None or compatible(observation)) and positive is None, 'Remote observation conflicts with non-acceptance proof', 'CONFLICT')

    def _confirm_not_accepted(self, db, body, lease):
        """Retain a real error response and separately record a trusted proof.

        An error or NOT_FOUND alone is never a proof. The caller must verify the
        original deployment, request, closed transport and no admitted task.
        """
        m.fields(body, 'action_id operation_id request_digest evidence_ref')
        m.uuid4(body['operation_id']); m.sha(body['request_digest']); m.identity(body['evidence_ref'])
        action = self._action(db, body['action_id'])
        old = action.get('non_acceptance')
        if old is not None:
            m.require(old == body, 'Original non-acceptance proof differs', 'CONFLICT')
            return dict(run_id=action['run_id'], action_id=body['action_id'], non_acceptance_recorded=True)
        state = self._load(db, action['run_id'])
        attempt = state['attempt']
        m.require(action['kind'] == 'submit' and action['state'] == 'RESPONDED'
                  and action['call_closed'] is True and type(action['response']) is dict
                  and set(action['response']) in ({'remote_error'}, {'remote_error', 'call_id', 'response_sha256'})
                  and type(action['response'].get('remote_error')) is str,
                  'Non-acceptance requires a closed submission error', 'CONFLICT')
        m.identity(action['response']['remote_error'])
        if 'call_id' in action['response']:
            m.identity(action['response']['call_id']); m.sha(action['response']['response_sha256'])
        m.require(action['attempt_id'] == attempt['attempt_id'] and attempt['submission_recorded']
                  and body['operation_id'] == attempt['operation_id'] == action['payload']['operation_id']
                  and body['request_digest'] == action['payload']['request_digest']
                  == attempt['prepared_request']['request_digest'], 'Submission proof binding differs', 'CONFLICT')
        m.require(not self._pending(db, state['run_id']), 'Calls remain unclosed', 'PENDING')
        self._require_no_execution_observed(db, state)
        m.require(state['counters']['attempts'] > 0, 'Missing business reservation', 'STORE_INVALID')
        action['non_acceptance'] = body
        attempt['submission_recorded'] = False
        state['counters']['attempts'] -= 1
        self._save_action(db, action)
        self._save(db, state)
        return dict(run_id=state['run_id'], action_id=body['action_id'], non_acceptance_recorded=True)

    @staticmethod
    def _stopped_binding(state):
        proof = state['attempt'].get('maintenance_stop')
        if proof is None:
            return False
        attempt = state['attempt']
        m.require(proof['run_id'] == state['run_id'] and proof['attempt_id'] == attempt['attempt_id']
                  and proof['operation_id'] == attempt['operation_id']
                  and proof['request_digest'] == attempt['prepared_request']['request_digest']
                  and proof['binding_digest'] == m.digest(state['binding'])
                  and proof['effect_scope'] == 'TASK_OWNED_COPY'
                  and all(proof[k] is True for k in ('future_start_blocked', 'tree_exited', 'writers_stopped')),
                  'Maintenance stop proof binding differs', 'STORE_INVALID')
        return True

    def _confirm_stopped(self, db, body, lease):
        """Retain a trusted maintenance proof without rewriting remote status."""
        m.fields(body, 'run_id attempt_id operation_id request_digest binding_digest evidence_ref '
                      'future_start_blocked tree_exited writers_stopped effect_scope')
        state = self._load(db, body['run_id']); attempt = state['attempt']
        m.identity(body['evidence_ref']); m.sha(body['request_digest']); m.sha(body['binding_digest'])
        m.require(attempt['submission_recorded'] and attempt['prepared_request'] is not None,
                  'No original submission to close', 'NOT_READY')
        m.require(body['attempt_id'] == attempt['attempt_id'] and body['operation_id'] == attempt['operation_id']
                  and body['request_digest'] == attempt['prepared_request']['request_digest']
                  and body['binding_digest'] == m.digest(state['binding']),
                  'Maintenance proof identity differs', 'IDENTITY_CONFLICT')
        m.require(attempt['prepared_request']['kind'] == 'workspace.edit'
                  and body['effect_scope'] == 'TASK_OWNED_COPY'
                  and all(body[k] is True for k in ('future_start_blocked', 'tree_exited', 'writers_stopped')),
                  'Complete stop proof for task-owned workspace required', 'NOT_READY')
        old = attempt.get('maintenance_stop')
        if old is not None:
            m.require(old == body, 'Original stop proof differs', 'CONFLICT')
            return dict(run_id=state['run_id'], attempt_id=attempt['attempt_id'], stop_proof_recorded=True)
        m.require(not self._pending(db, state['run_id']), 'Original calls remain unclosed', 'PENDING')
        attempt['maintenance_stop'] = body
        self._save(db, state)
        return dict(run_id=state['run_id'], attempt_id=attempt['attempt_id'], stop_proof_recorded=True)

    def _not_started(self, db, body, lease):
        m.fields(body, "action_id evidence_ref")
        m.identity(body["evidence_ref"])
        action = self._action(db, body["action_id"])
        state = self._load(db, action["run_id"])
        m.require(self._awaiting_response(action), "Call already has a confirmed response", "CONFLICT")
        same_attempt = action["attempt_id"] == state["attempt"]["attempt_id"]
        # A closed preparation/query can receive a no-start proof after plan
        # revision. Refund that call without changing the current task or READY.
        m.require(same_attempt or action["kind"] in ("prepare", "query"), "Attempt changed", "CONFLICT")
        if action["kind"] == "submit":
            # A concurrent NOT_FOUND is not proof of execution or non-execution.
            # Accept an independent no-start proof only if it conflicts with no
            # observation of this attempt, including facts later hidden by NOT_FOUND.
            self._require_no_execution_observed(db, state)
            state["attempt"]["submission_recorded"] = False
            state["counters"]["attempts"] -= 1
        counter = {"query": "queries", "cancel": "cancellations"}.get(action["kind"], "actions")
        state["counters"][counter] -= 1
        action.update(state="NOT_SENT", call_closed=True, response={"local_failure": "CALL_NOT_STARTED", "evidence_ref": body["evidence_ref"]})
        self._save_action(db, action)
        if action["kind"] == "prepare" and same_attempt:
            state["attempt"]["prepared_request"] = self._prepared_from_history(db, state)
        self._save(db, state)
        return {"run_id": action["run_id"], "action_id": action["action_id"], "state": "NOT_SENT"}

    def _observe(self, db, body, lease):
        m.fields(body, "run_id attempt_id operation_id request_digest remote_state outcome exited evidence_state evidence_ref")
        state = self._load(db, body["run_id"])
        attempt = state["attempt"]
        m.require(attempt["submission_recorded"] and attempt["prepared_request"], "No original submission to observe", "NOT_READY")
        m.require(body["attempt_id"] == attempt["attempt_id"] and body["operation_id"] == attempt["operation_id"]
                  and body["request_digest"] == attempt["prepared_request"]["request_digest"], "Observation identity mismatch", "IDENTITY_CONFLICT")
        m.identity(body["evidence_ref"])
        m.require(body["remote_state"] in ("NOT_FOUND", "RUNNING", "TERMINAL"), "Invalid remote state")
        m.require(body["outcome"] in ("UNKNOWN", "SUCCEEDED", "FAILED", "CANCELLED"), "Invalid outcome")
        m.require(body["evidence_state"] in ("UNKNOWN", "STAGING", "SEALED"), "Invalid evidence state")
        m.require(type(body["exited"]) is bool, "exited must be boolean")
        m.require(not body["exited"] or body["remote_state"] == "TERMINAL", "Exit requires terminal observation")
        m.require(body["remote_state"] == "TERMINAL" or body["outcome"] == "UNKNOWN", "Nonterminal outcome must be unknown")
        old = attempt["observation"]
        if old and old["exited"]:
            m.require(body["exited"] and body["remote_state"] == old["remote_state"]
                      and (old["outcome"] == "UNKNOWN" or body["outcome"] == old["outcome"]),
                      "Observation conflicts with retained terminal result", "CONFLICT")
        attempt["observation"] = body
        self._save(db, state)
        return {"run_id": body["run_id"], "attempt_id": body["attempt_id"]}

    def _candidate(self, db, body, lease):
        m.fields(body, "run_id attempt_id artifact_id version_id source readback_ref receipt_sha256")
        state = self._load(db, body["run_id"])
        attempt, plan = state["attempt"], state["plan"]
        m.require(body["attempt_id"] == attempt["attempt_id"], "Candidate belongs to another attempt", "CONFLICT")
        for key in ("artifact_id", "version_id", "readback_ref"):
            m.identity(body[key])
        m.sha(body["receipt_sha256"])
        observed = attempt["observation"]
        m.require(observed and observed["exited"] and observed["outcome"] == "SUCCEEDED"
                  and observed["evidence_state"] == "SEALED", "Candidate requires successful sealed execution", "NOT_READY")
        source = body["source"]
        m.require(type(source) is dict and source.get("schema_version") == "lh-artifact-source-v1"
                  and source.get("producer") == state["binding"]["producer"]
                  and source.get("operation_id") == attempt["operation_id"]
                  and source.get("request_digest") == attempt["prepared_request"]["request_digest"],
                  "Candidate source differs from the run", "IDENTITY_CONFLICT")
        workspace = source.get("workspace")
        for key in ("seal_sha256", "status_sha256"):
            m.sha(source.get(key))
        m.identity(source.get("seal_id"))
        m.require(type(workspace) is dict and all(workspace.get(k) == v for k, v in dict(
            schema_version="lh-workspace-edit-v1",
            source_ref=plan["source_ref"], source_commit=plan["source_commit"], relative_path=plan["relative_path"],
            validation_ref=plan["validation_ref"], before_sha256=plan["expected_sha256"],
            after_sha256=hashlib.sha256(plan["content"].encode()).hexdigest()).items()),
            "Candidate workspace differs from the plan", "IDENTITY_CONFLICT")
        m.require(workspace.get("scope") == "TASK_OWNED_COPY" and workspace.get("original_modified") is False
                  and all(workspace.get(key) is True for key in ("read_back_verified", "validation_succeeded",
                                                               "edited_file_unchanged_by_validation")),
                  "Candidate validation is not confirmed", "NOT_READY")
        candidate = dict(body, goal_digest=state["goal_digest"], revision=state["revision"])
        old = attempt.get("candidate")
        m.require(old is None or {k:v for k,v in old.items() if k != "readback_ref"}
                  == {k:v for k,v in candidate.items() if k != "readback_ref"},
                  "A different candidate is already recorded", "CONFLICT")
        # Another verified read of identical bytes keeps the original candidate
        # identity; its new use reference is retained in this command's event.
        candidate = old or candidate
        attempt["candidate"] = candidate
        self._save(db, state)
        return {"run_id": state["run_id"], "candidate_digest": m.digest(candidate)}

    def _decide(self, db, body, lease):
        m.fields(body, "run_id attempt_id candidate_digest actor decision checks reason")
        state = self._load(db, body["run_id"])
        attempt = state["attempt"]
        candidate = attempt.get("candidate")
        m.require(body["attempt_id"] == attempt["attempt_id"] and candidate is not None,
                  "No matching candidate", "CONFLICT")
        m.require(body["candidate_digest"] == m.digest(candidate), "Candidate changed", "CONFLICT")
        m.identity(body["actor"])
        m.text(body["reason"], 4096)
        m.require(body["decision"] in ("ACCEPT", "REJECT"), "Invalid decision")
        checks = body["checks"]
        m.require(type(checks) is list and len(checks) == len(state["goal"]["acceptance"]), "Acceptance checks are incomplete")
        for condition, check in zip(state["goal"]["acceptance"], checks):
            m.fields(check, "condition passed evidence_ref")
            m.require(check["condition"] == condition and type(check["passed"]) is bool, "Acceptance condition differs")
            m.identity(check["evidence_ref"])
        m.require(body["decision"] != "ACCEPT" or all(c["passed"] for c in checks), "Unmet acceptance condition", "NOT_READY")
        m.require(attempt.get("decision") is None or attempt["decision"] == body, "Decision already retained", "CONFLICT")
        attempt["decision"] = body
        self._save(db, state)
        return {"run_id": state["run_id"], "decision": body["decision"], "candidate_digest": body["candidate_digest"]}

    def _learning(self, db, body, lease):
        m.fields(body, "run_id attempt_id candidate_id explanation evidence_refs artifact_versions")
        m.identity(body["candidate_id"])
        m.text(body["explanation"], 8192)
        state = self._load(db, body["run_id"])
        m.require(body["attempt_id"] == state["attempt"]["attempt_id"], "Candidate attempt differs", "CONFLICT")
        m.require(type(body["evidence_refs"]) is list and len(body["evidence_refs"]) <= 32, "Invalid evidence references")
        m.require(type(body["artifact_versions"]) is list and len(body["artifact_versions"]) <= 32, "Invalid artifact versions")
        m.require(body["evidence_refs"] or body["artifact_versions"], "Candidate requires recorded sources")
        for ref in body["evidence_refs"]:
            m.identity(ref)
        for version in body["artifact_versions"]:
            m.fields(version, "artifact_id version_id")
            m.identity(version["artifact_id"])
            m.identity(version["version_id"])
        document = dict(body, goal_digest=state["goal_digest"], revision=state["revision"],
                        operation_id=state["attempt"]["operation_id"], binding=state["binding"],
                        delivery="UNCONFIGURED", admission="NOT_REQUESTED")
        existing = db.execute("SELECT document FROM candidates WHERE candidate_id=?", (body["candidate_id"],)).fetchone()
        if existing:
            m.require(json.loads(existing[0]) == document, "Candidate ID binds other content", "CONFLICT")
        else:
            db.execute("INSERT INTO candidates VALUES (?,?,?)", (body["candidate_id"], body["run_id"], m.canonical(document).decode()))
        return {"run_id": body["run_id"], "candidate_id": body["candidate_id"], "delivery": "UNCONFIGURED", "admission": "NOT_REQUESTED"}
