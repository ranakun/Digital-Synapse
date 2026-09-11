"""Exact proposal projection and evidence checks; no owner authority here."""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from synapse.config import resolve_timezone
from synapse.knowledge import decode_record, metadata_and_body, record_descriptor
from synapse.source_store import read_passage
from synapse.v2_contracts import V2Error, hash_bytes, validate_payload, version_for


def workspace_today(vault) -> date:
    return datetime.now(ZoneInfo(resolve_timezone(vault))).date()


def _expiry(value: str | None) -> None:
    if value is not None and datetime.fromisoformat(value.replace("Z", "+00:00")) <= datetime.now(
        UTC
    ):
        raise V2Error(
            "precondition-expired", "A current claim or proposal passed its stated deadline"
        )


def check_conditions(conditions: dict[str, Any], manifest: dict[str, Any]) -> None:
    _expiry(conditions.get("expires_at"))
    for reference in conditions["read_set"]:
        if manifest["records"].get(reference["id"], {}).get("version") != reference["version"]:
            raise V2Error(
                "stale-selection",
                "A selected record or premise changed",
                details={"id": reference["id"]},
            )
    for reference in conditions["source_preconditions"]:
        if manifest["sources"].get(reference["source_id"]) != reference["source_version"]:
            raise V2Error(
                "stale-selection",
                "A selected source condition changed",
                details={"source_id": reference["source_id"]},
            )


def validate_knowledge(
    record: dict[str, Any],
    manifest: dict[str, Any],
    read: Callable[[str], bytes],
    *,
    admission: bool = False,
    historical_records: dict[str, Any] | None = None,
    today: date | None = None,
) -> None:
    if "record_kind" not in record:
        return
    validate_payload("knowledge_record", record)
    subject = manifest["records"].get(record["subject_id"])
    if not subject or not subject["active"]:
        raise V2Error(
            "ambiguous-identity", "The knowledge subject must resolve to an active entity"
        )
    if (
        record.get("applies_from")
        and record.get("applies_until")
        and record["applies_from"] > record["applies_until"]
    ):
        raise V2Error("invalid-record", "Applicability window runs backwards")
    if (
        "interaction-observation" in record["epistemic_basis"]
        and record.get("applies_from", record["as_of"]) > (today or datetime.now(UTC).date()).isoformat()
    ):
        raise V2Error(
            "invalid-record", "A future occurrence cannot be admitted as an observed interaction"
        )
    if not record["evidence"] and record["record_kind"] not in {"question", "navigation"}:
        raise V2Error(
            "source-unavailable", "A material finding must retain its supporting source passages"
        )
    if admission:
        if (
            record["availability"] != "suggestion"
            or record["review_status"] != "proposed"
            or record["owner_review"] != {"status": "not-reviewed", "disposition": "none"}
        ):
            raise V2Error(
                "invalid-record", "Admission cannot adopt, attest or claim an owner review"
            )
        if record["owner_position"] == "accepted":
            raise V2Error("invalid-record", "An agent cannot create owner endorsement")
    for evidence in record["evidence"]:
        source = manifest["source_versions"].get(evidence["source_version"])
        if not source:
            raise V2Error(
                "source-unavailable", "Evidence version was not captured in this knowledge history"
            )
        read_passage(source, evidence, read, context_characters=0)
    for reference in record.get("dependencies", []):
        row = manifest["records"].get(reference["id"])
        if reference["id"] == record["id"]:
            raise V2Error("invalid-record", "A record cannot provide its own evidential support")
        if (
            not row
            or row["version"] != reference["version"]
            or not row["active"]
            or row["availability"] == "draft"
            or row["disposition"] in {"dismissed", "disputed"}
        ):
            raise V2Error("stale-selection", "A material premise is absent, changed or unavailable")
    for reference in record.get("context_refs", []):
        rows = [
            manifest["records"].get(reference["id"]),
            (historical_records or {}).get(reference["id"]),
        ]
        if not any(row and row["version"] == reference["version"] for row in rows):
            raise V2Error(
                "stale-selection",
                "A context/correction target does not resolve to the pinned record version",
            )
    relationship = record.get("relationship")
    if relationship:
        for endpoint in ("from_id", "to_id"):
            target = manifest["records"].get(relationship[endpoint])
            if not target or not target["active"]:
                raise V2Error(
                    "ambiguous-identity", "Relationship endpoints must resolve to active identities"
                )


def check_dependency_cycles(
    manifest: dict[str, Any], read: Callable[[str], bytes], changed: set[str]
) -> None:
    visiting, checked = set(), set()

    def walk(identity: str) -> None:
        if identity in visiting:
            raise V2Error("invalid-record", "Evidence dependencies contain a cycle")
        if identity in checked:
            return
        row = manifest["records"].get(identity)
        if not row:
            return
        visiting.add(identity)
        record = decode_record(read(row["version"]), path=row["path"])
        for reference in record.get("dependencies", []):
            walk(reference["id"])
        visiting.remove(identity)
        checked.add(identity)

    for identity in changed:
        walk(identity)


def selected_groups(packet: dict[str, Any], selected: list[str]) -> list[dict[str, Any]]:
    if (
        not selected
        or len(selected) != len(set(selected))
        or not set(selected) <= set(packet["presented_group_ids"])
    ):
        raise V2Error(
            "invalid-request", "Select only explicitly presented groups, without duplicates"
        )
    lookup = {group["id"]: group for group in packet["groups"]}
    if not set(selected) <= lookup.keys():
        raise V2Error("invalid-request", "Selected group is absent from the packet")
    groups = [lookup[identity] for identity in selected]
    for group in groups:
        if not set(group["requires"]) <= set(selected):
            raise V2Error("invalid-request", "The selection omits an explained prerequisite")
    return groups


def validate_packet_structure(packet: dict[str, Any]) -> None:
    validate_payload("proposal", packet)
    if packet["version"] != version_for("proposal", packet):
        raise V2Error("stale-selection", "Proposal bytes do not match their version")
    groups, effects, operations = set(), set(), set()
    for group in packet["groups"]:
        if group["id"] in groups:
            raise V2Error("invalid-request", "Proposal group IDs must be unique")
        groups.add(group["id"])
        local_effects = set()
        for effect in group["effects"]:
            if effect["id"] in effects:
                raise V2Error("invalid-request", "Material effect IDs must be unique")
            effects.add(effect["id"])
            local_effects.add(effect["id"])
            if (
                not 0
                <= effect["brief_span_start"]
                < effect["brief_span_end"]
                <= len(packet["brief"])
            ):
                raise V2Error(
                    "invalid-request", "Every effect requires a nonempty span in the shown brief"
                )
        covered = set()
        for operation in group["operations"]:
            if operation["id"] in operations:
                raise V2Error("invalid-request", "Operation IDs must be unique")
            operations.add(operation["id"])
            if not set(operation["effect_ids"]) <= local_effects:
                raise V2Error("invalid-request", "An operation maps to an unknown material effect")
            covered.update(operation["effect_ids"])
        if covered != local_effects:
            raise V2Error(
                "invalid-request", "Material effects and operations do not have complete coverage"
            )
    if (
        len(set(packet["presented_group_ids"])) != len(packet["presented_group_ids"])
        or not set(packet["presented_group_ids"]) <= groups
    ):
        raise V2Error("invalid-request", "Presented groups must exist and be unique")
    for group in packet["groups"]:
        if not set(group["requires"]) <= groups or group["id"] in group["requires"]:
            raise V2Error("invalid-request", "Group prerequisites are invalid")
    prerequisites = {group["id"]: group["requires"] for group in packet["groups"]}

    def visit(identity, ancestors):
        if identity in ancestors:
            raise V2Error("invalid-request", "Group prerequisites contain a cycle")
        for required in prerequisites[identity]:
            visit(required, ancestors | {identity})

    for identity in prerequisites:
        visit(identity, set())
    # Prerequisites and their effects must themselves have been presented.
    selected_groups(packet, packet["presented_group_ids"])


def project_selection(
    packet: dict[str, Any],
    selected: list[str],
    manifest: dict[str, Any],
    read: Callable[[str], bytes],
    *,
    check_versions: bool = True,
    today: date | None = None,
) -> tuple[dict[str, Any], set[str]]:
    validate_packet_structure(packet)
    groups = selected_groups(packet, selected)
    if check_versions:
        check_conditions(packet, manifest)
        for group in groups:
            check_conditions(group, manifest)
    projected = copy.deepcopy(manifest)
    targets: set[str] = set()
    for group in groups:
        for operation in group["operations"]:
            identity = operation["target_id"]
            if identity in targets:
                raise V2Error(
                    "invalid-request",
                    "Overlapping selected operations would lose an approved effect",
                )
            targets.add(identity)
            before = manifest["records"].get(identity)
            creating = operation["kind"] == "create-record"
            if creating:
                if before or operation["before_hash"] is not None:
                    raise V2Error("stale-selection", "Created identity already exists")
            elif not before or operation["before_hash"] != before["version"]:
                raise V2Error(
                    "stale-selection", "A selected target changed since the brief was prepared"
                )
            if before and operation.get("before_path", before["path"]) != before["path"]:
                raise V2Error("stale-selection", "A selected record moved")
            path = operation.get("after_path", before["path"] if before else "")
            raw = read(operation["after_hash"])
            if hash_bytes(raw) != operation["after_hash"]:
                raise V2Error("object-integrity", "The exact proposed bytes are unavailable")
            after = record_descriptor(raw, path=path)
            if after["id"] != identity or after["availability"] != "accepted":
                raise V2Error(
                    "invalid-record", "Adoption must describe the exact accepted target record"
                )
            if after["profile"] == "knowledge-v2" and (
                not before or before["version"] != after["version"]
            ):
                value = decode_record(raw)
                if value["owner_review"].get("status") != "reviewed" or value["owner_review"].get("disposition") != "adopted":
                    raise V2Error(
                        "invalid-record",
                        "Prepared adoption records must declare the selected adoption",
                    )
                if (
                    value["lifecycle"] == "current"
                    and value.get("applies_until", "9999") < (today or datetime.now(UTC).date()).isoformat()
                ):
                    raise V2Error(
                        "precondition-expired",
                        "An expired current assertion needs revalidation or a reviewed historical qualification",
                    )
            if before and path != before["path"] and operation["kind"] != "relocate-record":
                raise V2Error(
                    "invalid-request", "Changing a locator requires an explicit relocate operation"
                )
            if operation["kind"] == "withdraw-record" and after["active"]:
                raise V2Error(
                    "invalid-record", "A withdrawal must preserve an inactive historical record"
                )
            if after["review_status"] == "verified":
                if not before or before["review_status"] != "verified":
                    raise V2Error("approval-required", "Adoption cannot mint owner verification")
                if metadata_and_body(read(before["version"])) != metadata_and_body(raw):
                    raise V2Error(
                        "approval-required", "Changed meaning cannot inherit old verification"
                    )
            projected["records"][identity] = after
        merge = group.get("identity_merge")
        if merge:
            _validate_merge(merge, group, manifest, projected, read)
    for identity in targets:
        row = projected["records"][identity]
        record = decode_record(read(row["version"]), path=row["path"])
        if row["active"]:
            validate_knowledge(record, projected, read, historical_records=manifest["records"], today=today)
    check_dependency_cycles(projected, read, targets)
    return projected, targets


def _validate_merge(
    merge: dict[str, Any],
    group: dict[str, Any],
    before: dict[str, Any],
    manifest: dict[str, Any],
    read: Callable[[str], bytes],
) -> None:
    survivor = merge["survivor"]["id"]
    targets = {op["target_id"] for op in group["operations"]}
    absorbed = {ref["id"] for ref in merge["absorbed"]}
    if survivor in absorbed or not ({survivor} | absorbed) <= targets:
        raise V2Error(
            "invalid-request", "Merge must include exact survivor and absorbed record operations"
        )
    references = [merge["survivor"], *merge["absorbed"], *merge["rewritten_references"]]
    if len({ref["id"] for ref in references}) != len(references):
        raise V2Error("invalid-request", "Merge references must be unique")
    for ref in references:
        if (
            before["records"].get(ref["id"], {}).get("version") != ref["version"]
            or ref["id"] not in targets
        ):
            raise V2Error(
                "stale-selection", "Every merge reference must have its exact version and operation"
            )
    if {(ref["from_id"], ref["to_id"]) for ref in merge["redirects"]} != {
        (identity, survivor) for identity in absorbed
    } or len(merge["redirects"]) != len(absorbed):
        raise V2Error(
            "invalid-request", "Merge redirects must exactly match the absorbed identities"
        )
    for identity in absorbed:
        row = manifest["records"][identity]
        metadata, _ = metadata_and_body(read(row["version"]))
        if row["active"] or metadata.get("merged_into") != survivor:
            raise V2Error(
                "invalid-record", "Absorbed records must retain a redirect to their survivor"
            )
    # Explicit ID references must be rewritten in the same frozen group.
    # Historical evidence/context references retain their original versions.
    for identity, row in manifest["records"].items():
        if not row["active"]:
            continue
        raw = read(row["version"])
        value = decode_record(raw)
        meta, _ = metadata_and_body(raw)
        ids = {value.get("subject_id")}
        ids.update(value.get("relationship", {}).get(key) for key in ("from_id", "to_id"))
        for relation in meta.get("relations", []) or []:
            if isinstance(relation, dict):
                ids.update(
                    relation.get(key)
                    for key in ("target_id", "to_id", "target", "to")
                    if isinstance(relation.get(key), str)
                )
        if ids & absorbed:
            raise V2Error(
                "invalid-record",
                "Merge left an active explicit identity reference unreconciled",
                details={"id": identity},
            )
