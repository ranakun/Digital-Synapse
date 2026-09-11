"""Owner-event bridge for local Codex sessions; no conversation ingestion.

Only explicit user.text items grant owner authority. Codex also stores tool
notifications and environment context with role=user; those are not the owner.
This adapter is used by the trusted parent, never exposed on worker MCP.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from synapse.host_session import NativeHost
from synapse.revisions import durable_write
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes


class CodexEvents:
    def __init__(self, paths: list[Path]):
        self.paths = [Path(path) for path in paths]
        self._stamp = None
        self._messages = {}

    @classmethod
    def for_thread(cls, thread_id: str | None = None, *, sessions_root: Path | None = None):
        thread = thread_id or os.environ.get("CODEX_THREAD_ID", "")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", thread):
            raise V2Error("unsupported-operation", "A local Codex task ID is required for the native host")
        codex_root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        root = sessions_root or codex_root / "sessions"
        paths = sorted(root.rglob(f"*{thread}*.jsonl"))
        if not paths:
            raise V2Error("unsupported-operation", "Local Codex session events are unavailable; use the terminal owner host")
        return cls(paths)

    def _refresh(self):
        stamp = [(str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in self.paths]
        if stamp == self._stamp:
            return
        messages = {}
        for path in self.paths:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue  # the host may still be appending its final line
                    payload = entry.get("payload", {})
                    if entry.get("type") != "response_item" or payload.get("type") != "message":
                        continue
                    actor = payload.get("role")
                    metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
                    kinds = metadata.get("content_item_kinds")
                    if actor == "user" and kinds != ["user.text"]:
                        continue
                    if actor not in {"user", "assistant"}:
                        continue
                    parts = payload.get("content", [])
                    expected = "input_text" if actor == "user" else "output_text"
                    if not parts or any(part.get("type") != expected for part in parts):
                        continue
                    identity = payload.get("id")
                    text = "\n".join(part["text"] for part in parts)
                    if not isinstance(identity, str) or not text.strip():
                        continue
                    message = {"id": identity, "actor": actor, "text": text, "text_hash": hash_bytes(text.encode()), "occurred_at": entry.get("timestamp"), "turn_id": metadata.get("turn_id")}
                    if identity in messages and messages[identity]["text_hash"] != message["text_hash"]:
                        raise V2Error("approval-required", "The host event ID has conflicting content")
                    messages[identity] = message
        self._messages, self._stamp = messages, stamp

    def read(self, reference: str) -> dict:
        self._refresh()
        value = self._messages.get(reference)
        if value is None:
            raise V2Error("approval-required", "No actual owner or displayed assistant message matches that event")
        return dict(value)

    def recent(self, *, limit=12) -> list[dict]:
        self._refresh()
        if not 1 <= limit <= 50:
            raise V2Error("invalid-request", "Event discovery is limited to 1–50 messages")
        ordered = sorted(self._messages.values(), key=lambda message: (message["occurred_at"] or "", message["id"]))
        return [{key: value for key, value in message.items() if key != "text"} | {"preview": message["text"][:160]} for message in ordered[-limit:]]


class CodexSessionHost(NativeHost):
    def __init__(self, vault: Path, events: CodexEvents, *, reasoner, thread_id: str):
        self.events = events
        super().__init__(vault, event_reader=events.read, display=lambda text: None, reasoner=reasoner, host_id=f"codex:{thread_id}")

    def bind_display(self, proposal_id: str, version: str, assistant_event_ref: str) -> dict:
        packet = self.publisher.proposal(proposal_id, version)
        event = self.events.read(assistant_event_ref)
        if event["actor"] != "assistant" or packet["brief"] not in event["text"]:
            raise V2Error("approval-required", "The exact brief must already be visible in this task's assistant message")
        # The session message is the actual display. No duplicate prompt.
        result = super().show_proposal(proposal_id, version)
        entry = self._display(result["display_id"])
        entry["assistant_event_ref"] = assistant_event_ref
        entry["display_text_hash"] = event["text_hash"]
        with self.publisher.store.writer_lock():
            durable_write(self.publisher.store.root / "host-displays" / f"{entry['id']}.json", canonical_json(entry))
        return result

    def reply(self, owner_event_ref: str, *, display_id=None) -> dict:
        entry = self._display(display_id)
        displayed = self.events.read(entry.get("assistant_event_ref", ""))
        owner = self._event(owner_event_ref)
        if not owner.get("occurred_at") or not displayed.get("occurred_at") or owner["occurred_at"] <= displayed["occurred_at"]:
            raise V2Error("approval-required", "The owner reply must follow the displayed brief")
        if displayed["text_hash"] != entry["display_text_hash"]:
            raise V2Error("stale-selection", "The displayed host message changed")
        return super().reply(owner_event_ref, display_id=entry["id"])
