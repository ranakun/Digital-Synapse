"""Small trusted-parent control service shared by native and terminal hosts.

There is deliberately no worker MCP registration. The caller has resolved the
owner's actual instruction; the worker only receives a run scoped delegation.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from synapse.knowledge import encode_record
from synapse.proposal_builder import build_proposal
from synapse.research_sources import WebResearch
from synapse.revisions import durable_write
from synapse.runs import RunManager
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, canonical_json


def read_json(path: Path) -> dict:
    if Path(path).name == ".env":
        raise V2Error("invalid-path", "Environment secret files are not Synapse inputs")
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise V2Error("invalid-request", "The input is not a readable JSON object") from exc
    if not isinstance(value, dict):
        raise V2Error("invalid-request", "The input must be a JSON object")
    return value


class HostControl:
    def __init__(self, host):
        self.host = host
        self.store = host.publisher.store

    def _path(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", run_id):
            raise V2Error("invalid-request", "Invalid run identity")
        return self.store.root / "host-delegations" / f"{run_id}.json"

    def _save(self, delegation):
        with self.store.writer_lock():
            durable_write(self._path(delegation["run"]["id"]), canonical_json({"host_id": self.host.host_id, **delegation}))

    def delegation(self, run_id):
        value = read_json(self._path(run_id))
        if value["host_id"] != self.host.host_id:
            raise V2Error("approval-required", "This delegation belongs to another host; explicitly continue it in this host")
        value["run"] = RunManager(self.host.vault).get(run_id)
        return value

    def execute(self, operation: str, arguments: dict, *, owner_event_ref: str | None = None, cancelled=None, progress=None) -> dict:
        args = copy.deepcopy(arguments)
        if operation == "status":
            RunManager(self.host.vault).status(args["run_id"])
            return RunManager(self.host.vault).public(args["run_id"])
        if operation == "receipt":
            receipt = self.store.receipt(args["operation_id"])
            if not receipt:
                raise V2Error("revision-unavailable", "No committed operation receipt matches")
            return receipt
        if operation == "proposal":
            return self.host.publisher.proposal(args["proposal_id"], args["version"])
        if operation == "bind-display":
            return self.host.bind_display(args["proposal_id"], args["version"], args["assistant_event_ref"])
        if operation == "execute-run":
            delegation = self.delegation(args["run_id"])
            try:
                return self.host.investigate(delegation, cancelled=cancelled, progress=progress, research=WebResearch(self.host) if args.get("research", True) else None)
            except KeyboardInterrupt:
                RunManager(self.host.vault).cancel(args["run_id"], delegation["capability"])
                raise
        if operation in {"admit", "stage"}:
            delegation = self.delegation(args.pop("run_id"))
            if operation == "admit":
                records = {f"entities/insights/{record['id']}.md": encode_record(record) for record in args["records"]}
                return self.host.publisher.admit(delegation["capability"], records, run_id=delegation["run"]["id"], operation_id=args.get("operation_id") or generate_ulid(), request_id=args.get("request_id") or delegation["run"]["request"]["id"], expected_versions=args.get("expected_versions"))
            for group in args["groups"]:
                for change in group["changes"]:
                    if not isinstance(change.get("raw"), str):
                        raise V2Error("invalid-request", "Stage changes require exact UTF-8 Markdown in raw")
                    change["raw"] = change["raw"].encode()
            packet, objects = build_proposal(self.store, run_id=delegation["run"]["id"], **args)
            staged = self.host.stage(delegation, packet, objects)
            return {"proposal_id": staged["id"], "version": staged["version"], "brief": staged["brief"], "presented_group_ids": staged["presented_group_ids"], "semantic_review": staged["semantic_review"]}
        if not owner_event_ref:
            raise V2Error("approval-required", "This action requires the actual owner instruction")
        self.host._event(owner_event_ref)
        if operation == "capture-file":
            return self.host.capture_file(owner_event_ref, Path(args["path"]), operation_id=args.get("operation_id"), request_id=args.get("request_id"))
        if operation == "capture-message":
            return self.host.capture_message(owner_event_ref, args["material_ref"], operation_id=args.get("operation_id"), request_id=args.get("request_id"))
        if operation == "prepare-sources":
            return self.host.prepare_sources(owner_event_ref, **args)
        if operation == "start":
            delegation = self.host.start_investigation(owner_event_ref, **args)
            self._save(delegation)
            return RunManager(self.host.vault).public(delegation["run"]["id"])
        if operation == "continue":
            delegation = self.host.resume(owner_event_ref, **args)
            self._save(delegation)
            return RunManager(self.host.vault).public(delegation["run"]["id"])
        if operation == "cancel":
            delegation = self.delegation(args["run_id"])
            RunManager(self.host.vault).cancel(args["run_id"], delegation["capability"])
            return RunManager(self.host.vault).public(args["run_id"])
        if operation == "reply":
            return self.host.reply(owner_event_ref, display_id=args["display_id"])
        if operation == "dispose":
            return self.host.dispose(owner_event_ref, **args)
        if operation == "activate":
            from synapse.v2_migration import activate, prepare
            snapshot = prepare(self.host.vault)
            if args.get("snapshot_hash") != snapshot.review_fingerprint:
                raise V2Error("stale-selection", "The reviewed migration snapshot differs from the current vault")
            capability = self.host.owner.record_instruction(owner_event_ref, actions=["capture"], scope={"bootstrap": True, "snapshot_hash": snapshot.fingerprint})
            return activate(self.host.vault, snapshot, capability, operation_id=args.get("operation_id") or generate_ulid(), request_id=args.get("request_id") or generate_ulid())
        raise V2Error("unsupported-operation", "Unknown trusted host operation")
