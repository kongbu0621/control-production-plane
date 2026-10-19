"""Local records and explicitly configured installed CLI adapters; never a shell."""
import argparse
import json
import os
from pathlib import Path
import sys

from .model import ControlError, MAX_JSON, canonical, decode
from .store import initialize, open_store


def main(argv=None):
    parser = argparse.ArgumentParser(description="Persistent production control records")
    parser.add_argument("--store", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    for name in ("show", "recover", "events"):
        sub = commands.add_parser(name)
        sub.add_argument("--run-id", required=True)
        if name == "show":
            sub.add_argument("--revision", type=int)
        if name == "events":
            sub.add_argument("--after", type=int, default=0)
            sub.add_argument("--limit", type=int, default=10)
    write = commands.add_parser("write")
    write.add_argument("--request", required=True, type=Path)
    for name in ("inspect-executor", "execute-action", "reconcile-action", "collect-evidence"):
        sub = commands.add_parser(name)
        sub.add_argument("--adapter", required=True, type=Path)
        sub.add_argument("--data", required=True, type=Path)
        if name != "inspect-executor":
            sub.add_argument("--action-id", required=True)
        if name == "collect-evidence":
            sub.add_argument("--output", required=True, type=Path)
        if name == "execute-action":
            sub.add_argument("--knowledge-adapter", type=Path)
    for name in ("archive-result", "resume-archive", "read-artifact"):
        sub = commands.add_parser(name)
        sub.add_argument("--adapter", required=True, type=Path)
        sub.add_argument("--data", required=True, type=Path)
        sub.add_argument("--run-id", required=True)
        if name == "archive-result":
            sub.add_argument("--evidence", required=True, type=Path)
        if name == "read-artifact":
            sub.add_argument("--artifact-id", required=True)
            sub.add_argument("--version-id", required=True)
            sub.add_argument("--output", required=True, type=Path)
    for name in ("query-knowledge", "select-knowledge", "adopt-knowledge", "knowledge-feedback", "resume-knowledge"):
        sub = commands.add_parser(name)
        sub.add_argument("--adapter", required=True, type=Path)
        if name in ("query-knowledge", "select-knowledge", "adopt-knowledge"):
            sub.add_argument("--context", required=True, type=Path)
        if name == "query-knowledge":
            sub.add_argument("--kind", required=True)
            sub.add_argument("--after", default="")
            sub.add_argument("--limit", default=10, type=int)
        elif name == "select-knowledge":
            sub.add_argument("--version-id", required=True)
        else:
            sub.add_argument("--run-id", required=True)
        if name == "knowledge-feedback":
            sub.add_argument("--revision", required=True, type=int)
            sub.add_argument("--notes", required=True)
        if name == "resume-knowledge":
            sub.add_argument("--call-id", required=True)
    args = parser.parse_args(argv)
    mutated = False
    try:
        if args.command == "init":
            store = initialize(args.store)
            data = store.identity
            mutated = True
        else:
            store = open_store(args.store)
            if args.command == "show":
                data = store.get_run(args.run_id) if args.revision is None else store.get_revision(args.run_id, args.revision)
            elif args.command == "recover":
                data = store.recover(args.run_id)
            elif args.command == "events":
                data = store.events(args.run_id, after=args.after, limit=args.limit)
            elif args.command in ("inspect-executor", "execute-action", "reconcile-action", "collect-evidence"):
                from .local_hand import LocalHandAdapter
                from .calls import retained
                adapter = LocalHandAdapter(store, decode(retained(args.adapter)), args.data)
                if args.command == "inspect-executor":
                    data = adapter.capabilities()
                elif args.command == "execute-action":
                    knowledge = None
                    # Read recovery and cancellation remain available without K.
                    action = store.get_action(args.action_id)
                    if (action['kind'] == 'submit'
                            and store.get_run(action['run_id'])['plan']['knowledge']['status'] == 'SELECTED'
                            and args.knowledge_adapter is not None):
                        from .knowledge import KnowledgeAdapter
                        knowledge = KnowledgeAdapter(store, decode(retained(args.knowledge_adapter)))
                    data = adapter.perform(args.action_id, knowledge=knowledge)
                elif args.command == "reconcile-action":
                    data = adapter.reconcile(args.action_id)
                else:
                    data = adapter.download(args.action_id, args.output)
                mutated = True  # Includes retained local CLI/evidence records.
            elif args.command in ("archive-result", "resume-archive", "read-artifact"):
                from .artifacts import ArtifactAdapter
                from .calls import retained
                from .model import fields
                config = decode(retained(args.adapter))
                fields(config, "command archive_store")
                adapter = ArtifactAdapter(store, config["command"], config["archive_store"], args.data)
                if args.command == "archive-result":
                    data = adapter.archive(args.run_id, decode(retained(args.evidence)))
                elif args.command == "resume-archive":
                    data = adapter.resume(args.run_id)
                else:
                    data = adapter.read(args.run_id, artifact_id=args.artifact_id, version_id=args.version_id, output=args.output)
                mutated = True
            elif args.command in ("query-knowledge", "select-knowledge", "adopt-knowledge", "knowledge-feedback", "resume-knowledge"):
                from .knowledge import KnowledgeAdapter
                from .calls import retained
                adapter = KnowledgeAdapter(store, decode(retained(args.adapter)))
                if args.command == "query-knowledge":
                    data = adapter.query(args.kind, decode(retained(args.context)), after=args.after, limit=args.limit)
                elif args.command == "select-knowledge":
                    data = adapter.selection(args.version_id, decode(retained(args.context)))
                elif args.command == "adopt-knowledge":
                    data = adapter.adopt(args.run_id, decode(retained(args.context)))
                    mutated = True
                elif args.command == "knowledge-feedback":
                    data = adapter.feedback(args.run_id, args.revision, args.notes)
                    mutated = True
                else:
                    data = adapter.resume(args.run_id, args.call_id)
                    mutated = True
            else:
                with args.request.open("rb") as stream:
                    request = decode(stream.read(MAX_JSON + 1))
                if not isinstance(request, dict) or request.get("operation") == "dispatch":
                    raise ControlError("INVALID_INPUT", "Dispatch is library-only and requires a held executor lease")
                data = store.execute(request)
                mutated = True
        raw = canonical({"status": "OK", "data": data}) + b"\n"
        # A write receipt may be committed even if stdout disappears afterwards.
        offset = 0
        while offset < len(raw):
            written = sys.stdout.buffer.write(raw[offset:])
            if written is None or written <= 0:
                raise OSError("Output did not advance")
            offset += written
        sys.stdout.buffer.flush()
        return 0
    except ControlError as exc:
        error = exc.as_dict()
    except (OSError, ValueError, RecursionError) as exc:
        error = {"code": "IO_ERROR", "message": "Local I/O failed; retain the original request and inspect records",
                 "commit_state": "committed" if mutated else "unknown"}
    except Exception as exc:
        # Optional adapters may expose dependency validation errors. Preserve an
        # unknown outcome and return a bounded error, never a false rollback.
        error = {"code": "ADAPTER_ERROR", "message": "Adapter response or dependency failed; inspect retained call records",
                 "exception": type(exc).__name__, "commit_state": "unknown"}
    try:
        sys.stderr.write(json.dumps({"status": "ERROR", "error": error}, ensure_ascii=True) + "\n")
        sys.stderr.flush()
    except OSError:
        pass
    # Suppress a second broken-pipe flush during interpreter shutdown.
    try:
        sink = os.open(os.devnull, os.O_WRONLY)
        os.dup2(sink, sys.stdout.fileno())
        os.close(sink)
    except (OSError, ValueError, AttributeError):
        pass
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
