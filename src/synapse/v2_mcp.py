"""Read-only MCP adapter for the Synapse v2 protocol."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolRequest, CallToolResult, ServerResult, TextContent

from synapse.consultation_budget import (
    PRESETS,
    RECEIPT_GRACE_SECONDS,
    SESSION_CAPACITY,
    ConsultationSessions,
    preset_budget,
)
from synapse.gateway import Gateway, workspace_binding
from synapse.source_purpose import load_source_purposes
from synapse.v2_contracts import V2Error
from synapse.v2_protocol import dispatch

DESCRIBE_BUDGET = 16000
READ_FIELDS = frozenset({
    "operation", "arguments", "revision", "known_at", "timezone", "budget", "session_token",
})
SIZE_RECOVERY = (
    "Set top-level budget (characters, maximum 32000) only as needed and within the "
    "authorized output ceiling; retry with the same session_token and pinned revision/policy. "
    "Retries still cost reads/expansions. At the ceiling, narrow selection or page size; "
    "for passage reduce context_characters. Never split exact evidence or mandatory "
    "qualifications, or substitute raw pages. If a complete unit still cannot fit, "
    "withhold conclusions depending on it. Do not reset or bypass the session."
)


def _response_budget(value: Any) -> int:
    return value if type(value) is int and 256 <= value <= 32000 else 8000


def _serialized(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=True
    )


def _result(value: dict[str, Any], *, budget: int) -> CallToolResult:
    error = value.get("error")
    size_error = error == "insufficient-budget" or (
        isinstance(error, dict) and error.get("code") == "coverage-limited"
        and any(word in error.get("message", "") for word in ("fit", "exceeds"))
    )
    if size_error:
        value = dict(value)
        if isinstance(error, dict):
            value["error"] = {**error, "message": SIZE_RECOVERY}
        else:
            value["expansion"] = SIZE_RECOVERY
    text = _serialized(value)
    result = CallToolResult(content=[TextContent(type="text", text=text)], isError="error" in value)
    if len(result.model_dump_json(exclude_none=True)) <= budget:
        return result
    compact = {
        "error": {
            "code": error.get("code", "coverage-limited") if isinstance(error, dict) else "coverage-limited",
            "message": error.get("message", SIZE_RECOVERY) if isinstance(error, dict) else SIZE_RECOVERY,
        },
        "budget": {"unit": "characters", "limit": budget, "representation": "mcp"},
    }
    for key in ("usage", "metering"):
        if key in value:
            compact[key] = value[key]
    result = CallToolResult(
        content=[TextContent(type="text", text=_serialized(compact))],
        isError=True,
    )
    if len(result.model_dump_json(exclude_none=True)) <= budget:
        return result
    # Even the 256-character transport must preserve usage. The compact receipt
    # uses [calls, expansions, seconds] for remaining; discovery documents this.
    compact = {"error": {"code": value.get("error", {}).get("code", "coverage-limited")
                         if isinstance(value.get("error"), dict) else "coverage-limited"}}
    if "usage" in value:
        compact["usage"] = dict(value["usage"])
        remaining = compact["usage"]["remaining"]
        compact["usage"]["remaining"] = [remaining[key] for key in ("calls", "expansions", "seconds")]
    elif "metering" in value:
        compact["metering"] = value["metering"]
    hint = "See synapse_v2_describe for budget recovery."
    if isinstance(error, dict) and "Unknown top-level" in error.get("message", ""):
        hint = "Use top-level budget; see synapse_v2_describe."
    compact["error"]["message"] = hint
    result = CallToolResult(content=[TextContent(type="text", text=_serialized(compact))], isError=True)
    if len(result.model_dump_json(exclude_none=True)) <= budget:
        return result
    compact["error"].pop("message")
    result = CallToolResult(content=[TextContent(type="text", text=_serialized(compact))], isError=True)
    if len(result.model_dump_json(exclude_none=True)) > budget:
        compact["error"] = "coverage-limited"
        result = CallToolResult(content=[TextContent(type="text", text=_serialized(compact))], isError=True)
    return result


def _consultation_contract(*, detailed: bool = False) -> dict:
    contract = {
        "begin": {"operation": "begin_consultation", "arguments": {"preset": "consult"}},
        "target_check": "Compare describe.workspace.id with setup status.workspace_binding.id from the intended installation. Pass that expected ID as begin arguments.expected_workspace_id; a mismatch rejects before opening knowledge. Never copy the ID from an unintended connection to bypass this check.",
        "describe_budget_characters": DESCRIBE_BUDGET,
        "response_budget": {
            "field": "budget", "location": "top-level synapse_v2_read arguments",
            "unit": "characters", "default": 8000, "minimum": 256, "maximum": 32000,
            "begin_minimum": 1024,
            "scope": "Complete CallToolResult including escaping, metadata and usage; budget.limit reserves receipt space. Separate from session counters and final result ceiling. See tools/list for allowed fields.",
            "example": {"name": "synapse_v2_read", "arguments": {
                "operation": "context", "arguments": {"ids": ["me"], "knowledge_policy": "mixed"},
                "budget": 12000, "session_token": "<returned session_token>",
                "revision": "<returned knowledge_revision>",
            }},
            "recovery": SIZE_RECOVERY,
            "omissions": "Check budget.truncated, omitted_units and next_offset; empty omitted pages are not absence. Do not repeat a non-advancing page unchanged.",
            "tiny_errors": "Code-only errors: use free synapse_v2_describe for recovery guidance.",
        },
        "read": "Pass the returned session_token to synapse_v2_read; revision is pinned at begin.",
        "end": {"operation": "end_consultation", "arguments": {}},
        "presets": {name: {**preset_budget(name), "max_operations": limits.max_operations}
                    for name, limits in PRESETS.items()},
        "free_operations": ["describe", "schema"],
        "expansion_operations": ["source", "passage", "research"],
        "accounting": "Each attempted read costs one call even when it fails. Source/passage cost one expansion. Research is unavailable on this read-only MCP.",
        "usage": "elapsed_seconds, calls, expansions, remaining {calls, expansions, seconds}; tiny error receipts encode remaining as [calls, expansions, seconds].",
        "limits": {"sessions": SESSION_CAPACITY, "receipt_grace_seconds": RECEIPT_GRACE_SECONDS},
        "lifetime": "In memory only; fixed preset deadline, then receipt grace. End releases the token. Restart loses sessions. Limits apply per consultation, not per owner/account.",
        "legacy": "Tokenless reads remain available and are unmetered. Use a session for a normal consultation; do not restart one to evade its budget.",
        "exhaustion": "Stop retrieval and synthesize from evidence already read. End remains available.",
        "source_purpose_hash": "Pinned at begin; policy drift rejects reads even if knowledge_revision is unchanged.",
    }
    if not detailed:
        contract.pop("target_check")
        # The default read envelope also carries gateway metadata and usage.
        # Full operational detail remains on the free, larger describe tool.
        contract["lifetime"] = "Transient sessions; restart loses them. Never reset to evade limits."
        contract["accounting"] = "Attempted reads cost calls; source/passage also cost expansions."
        contract["legacy"] = "Tokenless reads are unmetered. Normal consultations require sessions."
        # Keep operation=describe useful at its existing default response limit.
        contract["response_budget"] = {
            "field": "budget", "unit": "characters", "minimum": 256, "maximum": 32000,
            "default": 8000, "guidance": "Use free synapse_v2_describe for the exact MCP call example and whole-evidence recovery guidance.",
        }
    return contract


def register_tools(server: FastMCP, vault_provider) -> None:
    """Register the same tools on the existing shared MCP service."""
    sessions = ConsultationSessions()

    @server.tool(name="synapse_v2_describe", structured_output=False,
                 description="Discover read operations and consultation sessions; complete response bounded to 16000 characters.")
    def describe() -> CallToolResult:
        payload = dispatch(vault_provider(), "describe", {}, representation="mcp", budget_chars=DESCRIBE_BUDGET - 3000)
        payload["consultation"] = _consultation_contract(detailed=True)
        payload["metering"] = "unmetered"
        return _result(payload, budget=DESCRIBE_BUDGET)

    @server.tool(name="synapse_v2_read", structured_output=False,
                 description="Read retained v2 knowledge. Top-level budget is the complete response character limit (default 8000, range 256–32000); unknown top-level fields are rejected. Begin with operation=begin_consultation and arguments={preset: consult|focused|broad}; pass the returned session_token on reads and end_consultation. Describe/schema are free; failures count. Tokenless legacy reads are unmetered. No capture, approval or run authority.")
    def read(operation: str, arguments: dict[str, Any] | None = None, revision: str | None = None, known_at: str | None = None, timezone: str | None = None, budget: int = 8000, session_token: str | None = None) -> CallToolResult:
        bounded = _response_budget(budget)
        session = None
        try:
            if operation == "begin_consultation":
                if session_token is not None:
                    # A begin call must never reset an existing session.
                    session = sessions.get(session_token)
                    with session.lock:
                        session.budget.charge(operation)
                        raise V2Error("invalid-request", "An existing consultation cannot be reset")
                if bounded != budget or budget < 1024:
                    raise V2Error("invalid-request", "Begin consultation requires a response budget of 1024–32000 characters")
                args = arguments if arguments is not None else {}
                if set(args) - {"preset", "expected_workspace_id"}:
                    raise V2Error("invalid-request", "Begin consultation accepts preset and expected_workspace_id")
                preset = args.get("preset", "consult")
                preset_budget(preset)
                vault = Path(vault_provider())
                binding = workspace_binding(vault)
                if "expected_workspace_id" in args and args["expected_workspace_id"] != binding["id"]:
                    raise V2Error("invalid-request", "This Synapse connection targets another workspace. Open the intended conversation workspace in a new Codex task and verify its connection; do not read or replace the existing connection.")
                gateway = Gateway(vault, revision=revision, known_at=known_at,
                                  timezone=timezone)
                token, session = sessions.begin(gateway.view.store.vault, gateway.revision,
                                                preset=preset, timezone=gateway.view.timezone,
                                                known_at=known_at,
                                                source_purpose_hash=gateway.view.source_purpose_policy().snapshot_hash)
                return _result({"session_token": token, "knowledge_revision": session.revision,
                                "workspace": binding,
                                "source_purpose_hash": session.source_purpose_hash,
                                "preset": preset, "metering": "session", "usage": session.budget.receipt()},
                               budget=bounded)
            if session_token is not None:
                session = sessions.get(session_token)
                with session.lock:
                    if operation != "end_consultation":
                        session.budget.charge(operation)
                    if Path(vault_provider()).resolve() != session.vault:
                        raise V2Error("invalid-request", "The consultation is bound to a different vault")
                    if (revision is not None and revision != session.revision
                            or known_at is not None and known_at != session.known_at
                            or timezone is not None and timezone != session.timezone):
                        raise V2Error("invalid-request", "A retrieval call cannot change the pinned revision or temporal view")
                    args = dict(arguments) if arguments is not None else {}
                    for key in ("revision", "pinned_revision"):
                        if key in args and args.pop(key) != session.revision:
                            raise V2Error("invalid-request", "A retrieval call cannot change the pinned revision")
                    if operation == "end_consultation":
                        if args:
                            raise V2Error("invalid-request", "End consultation accepts no arguments")
                        payload = {"status": "ended", "knowledge_revision": session.revision,
                                   "usage": sessions.end(session_token, session)}
                    else:
                        def check_policy():
                            current = load_source_purposes(session.vault, revision=session.revision).snapshot_hash
                            if current != session.source_purpose_hash:
                                raise V2Error("stale-selection", "Source purpose policy changed during this consultation; evidence must be revalidated")
                        check_policy()
                        # Reserve receipt overhead before protocol paging/whole-unit fitting.
                        payload = dispatch(session.vault, operation, args, revision=session.revision,
                                           timezone=session.timezone, budget_chars=max(256, bounded - 512)
                                           if bounded == budget else budget, representation="mcp")
                        # Discard output if policy changed while the read was in flight.
                        check_policy()
                        if operation == "describe":
                            payload["consultation"] = _consultation_contract()
                    payload.update(metering="session", usage=session.budget.receipt())
                    return _result(payload, budget=bounded)
            if operation == "end_consultation":
                raise V2Error("invalid-request", "End consultation requires session_token")
            payload = dispatch(vault_provider(), operation, arguments if arguments is not None else {},
                               revision=revision, known_at=known_at, timezone=timezone,
                               budget_chars=budget, representation="mcp")
            if operation == "describe":
                payload["consultation"] = _consultation_contract()
            payload["metering"] = "unmetered"
            return _result(payload, budget=bounded)
        except (V2Error, TypeError, ValueError) as exc:
            error = exc if isinstance(exc, V2Error) else V2Error("invalid-request", "Invalid consultation arguments")
            payload = {"error": error.to_dict()}
            if session is not None:
                payload.update(metering="session", usage=session.budget.receipt())
            return _result(payload, budget=bounded)

    # FastMCP's generated argument model ignores extras, and its MCP handler
    # disables JSON Schema input validation. Guard the actual request before
    # that conversion, scoped to these two tools on the shared v1/v2 service.
    # Preserve the existing handler (including other tools) and charge rejected
    # session reads exactly as read() does, without dispatching or ending them.
    previous_handler = server._mcp_server.request_handlers[CallToolRequest]
    for name in ("synapse_v2_describe", "synapse_v2_read"):
        server._tool_manager.get_tool(name).parameters["additionalProperties"] = False

    async def strict_arguments(request: CallToolRequest) -> ServerResult:
        name = request.params.name
        if name not in {"synapse_v2_describe", "synapse_v2_read"}:
            return await previous_handler(request)
        fields = request.params.arguments or {}
        allowed = READ_FIELDS if name == "synapse_v2_read" else frozenset()
        unknown = set(fields) - allowed
        if not unknown:
            return await previous_handler(request)
        bounded = _response_budget(fields.get("budget", 8000)) if allowed else DESCRIBE_BUDGET
        # Bound diagnostics independently of caller-controlled field lengths.
        names = ", ".join(key[:60] for key in sorted(unknown)[:4])
        error = V2Error("invalid-request", (
            f"Unknown top-level fields: {names}. "
            + ("Use top-level budget (256–32000 characters); put operation fields in arguments. "
               "See synapse_v2_describe for the exact shape."
               if allowed else "synapse_v2_describe accepts no arguments; set budget on synapse_v2_read.")
        ))
        payload = {"error": error.to_dict()}
        token = fields.get("session_token") if allowed else None
        if token is not None:
            try:
                session = sessions.get(token)
                with session.lock:
                    try:
                        operation = fields.get("operation")
                        if operation != "end_consultation":
                            session.budget.charge(operation if isinstance(operation, str) else "invalid-request")
                    except V2Error as exc:
                        payload["error"] = exc.to_dict()
                    payload.update(metering="session", usage=session.budget.receipt())
            except V2Error as exc:
                payload["error"] = exc.to_dict()
        return ServerResult(_result(payload, budget=bounded))

    server._mcp_server.request_handlers[CallToolRequest] = strict_arguments


def make_server(vault: Path) -> FastMCP:
    """Build an isolated read server bound to one vault."""

    server = FastMCP(
        "synapse-v2",
        instructions=(
            "Read-only Digital Synapse v2 gateway. Use synapse_v2_describe for the current "
            "allowlist and coverage, then synapse_v2_read with one operation: describe, "
            "catalog, context, record, source, search_sources, passage, neighbors, path, semantic, overview, suggestions, areas, area, leads or schema. "
            "Arguments are strict JSON objects; preserve knowledge_revision and next_offset. "
            "search_sources searches retained source text, including unprocessed material, "
            "and returns exact evidence spans. Responses are budgeted whole units. This "
            "server has no capture, approval, run-start or canonical-data write tools."
            " Begin normal consultations through synapse_v2_read operation=begin_consultation "
            "with arguments={preset: consult|focused|broad}, retain session_token on every read, "
            "and call end_consultation when done. Tokenless legacy reads are unmetered. "
            "At exhaustion synthesize from existing evidence; do not reset the session."
        ),
    )

    register_tools(server, lambda: vault)

    return server


def run_stdio(vault: Path) -> None:
    """Run the isolated v2 server over stdio."""

    asyncio.run(make_server(vault).run_stdio_async())


__all__ = ["make_server", "run_stdio"]
