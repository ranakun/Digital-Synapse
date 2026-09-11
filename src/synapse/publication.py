"""Authorized capture, suggestion and exact adoption services."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from synapse.knowledge import decode_record, encode_record, record_descriptor
from synapse.owner_host import check_capability, selection_hash
from synapse.revisions import RevisionStore, durable_write
from synapse.util import generate_ulid, utc_now
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes, validate_payload, version_for
from synapse.v2_proposals import (
    check_dependency_cycles,
    project_selection,
    validate_knowledge,
    validate_packet_structure,
    workspace_today,
)


def _fingerprint(value: Any) -> str:
    return hash_bytes(canonical_json(value))


class Publisher:
    def __init__(self, vault: Path):
        self.store = RevisionStore(vault)

    def _refresh(self, receipt, identities):
        previous = self.store.manifest(receipt["knowledge_revision"])["parent"]
        try:
            self.store.refresh_checkout(previous_revision=previous, identities=identities)
        except (OSError, V2Error):
            # Canonical commit already succeeded; readiness/recovery inspects
            # editable projection failures separately from immutable receipts.
            pass

    def bootstrap(
        self,
        capability: dict[str, str],
        *,
        operation_id: str,
        request_id: str,
        records: dict[str, bytes],
        sources: list[dict[str, Any]] | None = None,
        objects: dict[str, bytes] | None = None,
        legacy_edges: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Adopt a reviewed initial snapshot without rewriting legacy facts."""
        prepared = dict(objects or {})
        rows = {}
        for path, raw in records.items():
            row = record_descriptor(raw, path=path)
            if row["id"] in rows:
                raise V2Error("ambiguous-identity", "Baseline contains duplicate IDs")
            rows[row["id"]] = row
            prepared[row["version"]] = raw
        payload_hash = _fingerprint(
            {"records": rows, "sources": sources or [], "legacy_edges": legacy_edges or []}
        )

        def mutate(manifest, _read):
            event = check_capability(self.store, capability, action="capture")
            if (
                not event["scope"].get("bootstrap")
                or event["scope"].get("snapshot_hash") != payload_hash
            ):
                raise V2Error(
                    "approval-required",
                    "Baseline activation requires approval of this exact snapshot",
                )
            manifest["records"] = rows
            manifest["legacy_edges"] = legacy_edges or []
            for source in sources or []:
                manifest["source_versions"][source["version"]] = source
                manifest["sources"][source["id"]] = source["version"]

        return self.store.transact(
            operation_id=operation_id,
            request_id=request_id,
            kind="capture",
            payload_hash=payload_hash,
            mutate=mutate,
            objects=prepared,
            initialize=True,
            receipt_fields={
                "limitations": [
                    "Baseline originals retained; classification is not a new factual approval or owner verification."
                ]
            },
        )

    def capture(
        self,
        capability: dict[str, str],
        descriptor: dict[str, Any],
        objects: dict[str, bytes],
        *,
        operation_id: str,
        request_id: str,
        expected_version: str | None = None,
    ) -> dict[str, Any]:
        validate_payload("source_version", descriptor)

        def mutate(manifest, _read):
            event = check_capability(self.store, capability, action="capture")
            targets = event["scope"].get("capture_targets", {})
            if targets.get(descriptor["origin"]) != descriptor["original_hash"]:
                raise V2Error(
                    "approval-required",
                    "Capture instruction does not identify these original bytes",
                )
            previous = manifest["sources"].get(descriptor["id"])
            if expected_version is not None and previous != expected_version:
                raise V2Error("stale-selection", "Source changed before retained extraction could publish")
            if previous:
                old = manifest["source_versions"][previous]
                if old["source_family_id"] != descriptor["source_family_id"]:
                    raise V2Error(
                        "invalid-source",
                        "A retained source cannot silently change its evidence family",
                    )
            manifest["source_versions"][descriptor["version"]] = copy.deepcopy(descriptor)
            manifest["sources"][descriptor["id"]] = descriptor["version"]

        return self.store.transact(
            operation_id=operation_id,
            request_id=request_id,
            kind="capture",
            payload_hash=_fingerprint(descriptor if expected_version is None else {"descriptor": descriptor, "expected_version": expected_version}),
            mutate=mutate,
            objects=objects,
        )

    def _run(self, capability: dict[str, str], run_id: str, action: str) -> dict[str, Any]:
        event = check_capability(self.store, capability, action=action)
        if event["scope"].get("run_id") != run_id:
            raise V2Error(
                "approval-required", "Capability belongs to a different requested investigation"
            )
        if not run_id.isalnum() or len(run_id) != 26:
            raise V2Error("invalid-request", "Invalid investigation identity")
        try:
            run = json.loads((self.store.root / "runs" / f"{run_id}.json").read_bytes())
        except (OSError, ValueError) as exc:
            raise V2Error("approval-required", "Requested investigation is unavailable") from exc
        allowed_states = {"running", "completed"} if action == "stage" else {"running"}
        if run.get("status") not in allowed_states or run.get("owner_event_id") != event["id"]:
            raise V2Error(
                "approval-required",
                "This investigation is not active under the supplied owner event",
            )
        if action == "prepare":
            request = run.get("request", {})
            if (
                request.get("mode") != "prepare"
                or event["scope"].get("request_hash") != _fingerprint(request)
                or event["scope"].get("source_refs") != request.get("source_refs")
                or event.get("owner_message_ref") != request.get("owner_instruction_ref")
            ):
                raise V2Error("approval-required", "Preparation authority does not match this source request")
            deadline = run.get("segments", [{}])[-1].get("deadline_at")
            if not deadline or datetime.now(UTC) >= datetime.fromisoformat(deadline.replace("Z", "+00:00")):
                raise V2Error("invalid-request", "Preparation budget has expired")
        elif run.get("request", {}).get("mode") == "prepare":
            raise V2Error("approval-required", "Preparation cannot admit general findings or stage adoption")
        return run

    def admit(
        self,
        capability: dict[str, str],
        records: dict[str, bytes],
        *,
        run_id: str,
        operation_id: str,
        request_id: str,
        expected_versions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._admit(capability, records, run_id=run_id, operation_id=operation_id, request_id=request_id, expected_versions=expected_versions)

    def admit_preparation(
        self, capability: dict[str, str], records: dict[str, bytes], *, run_id: str, operation_id: str, request_id: str
    ) -> dict[str, Any]:
        return self._admit(capability, records, run_id=run_id, operation_id=operation_id, request_id=request_id, preparation=True)

    def _admit(
        self, capability, records, *, run_id, operation_id, request_id, expected_versions=None, preparation=False
    ):
        objects, rows = {}, {}
        for path, raw in records.items():
            row = record_descriptor(raw, path=path)
            if row["id"] in rows:
                raise V2Error("ambiguous-identity", "Suggestion IDs must be unique")
            rows[row["id"]], objects[row["version"]] = row, raw
        if not rows:
            raise V2Error("invalid-request", "Admission needs at least one complete finding")

        def mutate(manifest, read):
            run = self._run(capability, run_id, "prepare" if preparation else "admit")
            if preparation:
                frozen = {"operation_id": operation_id, "request_id": request_id, "records": {path: hash_bytes(raw) for path, raw in records.items()}}
                if run.get("prepared_output") != frozen:
                    raise V2Error("approval-required", "Preparation admission requires the exact frozen output")
                refs = run["request"]["source_refs"]
                for ref in refs:
                    if manifest["sources"].get(ref["source_id"]) != ref["source_version"]:
                        raise V2Error("stale-selection", "A preparation source changed before admission")
                kinds = {"question": 0, "navigation": 0}
                for path, raw in records.items():
                    value = decode_record(raw, path=path)
                    kind = value.get("record_kind")
                    if kind not in kinds or not value.get("evidence") or value.get("relationship") or value.get("dependencies"):
                        raise V2Error("invalid-record", "Preparation admits only source-anchored questions and navigation hints")
                    if value.get("owner_position") not in {"unreviewed", "not-applicable"}:
                        raise V2Error("invalid-record", "Preparation cannot claim an owner position")
                    kinds[kind] += 1
                    for evidence in value["evidence"]:
                        if {"source_id": evidence.get("source_id"), "source_version": evidence.get("source_version")} not in refs:
                            raise V2Error("approval-required", "Preparation evidence is outside the requested sources")
                budget = run["request"]["budget"]
                if kinds["question"] > budget["max_leads"] or kinds["navigation"] > budget["max_hints"]:
                    raise V2Error("invalid-record", "Preparation output exceeds its lead or hint budget")
            historical = copy.deepcopy(manifest["records"])
            for identity, row in rows.items():
                old = manifest["records"].get(identity)
                if old:
                    if (
                        old["availability"] != "suggestion"
                        or old["disposition"] != "none"
                        or (expected_versions or {}).get(identity) != old["version"]
                    ):
                        raise V2Error(
                            "stale-selection",
                            "Only an exact unreviewed suggestion version can be revised by admission",
                        )
                    if row["path"] != old["path"]:
                        raise V2Error(
                            "invalid-path", "Suggestion revision cannot silently relocate a record"
                        )
                elif identity in (expected_versions or {}):
                    raise V2Error("stale-selection", "The suggested version to revise is absent")
                manifest["records"][identity] = row
            for row in rows.values():
                record = decode_record(read(row["version"]), path=row["path"])
                if "record_kind" not in record or record.get("origin_run_id") != run_id:
                    raise V2Error(
                        "invalid-record", "Suggestions must identify their originating run"
                    )
                subjects = run.get("request", {}).get("subject_ids", [])
                if subjects and record["subject_id"] not in subjects:
                    raise V2Error(
                        "invalid-record", "Finding lies outside the requested subject scope"
                    )
                validate_knowledge(
                    record, manifest, read, admission=True, historical_records=historical, today=workspace_today(self.store.vault)
                )
            check_dependency_cycles(manifest, read, set(rows))

        result = self.store.transact(
            operation_id=operation_id,
            request_id=request_id,
            kind="suggestion-admission",
            payload_hash=_fingerprint(
                {"run_id": run_id, "records": rows, "expected_versions": expected_versions or {}}
            ),
            mutate=mutate,
            objects=objects,
        )
        self._refresh(result, list(rows))
        return result

    def stage(
        self,
        capability: dict[str, str],
        packet: dict[str, Any],
        objects: dict[str, bytes],
        *,
        semantic_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None,
    ) -> dict[str, Any]:
        value = copy.deepcopy(packet)
        value["semantic_review"] = {"status": "pending"}
        value["version"] = version_for("proposal", value)
        validate_packet_structure(value)
        self._run(capability, value["run_id"], "stage")
        manifest = self.store.manifest(value["base_revision"])
        for version, raw in objects.items():
            if hash_bytes(raw) != version:
                raise V2Error("object-integrity", "Proposed bytes do not match their hashes")

        def read(version):
            return objects[version] if version in objects else self.store.read_object(version)

        project_selection(value, value["presented_group_ids"], manifest, read, today=workspace_today(self.store.vault))
        if semantic_reviewer is None:
            raise V2Error(
                "approval-required",
                "A separate semantic comparison must check the brief before it is ready",
            )
        review_input = {"proposal_version": value["version"], "brief": value["brief"], "groups": []}
        for group in value["groups"]:
            if group["id"] not in value["presented_group_ids"]:
                continue
            changes = []
            for operation in group["operations"]:
                before = manifest["records"].get(operation["target_id"])
                changes.append(
                    {
                        "operation": operation,
                        "before": read(before["version"]).decode("utf-8") if before else None,
                        "after": read(operation["after_hash"]).decode("utf-8"),
                    }
                )
            review_input["groups"].append(
                {
                    "id": group["id"],
                    "effects": group["effects"],
                    "requires": group["requires"],
                    "changes": changes,
                }
            )
        verdict = semantic_reviewer(copy.deepcopy(review_input))
        if (
            not isinstance(verdict, dict)
            or verdict.get("passed") is not True
            or verdict.get("proposal_version") != value["version"]
            or not isinstance(verdict.get("reason"), str)
            or not verdict["reason"].strip()
        ):
            raise V2Error(
                "invalid-request", "The independent comparison did not establish a faithful brief"
            )
        review_id = generate_ulid()
        attestation = {
            "id": review_id,
            "proposal_version": value["version"],
            "input_hash": _fingerprint(review_input),
            "reason": verdict["reason"],
            "created_at": utc_now(),
        }
        with self.store.writer_lock():
            self._run(capability, value["run_id"], "stage")
            project_selection(value, value["presented_group_ids"], self.store.manifest(), read, today=workspace_today(self.store.vault))
            self.store._persist_objects(objects)
            directory = self.store.root / "proposals" / value["id"]
            path = directory / f"{value['version']}.json"
            if path.exists() and json.loads(path.read_bytes()) != value:
                raise V2Error("stale-selection", "Immutable proposal version cannot be overwritten")
            durable_write(path, canonical_json(value))
            durable_write(
                directory / f"{value['version']}.review.json", canonical_json(attestation)
            )
        return value | {"semantic_review": {"status": "passed", "review_id": review_id}}

    def proposal(self, proposal_id: str, version: str) -> dict[str, Any]:
        if (
            len(proposal_id) != 26
            or not proposal_id.isalnum()
            or len(version) != 64
            or any(x not in "0123456789abcdef" for x in version)
        ):
            raise V2Error("invalid-request", "Invalid proposal reference")
        directory = self.store.root / "proposals" / proposal_id
        try:
            packet = json.loads((directory / f"{version}.json").read_bytes())
            attestation = json.loads((directory / f"{version}.review.json").read_bytes())
        except (OSError, ValueError) as exc:
            raise V2Error(
                "approval-required", "A reviewed proposal version is unavailable"
            ) from exc
        validate_packet_structure(packet)
        if (
            packet["id"] != proposal_id
            or packet["version"] != version
            or attestation.get("proposal_version") != version
        ):
            raise V2Error("stale-selection", "Proposal and comparison versions disagree")
        packet["semantic_review"] = {"status": "passed", "review_id": attestation["id"]}
        return packet

    def publish(
        self,
        capability: dict[str, str],
        proposal_id: str,
        version: str,
        selected_group_ids: list[str],
        *,
        operation_id: str,
        request_id: str,
        fault: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        packet = self.proposal(proposal_id, version)
        binding = selection_hash(packet, selected_group_ids)
        event = check_capability(self.store, capability, action="approve", allow_revoked=True)
        fields = {
            "proposal_id": proposal_id,
            "proposal_version": version,
            "selected_group_ids": sorted(set(selected_group_ids)),
            "owner_message_ref": event["owner_message_ref"],
        }

        def mutate(manifest, read):
            current_event = check_capability(self.store, capability, action="approve")
            scope = current_event["scope"]
            if (
                scope.get("selection_hash") != binding
                or scope.get("proposal_id") != proposal_id
                or scope.get("proposal_version") != version
            ):
                raise V2Error(
                    "approval-required", "Owner reply is bound to a different brief or selection"
                )
            projected, targets = project_selection(packet, selected_group_ids, manifest, read, today=workspace_today(self.store.vault))
            aliases = manifest.setdefault("review_receipts", {})
            for group in packet["groups"]:
                if group["id"] not in selected_group_ids:
                    continue
                for operation in group["operations"]:
                    if operation["after_hash"] == operation["before_hash"]:
                        continue
                    value = decode_record(read(operation["after_hash"]))
                    alias = value.get("owner_review", {}).get("receipt_id")
                    if alias:
                        binding_row = {"operation_id": operation_id, "group_id": group["id"], "proposal_id": proposal_id, "proposal_version": version}
                        if alias in aliases and aliases[alias] != binding_row:
                            raise V2Error("invalid-record", "A reserved review receipt belongs to another group or committed operation")
                        aliases[alias] = binding_row
            for identity in targets:
                old = manifest["records"].get(identity)
                if old:
                    self.store.check_checkout(old)
                new = projected["records"][identity]
                path = self.store.checkout_path(new["path"])
                if (not old or old["path"] != new["path"]) and path.exists():
                    raise V2Error(
                        "external-edit-conflict",
                        "A proposed destination already contains an external file",
                    )
            manifest["records"] = projected["records"]

        result = self.store.transact(
            operation_id=operation_id,
            request_id=request_id,
            kind="adoption",
            payload_hash=binding,
            mutate=mutate,
            receipt_fields=fields,
            fault=fault,
        )
        targets = [
            operation["target_id"]
            for group in packet["groups"]
            if group["id"] in selected_group_ids
            for operation in group["operations"]
        ]
        previous = self.store.manifest(result["knowledge_revision"])["parent"]
        try:
            conflicts = self.store.refresh_checkout(previous_revision=previous, identities=targets)
        except (OSError, V2Error) as exc:
            conflicts = [{"reason": f"Checkout repair required: {exc}"}]
        return {
            "receipt": result,
            "readiness": {
                "revision": self.store.head(),
                "index_state": "stale",
                "checkout_conflicts": conflicts,
            },
        }

    def review_receipt(self, receipt_id: str, *, revision: str | None = None) -> dict[str, Any]:
        """Resolve a record's group receipt to its actual committed selection.

        Prepared Markdown reserves a per-group receipt ID. Only publication
        binds that ID in a manifest, so an inert proposal cannot claim a commit.
        One selected transaction can have multiple such stable record links.
        """
        manifest = self.store.manifest(revision)
        alias = manifest.get("review_receipts", {}).get(receipt_id)
        if alias:
            receipt = self.store.receipt(alias["operation_id"])
            if receipt and receipt["proposal_id"] == alias["proposal_id"] and alias["group_id"] in receipt["selected_group_ids"]:
                return receipt | {"id": receipt_id}
        # Dispositions have a single operation receipt, reserved directly.
        for operation in set(manifest["local_receipts"]) | set(manifest["receipt_origins"]):
            receipt = self.store.receipt(operation)
            if receipt and receipt["id"] == receipt_id:
                return receipt
        raise V2Error("approval-required", "This review receipt is not committed in the selected revision")

    def disposition(
        self,
        capability: dict[str, str],
        identities: list[str],
        action: str,
        *,
        operation_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        allowed = {"kept-as-possibility", "deferred", "dismissed", "disputed", "declined-adoption"}
        if action not in allowed or not identities or len(set(identities)) != len(identities):
            raise V2Error("invalid-request", "Choose a scoped suggestion disposition")
        event = check_capability(self.store, capability, action="dispose", allow_revoked=True)
        approved = event["scope"].get("records", {})
        if event["scope"].get("disposition") != action or set(approved) != set(identities):
            raise V2Error("approval-required", "Owner disposition does not identify this selection")
        payload_hash = _fingerprint({"action": action, "records": approved})
        prior = self.store.receipt(operation_id)
        if prior:
            if (
                prior["payload_hash"] != payload_hash
                or prior["request_id"] != request_id
                or prior["kind"] != "owner-disposition"
            ):
                raise V2Error(
                    "idempotency-conflict",
                    "Disposition operation already identifies different work",
                )
            self._refresh(prior, identities)
            return prior
        # Fix exact target versions before approval/commit. Later edits cannot
        # silently inherit an earlier disposition of different content.
        manifest = self.store.manifest()
        selected = {identity: manifest["records"].get(identity) for identity in identities}
        if any(row is None or row["availability"] != "suggestion" for row in selected.values()):
            raise V2Error("invalid-request", "Disposition applies to existing suggestions")
        if approved != {key: value["version"] for key, value in selected.items()}:
            raise V2Error(
                "stale-selection", "A suggestion changed after the disposition instruction"
            )
        receipt_id = generate_ulid()
        objects, replacements = {}, {}
        for identity, row in selected.items():
            value = self.store.read_record(identity)
            value["owner_review"] = {
                "status": "reviewed",
                "disposition": action,
                "receipt_id": receipt_id,
            }
            raw = encode_record(value, name=row["name"])
            replacements[identity] = record_descriptor(raw, path=row["path"])
            objects[hash_bytes(raw)] = raw

        def mutate(current, _read):
            event = check_capability(self.store, capability, action="dispose")
            if event["scope"].get("disposition") != action or event["scope"].get("records") != {
                key: value["version"] for key, value in selected.items()
            }:
                raise V2Error(
                    "approval-required",
                    "Owner disposition does not identify these suggestion versions",
                )
            for identity, row in selected.items():
                if current["records"].get(identity, {}).get("version") != row["version"]:
                    raise V2Error("stale-selection", "Suggestion changed before disposition")
                current["records"][identity] = replacements[identity]

        result = self.store.transact(
            operation_id=operation_id,
            request_id=request_id,
            kind="owner-disposition",
            payload_hash=payload_hash,
            mutate=mutate,
            objects=objects,
            receipt_fields={"id": receipt_id},
        )
        self._refresh(result, identities)
        return result


def snapshot_fingerprint(
    records: dict[str, bytes],
    sources: list[dict[str, Any]] | None = None,
    legacy_edges: list[dict[str, Any]] | None = None,
) -> str:
    """The exact baseline digest a trusted migration review must bind."""
    rows = {
        record_descriptor(raw, path=path)["id"]: record_descriptor(raw, path=path)
        for path, raw in records.items()
    }
    return _fingerprint(
        {"records": rows, "sources": sources or [], "legacy_edges": legacy_edges or []}
    )
