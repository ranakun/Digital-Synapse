"""Durable state for owner-requested investigations.

Run state is orchestration metadata.  It is deliberately separate from the
canonical knowledge manifest: a run can retain a checkpoint or a trace while
the investigation is still only producing suggestions and proposals.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from synapse.owner_host import check_capability
from synapse.revisions import RevisionStore, durable_write
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes, validate_payload

_ULID = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_PRESETS: dict[str, dict[str, int]] = {
    "consult": {
        "max_operations": 8,
        "max_minutes": 2,
        "max_source_expansions": 8,
        "max_result_characters": 8_000,
    },
    "focused": {
        "max_operations": 8,
        "max_minutes": 10,
        "max_source_expansions": 8,
        "max_result_characters": 8_000,
    },
    "broad": {
        "max_operations": 20,
        "max_minutes": 20,
        "max_source_expansions": 20,
        "max_result_characters": 12_000,
    },
}

# These are the current playbook ceilings.  max_operations is intentionally
# derived from the selected preset because it is not in the wire request
# schema.  The ceiling is also retained in the persisted budget.
_CEILINGS = {
    "max_operations": 20,
    "max_minutes": 20,
    "max_source_expansions": 20,
    "max_result_characters": 32_000,
}
_TERMINAL = {"completed", "partial", "cancelled", "failed"}
_WIRE_STATUS = {
    "running": "awaiting-context",
    "completed": "completed",
    "partial": "partial",
    "cancelled": "cancelled",
    "failed": "failed",
}
_DEFAULT_STOP_REASONS = {
    "completed": "Investigation completed.",
    "partial": "Investigation stopped with partial coverage.",
    "cancelled": "Investigation cancelled by the owner.",
    "failed": "Investigation failed.",
}
_TRACE_FORBIDDEN_KEYS = {
    "chainofthought",
    "chain_of_thought",
    "full_hostconversation",
    "full_host_conversation",
    "hostconversation",
    "host_conversation",
}


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise V2Error("recovery-required", "Run timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise V2Error("recovery-required", "Run timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise V2Error("recovery-required", "Run timestamp has no UTC offset")
    return parsed.astimezone(UTC)


def _require_ulid(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _ULID.fullmatch(value):
        raise V2Error("invalid-request", f"Invalid {label}")
    return value


def _strict_value(value: Any, *, label: str) -> None:
    try:
        canonical_json(value)
    except V2Error as exc:
        raise V2Error("invalid-request", f"{label} is not valid canonical JSON") from exc


def _check_trace(trace: Any) -> list[dict[str, Any]]:
    if trace is None:
        return []
    entries = trace if isinstance(trace, list) else [trace]
    if not entries or any(not isinstance(entry, dict) for entry in entries):
        raise V2Error("invalid-request", "A run trace must contain object entries")
    def scan(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).casefold().replace("-", "_")
                if normalized in _TRACE_FORBIDDEN_KEYS:
                    raise V2Error("invalid-request", "Run traces cannot retain host conversation or chain of thought")
                scan(child)
        elif isinstance(value, list):
            for child in value:
                scan(child)

    scan(entries)
    _strict_value(entries, label="trace")
    return copy.deepcopy(entries)


class RunManager:
    """Manage bounded, explicitly authorized investigation runs."""

    def __init__(self, vault: Path, *, clock: Callable[[], datetime | str] | None = None):
        self.store = RevisionStore(Path(vault))
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self._clock()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value.astimezone(UTC)
        if isinstance(value, str):
            return _parse_timestamp(value)
        raise TypeError("RunManager clock must return a UTC datetime or timestamp")

    def _path(self, run_id: str) -> Path:
        _require_ulid(run_id, label="run ID")
        return self.store.root / "runs" / f"{run_id}.json"

    def _read(self, run_id: str) -> dict[str, Any]:
        path = self._path(run_id)
        try:
            value = json.loads(path.read_bytes())
        except FileNotFoundError as exc:
            raise V2Error("approval-required", "Requested investigation is unavailable") from exc
        except (OSError, UnicodeError, ValueError) as exc:
            raise V2Error("recovery-required", "Requested investigation state is unreadable") from exc
        if not isinstance(value, dict) or value.get("id") != run_id:
            raise V2Error("recovery-required", "Requested investigation state is invalid")
        return value

    def get(self, run_id: str) -> dict[str, Any]:
        """Read a run without creating any vault directories."""

        return copy.deepcopy(self._read(run_id))

    def public(self, run_id: str) -> dict[str, Any]:
        """Return the persisted run projection for protocol adapters.

        There is no separate run object in the v2 wire schema.  The request
        and eventual result remain schema-validated independently; this
        projection preserves orchestration fields required by host adapters.
        """

        return self.get(run_id)

    @staticmethod
    def _budget(request: Mapping[str, Any]) -> dict[str, int | str]:
        budget = request["budget"]
        preset = budget["preset"]
        defaults = _PRESETS[preset]
        resolved: dict[str, int | str] = {
            "preset": preset,
            "max_operations": defaults["max_operations"],
            "max_minutes": budget["max_minutes"],
            "max_source_expansions": budget["max_source_expansions"],
            "max_result_characters": budget["max_result_characters"],
        }
        for key, ceiling in _CEILINGS.items():
            if resolved[key] > ceiling:
                raise V2Error(
                    "invalid-request",
                    f"{key} exceeds the bounded v2 ceiling",
                    details={"field": key, "ceiling": ceiling},
                )
        return resolved

    @staticmethod
    def _validate_request(request: Any, *, resume: bool = False) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise V2Error("invalid-request", "Investigation request must be a dictionary payload")
        validate_payload("request", request)
        if request["mode"] != ("continue" if resume else "investigate"):
            expected = "continue" if resume else "investigate"
            raise V2Error("invalid-request", f"Run manager requires an {expected} request")
        if resume:
            _require_ulid(request["continuation_id"], label="continuation ID")
        return copy.deepcopy(request)

    def _authorize(
        self,
        capability: dict[str, str],
        run_id: str,
        request: Mapping[str, Any],
        *,
        action: str,
    ) -> dict[str, Any]:
        if request.get("mode") == "prepare":
            if action != "prepare":
                raise V2Error("approval-required", "Preparation cannot delegate investigation authority")
            event = check_capability(self.store, capability, action="prepare")
            scope = event.get("scope", {})
            if (
                scope.get("run_id") != run_id
                or scope.get("source_refs") != request["source_refs"]
                or scope.get("request_hash") != hash_bytes(canonical_json(request))
                or event.get("owner_message_ref") != request["owner_instruction_ref"]
            ):
                raise V2Error("approval-required", "Preparation authority differs from the exact requested sources")
            return event
        event = check_capability(self.store, capability, action=action)
        scope = event.get("scope")
        if not isinstance(scope, dict) or scope.get("run_id") != run_id:
            raise V2Error("approval-required", "Capability is scoped to a different investigation")
        expected_subjects = request.get("subject_ids")
        actual_subjects = scope.get("subject_ids")
        if not isinstance(actual_subjects, list) or actual_subjects != expected_subjects:
            raise V2Error("approval-required", "Capability subject scope differs from the request")
        if event.get("owner_message_ref") != request.get("owner_instruction_ref"):
            raise V2Error("approval-required", "Request origin does not match the trusted owner event")
        return event

    @staticmethod
    def _update_elapsed(run: dict[str, Any], now: datetime) -> bool:
        created = _parse_timestamp(run["created_at"])
        elapsed = max(0, int((now - created).total_seconds()))
        usage = run["usage"]
        changed = usage.get("elapsed_seconds") != elapsed
        usage["elapsed_seconds"] = elapsed
        usage["elapsed_ms"] = max(0, int((now - created).total_seconds() * 1000))
        segments = run.get("segments", [])
        segment_elapsed = elapsed
        if segments:
            segment = segments[-1]
            segment_elapsed = max(0, int((now - _parse_timestamp(segment["started_at"])).total_seconds()))
            changed = changed or segment.get("elapsed_seconds") != segment_elapsed
            segment["elapsed_seconds"] = segment_elapsed
            budget = segment["budget"]
        else:
            budget = usage["budget"]
        checkpoint = run.get("checkpoint")
        if isinstance(checkpoint, dict) and checkpoint.get("segment") == len(segments) - 1:
            changed = changed or checkpoint.get("elapsed_seconds") != elapsed
            changed = changed or checkpoint.get("segment_elapsed_seconds") != segment_elapsed
            checkpoint["elapsed_seconds"] = elapsed
            checkpoint["segment_elapsed_seconds"] = segment_elapsed
        if run.get("status") == "running" and segment_elapsed >= int(budget["max_minutes"]) * 60:
            run["status"] = "partial"
            run["stop_reason"] = "Wall-clock budget exhausted."
            changed = True
        return changed

    @staticmethod
    def _new_checkpoint(run: Mapping[str, Any], *, state: Any = None) -> dict[str, Any]:
        segment = run["segments"][-1]
        checkpoint: dict[str, Any] = {
            "segment": len(run["segments"]) - 1,
            "operations": segment["operations"],
            "source_expansions": segment["source_expansions"],
            "elapsed_seconds": run["usage"]["elapsed_seconds"],
            "segment_elapsed_seconds": segment["elapsed_seconds"],
            "deadline_at": segment["deadline_at"],
        }
        if state is not None:
            _strict_value(state, label="checkpoint state")
            checkpoint["state"] = copy.deepcopy(state)
        elif "state" in run.get("checkpoint", {}):
            checkpoint["state"] = copy.deepcopy(run["checkpoint"]["state"])
        return checkpoint

    def start(self, request: dict[str, Any], capability: dict[str, str], *, run_id: str) -> dict[str, Any]:
        request = self._validate_request(request)
        return self._start(request, capability, run_id=run_id)

    def start_preparation(self, request: dict[str, Any], capability: dict[str, str], *, run_id: str) -> dict[str, Any]:
        validate_payload("preparation_request", request)
        refs = request["source_refs"]
        if len({ref["source_id"] for ref in refs}) != len(refs) or refs != sorted(refs, key=lambda ref: ref["source_id"]):
            raise V2Error("invalid-request", "Preparation source references must be unique and sorted")
        return self._start(copy.deepcopy(request), capability, run_id=run_id, preparation=True)

    def _start(self, request, capability, *, run_id, preparation=False):
        _require_ulid(run_id, label="run ID")
        request_hash = hash_bytes(canonical_json(request))
        budget = copy.deepcopy(request["budget"]) if preparation else self._budget(request)
        now = self._now()
        path = self._path(run_id)
        with self.store.writer_lock():
            event = self._authorize(capability, run_id, request, action="prepare" if preparation else "investigate")
            knowledge_revision = request.get("pinned_revision") or self.store.head()
            if request.get("pinned_revision") is not None:
                self.store.manifest(request["pinned_revision"])
            if path.exists():
                existing = self._read(run_id)
                if existing.get("request_hash") != request_hash:
                    raise V2Error("idempotency-conflict", "Run ID already has a different request")
                if existing.get("owner_event_id") != event["id"]:
                    raise V2Error("approval-required", "Run ID is bound to a different owner event")
                return copy.deepcopy(existing)
            if preparation:
                manifest = self.store.manifest(knowledge_revision)
                current = self.store.manifest()
                for ref in request["source_refs"]:
                    if manifest["sources"].get(ref["source_id"]) != ref["source_version"] or current["sources"].get(ref["source_id"]) != ref["source_version"]:
                        raise V2Error("stale-selection", "Preparation requires the exact current source versions")
            segment = {
                "request_id": request["id"],
                "owner_event_id": event["id"],
                "started_at": _timestamp(now),
                "operations": 0,
                "source_expansions": 0,
                "elapsed_seconds": 0,
                "deadline_at": _timestamp(now + timedelta(minutes=int(budget["max_minutes"]))),
                "budget": copy.deepcopy(budget),
            }
            run: dict[str, Any] = {
                "id": run_id,
                "status": "running",
                "owner_event_id": event["id"],
                "request": request,
                "request_hash": request_hash,
                "knowledge_revision": knowledge_revision,
                "usage": {
                    "operations": 0,
                    "source_expansions": 0,
                    "elapsed_seconds": 0,
                    "elapsed_ms": 0,
                    "budget": copy.deepcopy(budget),
                },
                "created_at": _timestamp(now),
                "updated_at": _timestamp(now),
                "checkpoint": {
                    "segment": 0,
                    "operations": 0,
                    "source_expansions": 0,
                    "elapsed_seconds": 0,
                    "segment_elapsed_seconds": 0,
                    "deadline_at": segment["deadline_at"],
                },
                "trace": [],
                "segments": [segment],
                "revalidation_required": False,
                "changed_refs": [],
                "stop_reason": "Running.",
            }
            durable_write(path, canonical_json(run))
            return copy.deepcopy(run)

    def _authorized_run(self, run_id: str, capability: dict[str, str], *, action: str) -> tuple[dict[str, Any], dict[str, Any]]:
        run = self._read(run_id)
        if action == "work":
            action = "prepare" if run["request"].get("mode") == "prepare" else "investigate"
        event = self._authorize(capability, run_id, run["request"], action=action)
        if event["id"] != run.get("owner_event_id"):
            raise V2Error("approval-required", "Capability is not bound to the active owner event")
        return run, event

    def freeze_preparation(self, run_id: str, capability: dict[str, str], records: dict[str, bytes], *, operation_id: str, request_id: str) -> dict[str, Any]:
        """Retain one exact output before admission so response loss is recoverable."""
        from synapse.knowledge import record_descriptor

        _require_ulid(operation_id, label="operation ID")
        _require_ulid(request_id, label="request ID")
        if not records or len(records) > 6:
            raise V2Error("invalid-request", "Preparation output must contain one to six records")
        objects = {}
        for path, raw in records.items():
            row = record_descriptor(raw, path=path)
            objects[row["version"]] = raw
        frozen = {"operation_id": operation_id, "request_id": request_id, "records": {path: hash_bytes(raw) for path, raw in records.items()}}
        with self.store.writer_lock():
            run, _ = self._authorized_run(run_id, capability, action="prepare")
            old = run.get("prepared_output")
            if old is not None:
                if old != frozen:
                    raise V2Error("idempotency-conflict", "Preparation output was already frozen differently")
                return copy.deepcopy(run)
            self._update_elapsed(run, self._now())
            if run["status"] != "running":
                durable_write(self._path(run_id), canonical_json(run))
                raise V2Error("invalid-request", "Preparation is no longer running")
            self.store._persist_objects(objects)
            run["prepared_output"] = frozen
            durable_write(self._path(run_id), canonical_json(run))
            return copy.deepcopy(run)

    def checkpoint(
        self,
        run_id: str,
        capability: dict[str, str],
        *,
        operations: int = 0,
        source_expansions: int = 0,
        trace: Any = None,
        state: Any = None,
    ) -> dict[str, Any]:
        if isinstance(operations, bool) or not isinstance(operations, int) or operations < 0:
            raise V2Error("invalid-request", "operations must be a non-negative integer")
        if isinstance(source_expansions, bool) or not isinstance(source_expansions, int) or source_expansions < 0:
            raise V2Error("invalid-request", "source_expansions must be a non-negative integer")
        additions = _check_trace(trace)
        _strict_value(state, label="checkpoint state") if state is not None else None
        path = self._path(run_id)
        if not path.exists():
            self._read(run_id)
        now = self._now()
        with self.store.writer_lock():
            run, _ = self._authorized_run(run_id, capability, action="work")
            if run["status"] != "running":
                raise V2Error("invalid-request", f"Run is already {run['status']}")
            self._update_elapsed(run, now)
            if run["status"] == "running":
                segment = run["segments"][-1]
                budget = segment["budget"]
                remaining_operations = max(0, int(budget["max_operations"]) - segment["operations"])
                remaining_expansions = max(0, int(budget["max_source_expansions"]) - segment["source_expansions"])
                accepted_operations = min(operations, remaining_operations)
                accepted_expansions = min(source_expansions, remaining_expansions)
                segment["operations"] += accepted_operations
                segment["source_expansions"] += accepted_expansions
                run["usage"]["operations"] += accepted_operations
                run["usage"]["source_expansions"] += accepted_expansions
                if accepted_operations != operations or accepted_expansions != source_expansions:
                    run["status"] = "partial"
                    run["stop_reason"] = "Operation or source-expansion budget exhausted."
                elif run["request"].get("mode") != "prepare" and (segment["operations"] >= int(budget["max_operations"]) or segment["source_expansions"] >= int(budget["max_source_expansions"])):
                    run["status"] = "partial"
                    run["stop_reason"] = "Operation or source-expansion budget exhausted."
            run["trace"].extend(additions)
            run["checkpoint"] = self._new_checkpoint(run, state=state)
            run["updated_at"] = _timestamp(now)
            durable_write(path, canonical_json(run))
            return copy.deepcopy(run)

    def finish(
        self,
        run_id: str,
        capability: dict[str, str],
        *,
        status: str,
        stop_reason: str | None = None,
        state: Any = None,
    ) -> dict[str, Any]:
        if status not in _TERMINAL:
            raise V2Error("invalid-request", "Run status must be completed, partial, cancelled or failed")
        if stop_reason is not None and (not isinstance(stop_reason, str) or not stop_reason.strip()):
            raise V2Error("invalid-request", "stop_reason must be a non-empty string when supplied")
        _strict_value(state, label="checkpoint state") if state is not None else None
        path = self._path(run_id)
        if not path.exists():
            self._read(run_id)
        now = self._now()
        with self.store.writer_lock():
            run, _ = self._authorized_run(run_id, capability, action="work")
            if run["status"] != "running":
                if run["status"] == status:
                    return copy.deepcopy(run)
                raise V2Error("invalid-request", f"Investigation is already {run['status']}")
            self._update_elapsed(run, now)
            if run["status"] != "running":
                run["updated_at"] = _timestamp(now)
                durable_write(path, canonical_json(run))
                if run["status"] == status:
                    return copy.deepcopy(run)
                raise V2Error("invalid-request", "Wall-clock budget exhausted before the requested finish")
            run["status"] = status
            run["stop_reason"] = stop_reason.strip() if stop_reason is not None else _DEFAULT_STOP_REASONS[status]
            run["checkpoint"] = self._new_checkpoint(run, state=state)
            run["updated_at"] = _timestamp(now)
            durable_write(path, canonical_json(run))
            return copy.deepcopy(run)

    def cancel(self, run_id: str, capability: dict[str, str]) -> dict[str, Any]:
        path = self._path(run_id)
        if not path.exists():
            self._read(run_id)
        now = self._now()
        with self.store.writer_lock():
            run, _ = self._authorized_run(run_id, capability, action="work")
            if run["status"] == "cancelled":
                return copy.deepcopy(run)
            if run["status"] in {"completed", "failed"}:
                raise V2Error("invalid-request", f"Investigation is already {run['status']}")
            self._update_elapsed(run, now)
            run["status"] = "cancelled"
            run["stop_reason"] = "Investigation cancelled by the owner."
            run["updated_at"] = _timestamp(now)
            durable_write(path, canonical_json(run))
            return copy.deepcopy(run)

    def _changed_refs(self, previous: str, current: str) -> list[dict[str, Any]]:
        old = self.store.manifest(previous)
        new = self.store.manifest(current)
        changed: list[dict[str, Any]] = []
        for kind, field in (("record", "records"), ("source", "sources")):
            old_rows = old.get(field, {})
            new_rows = new.get(field, {})
            for identity in sorted(set(old_rows) | set(new_rows)):
                if old_rows.get(identity) == new_rows.get(identity):
                    continue
                old_row = old_rows.get(identity) or {}
                new_row = new_rows.get(identity) or {}
                old_version = old_row.get("version") if isinstance(old_row, dict) else old_row
                new_version = new_row.get("version") if isinstance(new_row, dict) else new_row
                changed.append({
                    "kind": kind,
                    "id": identity,
                    "old_version": old_version,
                    "new_version": new_version,
                })
        return changed

    def resume(
        self,
        run_id: str,
        new_request: dict[str, Any],
        capability: dict[str, str],
        *,
        accept_current_revision: bool = False,
    ) -> dict[str, Any]:
        request = self._validate_request(new_request, resume=True)
        budget = self._budget(request)
        _require_ulid(run_id, label="run ID")
        path = self._path(run_id)
        if not path.exists():
            self._read(run_id)
        now = self._now()
        with self.store.writer_lock():
            run = self._read(run_id)
            if run["request"].get("mode") == "prepare":
                raise V2Error("approval-required", "Request preparation of selected sources again; it cannot resume as an investigation")
            event = self._authorize(capability, run_id, request, action="resume")
            if event["id"] == run.get("owner_event_id"):
                raise V2Error("approval-required", "Resumption requires a distinct owner event")
            if run["status"] == "completed":
                raise V2Error("invalid-request", "A completed investigation cannot be resumed")
            if run["status"] == "running":
                raise V2Error("invalid-request", "Investigation is already running")
            if request["continuation_id"] != run_id:
                raise V2Error("approval-required", "Continuation does not identify this investigation")
            if request["subject_ids"] != run["request"].get("subject_ids"):
                raise V2Error("approval-required", "A continuation cannot change the requested subject scope")
            self._update_elapsed(run, now)
            current_revision = self.store.head()
            previous_revision = run["knowledge_revision"]
            changed_refs = [] if previous_revision == current_revision else self._changed_refs(previous_revision, current_revision)
            if previous_revision != current_revision and not accept_current_revision:
                raise V2Error(
                    "revision-expired",
                    "Investigation revision changed; explicit revalidation is required",
                    details={"current_revision": current_revision, "changed_refs": changed_refs},
                )
            segment = {
                "request_id": request["id"],
                "owner_event_id": event["id"],
                "started_at": _timestamp(now),
                "operations": 0,
                "source_expansions": 0,
                "elapsed_seconds": 0,
                "deadline_at": _timestamp(now + timedelta(minutes=int(budget["max_minutes"]))),
                "budget": copy.deepcopy(budget),
            }
            run["request"] = request
            run["request_hash"] = hash_bytes(canonical_json(request))
            run["owner_event_id"] = event["id"]
            run["knowledge_revision"] = current_revision
            run["usage"]["budget"] = copy.deepcopy(budget)
            run["segments"].append(segment)
            run["status"] = "running"
            run["stop_reason"] = "Running continuation."
            run["revalidation_required"] = previous_revision != current_revision
            run["changed_refs"] = changed_refs
            run["checkpoint"] = {
                "segment": len(run["segments"]) - 1,
                "operations": 0,
                "source_expansions": 0,
                "elapsed_seconds": run["usage"]["elapsed_seconds"],
                "segment_elapsed_seconds": 0,
                "deadline_at": segment["deadline_at"],
            }
            run["updated_at"] = _timestamp(now)
            durable_write(path, canonical_json(run))
            return copy.deepcopy(run)

    def status(self, run_id: str) -> dict[str, Any]:
        """Return internal and wire status, persisting a reached deadline."""

        path = self._path(run_id)
        run = self._read(run_id)
        if run["status"] != "running":
            return copy.deepcopy(run | {"wire_status": _WIRE_STATUS[run["status"]]})
        now = self._now()
        with self.store.writer_lock():
            run = self._read(run_id)
            changed = self._update_elapsed(run, now)
            if changed:
                run["updated_at"] = _timestamp(now)
                durable_write(path, canonical_json(run))
            return copy.deepcopy(run | {"wire_status": _WIRE_STATUS[run["status"]]})


__all__ = ["RunManager"]
