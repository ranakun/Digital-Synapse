"""Construct inert, exact v2 proposal packets from retained revision bytes."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from synapse.knowledge import (
    decode_record,
    encode_record,
    metadata_and_body,
    record_descriptor,
)
from synapse.revisions import RevisionStore
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, hash_bytes
from synapse.v2_proposals import project_selection, validate_packet_structure, workspace_today

_GROUP_KEYS = {
    "id",
    "requires",
    "effects",
    "changes",
    "read_set",
    "source_preconditions",
    "identity_merge",
}
_EFFECT_KEYS = {
    "id",
    "kind",
    "meaning",
    "brief_span_start",
    "brief_span_end",
}
_CHANGE_KEYS = {"kind", "path", "raw", "target_id", "before_version"}
_CHANGE_KINDS = {"create-record", "replace-record", "relocate-record", "withdraw-record"}


def _fail(message: str, *, details: Mapping[str, Any] | None = None) -> None:
    raise V2Error("invalid-request", message, details=details)


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{field} must be a nonempty string")
    return value


def _path(value: Any) -> str:
    value = _string(value, "path")
    candidate = PurePosixPath(value)
    if (
        "\\" in value
        or candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.parts[:1] != ("entities",)
        or candidate.suffix != ".md"
        or str(candidate) != value
    ):
        raise V2Error("invalid-path", "Records require a relative entities/*.md path")
    return value


def _refs(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        _fail(f"{field} must be a list")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "version"}:
            _fail(f"{field} contains an invalid record reference")
        identity = _string(item["id"], f"{field}.id")
        version = _string(item["version"], f"{field}.version")
        if identity in seen:
            _fail(f"{field} contains duplicate record IDs")
        seen.add(identity)
        result.append({"id": identity, "version": version})
    return result


def _source_preconditions(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        _fail("source_preconditions must be a list")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"source_id", "source_version"}:
            _fail("source_preconditions contains an invalid source reference")
        source_id = _string(item["source_id"], "source_preconditions.source_id")
        version = _string(item["source_version"], "source_preconditions.source_version")
        if source_id in seen:
            _fail("source_preconditions contains duplicate source IDs")
        seen.add(source_id)
        result.append({"source_id": source_id, "source_version": version})
    return result


def _merge_refs(*collections: list[dict[str, str]]) -> list[dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for collection in collections:
        for ref in collection:
            prior = result.get(ref["id"])
            if prior is not None and prior["version"] != ref["version"]:
                _fail("A record has conflicting frozen versions")
            result[ref["id"]] = ref
    return [result[key] for key in sorted(result)]


def _merge_sources(*collections: list[dict[str, str]]) -> list[dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for collection in collections:
        for ref in collection:
            prior = result.get(ref["source_id"])
            if prior is not None and prior["source_version"] != ref["source_version"]:
                _fail("A source has conflicting frozen versions")
            result[ref["source_id"]] = ref
    return [result[key] for key in sorted(result)]


def _record_refs(value: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, str]]:
    """Return current premise/subject preconditions used by a v2 record.

    Context and correction references can intentionally point at historical
    versions.  They are checked by the proposal projector against history and
    therefore must not be converted into current read-set preconditions.
    """

    if "record_kind" not in value:
        return []
    identities: list[str] = [value["subject_id"]]
    identities.extend(ref["id"] for ref in value.get("dependencies", []))
    refs: list[dict[str, str]] = []
    for identity in identities:
        row = manifest["records"].get(identity)
        if row is None:
            _fail("A proposed record references an unavailable subject or premise", details={"id": identity})
        refs.append({"id": identity, "version": row["version"]})
    return refs


def _current_source_refs(value: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, str]]:
    """Pin only source evidence that is currently the source pointer.

    Evidence against an older retained source version remains valid without a
    latest-source precondition.  A current pointer is a material precondition
    because replacing that source would otherwise silently alter the selected
    evidence context.
    """

    if "record_kind" not in value:
        return []
    refs: dict[str, dict[str, str]] = {}
    for evidence in value.get("evidence", []):
        source_id = evidence["source_id"]
        source_version = evidence["source_version"]
        if manifest["sources"].get(source_id) == source_version:
            refs[source_id] = {"source_id": source_id, "source_version": source_version}
    return [refs[key] for key in sorted(refs)]


def _freeze_after(
    raw: bytes,
    *,
    path: str,
    target: dict[str, Any] | None,
    group_receipt_id: str,
) -> tuple[bytes, dict[str, Any]]:
    if not isinstance(raw, bytes):
        _fail("change.raw must contain exact bytes")
    descriptor = record_descriptor(raw, path=path)
    if descriptor["review_status"] == "verified":
        raise V2Error(
            "approval-required",
            "A caller-supplied verified record cannot be changed by a proposal",
        )
    if target and target["review_status"] == "verified":
        raise V2Error(
            "approval-required",
            "A verified record cannot be changed by a proposal",
            details={"id": target["id"]},
        )

    if descriptor["profile"] == "knowledge-v2":
        value = decode_record(raw, path=path)
        metadata, _ = metadata_and_body(raw)
        value["availability"] = "accepted"
        value["review_status"] = "proposed"
        value["owner_review"] = {
            "status": "reviewed",
            "disposition": "adopted",
            "receipt_id": group_receipt_id,
        }
        # encode_record validates the complete payload and deliberately
        # preserves owner_position from the caller's exact payload.
        raw = encode_record(value, name=metadata["name"])
        descriptor = record_descriptor(raw, path=path)
    if descriptor["availability"] != "accepted":
        raise V2Error("invalid-record", "Proposal operations must produce accepted records")
    return raw, descriptor


def _validate_expiry(value: str | None) -> None:
    if value is None:
        return
    from datetime import datetime

    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise V2Error("invalid-request", "expires_at must be an ISO date-time") from exc


def build_proposal(
    store: RevisionStore,
    *,
    run_id: str,
    brief: str,
    groups: list[dict[str, Any]],
    proposal_id: str | None = None,
    base_revision: str | None = None,
    expires_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Build a strictly validated proposal without staging or publishing it."""

    _string(run_id, "run_id")
    if not isinstance(brief, str) or not brief:
        _fail("brief must be a nonempty string")
    if not isinstance(groups, list) or not 1 <= len(groups) <= 3:
        _fail("briefs must contain between one and three groups")
    _validate_expiry(expires_at)

    pinned_revision = base_revision or store.head()
    manifest = store.manifest(pinned_revision)
    proposal_id = proposal_id or generate_ulid()
    _string(proposal_id, "proposal_id")

    objects: dict[str, bytes] = {}
    packet_groups: list[dict[str, Any]] = []
    packet_read_set: list[dict[str, str]] = []
    packet_sources: list[dict[str, str]] = []
    seen_groups: set[str] = set()
    seen_effects: set[str] = set()
    seen_targets: set[str] = set()
    occupied_paths = {row["path"] for row in manifest["records"].values()}
    proposed_paths: set[str] = set()

    for input_group in groups:
        if not isinstance(input_group, dict) or set(input_group) - _GROUP_KEYS:
            _fail("Each group must use only the approved group fields")
        required = {"id", "requires", "effects", "changes", "read_set", "source_preconditions"}
        if not isinstance(input_group, dict) or not required <= set(input_group):
            _fail("Each group requires id, requires, effects, changes and read conditions")
        group_id = _string(input_group["id"], "group.id")
        if group_id in seen_groups:
            _fail("Proposal group IDs must be unique")
        seen_groups.add(group_id)

        requires = input_group["requires"]
        if not isinstance(requires, list) or any(not isinstance(x, str) or not x for x in requires):
            _fail("group.requires must contain nonempty group IDs")
        if len(set(requires)) != len(requires) or group_id in requires:
            _fail("Group prerequisites must be unique and cannot require themselves")

        effects = input_group["effects"]
        if not isinstance(effects, list) or not effects:
            _fail("Each group requires at least one effect")
        canonical_effects: list[dict[str, Any]] = []
        for effect in effects:
            if not isinstance(effect, dict) or set(effect) != _EFFECT_KEYS:
                _fail("Effects must use the exact approved fields")
            effect_id = _string(effect["id"], "effect.id")
            if effect_id in seen_effects:
                _fail("Material effect IDs must be unique")
            seen_effects.add(effect_id)
            start, end = effect["brief_span_start"], effect["brief_span_end"]
            if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
                _fail("Effect brief spans must be character offsets")
            if not 0 <= start < end <= len(brief):
                raise V2Error("invalid-span", "Every effect must cover a nonempty span in the brief")
            canonical_effects.append(
                {
                    "id": effect_id,
                    "kind": _string(effect["kind"], "effect.kind"),
                    "meaning": _string(effect["meaning"], "effect.meaning"),
                    "brief_span_start": start,
                    "brief_span_end": end,
                }
            )

        group_read = _refs(input_group["read_set"], "group.read_set")
        group_sources = _source_preconditions(input_group["source_preconditions"])
        for ref in group_read:
            row = manifest["records"].get(ref["id"])
            if not row or row["version"] != ref["version"]:
                raise V2Error("stale-selection", "A group read-set record is not at the pinned version", details={"id": ref["id"]})
        for ref in group_sources:
            if manifest["sources"].get(ref["source_id"]) != ref["source_version"]:
                raise V2Error("stale-selection", "A source precondition is not at the pinned version", details={"source_id": ref["source_id"]})

        changes = input_group["changes"]
        if not isinstance(changes, list) or not changes:
            _fail("Each group requires at least one change")
        operations: list[dict[str, Any]] = []
        inferred_refs: list[dict[str, str]] = []
        inferred_sources: list[dict[str, str]] = []
        group_receipt_id = generate_ulid()
        for change in changes:
            if not isinstance(change, dict) or set(change) - _CHANGE_KEYS:
                _fail("Changes must use only the approved change fields")
            if not {"kind", "path", "raw"} <= set(change):
                _fail("Every change requires kind, path and raw")
            kind = change["kind"]
            if kind not in _CHANGE_KINDS:
                _fail("Unsupported proposal change kind")
            path = _path(change["path"])
            target_id = change.get("target_id")
            if kind == "create-record":
                if target_id is not None:
                    _fail("create-record cannot supply target_id")
                target = None
            else:
                target_id = _string(target_id, "change.target_id")
                target = manifest["records"].get(target_id)
                if target is None:
                    raise V2Error("stale-selection", "The changed target is absent at the pinned revision", details={"id": target_id})
                if target_id in seen_targets:
                    _fail("Overlapping operations on one target must be one group")
                seen_targets.add(target_id)

            before_hash = target["version"] if target else None
            if "before_version" in change and change["before_version"] != before_hash:
                raise V2Error("stale-selection", "The supplied before_version differs from the pinned record")
            raw, after = _freeze_after(
                change["raw"],
                path=path,
                target=target,
                group_receipt_id=group_receipt_id,
            )
            identity = after["id"]
            if kind == "create-record":
                target_id = identity
                if identity in manifest["records"] or identity in seen_targets:
                    _fail("Created identity already exists or is changed twice")
                seen_targets.add(identity)
                if path in occupied_paths or path in proposed_paths:
                    raise V2Error("path-conflict", "A created record path is already occupied")
            elif identity != target_id:
                _fail("The exact after bytes must retain the operation target ID")

            if path in occupied_paths and (target is None or path != target["path"]):
                raise V2Error("path-conflict", "An operation would occupy another retained record path")

            if target and path != target["path"] and kind != "relocate-record":
                raise V2Error("invalid-request", "Changing a record path requires relocate-record")
            if target and path == target["path"] and kind == "relocate-record":
                _fail("relocate-record requires a changed path")
            if path in proposed_paths:
                _fail("Two operations cannot publish the same path")
            proposed_paths.add(path)
            if kind == "withdraw-record" and after["active"]:
                raise V2Error("invalid-record", "withdraw-record must produce an inactive record")

            after_hash = hash_bytes(raw)
            objects[after_hash] = raw
            operation_id = generate_ulid()
            effect_ids = [effect["id"] for effect in canonical_effects]
            operations.append(
                {
                    "id": operation_id,
                    "kind": kind,
                    "target_id": target_id,
                    "before_hash": before_hash,
                    **({"before_path": target["path"]} if target else {}),
                    "after_hash": after_hash,
                    "after_path": path,
                    "effect_ids": effect_ids,
                }
            )
            if target:
                inferred_refs.append({"id": target_id, "version": before_hash})
            decoded = decode_record(raw, path=path)
            inferred_refs.extend(_record_refs(decoded, manifest))
            inferred_sources.extend(_current_source_refs(decoded, manifest))

        group_sources = _merge_sources(group_sources, inferred_sources)

        group = {
            "id": group_id,
            "requires": list(requires),
            "effects": canonical_effects,
            "operations": operations,
            "read_set": _merge_refs(group_read, inferred_refs),
            "source_preconditions": group_sources,
        }
        if input_group.get("identity_merge") is not None:
            group["identity_merge"] = copy.deepcopy(input_group["identity_merge"])
        packet_groups.append(group)
        # Group-local conditions must not become packet-global conditions:
        # an unrelated, unselected group may legitimately drift.

    packet: dict[str, Any] = {
        "id": proposal_id,
        "version": "0" * 64,
        "run_id": run_id,
        "base_revision": pinned_revision,
        "brief": brief,
        "presented_group_ids": [group["id"] for group in packet_groups],
        "groups": packet_groups,
        "read_set": packet_read_set,
        "source_preconditions": packet_sources,
        "semantic_review": {"status": "pending"},
    }
    if expires_at is not None:
        packet["expires_at"] = expires_at
    from synapse.v2_contracts import version_for

    packet["version"] = version_for("proposal", packet)
    validate_packet_structure(packet)

    def read(version: str) -> bytes:
        if version in objects:
            return objects[version]
        return store.read_object(version)

    project_selection(packet, packet["presented_group_ids"], manifest, read, today=workspace_today(store.vault))
    return packet, objects
