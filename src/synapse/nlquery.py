"""Natural-language query plan compilation and execution."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from synapse.index import connect, reindex, resolve_entity_ref
from synapse.providers import CompletionRequest, Generator, OpenAICompatibleGenerator
from synapse.queries import (
    find_entities,
    neighbors,
    path_between,
    subgraph_for_entities,
    validate_query_plan,
)

ALLOWED_OPERATIONS = {"find", "neighbors", "path", "filter"}


def validate_plan(data: dict[str, Any]) -> dict[str, Any]:
    return validate_query_plan(data)


def plan_prompt(question: str) -> str:
    return f"""
Compile this Digital Synapse question to a JSON query plan. Return JSON only.
Allowed operations: find, neighbors, path, filter.
Allowed relation vocabulary is fixed by the SDS.
Params:
- find: {{"text": "..."}}
- neighbors: {{"start": "entity-ref", "depth": 1, "relation_types": [], "undirected": true}}
- path: {{"start": "entity-ref", "end": "entity-ref", "max_hops": 4}}
- filter: {{"type": "person|company|project|goal|finance", "tag": "..."}}

Question: {question}
""".strip()


def compile_plan(
    question: str, *, vault: str | Path | None = None, generator: Generator | None = None
) -> dict[str, Any]:
    gen = generator or OpenAICompatibleGenerator(vault)
    result = gen.complete(
        CompletionRequest(task="query", schema_name="query_plan", prompt=plan_prompt(question))
    )
    return validate_plan(result.data)


def _resolve_single(conn, ref: str) -> str:
    matches = resolve_entity_ref(conn, ref)
    if not matches:
        raise ValueError(f"Could not resolve entity reference: {ref}")
    if len(matches) > 1:
        names = ", ".join(f"{item['name']} ({item['id']})" for item in matches[:5])
        raise ValueError(f"Disambiguation required for entity reference {ref}. Candidates: {names}")
    return matches[0]["id"]


def execute_plan(vault: str | Path | None, plan: dict[str, Any]) -> dict[str, Any]:
    plan = validate_plan(plan)
    operation = plan["operation"]
    params = plan["params"]
    reindex(vault)
    conn = connect(vault)
    try:
        if operation == "find":
            return subgraph_for_entities(
                vault, find_entities(vault, str(params.get("text") or ""))
            ).to_dict()
        if operation == "neighbors":
            start = _resolve_single(conn, str(params.get("start") or ""))
            return neighbors(
                vault,
                start,
                depth=int(params.get("depth") or 1),
                relation_types=[str(item) for item in params.get("relation_types") or []],
                undirected=bool(params.get("undirected", True)),
            ).to_dict()
        if operation == "path":
            start = _resolve_single(conn, str(params.get("start") or ""))
            end = _resolve_single(conn, str(params.get("end") or ""))
            return path_between(
                vault, start, end, max_hops=int(params.get("max_hops") or 4)
            ).to_dict()
        if operation == "filter":
            from synapse.queries import filter_entities

            return subgraph_for_entities(
                vault,
                filter_entities(vault, entity_type=params.get("type"), tag=params.get("tag")),
            ).to_dict()
        raise ValueError(f"Unsupported query operation: {operation}")
    finally:
        conn.close()


def execute_question(
    question: str,
    *,
    vault: str | Path | None = None,
    generator: Generator | None = None,
) -> dict[str, Any]:
    # Deterministic shorthand for tests and exports.
    parts = question.split()
    if len(parts) >= 2 and parts[0].casefold() == "find":
        return subgraph_for_entities(vault, find_entities(vault, " ".join(parts[1:]))).to_dict()
    if len(parts) >= 2 and parts[0].casefold() == "neighbors":
        plan = {
            "operation": "neighbors",
            "params": {"start": " ".join(parts[1:]), "depth": 1, "undirected": True},
        }
        return execute_plan(vault, plan)
    if len(parts) >= 3 and parts[0].casefold() == "path":
        return execute_plan(
            vault,
            {"operation": "path", "params": {"start": parts[1], "end": parts[2], "max_hops": 4}},
        )
    match = re.search(r"\b(?:to|at|with)\s+(.+?)\??$", question, flags=re.IGNORECASE)
    if match:
        ref = match.group(1).strip()
        plan = {"operation": "neighbors", "params": {"start": ref, "depth": 2, "undirected": True}}
        return execute_plan(vault, plan)
    return execute_plan(vault, compile_plan(question, vault=vault, generator=generator))


def plan_to_json(plan: dict[str, Any]) -> str:
    return json.dumps(validate_plan(plan), indent=2, sort_keys=True)
