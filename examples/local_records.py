"""Run with an installed wheel and an absent private store directory.

All identities and source contents are synthetic; this never calls an executor.
"""
import argparse
import hashlib
import json
import time

from control_production_plane import initialize, open_store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store")
    args = parser.parse_args()
    spec = {
        "run_id": "example",
        "goal": {"text": "Correct a synthetic greeting", "acceptance": ["The proposed file prints hello"]},
        "binding": {"executor_id": "demo:executor", "authority_id": "demo:authority", "principal_id": "demo:caller",
                    "profile_ref": "demo:profile", "tool_schema_digest": "1" * 64,
                    "archive_id": "demo:archive", "producer": "demo:producer"},
        "plan": {"source_ref": "demo:source", "source_commit": "2" * 40, "relative_path": "greeting.py",
                 "expected_sha256": hashlib.sha256(b"print('helo')\n").hexdigest(), "content": "print('hello')\n",
                 "validation_ref": "demo:validation", "knowledge": {
                     "required": False, "status": "UNCONFIGURED", "reason": "Local record example", "items": []}},
        "budget": {"max_attempts": 2, "max_actions": 10, "max_queries": 20, "max_cancellations": 10,
                   "deadline_utc": int(time.time()) + 600, "max_response_bytes": 32768},
    }
    store = initialize(args.store)
    request = {"schema": "production-plane-v1", "request_id": "example:create", "operation": "create", "body": spec}
    store.execute(request)
    assert store.execute(request)["replayed"]
    store.execute({"schema": "production-plane-v1", "request_id": "example:cancel", "operation": "cancel",
                   "body": {"run_id": "example", "reason": "Record-only example; do not submit a task"}})
    restored = open_store(args.store).recover("example")
    assert restored["next_step"] == "NO_NEW_SUBMISSION"
    print(json.dumps(restored, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
