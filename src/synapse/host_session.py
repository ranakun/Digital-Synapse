"""Trusted owner-interaction adapter; never registered as worker MCP tools.

A native host supplies its own owner-event reader and display callback. Worker
requests cannot supply either one. This is an orchestration boundary: it is
not intended to isolate hostile programs running as the same operating user.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from pathlib import Path

from synapse.owner_host import OwnerHost
from synapse.publication import Publisher
from synapse.revisions import durable_write
from synapse.runs import RunManager
from synapse.source_store import prepare_source
from synapse.specialist import Specialist, request_for
from synapse.util import generate_ulid, utc_now
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes


class NativeHost:
    def __init__(self, vault: Path, *, event_reader: Callable[[str], dict], display: Callable[[str], None], reasoner, host_id="native"):
        self.vault = Path(vault)
        self.publisher = Publisher(self.vault)
        self.owner = OwnerHost(self.publisher.store, host_id=host_id)
        self.event_reader = event_reader
        self.display = display
        self.reasoner = reasoner
        self.host_id = host_id
        self._active_display: str | None = None

    def _event(self, reference: str) -> dict:
        try:
            event = self.event_reader(reference)
        except (KeyError, ValueError) as exc:
            raise V2Error("approval-required", "The host could not resolve that owner input event") from exc
        if not isinstance(event, dict) or event.get("id") != reference or event.get("actor") != "user" or not isinstance(event.get("text"), str) or not event["text"].strip():
            raise V2Error("approval-required", "Only an actual owner input event can grant this action")
        return copy.deepcopy(event)

    def consult(self, purpose: str, *, context="No additional context supplied.", subject_ids=None, cancelled=None) -> dict:
        return Specialist(self.vault, self.reasoner).run(request_for(purpose, context=context, subject_ids=subject_ids), cancelled=cancelled)

    def capture_file(self, owner_event_ref: str, source: Path, *, operation_id=None, request_id=None) -> dict:
        from synapse.source_extractors import retain_source_file

        self._event(owner_event_ref)
        self.publisher.store.head()
        operation_id = operation_id or generate_ulid()
        request_id = request_id or operation_id
        with self.publisher.store.operation_lock(operation_id):
            path = self._capture_path(operation_id)
            if path.exists():
                entry = self._journal(path)
                if entry["owner_event_ref"] != owner_event_ref or entry["request_id"] != request_id or entry["descriptor"]["origin"] != str(source):
                    raise V2Error("idempotency-conflict", "Capture retry identifies different material or owner input")
                descriptor, objects = entry["descriptor"], {}
            else:
                descriptor, objects = retain_source_file(source)
            receipt = self._capture(owner_event_ref, descriptor, objects, operation_id=operation_id, request_id=request_id)
            return self._prepare_capture(owner_event_ref, operation_id, receipt)

    def capture_message(self, instruction_ref: str, material_ref: str, *, operation_id=None, request_id=None) -> dict:
        self._event(instruction_ref)
        self.publisher.store.head()
        try:
            material = self.event_reader(material_ref)
        except (KeyError, ValueError) as exc:
            raise V2Error("source-unavailable", "The actual conversation message could not be resolved") from exc
        if not isinstance(material, dict) or material.get("id") != material_ref or material.get("actor") not in {"user", "assistant"} or not isinstance(material.get("text"), str) or not material["text"].strip():
            raise V2Error("source-unavailable", "Capture needs an actual user or assistant message; tool output cannot grant authority")
        operation_id = operation_id or generate_ulid()
        with self.publisher.store.operation_lock(operation_id):
            descriptor, objects = prepare_source(material["text"].encode(), origin=f"conversation:{self.host_id}:{material['actor']}:{material_ref}")
            receipt = self._capture(instruction_ref, descriptor, objects, operation_id=operation_id, request_id=request_id)
            return self._prepare_capture(instruction_ref, operation_id, receipt)

    def _capture_path(self, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", operation_id):
            raise V2Error("invalid-request", "Invalid capture operation ID")
        return self.publisher.store.root / "host-captures" / f"{operation_id}.json"

    @staticmethod
    def _journal(path):
        try:
            value = json.loads(path.read_bytes())
            if not isinstance(value, dict):
                raise ValueError("not an object")
            return value
        except (OSError, ValueError) as exc:
            raise V2Error("recovery-required", "Host operation journal is unreadable") from exc

    def _save_journal(self, path, entry):
        with self.publisher.store.writer_lock():
            durable_write(path, canonical_json(entry))

    def _capture(self, instruction_ref, descriptor, objects, *, operation_id=None, request_id=None):
        operation_id = operation_id or generate_ulid()
        request_id = request_id or operation_id
        if not re.fullmatch(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", operation_id):
            raise V2Error("invalid-request", "Invalid capture operation ID")
        path = self._capture_path(operation_id)
        with self.publisher.store.writer_lock():
            if path.exists():
                prepared = json.loads(path.read_bytes())
                old = prepared["descriptor"]
                if prepared["owner_event_ref"] != instruction_ref or prepared["request_id"] != request_id or old["origin"] != descriptor["origin"] or old["original_hash"] != descriptor["original_hash"]:
                    raise V2Error("idempotency-conflict", "Capture retry identifies different material or owner input")
                descriptor = old
            else:
                self.publisher.store._persist_objects(objects)
                durable_write(path, canonical_json({"owner_event_ref": instruction_ref, "request_id": request_id, "descriptor": descriptor}))
        capability = self.owner.record_instruction(instruction_ref, actions=["capture"], scope={"capture_targets": {descriptor["origin"]: descriptor["original_hash"]}})
        return self.publisher.capture(capability, descriptor, objects, operation_id=operation_id, request_id=request_id)

    def _run_preparation(self, instruction_ref, path, entry, source_refs):
        from synapse.preparation import Preparation, request_for_preparation

        if "preparation_result" in entry and not entry["preparation_result"].get("recovery_pending"):
            return entry["preparation_result"]
        if "preparation_request" not in entry:
            request = request_for_preparation(source_refs, instruction_ref, revision=self.publisher.store.head())
            run_id = generate_ulid()
            capability = self.owner.record_instruction(instruction_ref, actions=["prepare"], scope={"run_id": run_id, "source_refs": request["source_refs"], "request_hash": hash_bytes(canonical_json(request))})
            entry.update(preparation_request=request, preparation_run_id=run_id, preparation_capability=capability)
            self._save_journal(path, entry)
        request, run_id, capability = entry["preparation_request"], entry["preparation_run_id"], entry["preparation_capability"]
        manager = RunManager(self.vault)
        run = manager.start_preparation(request, capability, run_id=run_id)
        if entry.get("preparation_started") and not run.get("prepared_output"):
            if run["status"] == "running":
                run = manager.status(run_id)
                if run["status"] == "running":
                    run = manager.finish(run_id, capability, status="partial", stop_reason="Interrupted preparation requires a new explicit instruction.")
            result = {"run_id": run_id, "status": "complete" if run["status"] == "completed" else run["status"], "lead_refs": [], "navigation_refs": [], "coverage": {"source_expansions": run["usage"]["source_expansions"]}, "limitations": ["The preparation response was interrupted. Retained results remain discoverable; no reasoning was resumed. A partial run needs a new explicit preparation request."]}
        else:
            entry["preparation_started"] = True
            self._save_journal(path, entry)
            result = Preparation(self.vault, self.reasoner).run(request, capability=capability, run_id=run_id)
        entry["preparation_result"] = result
        self._save_journal(path, entry)
        return result

    def _prepare_capture(self, instruction_ref, operation_id, receipt):
        from synapse.source_extractors import extract_retained_source

        path = self._capture_path(operation_id)
        entry = self._journal(path)
        if "result" in entry:
            return copy.deepcopy(entry["result"])
        descriptor = entry["descriptor"]
        limitations = []
        preparation = {"status": "partial", "lead_refs": [], "coverage": {}}
        try:
            if descriptor["extraction"]["completeness"] == "pending":
                if "extracted_descriptor" not in entry:
                    extracted, objects = extract_retained_source(descriptor, self.publisher.store.read_object)
                    with self.publisher.store.writer_lock():
                        self.publisher.store._persist_objects(objects)
                        entry.update(extracted_descriptor=extracted, extraction_operation_id=generate_ulid())
                        durable_write(path, canonical_json(entry))
                extracted = entry["extracted_descriptor"]
                capability = self.owner.record_instruction(instruction_ref, actions=["capture"], scope={"capture_targets": {descriptor["origin"]: descriptor["original_hash"]}})
                self.publisher.capture(capability, extracted, {}, operation_id=entry["extraction_operation_id"], request_id=entry["request_id"], expected_version=descriptor["version"])
                descriptor = extracted
            source_refs = [{"source_id": descriptor["id"], "source_version": descriptor["version"]}]
            if descriptor.get("text_version"):
                preparation = self._run_preparation(instruction_ref, path, entry, source_refs)
            else:
                limitations.append("Original retained; extracted text is unavailable, so preparation is incomplete.")
        except Exception as exc:
            preparation = {"status": "failed", "lead_refs": [], "coverage": {}}
            limitations.append(f"Original capture succeeded; preparation did not complete ({getattr(exc, 'code', type(exc).__name__)}).")
        state = preparation["status"]
        result = dict(receipt, capture_receipt=receipt, source_refs=[{"source_id": descriptor["id"], "source_version": descriptor["version"]}], prepared_knowledge_revision=self.publisher.store.head(), search_state=("partial" if descriptor["extraction"]["completeness"] == "partial" else "available") if descriptor.get("text_version") else "unavailable", preparation_run_id=entry.get("preparation_run_id"), preparation_state="complete" if state == "completed" else state, lead_refs=preparation.get("lead_refs", []), coverage=preparation.get("coverage", {}), limitations=limitations + preparation.get("limitations", []))
        if preparation.get("recovery_pending"):
            result["recovery_pending"] = True
        if state != "failed" and not preparation.get("recovery_pending"):
            entry["result"] = result
        self._save_journal(path, entry)
        return copy.deepcopy(result)

    def prepare_sources(self, owner_event_ref: str, source_ids: list[str], *, operation_id=None, request_id=None) -> dict:
        """Explicit bounded backfill, also used to continue incomplete preparation."""
        self._event(owner_event_ref)
        self.publisher.store.head()
        if not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 8 or any(not isinstance(identity, str) for identity in source_ids) or len(set(source_ids)) != len(source_ids):
            raise V2Error("invalid-request", "Select one to eight distinct retained sources")
        operation_id = operation_id or generate_ulid()
        request_id = request_id or operation_id
        with self.publisher.store.operation_lock(operation_id):
            path = self.publisher.store.root / "host-preparations" / f"{operation_id}.json"
            if path.exists():
                entry = self._journal(path)
                if entry["owner_event_ref"] != owner_event_ref or entry["request_id"] != request_id or entry["source_ids"] != sorted(source_ids):
                    raise V2Error("idempotency-conflict", "Preparation retry identifies another source selection or instruction")
            else:
                manifest = self.publisher.store.manifest()
                if any(identity not in manifest["sources"] for identity in source_ids):
                    raise V2Error("source-unavailable", "A selected retained source is unavailable")
                entry = {"owner_event_ref": owner_event_ref, "request_id": request_id, "source_ids": sorted(source_ids), "source_refs": [{"source_id": identity, "source_version": manifest["sources"][identity]} for identity in sorted(source_ids)]}
                self._save_journal(path, entry)
            if "preparation_request" not in entry:
                self._extract_selection(owner_event_ref, path, entry)
            return self._run_preparation(owner_event_ref, path, entry, entry["source_refs"])

    def _extract_selection(self, owner_event_ref, path, entry):
        from synapse.source_extractors import extract_retained_source

        refs = []
        for ref in entry["source_refs"]:
            manifest = self.publisher.store.manifest()
            descriptor = manifest["source_versions"][ref["source_version"]]
            if not descriptor.get("text_version"):
                extractions = entry.setdefault("extractions", {})
                if ref["source_id"] not in extractions:
                    if manifest["sources"].get(ref["source_id"]) != descriptor["version"]:
                        raise V2Error("stale-selection", "Selected source changed before extraction")
                    extracted, objects = extract_retained_source(descriptor, self.publisher.store.read_object)
                    with self.publisher.store.writer_lock():
                        self.publisher.store._persist_objects(objects)
                        extractions[ref["source_id"]] = {"descriptor": extracted, "operation_id": generate_ulid(), "expected_version": descriptor["version"]}
                        durable_write(path, canonical_json(entry))
                extraction = extractions[ref["source_id"]]
                extracted = extraction["descriptor"]
                if extracted["version"] != extraction["expected_version"]:
                    capability = self.owner.record_instruction(owner_event_ref, actions=["capture"], scope={"capture_targets": {descriptor["origin"]: descriptor["original_hash"]}})
                    self.publisher.capture(capability, extracted, {}, operation_id=extraction["operation_id"], request_id=entry["request_id"], expected_version=extraction["expected_version"])
                descriptor = extracted
            refs.append({"source_id": descriptor["id"], "source_version": descriptor["version"]})
        entry["source_refs"] = refs
        self._save_journal(path, entry)

    def start_investigation(self, owner_event_ref: str, *, purpose: str, context="No additional context supplied.", subject_ids=None, preset="focused") -> dict:
        self._event(owner_event_ref)
        run_id = generate_ulid()
        request = request_for(purpose, mode="investigate", context=context, subject_ids=subject_ids, preset=preset, owner_instruction_ref=owner_event_ref)
        capability = self.owner.record_instruction(owner_event_ref, actions=["investigate", "admit", "stage"], scope={"run_id": run_id, "subject_ids": request["subject_ids"]})
        run = RunManager(self.vault).start(request, capability, run_id=run_id)
        return {"run": run, "capability": capability}

    def investigate(self, delegation: dict, *, cancelled=None, research=None, progress=None) -> dict:
        run = delegation["run"]
        return Specialist(self.vault, self.reasoner, research=research, progress=progress).run(run["request"], capability=delegation["capability"], run_id=run["id"], cancelled=cancelled)

    def resume(self, owner_event_ref: str, run_id: str, *, accept_current_revision=False, preset="focused") -> dict:
        self._event(owner_event_ref)
        manager = RunManager(self.vault)
        old = manager.get(run_id)
        request = request_for(old["request"]["purpose"], mode="continue", context=old["request"]["context"], subject_ids=old["request"]["subject_ids"], preset=preset, owner_instruction_ref=owner_event_ref, continuation_id=run_id)
        capability = self.owner.record_instruction(owner_event_ref, actions=["resume", "investigate", "admit", "stage"], scope={"run_id": run_id, "subject_ids": request["subject_ids"]})
        run = manager.resume(run_id, request, capability, accept_current_revision=accept_current_revision)
        return {"run": run, "capability": capability}

    def stage(self, delegation: dict, packet: dict, objects: dict) -> dict:
        return self.publisher.stage(delegation["capability"], packet, objects, semantic_reviewer=self.reasoner.review)

    def show_proposal(self, proposal_id: str, version: str) -> dict:
        packet = self.publisher.proposal(proposal_id, version)
        display_id = generate_ulid()
        # The callback must actually present the brief before it is eligible.
        self.display(packet["brief"])
        entry = {"id": display_id, "host_id": self.host_id, "created_at": utc_now(), "proposal_id": proposal_id, "proposal_version": version, "brief": packet["brief"], "presented_group_ids": packet["presented_group_ids"], "status": "active", "operation_id": generate_ulid(), "request_id": generate_ulid()}
        with self.publisher.store.writer_lock():
            durable_write(self.publisher.store.root / "host-displays" / f"{display_id}.json", canonical_json(entry))
        self._active_display = display_id
        return {"display_id": display_id, "brief": packet["brief"], "groups": packet["presented_group_ids"]}

    def _display(self, display_id: str | None) -> dict:
        identity = display_id or self._active_display
        if not isinstance(identity, str) or not re.fullmatch(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", identity):
            raise V2Error("approval-required", "No unambiguous active displayed brief is selected")
        try:
            entry = json.loads((self.publisher.store.root / "host-displays" / f"{identity}.json").read_bytes())
        except (OSError, ValueError) as exc:
            raise V2Error("approval-required", "The displayed brief is unavailable") from exc
        if entry.get("host_id") != self.host_id:
            raise V2Error("approval-required", "This brief belongs to a different interaction host")
        return entry

    def reply(self, owner_event_ref: str, *, display_id: str | None = None) -> dict:
        """One clear owner reply selects the exact already displayed groups."""
        event, entry = self._event(owner_event_ref), self._display(display_id)
        if entry.get("status") == "committed":
            receipt = self.publisher.store.receipt(entry["operation_id"])
            return {"receipt": receipt, "status": "committed"}
        if entry.get("status") != "active":
            raise V2Error("approval-required", "This brief is no longer active")
        text = event["text"].strip().casefold().rstrip(".!")
        groups = entry["presented_group_ids"]
        if text in {"not now", "leave this for later", "defer", "keep as a possibility", "keep it as a possibility", "decline adoption", "do not adopt"}:
            entry["status"] = "deferred" if text in {"not now", "leave this for later", "defer"} else "declined-adoption"
            entry["owner_event_ref"] = owner_event_ref
            with self.publisher.store.writer_lock():
                durable_write(self.publisher.store.root / "host-displays" / f"{entry['id']}.json", canonical_json(entry))
            return {"status": entry["status"], "published": False, "suggestions_remain_available": True}
        if text in {"approve both", "accept both"} and len(groups) != 2:
            raise V2Error("ambiguous-identity", "Both does not identify a selection from this brief")
        if text in {"yes", "approve", "approved", "approve all", "approve both", "go ahead", "accept all", "accept both"}:
            selected = groups
        else:
            match = re.fullmatch(r"(?:approve|accept)\s+(?:group\s+)?(.+)", text)
            words = re.split(r"\s*(?:,|\band\b)\s*", match[1]) if match else []
            positions = {"first": 0, "second": 1, "third": 2, "1": 0, "2": 1, "3": 2}
            selected = []
            for word in words:
                if word in groups:
                    selected.append(word)
                elif word in positions and positions[word] < len(groups):
                    selected.append(groups[positions[word]])
                else:
                    selected = []
                    break
            if not selected:
                resolver = getattr(self.reasoner, "selection", None)
                resolved = resolver(brief=entry["brief"], groups=groups, reply=event["text"]) if callable(resolver) else {}
                selected = resolved.get("selected_group_ids", [])
                if resolved.get("action") != "approve" or not isinstance(selected, list) or not selected or len(set(selected)) != len(selected) or not set(selected) <= set(groups):
                    raise V2Error("ambiguous-identity", "The reply is unclear and does not clearly approve a presented selection")
        packet = self.publisher.proposal(entry["proposal_id"], entry["proposal_version"])
        capability = self.owner.approve_displayed(packet, selected, displayed_brief=entry["brief"], owner_message_ref=owner_event_ref)
        result = self.publisher.publish(capability, packet["id"], packet["version"], selected, operation_id=entry["operation_id"], request_id=entry["request_id"])
        entry.update(status="committed", owner_event_ref=owner_event_ref, selected_group_ids=selected)
        with self.publisher.store.writer_lock():
            durable_write(self.publisher.store.root / "host-displays" / f"{entry['id']}.json", canonical_json(entry))
        self._active_display = None
        return result

    def dispose(self, owner_event_ref: str, identities: list[str], disposition: str, *, operation_id=None, request_id=None) -> dict:
        self._event(owner_event_ref)
        operation_id = operation_id or generate_ulid()
        request_id = request_id or operation_id
        if not re.fullmatch(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", operation_id):
            raise V2Error("invalid-request", "Invalid disposition operation ID")
        manifest = self.publisher.store.manifest()
        if any(identity not in manifest["records"] for identity in identities):
            raise V2Error("ambiguous-identity", "Disposition identity is not present")
        path = self.publisher.store.root / "host-dispositions" / f"{operation_id}.json"
        scope = {"records": {identity: manifest["records"][identity]["version"] for identity in identities}, "disposition": disposition}
        with self.publisher.store.writer_lock():
            if path.exists():
                prepared = json.loads(path.read_bytes())
                if prepared["owner_event_ref"] != owner_event_ref or prepared["request_id"] != request_id or prepared["scope"]["disposition"] != disposition or set(prepared["scope"]["records"]) != set(identities):
                    raise V2Error("idempotency-conflict", "Disposition retry identifies different work")
                scope = prepared["scope"]
            else:
                durable_write(path, canonical_json({"owner_event_ref": owner_event_ref, "request_id": request_id, "scope": scope}))
        capability = self.owner.record_instruction(owner_event_ref, actions=["dispose"], scope=scope)
        return self.publisher.disposition(capability, identities, disposition, operation_id=operation_id, request_id=request_id)
