"""Public, read-only protocol adapter for the Synapse v2 gateway."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from synapse.gateway import Gateway, transport_size
from synapse.v2_contracts import V2Error

READ_OPERATIONS = frozenset(
    {
        "describe",
        "catalog",
        "context",
        "record",
        "source",
        "search_sources",
        "passage",
        "neighbors",
        "path",
        "overview",
        "suggestions",
        "schema",
        "semantic",
        "areas",
        "area",
        "leads",
    }
)
_ARGUMENTS: dict[str, frozenset[str]] = {
    "areas": frozenset({"query", "organization_revision", "offset", "limit"}),
    "area": frozenset({"area_id", "organization_revision", "offset", "limit"}),
    "leads": frozenset({"query", "subject_id", "source_id", "offset", "limit"}),
    "describe": frozenset(),
    "overview": frozenset(),
    "suggestions": frozenset({"subject_id", "facet", "include_parked", "offset", "limit"}),
    "schema": frozenset({"kind"}),
    "semantic": frozenset({"query", "subject_id", "facet", "knowledge_policy", "limit", "source_scope"}),
    "catalog": frozenset(
        {"kind", "subject_id", "facet", "availability", "query", "offset", "limit", "source_scope"}
    ),
    "context": frozenset(
        {
            "ids",
            "query",
            "subject_id",
            "facet",
            "knowledge_policy",
            "valid_at",
            "offset",
            "limit",
            "closure_limit",
            "supports_qualifications",
        }
    ),
    "record": frozenset({"id", "identity", "offset", "limit"}),
    "source": frozenset({"id", "source_id", "version", "offset", "limit"}),
    "search_sources": frozenset({"query", "offset", "limit", "source_scope"}),
    "passage": frozenset({"evidence", "context_characters"}),
    "neighbors": frozenset({"id", "identity", "include_suggestions", "limit", "offset", "relation", "direction", "node_type"}),
    "path": frozenset({"start", "end", "include_suggestions", "max_hops", "max_nodes"}),
}
_POLICIES = frozenset({"mixed", "accepted-only", "current-state"})


def operation_contracts() -> dict[str, Any]:
    """One argument vocabulary shared by native and hosted specialists."""
    examples = {
        "areas": {"query": "learning", "limit": 10},
        "area": {"area_id": "<returned area id>", "organization_revision": "<returned organization revision>", "limit": 30},
        "leads": {"query": "ideas", "limit": 5},
        "catalog": {"kind": "sources", "query": "project", "limit": 10},
        "context": {"query": "project", "knowledge_policy": "mixed", "limit": 5},
        "record": {"id": "me", "offset": 0, "limit": 4000},
        "source": {"source_id": "<returned source id>", "version": "<returned source version>", "offset": 0, "limit": 4000},
        "search_sources": {"query": "project", "limit": 5},
        "passage": {"evidence": "<exact returned evidence object>", "context_characters": 500},
        "neighbors": {"id": "me", "limit": 30},
        "path": {"start": "<record id>", "end": "<record id>", "max_hops": 4},
        "semantic": {"query": "project", "limit": 5},
        "suggestions": {"subject_id": "me", "limit": 5},
    }
    return {method: {"allowed_arguments": sorted(arguments), "example": examples.get(method, {})} for method, arguments in _ARGUMENTS.items()}


def _validate_budget(budget_chars: int) -> None:
    if (
        isinstance(budget_chars, bool)
        or not isinstance(budget_chars, int)
        or not 256 <= budget_chars <= 32_000
    ):
        raise V2Error("invalid-request", "Response budget must be 256–32000 characters")


def _size(value: Mapping[str, Any], representation: str) -> int:
    return transport_size(dict(value), representation)


def _budget_metadata(
    budget_chars: int, representation: str, *, truncated: bool = False
) -> dict[str, Any]:
    return {
        "unit": "characters",
        "limit": budget_chars,
        "representation": representation,
        "truncated": truncated,
    }


def _small_error(
    code: str, message: str, *, budget_chars: int, representation: str
) -> dict[str, Any]:
    """Return an error which itself fits the requested transport budget."""

    value: dict[str, Any] = {
        "error": {"code": code, "message": message},
        "budget": _budget_metadata(budget_chars, representation),
    }
    if _size(value, representation) <= budget_chars:
        return value
    value["error"] = {
        "code": code,
        "message": "Response exceeds the requested character budget; retry with a larger budget.",
    }
    if _size(value, representation) <= budget_chars:
        return value
    return {"error": {"code": code}, "budget": {"limit": budget_chars}}


def _fit_whole(
    value: dict[str, Any], *, budget_chars: int, representation: str, operation: str
) -> dict[str, Any]:
    candidate = copy.deepcopy(value)
    candidate.setdefault("budget", _budget_metadata(budget_chars, representation))
    if _size(candidate, representation) <= budget_chars:
        return candidate
    return _small_error(
        "coverage-limited",
        f"The complete {operation} response does not fit this budget; retry with a larger budget or narrower scope.",
        budget_chars=budget_chars,
        representation=representation,
    )


def _page_flags(value: dict[str, Any], text: str, *, offset: int) -> None:
    next_offset = offset + len(text)
    total = value["total_characters"]
    value["next_offset"] = next_offset if next_offset < total else None
    if "truncated" in value:
        value["truncated"] = value["next_offset"] is not None
    if "end_of_source" in value:
        value["end_of_source"] = value["next_offset"] is None
    if "end_of_record" in value:
        value["end_of_record"] = value["next_offset"] is None
    if "complete" in value:
        value["complete"] = offset == 0 and value["next_offset"] is None
    limitations = value.get("limitations")
    if isinstance(limitations, list):
        value["limitations"] = [
            item for item in limitations if "page is truncated" not in str(item)
        ]
        if value["next_offset"] is not None:
            value["limitations"].append("page is truncated; request next_offset to continue")


def _fit_page(
    value: dict[str, Any],
    *,
    budget_chars: int,
    representation: str,
    operation: str,
    refetch: Callable[[int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    text = value.get("text", "")
    if not isinstance(text, str):
        return _fit_whole(
            value, budget_chars=budget_chars, representation=representation, operation=operation
        )
    offset = int(value["offset"])
    low, high = 0, len(text)
    best: dict[str, Any] | None = None
    while low <= high:
        length = (low + high) // 2
        if refetch is not None:
            if length == 0:
                high = -1
                continue
            candidate = refetch(length)
        else:
            candidate = copy.deepcopy(value)
            candidate["text"] = text[:length]
        _page_flags(candidate, candidate["text"], offset=offset)
        candidate["budget"] = _budget_metadata(
            budget_chars, representation, truncated=length < len(text)
        )
        if _size(candidate, representation) <= budget_chars:
            best = candidate
            low = length + 1
        else:
            high = length - 1
    if best is None:
        return _small_error(
            "coverage-limited",
            f"The {operation} page metadata does not fit this budget; retry with a larger budget.",
            budget_chars=budget_chars,
            representation=representation,
        )
    return best


def _validate_arguments(operation: str, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise V2Error("invalid-request", "Operation arguments must be a dictionary")
    allowed = _ARGUMENTS[operation]
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise V2Error("invalid-request", f"Unknown arguments for {operation}: {', '.join(unknown)}")
    return copy.deepcopy(arguments)


def _alias(arguments: dict[str, Any], primary: str, alternate: str) -> None:
    if primary in arguments and alternate in arguments:
        raise V2Error("invalid-request", f"Supply only one of {primary} and {alternate}")
    if alternate in arguments:
        arguments[primary] = arguments.pop(alternate)


def _build(gateway: Gateway, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if operation == "describe":
        return gateway.describe()
    if operation in {"overview", "suggestions", "semantic", "areas", "leads"}:
        return getattr(gateway, operation)(**arguments)
    if operation == "area":
        if "area_id" not in arguments or "organization_revision" not in arguments:
            raise V2Error("invalid-request", "area requires area_id and organization_revision")
        return gateway.area(**arguments)
    if operation == "schema":
        from synapse.v2_contracts import payload_schema
        return payload_schema(arguments.get("kind", "knowledge_record"))
    if operation == "catalog":
        return gateway.catalog(**arguments)
    if operation == "context":
        policy = arguments.get("knowledge_policy", "mixed")
        if policy not in _POLICIES:
            raise V2Error(
                "invalid-request", "knowledge_policy must be mixed, accepted-only or current-state"
            )
        return gateway.context(**arguments)
    if operation == "record":
        _alias(arguments, "id", "identity")
        if "id" not in arguments:
            raise V2Error("invalid-request", "record requires id")
        identity = arguments.pop("id")
        return gateway.record(identity, **arguments)
    if operation == "source":
        _alias(arguments, "source_id", "id")
        if "source_id" not in arguments:
            raise V2Error("invalid-request", "source requires id")
        source_id = arguments.pop("source_id")
        return gateway.source(source_id, **arguments)
    if operation == "search_sources":
        if "query" not in arguments:
            raise V2Error("invalid-request", "search_sources requires query")
        return gateway.search_sources(**arguments)
    if operation == "passage":
        if "evidence" not in arguments:
            raise V2Error("invalid-request", "passage requires evidence")
        evidence = arguments.pop("evidence")
        return gateway.passage(evidence, **arguments)
    if operation == "neighbors":
        _alias(arguments, "id", "identity")
        if "id" not in arguments:
            raise V2Error("invalid-request", "neighbors requires id")
        identity = arguments.pop("id")
        return gateway.neighbors(identity, **arguments)
    if operation == "path":
        if "start" not in arguments or "end" not in arguments:
            raise V2Error("invalid-request", "path requires start and end")
        return gateway.path(**arguments)
    raise V2Error("unsupported-operation", f"Unsupported read operation: {operation}")


def dispatch(
    vault: Path,
    operation: str,
    arguments: dict[str, Any],
    *,
    revision: str | None = None,
    known_at: str | None = None,
    timezone: str | None = None,
    budget_chars: int = 8000,
    representation: str = "json",
    gateway: Gateway | None = None,
) -> dict[str, Any]:
    """Dispatch one explicitly allowlisted read over one pinned Gateway."""

    try:
        _validate_budget(budget_chars)
        if representation not in {"json", "mcp"}:
            raise V2Error("invalid-request", "Supported representations are json and mcp")
        if operation not in READ_OPERATIONS:
            raise V2Error("unsupported-operation", f"Unsupported read operation: {operation}")
        checked = _validate_arguments(operation, arguments)
        if operation in {"catalog", "context", "search_sources", "suggestions", "semantic", "areas", "area", "leads", "neighbors"}:
            checked["budget_chars"] = budget_chars
            checked["representation"] = representation
        source_id = source_version = source_offset = None
        if operation == "source":
            source_arguments = copy.deepcopy(checked)
            _alias(source_arguments, "source_id", "id")
            source_id = source_arguments.get("source_id")
            source_version = source_arguments.get("version")
            source_offset = source_arguments.get("offset", 0)
        if gateway is None:
            gateway = Gateway(Path(vault), revision=revision, known_at=known_at, timezone=timezone)
        elif Path(vault).resolve() != gateway.view.store.vault.resolve() or (revision is not None and gateway.revision != revision) or known_at is not None:
            raise V2Error("invalid-request", "The supplied read session does not match the requested vault/revision")
        value = _build(gateway, operation, checked)
        if operation == "source":
            return _fit_page(
                value,
                budget_chars=budget_chars,
                representation=representation,
                operation=operation,
                refetch=lambda limit: gateway.source(
                    source_id,
                    version=source_version,
                    offset=source_offset,
                    limit=limit,
                ),
            )
        if operation == "record":
            return _fit_page(
                value, budget_chars=budget_chars, representation=representation, operation=operation
            )
        if operation == "passage":
            candidate = copy.deepcopy(value)
            candidate["budget"] = _budget_metadata(budget_chars, representation)
            if _size(candidate, representation) > budget_chars:
                return _small_error(
                    "coverage-limited",
                    "The complete evidence passage does not fit; retry with a larger budget or request a smaller surrounding context. The exact passage is never truncated.",
                    budget_chars=budget_chars,
                    representation=representation,
                )
            return candidate
        return _fit_whole(
            value, budget_chars=budget_chars, representation=representation, operation=operation
        )
    except V2Error as exc:
        return _small_error(
            exc.to_dict()["code"],
            exc.message,
            budget_chars=budget_chars,
            representation=representation,
        )
    except (TypeError, ValueError) as exc:
        return _small_error(
            "invalid-request", str(exc), budget_chars=budget_chars, representation=representation
        )


__all__ = ["READ_OPERATIONS", "dispatch"]
