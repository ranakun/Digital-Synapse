"""Trusted host control plane, separate from worker-accessible data tools.

Only an interaction host which received the owner's actual input calls these
methods. They are deliberately not MCP tools or model-selectable operations.
Capabilities delegate particular actions, never an arbitrary approval field.
This is an orchestration boundary, not isolation from hostile same-user code.
"""

from __future__ import annotations

import copy
import hmac
import json
import secrets
from typing import Any

from synapse.revisions import RevisionStore, durable_write
from synapse.util import generate_ulid, utc_now
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes

ACTIONS = {"capture", "prepare", "investigate", "stage", "admit", "dispose", "approve", "resume"}


def selection_hash(packet: dict[str, Any], selected: list[str]) -> str:
    return hash_bytes(
        canonical_json(
            {
                "proposal_id": packet["id"],
                "proposal_version": packet["version"],
                "brief_hash": hash_bytes(packet["brief"].encode()),
                "selected_group_ids": sorted(set(selected)),
            }
        )
    )


class OwnerHost:
    """Used by direct human chat/approved parent-host adapters, never workers."""

    def __init__(self, store: RevisionStore, *, host_id: str = "local-interactive"):
        self.store = store
        self.host_id = host_id

    def record_instruction(
        self,
        owner_message_ref: str,
        *,
        actions: list[str],
        scope: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        if not owner_message_ref.strip() or not actions or not set(actions) <= ACTIONS:
            raise V2Error(
                "invalid-request", "A trusted owner event and bounded actions are required"
            )
        if "approve" in actions:
            raise V2Error(
                "approval-required", "Approval must bind a displayed proposal and selection"
            )
        return self._record(owner_message_ref, actions, scope or {})

    def _record(
        self, owner_message_ref: str, actions: list[str], scope: dict[str, Any]
    ) -> dict[str, str]:
        event_id, token = generate_ulid(), secrets.token_urlsafe(32)
        event = {
            "id": event_id,
            "host_id": self.host_id,
            "owner_message_ref": owner_message_ref,
            "created_at": utc_now(),
            "actions": sorted(set(actions)),
            "scope": copy.deepcopy(scope),
            "capability_hash": hash_bytes(token.encode()),
            "revoked": False,
        }
        with self.store.writer_lock():
            durable_write(
                self.store.root / "approval-journal" / f"{event_id}.json", canonical_json(event)
            )
        return {"event_id": event_id, "token": token}

    def approve_displayed(
        self,
        packet: dict[str, Any],
        selected_group_ids: list[str],
        *,
        displayed_brief: str,
        owner_message_ref: str,
    ) -> dict[str, str]:
        if displayed_brief != packet["brief"]:
            raise V2Error("stale-selection", "The displayed brief differs from the prepared packet")
        if not owner_message_ref.strip() or not selected_group_ids:
            raise V2Error("approval-required", "A real reply and explicit selection are required")
        if len(set(selected_group_ids)) != len(selected_group_ids) or not set(
            selected_group_ids
        ) <= set(packet["presented_group_ids"]):
            raise V2Error("invalid-request", "Selection contains duplicate or unpresented groups")
        return self._record(
            owner_message_ref,
            ["approve"],
            {
                "proposal_id": packet["id"],
                "proposal_version": packet["version"],
                "selection_hash": selection_hash(packet, selected_group_ids),
                "selected_group_ids": selected_group_ids,
            },
        )

    def revoke(self, capability: dict[str, str]) -> dict[str, Any]:
        with self.store.writer_lock():
            event = check_capability(self.store, capability, action=None, allow_revoked=True)
            if self.store.root.joinpath("HEAD").exists():
                manifest = self.store.manifest()
                operations = set(manifest["receipt_origins"]) | set(manifest["local_receipts"])
                for operation in operations:
                    receipt = self.store.receipt(operation)
                    if (
                        receipt
                        and receipt.get("owner_message_ref") == event["owner_message_ref"]
                        and receipt["kind"] == "adoption"
                    ):
                        return {
                            "status": "already-committed",
                            "receipt": receipt,
                            "next": "A compensating proposal preserves later work.",
                        }
            event["revoked"] = True
            durable_write(
                self.store.root / "approval-journal" / f"{event['id']}.json", canonical_json(event)
            )
            return {"status": "revoked", "event_id": event["id"]}


def check_capability(
    store: RevisionStore,
    capability: dict[str, str],
    *,
    action: str | None,
    allow_revoked: bool = False,
) -> dict[str, Any]:
    """Read only; mutating services call this again while holding the store lock."""
    if not isinstance(capability, dict) or set(capability) != {"event_id", "token"}:
        raise V2Error("approval-required", "The trusted host must supply a scoped capability")
    identity, token = capability.get("event_id"), capability.get("token")
    if (
        not isinstance(identity, str)
        or len(identity) != 26
        or not identity.isalnum()
        or not isinstance(token, str)
    ):
        raise V2Error("approval-required", "Invalid host capability")
    try:
        event = json.loads((store.root / "approval-journal" / f"{identity}.json").read_bytes())
        actual = hash_bytes(token.encode())
        if (
            not isinstance(event, dict)
            or event.get("id") != identity
            or not hmac.compare_digest(actual, str(event.get("capability_hash", "")))
        ):
            raise ValueError("Unknown capability")
    except (OSError, ValueError, UnicodeError) as exc:
        raise V2Error("approval-required", "Host capability could not be verified") from exc
    if event.get("revoked") and not allow_revoked:
        raise V2Error("approval-revoked", "The owner revoked this host decision")
    if action is not None and action not in event.get("actions", []):
        raise V2Error("approval-required", f"This owner event did not delegate {action}")
    return event
