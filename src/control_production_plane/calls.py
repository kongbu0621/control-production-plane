"""Bounded, retained local CLI calls. A recorded call is never automatically replayed."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import stat
import subprocess
import time
import uuid

from .model import CallNotStarted, ControlError, canonical, identity, require


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def response_json(raw, maximum=512 * 1024):
    """Remote responses can contain finite timestamps; core commands cannot."""
    require(type(raw) is bytes and len(raw) <= maximum, "Response exceeds budget", "LIMIT")
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate response field", "PROTOCOL_ERROR")
            result[key] = value
        return result
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
        pending = [(result, 0)]
        nodes = 0
        while pending:
            value, depth = pending.pop()
            nodes += 1
            require(depth <= 24 and nodes <= 50000, "Response structure exceeds budget", "LIMIT")
            if type(value) is dict:
                pending.extend((v, depth + 1) for item in value.items() for v in item)
            elif type(value) is list:
                pending.extend((v, depth + 1) for v in value)
            elif type(value) is int:
                require(abs(value) <= 9007199254740991, "Unsafe response integer", "PROTOCOL_ERROR")
            elif type(value) is float:
                require(math.isfinite(value), "Nonfinite response number", "PROTOCOL_ERROR")
            elif type(value) is str:
                require(not any(0xD800 <= ord(c) <= 0xDFFF for c in value), "Invalid response text", "PROTOCOL_ERROR")
        require(type(result) is dict, "Response must be an object", "PROTOCOL_ERROR")
        return result
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ControlError("PROTOCOL_ERROR", "Invalid JSON response") from exc


def private_directory(path, *, create=True):
    path = Path(path).absolute()
    if create and not path.exists():
        path.mkdir(mode=0o700)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
            "Adapter directory must be private and owned by the caller", "STORE_INVALID")
    return path


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(path, raw):
    """Create-only durable file; existing and interrupted records are preserved."""
    path = Path(path)
    temp = path.with_name(".pending-" + str(uuid.uuid4()))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.link(temp, path, follow_symlinks=False)
    temp.unlink()
    sync_directory(path.parent)


def retained(path, maximum=512 * 1024):
    # A FIFO must be rejected by fstat without blocking on open first.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and not info.st_mode & 0o077 and info.st_size <= maximum,
                "Invalid retained adapter file", "STORE_INVALID")
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum, "Retained data exceeds budget", "LIMIT")
    return raw


class CliCalls:
    """For trusted non-forking Local Hand CLIs, with no shell or automatic retry.

    An interrupted owner can leave an original child. The intent and process
    identity remain; absence of a completed record is never an exit/EOF proof.
    Independent read-only calls may still be made under new call IDs.
    """
    def __init__(self, directory, *, timeout=15, max_bytes=512 * 1024):
        require(type(timeout) is int and 1 <= timeout <= 120, "Invalid CLI timeout")
        require(type(max_bytes) is int and 1024 <= max_bytes <= 4 * 1024 * 1024, "Invalid output budget")
        self.root = private_directory(directory)
        self.timeout, self.max_bytes = timeout, max_bytes

    def not_started(self, call_id, argv, binding, reason):
        """Record a local rejection before starting a child or transport."""
        identity(call_id)
        require('/' not in call_id and ':' not in call_id, "Invalid call ID")
        directory = self.root/call_id
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ControlError("PENDING", "Original call already exists") from exc
        sync_directory(self.root)
        publish(directory/'intent.json', canonical(dict(argv=argv, binding=binding, call_id=call_id)))
        publish(directory/'not-started.json', canonical(dict(error=reason, call_id=call_id)))
        raise CallNotStarted("cli:" + call_id)

    def stopped(self, call_id):
        """Confirm no old CLI is running; this does not invent output/EOF proof."""
        directory = self.root/call_id
        if (directory/'not-started.json').exists():
            return True
        if (directory/'result.json').exists() and response_json(retained(directory/'result.json')).get('call_closed') is True:
            return True
        if not (directory/'process.json').exists():
            return False  # Death between spawn and process identity publication.
        process = response_json(retained(directory/'process.json'))
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        stopped = boot != process['boot_id']
        if not stopped:
            try:
                fields = Path(f"/proc/{process['pid']}/stat").read_text().rsplit(')', 1)[1].split()
            except FileNotFoundError:
                stopped = True
            else:
                stopped = (fields[0] == 'Z' or (process['start_ticks'] is not None and fields[19] != process['start_ticks']))
        if stopped and not (directory/'exit-observed.json').exists():
            publish(directory/'exit-observed.json', canonical(dict(call_id=call_id, process=process,
                observed_boot_id=boot, child_stopped=True, output_complete=False)))
        return stopped

    @contextmanager
    def serial(self):
        """Serialize archive callers; a dead owner alone never releases its child."""
        fd = os.open(self.root/'owner.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ControlError('BUSY', 'Another archive caller is active') from exc
            for directory in self.root.iterdir():
                if directory.is_dir():
                    require(self.stopped(directory.name), 'An earlier archive CLI has no stop proof', 'PENDING')
            yield
        finally:
            os.close(fd)

    def run(self, argv, *, call_id=None, binding=None):
        call_id = call_id or str(uuid.uuid4())
        identity(call_id)
        require('/' not in call_id and ':' not in call_id, "Call ID must be a filename")
        require(type(argv) is list and argv and Path(argv[0]).is_absolute()
                and all(type(a) is str and '\0' not in a for a in argv), "Expected an explicit CLI argv")
        directory = self.root / call_id
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ControlError("PENDING", "Original CLI call exists; inspect it instead of invoking again") from exc
        sync_directory(self.root)
        publish(directory / "intent.json", canonical(dict(argv=argv, binding=binding, call_id=call_id)))
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=env, cwd=directory, start_new_session=True)
        except OSError as exc:
            publish(directory / "not-started.json", canonical(dict(error=type(exc).__name__, call_id=call_id)))
            raise CallNotStarted("cli:" + call_id) from exc
        selector = selectors.DefaultSelector()
        streams = {}
        reason, eof = None, set()
        deadline = time.monotonic() + self.timeout
        try:
            try:
                start = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            except FileNotFoundError:
                start = None
            publish(directory / "process.json", canonical(dict(pid=process.pid, start_ticks=start,
                boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())))
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
                fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                streams[name] = [os.fdopen(fd, "wb"), 0, hashlib.sha256()]
            while selector.get_map():
                if time.monotonic() >= deadline:
                    reason = reason or "TIMEOUT"
                    process.kill()
                    # Bound drainage too. Inherited open pipes are not EOF.
                    if time.monotonic() >= deadline + 2:
                        break
                for key, _ in selector.select(0.05):
                    data = os.read(key.fd, 65536)
                    if not data:
                        eof.add(key.data)
                        selector.unregister(key.fileobj)
                        continue
                    stream, size, digest = streams[key.data]
                    keep = data[:max(0, self.max_bytes - size)]
                    stream.write(keep)
                    digest.update(keep)
                    streams[key.data][1] += len(keep)
                    if len(keep) != len(data):
                        reason = "OUTPUT_LIMIT"
                        process.kill()
            try:
                exit_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = reason or "TIMEOUT"
                process.kill()
                exit_code = process.wait(timeout=2)
            for stream, _, _ in streams.values():
                stream.flush()
                os.fsync(stream.fileno())
            result = dict(call_id=call_id, exit_code=exit_code, reason=reason,
                          call_closed=eof == {"stdout", "stderr"},
                          files={k:dict(size=v[1], sha256=v[2].hexdigest()) for k,v in streams.items()})
            publish(directory / "result.json", canonical(result))
            return result
        finally:
            selector.close()
            for stream, _, _ in streams.values():
                stream.close()
            for stream in (process.stdout, process.stderr):
                stream.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)

    def read(self, call_id, name="stdout"):
        identity(call_id)
        require('/' not in call_id and ':' not in call_id and name in ("stdout", "stderr"), "Invalid call reference")
        directory = private_directory(self.root / call_id, create=False)
        result = response_json(retained(directory / "result.json"))
        raw = retained(directory / name, self.max_bytes)
        require(dict(size=len(raw), sha256=sha(raw)) == result["files"][name],
                "Retained CLI output changed", "STORE_INVALID")
        return raw
