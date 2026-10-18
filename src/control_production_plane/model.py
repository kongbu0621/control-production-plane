"""Bounded JSON contracts shared by the library and command line."""
import hashlib
import json
import re
import uuid
from pathlib import PurePosixPath

MAX_JSON = 256 * 1024
MAX_CONTENT = 16 * 1024
MAX_STORE = 256 * 1024 * 1024
KINDS = {"prepare", "submit", "query", "cancel"}


class ControlError(Exception):
    def __init__(self, code, message, *, commit_state="not_committed"):
        super().__init__(message)
        self.code, self.message, self.commit_state = code, message, commit_state
        self.action_id = None
        self.dispatch_recorded = None

    def as_dict(self):
        result = {"code": self.code, "message": self.message, "commit_state": self.commit_state}
        if self.action_id is not None:
            result.update(action_id=self.action_id, dispatch_recorded=self.dispatch_recorded)
        return result


class CallNotStarted(ControlError):
    """Trusted adapter proof: no child/transport could have delivered the call.

    Never use for timeouts, missing receipts or uncertain subprocess state.
    """

    def __init__(self, evidence_ref):
        identity(evidence_ref)
        self.evidence_ref = evidence_ref
        super().__init__("CALL_NOT_STARTED", "Local invocation did not start")


def require(condition, message, code="INVALID_INPUT"):
    if not condition:
        raise ControlError(code, message)


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                          allow_nan=False).encode("ascii")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ControlError("INVALID_INPUT", "Expected bounded JSON") from exc


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "Duplicate JSON field")
            result[key] = value
        return result

    require(type(raw) is bytes and len(raw) <= MAX_JSON, "JSON exceeds 256 KiB", "LIMIT")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ControlError("INVALID_INPUT", "Expected UTF-8 JSON") from exc
    validate_tree(value)
    return value


def validate_tree(value):
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        require(depth <= 20 and nodes <= 20000, "JSON structure limit", "LIMIT")
        if type(item) is dict:
            require(all(type(key) is str for key in item), "JSON keys must be strings")
            pending.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif type(item) is list:
            pending.extend((part, depth + 1) for part in item)
        elif type(item) is str:
            require(not any(0xD800 <= ord(char) <= 0xDFFF for char in item), "Invalid Unicode")
        elif type(item) is int:
            require(abs(item) <= 9007199254740991, "Integer exceeds safe range")
        else:
            require(item is None or type(item) is bool, "Only integer JSON numbers are supported")
    require(len(canonical(value)) <= MAX_JSON, "Canonical JSON exceeds 256 KiB", "LIMIT")


def fields(value, names):
    require(type(value) is dict and set(value) == set(names.split()), "Unexpected or missing fields")


def text(value, maximum=512):
    require(type(value) is str and 0 < len(value.encode("utf-8")) <= maximum, "Invalid text size")


def identity(value):
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", value), "Invalid identity")


def sha(value):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "Expected SHA-256")


def natural(value, low=1, high=1000):
    require(type(value) is int and low <= value <= high, "Integer outside supported range")


def uuid4(value):
    try:
        parsed = uuid.UUID(value)
        require(parsed.version == 4 and str(parsed) == value, "Expected lowercase UUID v4")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ControlError("INVALID_INPUT", "Expected lowercase UUID v4") from exc


def binding(value):
    fields(value, "executor_id authority_id principal_id profile_ref tool_schema_digest archive_id producer")
    for key, part in value.items():
        sha(part) if key == "tool_schema_digest" else identity(part)


def knowledge(value):
    fields(value, "required status reason items")
    require(type(value["required"]) is bool, "required must be boolean")
    require(value["status"] in ("UNCONFIGURED", "NO_MATCH", "QUERY_FAILED", "SELECTED"), "Unknown knowledge status")
    text(value["reason"], 2048)
    require(type(value["items"]) is list and len(value["items"]) <= 16, "Invalid knowledge items")
    require(bool(value["items"]) == (value["status"] == "SELECTED"), "Knowledge status does not match items")
    seen = set()
    for item in value["items"]:
        fields(item, "knowledge_id version_id content_sha256 admission_ref scope")
        for key in ("knowledge_id", "version_id", "admission_ref"):
            identity(item[key])
        sha(item["content_sha256"])
        text(item["scope"], 2048)
        pair = (item["knowledge_id"], item["version_id"])
        require(pair not in seen, "Duplicate knowledge version")
        seen.add(pair)


def plan(value):
    fields(value, "source_ref source_commit relative_path expected_sha256 content validation_ref knowledge")
    identity(value["source_ref"])
    identity(value["validation_ref"])
    require(type(value["source_commit"]) is str and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value["source_commit"]), "Expected source commit")
    relative = value["relative_path"]
    text(relative, 256)
    path = PurePosixPath(relative)
    require(not path.is_absolute() and str(path) == relative and ".." not in path.parts
            and relative != "." and "\\" not in relative and "\x00" not in relative,
            "Expected normalized relative file path")
    sha(value["expected_sha256"])
    require(type(value["content"]) is str and len(value["content"].encode("utf-8")) <= MAX_CONTENT,
            "Replacement content exceeds 16 KiB", "LIMIT")
    knowledge(value["knowledge"])


def specification(value):
    fields(value, "run_id goal binding plan budget")
    identity(value["run_id"])
    fields(value["goal"], "text acceptance")
    text(value["goal"]["text"], 8192)
    conditions = value["goal"]["acceptance"]
    require(type(conditions) is list and 1 <= len(conditions) <= 32, "Expected acceptance conditions")
    for condition in conditions:
        text(condition, 2048)
    require(len(set(conditions)) == len(conditions), "Duplicate acceptance condition")
    binding(value["binding"])
    plan(value["plan"])
    fields(value["budget"], "max_attempts max_actions max_queries max_cancellations deadline_utc max_response_bytes")
    for name in ("max_actions", "max_queries", "max_cancellations"):
        natural(value["budget"][name])
    natural(value["budget"]["max_attempts"], high=10)
    natural(value["budget"]["deadline_utc"], high=9007199254740991)
    natural(value["budget"]["max_response_bytes"], high=MAX_JSON)


def inputs(value):
    return {key: value[key] for key in ("source_ref", "relative_path", "expected_sha256", "content", "validation_ref")}


def prepared(request, state):
    """Check plan/identity binding; the executor adapter must validate its full schema."""
    require(type(request) is dict, "Prepared request must be an object")
    for key in ("operation_id", "kind", "profile_ref", "inputs", "expires_at", "request_digest"):
        require(key in request, "Prepared request is incomplete")
    require(request["operation_id"] == state["attempt"]["operation_id"]
            and request["kind"] == "workspace.edit"
            and request["profile_ref"] == state["binding"]["profile_ref"]
            and request["inputs"] == inputs(state["plan"]), "Prepared request differs from bound plan", "IDENTITY_CONFLICT")
    natural(request["expires_at"], high=9007199254740991)
    sha(request["request_digest"])
    require(digest({key: value for key, value in request.items() if key != "request_digest"}) == request["request_digest"],
            "Prepared request digest differs", "IDENTITY_CONFLICT")
