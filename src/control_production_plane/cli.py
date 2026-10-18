"""Local record commands. No remote task execution or shell command entrypoint."""
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
