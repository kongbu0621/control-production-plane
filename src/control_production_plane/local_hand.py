"""Optional adapter for installed Local Hand public CLI and evidence interfaces."""
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import uuid

from . import model as m
from .calls import CliCalls, private_directory, publish, retained, response_json, sha

TOOLS = {"prepare": "lh_job_prepare", "submit": "lh_job_submit",
         "query": "lh_job_status", "cancel": "lh_job_cancel"}


def contract():
    try:
        from local_hand_jobs import contract as api
        return api
    except ImportError as exc:
        raise m.ControlError("DEPENDENCY_MISSING", "Install the verified Local Hand wheel in this environment") from exc


class LocalHandAdapter:
    def __init__(self, store, config, directory):
        m.fields(config, "schema binding command socket server_uid policy_digest catalog_version")
        m.require(config["schema"] == "production-plane-local-hand-v1", "Unsupported adapter config")
        m.binding(config["binding"])
        m.sha(config["policy_digest"])
        m.sha(config["catalog_version"])
        m.natural(config["server_uid"], low=0, high=2**32-1)
        m.require(type(config["command"]) is list and config["command"]
                  and all(type(s) is str and '\0' not in s for s in config["command"])
                  and Path(config["command"][0]).is_absolute(), "Expected installed CLI command")
        m.require(type(config["socket"]) is str and Path(config["socket"]).is_absolute(), "Expected absolute socket path")
        self.store = store
        self.config = json.loads(m.canonical(config))
        self.root = private_directory(directory)
        record = m.canonical(dict(store_id=store.identity["store_id"], config=self.config))
        path = self.root / "adapter.json"
        if path.exists():
            m.require(retained(path) == record, "Adapter/store binding changed", "IDENTITY_CONFLICT")
        else:
            publish(path, record)
        self.calls = CliCalls(self.root / "calls")

    def _endpoint(self):
        info = Path(self.config["socket"]).lstat()
        m.require(stat.S_ISSOCK(info.st_mode) and info.st_uid == self.config["server_uid"],
                  "Socket ownership differs from configured deployment", "IDENTITY_CONFLICT")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(2)
            peer.connect(self.config["socket"])
            _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            m.require(uid == self.config["server_uid"], "Socket server identity differs", "IDENTITY_CONFLICT")
        return info.st_dev, info.st_ino

    def _tool(self, tool, arguments, *, call_id=None):
        api = contract()
        m.require(api.TOOL_SCHEMA_DIGEST == self.config["binding"]["tool_schema_digest"],
                  "Installed client contract differs from bound schema", "IDENTITY_CONFLICT")
        argv = self.config["command"] + ["--socket", self.config["socket"], tool,
                                            "--json", m.canonical(arguments).decode()]
        binding = dict(tool=tool, arguments=arguments, store_id=self.store.identity["store_id"],
                       config_sha256=m.digest(self.config))
        try:
            api.validate_tool_args(tool, arguments)
            before = self._endpoint()
        except (api.JobError, OSError, m.ControlError) as exc:
            if call_id is not None:
                self.calls.not_started(call_id, argv, binding, type(exc).__name__)
            if isinstance(exc, m.ControlError):
                raise
            raise m.ControlError("PREFLIGHT_FAILED", "Local CLI input or endpoint is unavailable") from exc
        result = self.calls.run(argv, call_id=call_id, binding=binding)
        m.require(self._endpoint() == before, "Endpoint changed during the call; retain original result", "IDENTITY_CONFLICT")
        publish(self.calls.root/result["call_id"]/"endpoint.json", m.canonical(dict(
            config_sha256=m.digest(self.config), socket_identity=list(before))))
        m.require(result["call_closed"] and result["reason"] is None,
                  "CLI output or lifetime is incomplete; retain original call", "CALL_UNKNOWN")
        name = "stdout" if result["exit_code"] == 0 else "stderr"
        raw = self.calls.read(result["call_id"], name)
        value = response_json(raw)
        if result["exit_code"] != 0:
            m.require(result["exit_code"] == 2 and type(value.get("error")) is dict
                      and type(value["error"].get("code")) is str, "Invalid CLI error response", "PROTOCOL_ERROR")
            value = {"remote_error": value["error"]["code"]}
        return value, result, sha(raw)

    def capabilities(self):
        profiles, cursor, seen = [], None, set()
        for _ in range(16):
            value, _, _ = self._tool("lh_capabilities", dict(page_size=100, **({"cursor": cursor} if cursor else {})))
            for field, expected in (("schema_version", "lh-job-v1"),
                ("authority_id", self.config["binding"]["authority_id"]),
                ("tool_schema_digest", self.config["binding"]["tool_schema_digest"]),
                ("policy_digest", self.config["policy_digest"]),
                ("catalog_version", self.config["catalog_version"])):
                m.require(value.get(field) == expected, "Capabilities differ from bound deployment", "IDENTITY_CONFLICT")
            m.require(type(value.get("profiles")) is list, "Missing profile catalog", "PROTOCOL_ERROR")
            profiles.extend(value["profiles"])
            cursor = value.get("next_cursor")
            if cursor is None:
                return dict(value, profiles=profiles)
            m.require(type(cursor) is str and cursor not in seen, "Invalid capability pagination", "PROTOCOL_ERROR")
            seen.add(cursor)
        raise m.ControlError("LIMIT", "Capability page budget exhausted")

    def workspace_read(self, plan):
        args = dict(profile_ref=self.config["binding"]["profile_ref"], source_ref=plan["source_ref"],
                    relative_path=plan["relative_path"])
        value, result, digest = self._tool("lh_workspace_read", args)
        m.require(all(value.get(k) == args[k] for k in ("source_ref", "relative_path"))
                  and value.get("source_commit") == plan["source_commit"]
                  and value.get("sha256") == plan["expected_sha256"]
                  and type(value.get("content")) is str, "Workspace source differs from plan", "IDENTITY_CONFLICT")
        raw = value["content"].encode("utf-8")
        m.require(type(value.get("bytes")) is int and value["bytes"] == len(raw)
                  and sha(raw) == value["sha256"], "Workspace bytes differ", "IDENTITY_CONFLICT")
        return dict(value, call_id=result["call_id"], response_sha256=digest)

    def preflight(self, state, kind):
        m.require(state["binding"] == self.config["binding"], "Run and adapter differ", "IDENTITY_CONFLICT")
        caps = self.capabilities()
        matches = [p for p in caps["profiles"] if p.get("profile_ref") == state["binding"]["profile_ref"]]
        m.require(len(matches) == 1, "Bound profile is unavailable", "NOT_READY")
        if kind in ("prepare", "submit"):
            profile, plan = matches[0], state["plan"]
            m.require(re.fullmatch(r"[A-Za-z0-9_./-]+", plan["relative_path"]) is not None,
                      "This adapter requires a portable ASCII target path", "UNSUPPORTED")
            m.require("workspace.edit" in profile.get("allowed_kinds", [])
                      and plan["source_ref"] in profile.get("source_refs", [])
                      and plan["validation_ref"] in profile.get("validation_refs", []),
                      "Source or validation is not admitted", "NOT_READY")
            self.workspace_read(plan)
        return caps

    def _summary(self, action, value, result, digest):
        state = self.store.get_run(action["run_id"])
        summary = dict(call_id=result["call_id"], response_sha256=digest)
        if "remote_error" in value:
            return dict(summary, remote_error=value["remote_error"])
        m.require(value.get("operation_id") == action["payload"]["operation_id"],
                  "Response belongs to another task", "IDENTITY_CONFLICT")
        if action["kind"] == "prepare":
            m.require(value.get("state") in ("READY", "PREPARING", "FAILED"), "Invalid preparation response", "PROTOCOL_ERROR")
            summary.update(state=value["state"], operation_id=value["operation_id"])
            if value["state"] == "READY":
                api = contract()
                try:
                    request = api.validate_submit(value.get("request"))
                except api.JobError as exc:
                    raise m.ControlError("PROTOCOL_ERROR", "Invalid prepared request") from exc
                m.prepared(request, state)
                summary["request"] = request
        else:
            request = state["attempt"]["prepared_request"]
            m.require(request is not None and value.get("request_digest") == request["request_digest"],
                      "Status differs from original request", "IDENTITY_CONFLICT")
            m.require(value.get("lifecycle") in ("ACCEPTED", "RUNNING", "RECONCILE_REQUIRED", "TERMINAL")
                      and value.get("outcome") in ("PENDING", "UNKNOWN", "SUCCEEDED", "FAILED", "CANCELLED")
                      and value.get("evidence") in ("STAGING", "SEALED", "DURABILITY_UNKNOWN", "FAILED")
                      and type(value.get("phase")) is str, "Invalid task status", "PROTOCOL_ERROR")
            proof = value.get("exit_proof")
            exited = value["lifecycle"] == "TERMINAL" and value["phase"] == "EXITED" and isinstance(proof, dict) and all(
                proof.get(k) is True for k in ("future_start_blocked", "tree_exited", "writers_stopped"))
            summary["observation"] = dict(run_id=state["run_id"], attempt_id=action["attempt_id"],
                operation_id=value["operation_id"], request_digest=value["request_digest"],
                remote_state="TERMINAL" if value["lifecycle"] == "TERMINAL" else "RUNNING",
                outcome=value["outcome"] if value["lifecycle"] == "TERMINAL" and value["outcome"] != "PENDING" else "UNKNOWN",
                exited=exited, evidence_state=value["evidence"] if value["evidence"] in ("STAGING", "SEALED") else "UNKNOWN",
                evidence_ref="cli:" + result["call_id"])
        return summary

    def perform(self, action_id):
        action = self.store.get_action(action_id)
        state = self.store.get_run(action["run_id"])
        def preflight(_):
            self.preflight(state, action["kind"])
        def invoke(payload):
            value, result, digest = self._tool(TOOLS[action["kind"]], payload, call_id=action_id)
            return dict(response=self._summary(action, value, result, digest), call_closed=result["call_closed"])
        try:
            receipt = self.store.perform(action_id, self.config["binding"], invoke, preflight=preflight)
        except m.ControlError:
            # Only a completed local CLI record proves both exit and pipe EOF.
            path = self.calls.root / action_id / "result.json"
            if path.exists() and self.store.get_action(action_id)["state"] == "RESPONDED":
                result = response_json(retained(path))
                if result.get("call_closed"):
                    self.store.execute(dict(schema="production-plane-v1", request_id="close:" + action_id,
                        operation="close_call", body=dict(action_id=action_id, evidence_ref="cli:" + action_id)))
            raise
        self._observe(action_id)
        return receipt

    def _observe(self, action_id):
        action = self.store.get_action(action_id)
        response = action["response"]
        state = self.store.get_run(action["run_id"])
        if not state["attempt"]["submission_recorded"] or action["attempt_id"] != state["attempt"]["attempt_id"]:
            return
        observation = response.get("observation")
        if response.get("remote_error") == "NOT_FOUND" and action["kind"] == "query":
            observation = dict(run_id=state["run_id"], attempt_id=action["attempt_id"],
                operation_id=state["attempt"]["operation_id"], request_digest=state["attempt"]["prepared_request"]["request_digest"],
                remote_state="NOT_FOUND", outcome="UNKNOWN", exited=False, evidence_state="UNKNOWN", evidence_ref="cli:" + action_id)
        if observation:
            self.store.execute(dict(schema="production-plane-v1", request_id="observe:" + action_id,
                                    operation="observe", body=observation))

    def reconcile(self, action_id):
        """Replay local response recording from complete retained bytes; no CLI call."""
        action = self.store.get_action(action_id)
        directory = self.calls.root / action_id
        intent = response_json(retained(directory / "intent.json"))
        m.require(intent["binding"] == dict(tool=TOOLS[action["kind"]], arguments=action["payload"],
            store_id=self.store.identity["store_id"], config_sha256=m.digest(self.config)),
            "Retained call differs from action", "IDENTITY_CONFLICT")
        if (directory/'not-started.json').exists():
            self.store.execute(dict(schema="production-plane-v1", request_id="not-started:" + action_id,
                operation="not_started", body=dict(action_id=action_id, evidence_ref="cli:" + action_id)))
            return self.store.get_action(action_id)
        m.require((directory/'result.json').exists() and (directory/'endpoint.json').exists(),
                  "Original call lacks completed collection or endpoint verification", "PENDING")
        endpoint = response_json(retained(directory/'endpoint.json'))
        m.require(endpoint.get('config_sha256') == m.digest(self.config), "Endpoint binding differs", "IDENTITY_CONFLICT")
        result = response_json(retained(directory / "result.json"))
        m.require(result["call_closed"] and result["reason"] is None, "Original collection remains incomplete", "PENDING")
        raw = self.calls.read(action_id, "stdout" if result["exit_code"] == 0 else "stderr")
        value = response_json(raw)
        if result["exit_code"]:
            m.require(result["exit_code"] == 2 and isinstance(value.get("error"), dict), "Invalid retained error", "PROTOCOL_ERROR")
            value = dict(remote_error=value["error"]["code"])
        summary = self._summary(action, value, result, sha(raw))
        if action["response"] != summary:
            self.store.execute(dict(schema="production-plane-v1", request_id="reconcile:" + action_id,
                operation="respond", body=dict(action_id=action_id, response=summary, call_closed=True)))
        self._observe(action_id)
        return self.store.get_action(action_id)

    def download(self, action_id, directory):
        """Retain a verified completed status and all three sealed files."""
        from local_hand_jobs.evidence_client import BoundedFileWriter, EvidenceClient
        action = self.store.get_action(action_id)
        state = self.store.get_run(action["run_id"])
        self.preflight(state, "query")
        m.require(action["attempt_id"] == state["attempt"]["attempt_id"]
                  and action["kind"] in ("submit", "query", "cancel"), "Status action differs from current attempt", "CONFLICT")
        raw = self.calls.read(action_id)
        m.require(sha(raw) == action["response"]["response_sha256"], "Status bytes changed", "IDENTITY_CONFLICT")
        value = response_json(raw)
        m.require(all(value.get(k) == v for k,v in dict(lifecycle="TERMINAL", phase="EXITED", outcome="SUCCEEDED", evidence="SEALED").items()),
                  "Only successful sealed tasks can be archived", "NOT_READY")
        self._summary(action, value, response_json(retained(self.calls.root/action_id/'result.json')), sha(raw))
        refs = value.get("seal_refs")
        m.require(type(refs) is list and len(refs) == 1 and type(refs[0]) is dict, "Expected one trusted seal", "PROTOCOL_ERROR")
        operation = value["operation_id"]
        artifacts, cursor, catalog, seen = [], None, None, set()
        for _ in range(16):
            page, _, _ = self._tool("lh_evidence_manifest", dict(operation_id=operation, page_size=100,
                **({"cursor": cursor} if cursor else {})))
            m.require(page.get("operation_id") == operation and type(page.get("artifacts")) is list,
                      "Evidence manifest task differs", "IDENTITY_CONFLICT")
            catalog = catalog or page.get("catalog_digest")
            m.require(catalog and page.get("catalog_digest") == catalog, "Evidence catalog changed", "CONFLICT")
            artifacts.extend(page["artifacts"])
            cursor = page.get("next_cursor")
            if cursor is None:
                break
            m.require(type(cursor) is str and cursor not in seen, "Invalid evidence pagination", "PROTOCOL_ERROR")
            seen.add(cursor)
        else:
            raise m.ControlError("LIMIT", "Evidence page budget exhausted")
        selected = [a for a in artifacts if a.get("seal_id") == refs[0].get("seal_id")]
        m.require(len(selected) == 3 and {a.get("role") for a in selected} == {"zip", "manifest", "seal"}
                  and all(a.get("operation_id") == operation and a.get("seal_sha256") == refs[0].get("seal_sha256") for a in selected),
                  "Manifest differs from trusted seal", "IDENTITY_CONFLICT")
        root = private_directory(directory)
        for name, data in (("status.json", raw), ("binding.json", m.canonical(dict(action_id=action_id, status_sha256=sha(raw), seal=refs[0])))):
            if (root/name).exists():
                m.require(retained(root/name) == data, "Existing evidence binding differs", "CONFLICT")
            else:
                publish(root/name, data)
        calls = 0
        def callback(tool, args):
            nonlocal calls
            calls += 1
            m.require(calls <= 1024, "Evidence read call budget exhausted", "LIMIT")
            reply, _, _ = self._tool(tool, args)
            return reply
        client = EvidenceClient(callback)
        for artifact in selected:
            role = artifact["role"]
            path = client.download(artifact, BoundedFileWriter(root/role, max_bytes=16*1024*1024))
            data = path.read_bytes()
            target = root / ("evidence." + role)
            if target.exists():
                m.require(retained(target, 16*1024*1024) == data, "Existing evidence file differs", "CONFLICT")
            else:
                publish(target, data)
        return dict(directory=str(root), status_sha256=sha(raw), operation_id=operation, seal=refs[0])
