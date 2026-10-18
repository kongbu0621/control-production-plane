"""Archive and consume exact workspace versions via the installed public CLI."""
from functools import wraps
import io
import json
from pathlib import Path
import re
import uuid
import zipfile

from . import model as m
from .calls import CliCalls, private_directory, publish, retained, response_json, sha


def verify_readback(state, reply, directory, trusted):
    """Validate the bytes actually consumed, not just a CLI success flag."""
    from local_hand_jobs.evidence_client import verify_local_bundle
    root = private_directory(directory, create=False)
    m.require(reply.get("status") == "READ" and reply.get("content_verified") is True,
              "Archive read did not confirm content", "PROTOCOL_ERROR")
    expected = {"evidence.zip", "evidence.manifest", "evidence.seal", "status.json", "source.json"}
    m.require(type(reply.get("files")) is dict and set(reply["files"]) == expected,
              "Read-back file list differs", "PROTOCOL_ERROR")
    files = {}
    for name in expected:
        raw = retained(root/name, 16*1024*1024 if name.endswith('.zip') else 4*1024*1024)
        m.require(reply["files"][name] == dict(size=len(raw), sha256=sha(raw)),
                  "Read-back bytes changed", "IDENTITY_CONFLICT")
        files[name] = raw
    source, status = response_json(files["source.json"]), response_json(files["status.json"])
    m.require(source.get("status_sha256") == trusted["status_sha256"]
              and all(source.get(k) == trusted["seal"][k] for k in ("seal_id", "seal_sha256")),
              "Read-back differs from the originally authenticated status and seal", "IDENTITY_CONFLICT")
    attempt, plan = state["attempt"], state["plan"]
    m.require(source == reply.get("source") and source.get("schema_version") == "lh-artifact-source-v1"
              and source.get("producer") == state["binding"]["producer"]
              and source.get("operation_id") == attempt["operation_id"]
              and source.get("request_digest") == attempt["prepared_request"]["request_digest"]
              and source.get("status_sha256") == sha(files["status.json"]),
              "Archived source differs from this run", "IDENTITY_CONFLICT")
    m.require(status.get("operation_id") == source["operation_id"]
              and status.get("request_digest") == source["request_digest"]
              and all(status.get(k) == v for k,v in dict(lifecycle="TERMINAL",phase="EXITED",outcome="SUCCEEDED",evidence="SEALED").items())
              and status.get("seal_refs") == [{k:source[k] for k in ("seal_id", "seal_sha256")}],
              "Archived status does not match the completed task", "IDENTITY_CONFLICT")
    manifest = verify_local_bundle(files["evidence.zip"], files["evidence.manifest"], files["evidence.seal"],
        operation_id=source["operation_id"], seal_id=source["seal_id"], seal_sha256=source["seal_sha256"], maximum=16*1024*1024)
    members = {entry["name"]:entry for entry in manifest["members"]}
    names = [name for name in members if name.endswith('/workspace-result.json')]
    m.require(len(names) == 1, "Expected one workspace result", "IDENTITY_CONFLICT")
    prefix = names[0].rsplit('/',1)[0] + '/'
    with zipfile.ZipFile(io.BytesIO(files["evidence.zip"])) as archive:
        def read(name):
            key = prefix + name
            m.require(key in members and members[key]["size"] <= 512*1024, "Missing or oversized workspace evidence", "LIMIT")
            return archive.read(key)
        workspace = response_json(read("workspace-result.json"))
        before, after = read("workspace-before.txt"), read("workspace-after.txt")
        patch, final_patch = read("workspace.patch"), read("workspace-final.patch")
    m.require(workspace == source.get("workspace") and all(workspace.get(k) == v for k,v in dict(
        schema_version="lh-workspace-edit-v1", source_ref=plan["source_ref"], source_commit=plan["source_commit"],
        relative_path=plan["relative_path"], validation_ref=plan["validation_ref"], scope="TASK_OWNED_COPY",
        before_sha256=plan["expected_sha256"], after_sha256=sha(plan["content"].encode())).items()),
        "Workspace evidence differs from the bound plan", "IDENTITY_CONFLICT")
    m.require(workspace.get("original_modified") is False and all(workspace.get(k) is True for k in
        ("read_back_verified", "validation_succeeded", "edited_file_unchanged_by_validation")),
        "Workspace validation is not confirmed", "NOT_READY")
    m.require(sha(before) == plan["expected_sha256"] and after == plan["content"].encode(),
              "Consumed before/after bytes differ from the plan", "IDENTITY_CONFLICT")
    # First adapter admits simple portable paths; complex porcelain quoting is
    # rejected explicitly rather than accepting an ambiguous changed-file list.
    m.require(re.fullmatch(r"[A-Za-z0-9_./-]+", plan["relative_path"]) is not None,
              "Candidate review currently requires a portable ASCII path", "UNSUPPORTED")
    expected_status = " M " + plan["relative_path"] + "\n" if before != after else ""
    m.require(patch == final_patch and workspace.get("git_status") == expected_status,
              "Validation changed additional files or the final patch", "CONFLICT")
    return dict(source=source, files=reply["files"], consumed_after_sha256=sha(after), patch_sha256=sha(patch))


def serial(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.calls.serial():
            try:
                return method(self, *args, **kwargs)
            except m.ControlError as exc:
                # A rejected receipt/candidate does not roll back an external
                # archive transaction or published read directory.
                exc.commit_state = "unknown"
                raise
    return wrapped


class ArtifactAdapter:
    def __init__(self, store, command, archive_store, directory):
        self.store, self.command = store, list(command)
        m.require(self.command and Path(self.command[0]).is_absolute()
                  and all(type(arg) is str and '\0' not in arg for arg in self.command), "Expected installed archive CLI")
        self.archive_store = str(Path(archive_store).absolute())
        self.root = private_directory(directory)
        config = m.canonical(dict(store_id=store.identity["store_id"], command=self.command, archive_store=self.archive_store))
        if (self.root/'adapter.json').exists():
            m.require(retained(self.root/'adapter.json') == config, "Archive binding changed", "IDENTITY_CONFLICT")
        else:
            publish(self.root/'adapter.json', config)
        self.calls = CliCalls(self.root/'calls', timeout=60)

    def _call(self, args, call_id):
        directory = self.calls.root / call_id
        argv = self.command + args
        base_id = call_id
        for retry in range(101):
            if not directory.exists():
                break
            original = response_json(retained(directory/'intent.json'))
            m.require(original['argv'] == argv, "Original archive call differs", "CONFLICT")
            if not (directory/'not-started.json').exists():
                # Only an explicit subsequent archive invocation can continue a
                # completed ERROR. The public archive API reconciles the same
                # producer/operation and refuses conflicting or lost identities.
                # Unknown output/lifetime still requires resume, not redelivery.
                if args[0] != 'archive' or not (directory/'result.json').exists():
                    break
                previous = response_json(retained(directory/'result.json'))
                if not previous['call_closed'] or previous['reason'] is not None:
                    break
                value = response_json(self.calls.read(call_id))
                if previous['exit_code'] != 1 or value.get('status') != 'ERROR':
                    break
            m.require(retry < 100, "Archive continuation budget exhausted", "LIMIT")
            call_id = base_id + '-retry-' + str(retry + 1)
            directory = self.calls.root/call_id
        if directory.exists():
            intent = response_json(retained(directory/'intent.json'))
            m.require(intent["argv"] == argv, "Original archive call differs", "CONFLICT")
            m.require((directory/'result.json').exists(), "Original archive call has no exit/EOF proof", "PENDING")
            result = response_json(retained(directory/'result.json'))
        else:
            result = self.calls.run(argv, call_id=call_id)
        m.require(result["call_closed"] and result["reason"] is None, "Archive CLI outcome remains unresolved", "CALL_UNKNOWN")
        value = response_json(self.calls.read(call_id))
        m.require(result["exit_code"] == 0 and value.get("status") != "ERROR",
                  "Archive CLI failed; retain the original store and reconcile", "ARCHIVE_ERROR")
        return value

    def _receipt(self, state, result):
        m.require(result.get("status") == "ARCHIVED"
                  and result.get("operation_id") == state["attempt"]["operation_id"]
                  and result.get("producer") == state["binding"]["producer"]
                  and result.get("content_verified") is True,
                  "Archive receipt identity or verification differs", "IDENTITY_CONFLICT")
        m.identity(result.get("artifact_id")); m.identity(result.get("version_id"))
        return result

    @serial
    def archive(self, run_id, evidence):
        state = self.store.get_run(run_id)
        attempt = state["attempt"]
        observed = attempt["observation"]
        m.require(observed and observed["exited"] and observed["outcome"] == "SUCCEEDED"
                  and observed["evidence_state"] == "SEALED", "No successful sealed observation", "NOT_READY")
        evidence = dict(evidence)
        m.require(evidence["operation_id"] == attempt["operation_id"], "Evidence task differs", "IDENTITY_CONFLICT")
        root = private_directory(self.root/attempt["attempt_id"])
        request = dict(run_id=run_id, attempt_id=attempt["attempt_id"], producer=state["binding"]["producer"],
                       archive_id=state["binding"]["archive_id"], evidence=evidence)
        if (root/'archive.json').exists():
            m.require(response_json(retained(root/'archive.json')) == request, "Retained archive input differs", "CONFLICT")
        else:
            publish(root/'archive.json', m.canonical(request))
        call_id = "archive-" + attempt["attempt_id"]
        args = ["archive", "--store", self.archive_store, "--producer", state["binding"]["producer"],
                "--status", str(Path(evidence["directory"])/'status.json'),
                "--status-sha256", evidence["status_sha256"], "--evidence-dir", evidence["directory"]]
        return self._receipt(state, self._call(args, call_id))

    @serial
    def resume(self, run_id):
        state = self.store.get_run(run_id)
        attempt = state["attempt"]
        original = response_json(retained(self.root/attempt['attempt_id']/'archive.json'))
        m.require(original['run_id'] == run_id and original['attempt_id'] == attempt['attempt_id']
                  and original['producer'] == state['binding']['producer']
                  and original['archive_id'] == state['binding']['archive_id']
                  and original['evidence']['operation_id'] == attempt['operation_id'],
                  "Original archive binding differs", "IDENTITY_CONFLICT")
        m.require(self.calls.stopped("archive-" + attempt['attempt_id']),
                  "Original archive CLI has no stop proof", "PENDING")
        result = self._call(["resume", "--store", self.archive_store, "--producer", state["binding"]["producer"],
                            "--operation-id", attempt["operation_id"]], "resume-" + str(uuid.uuid4()))
        return self._receipt(state, result)

    @serial
    def read(self, run_id, *, artifact_id, version_id, output):
        state = self.store.get_run(run_id)
        m.identity(artifact_id); m.identity(version_id)
        output = Path(output).absolute()
        m.require(not output.exists(), "Read-back output must be new; retain earlier outputs", "ALREADY_EXISTS")
        call_id = "read-" + str(uuid.uuid4())
        result = self._call(["read", "--store", self.archive_store, "--artifact-id", artifact_id,
                            "--version-id", version_id, "--output", str(output)], call_id)
        m.require(result.get("artifact_id") == artifact_id and result.get("version_id") == version_id,
                  "Read-back version differs", "IDENTITY_CONFLICT")
        original = response_json(retained(self.root/state["attempt"]["attempt_id"]/'archive.json'))
        checked = verify_readback(state, result, output, original["evidence"])
        body = dict(run_id=run_id, attempt_id=state["attempt"]["attempt_id"], artifact_id=artifact_id,
                    version_id=version_id, source=checked["source"], readback_ref="cli:" + call_id,
                    receipt_sha256=m.digest(checked))
        self.store.execute(dict(schema="production-plane-v1", request_id="candidate:" + call_id,
                                operation="candidate", body=body))
        return dict(result, use=checked, call_id=call_id)
