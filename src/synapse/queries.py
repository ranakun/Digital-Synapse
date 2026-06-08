"""Deterministic query operations."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

from synapse.index import (
    all_relations,
    connect,
    entity_by_id,
    reindex,
    resolve_entity_ref,
    row_to_entity,
)
from synapse.models import ENTITY_TYPES, RELATION_TYPES, QueryResult
from synapse.util import normalize_name


def find_entities(vault: str | Path | None, text: str, *, limit: int = 20) -> list[dict[str, Any]]:
    reindex(vault)
    conn = connect(vault)
    try:
        rows = []
        try:
            rows = conn.execute(
                """
                SELECT e.* FROM entities_fts f
                JOIN entities e ON e.rowid = f.rowid
                WHERE entities_fts MATCH ?
                LIMIT ?
                """,
                (text, limit),
            ).fetchall()
        except Exception:
            rows = []
        if rows:
            return [row_to_entity(row) for row in rows]

        needle = normalize_name(text)
        results = []
        for row in conn.execute("SELECT * FROM entities ORDER BY name").fetchall():
            entity = row_to_entity(row)
            haystack = normalize_name(
                "\n".join(
                    [
                        entity["name"],
                        " ".join(entity.get("aliases", [])),
                        " ".join(entity.get("tags", [])),
                        row["body"] or "",
                    ]
                )
            )
            if needle and needle in haystack:
                results.append(entity)
            if len(results) >= limit:
                break
        return results
    finally:
        conn.close()


def filter_entities(
    vault: str | Path | None,
    *,
    entity_type: str | None = None,
    tag: str | None = None,
    property_key: str | None = None,
    property_value: str | None = None,
) -> list[dict[str, Any]]:
    reindex(vault)
    conn = connect(vault)
    try:
        rows = conn.execute(
            "SELECT * FROM entities WHERE (? IS NULL OR type = ?) ORDER BY name",
            (entity_type, entity_type),
        ).fetchall()
        results = []
        for row in rows:
            entity = row_to_entity(row)
            if tag and tag not in entity.get("tags", []):
                continue
            if property_key:
                props = entity.get("properties", {})
                if property_key not in props:
                    continue
                if property_value is not None and str(props[property_key]) != property_value:
                    continue
            results.append(entity)
        return results
    finally:
        conn.close()


def neighbors(
    vault: str | Path | None,
    entity_id: str,
    *,
    depth: int = 1,
    relation_types: list[str] | None = None,
    undirected: bool = False,
    include_weak: bool = False,
) -> QueryResult:
    reindex(vault)
    conn = connect(vault)
    try:
        rels = all_relations(conn, include_weak=include_weak)
        allowed = set(relation_types or [])
        adjacency: dict[str, list[dict[str, Any]]] = {}
        for rel in rels:
            if allowed and rel["type"] not in allowed:
                continue
            adjacency.setdefault(rel["from_id"], []).append(rel)
            if undirected:
                reverse = dict(rel)
                reverse["from_id"], reverse["to_id"] = rel["to_id"], rel["from_id"]
                adjacency.setdefault(rel["to_id"], []).append(reverse)

        visited = {entity_id}
        queue: deque[tuple[str, int]] = deque([(entity_id, 0)])
        node_ids: set[str] = {entity_id}
        edges: list[dict[str, Any]] = []
        seen_edges: set[tuple[Any, ...]] = set()
        while queue:
            current, current_depth = queue.popleft()
            if current_depth >= depth:
                continue
            for rel in adjacency.get(current, []):
                target = rel["to_id"]
                edge_key = (rel.get("id"), rel["from_id"], rel["to_id"])
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edges.append(rel)
                node_ids.add(target)
                if target not in visited:
                    visited.add(target)
                    queue.append((target, current_depth + 1))
        nodes = [entity_by_id(conn, node_id) for node_id in sorted(node_ids)]
        return QueryResult(nodes=[node for node in nodes if node], edges=edges)
    finally:
        conn.close()


def path_between(
    vault: str | Path | None,
    start_id: str,
    end_id: str,
    *,
    max_hops: int = 4,
    include_weak: bool = False,
    all_paths: bool = False,
    undirected: bool = True,
) -> QueryResult:
    reindex(vault)
    conn = connect(vault)
    try:
        rels = all_relations(conn, include_weak=include_weak)
        adjacency: dict[str, list[dict[str, Any]]] = {}
        for rel in rels:
            adjacency.setdefault(rel["from_id"], []).append(rel)
            if undirected:
                reverse = dict(rel)
                reverse["from_id"], reverse["to_id"] = rel["to_id"], rel["from_id"]
                adjacency.setdefault(rel["to_id"], []).append(reverse)

        queue: deque[tuple[str, list[str], list[dict[str, Any]]]] = deque(
            [(start_id, [start_id], [])]
        )
        matches: list[tuple[list[str], list[dict[str, Any]]]] = []
        shortest: int | None = None
        while queue:
            current, node_path, edge_path = queue.popleft()
            if shortest is not None and len(edge_path) > shortest:
                continue
            if len(edge_path) >= max_hops:
                continue
            for rel in adjacency.get(current, []):
                target = rel["to_id"]
                if target in node_path:
                    continue
                next_nodes = [*node_path, target]
                next_edges = [*edge_path, rel]
                if target == end_id:
                    shortest = len(next_edges)
                    matches.append((next_nodes, next_edges))
                    if not all_paths:
                        queue.clear()
                        break
                else:
                    queue.append((target, next_nodes, next_edges))

        if not matches:
            start = entity_by_id(conn, start_id)
            end = entity_by_id(conn, end_id)
            return QueryResult(nodes=[node for node in [start, end] if node], edges=[])

        node_ids: set[str] = set()
        edges: list[dict[str, Any]] = []
        seen_edges: set[tuple[Any, ...]] = set()
        for nodes, edge_path in matches:
            node_ids.update(nodes)
            for edge in edge_path:
                key = (edge.get("id"), edge["from_id"], edge["to_id"])
                if key not in seen_edges:
                    seen_edges.add(key)
                    edges.append(edge)
        nodes = [entity_by_id(conn, node_id) for node_id in sorted(node_ids)]
        return QueryResult(nodes=[node for node in nodes if node], edges=edges)
    finally:
        conn.close()


def subgraph_for_entities(vault: str | Path | None, entities: list[dict[str, Any]]) -> QueryResult:
    ids = {entity["id"] for entity in entities}
    conn = connect(vault)
    edges = [
        rel
        for rel in all_relations(conn, include_weak=True)
        if rel["from_id"] in ids and rel["to_id"] in ids
    ]
    conn.close()
    return QueryResult(nodes=entities, edges=edges)


_PLAN_PARAMS = {
    "find": {"text"},
    "neighbors": {"start", "depth", "relation_types", "undirected", "include_weak"},
    "path": {"start", "end", "max_hops", "all", "include_weak"},
    "filter": {"type", "tag", "property", "value"},
}


def _validate_positive_int(params: dict[str, Any], key: str) -> None:
    if key in params and (not isinstance(params[key], int) or params[key] < 1):
        raise ValueError(f"Query plan parameter {key} must be a positive integer")


def validate_query_plan(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Query plan must be an object")
    operation = data.get("operation")
    params = data.get("params") or {}
    if operation not in {"find", "neighbors", "path", "filter"}:
        raise ValueError(f"Unsupported query operation: {operation}")
    if not isinstance(params, dict):
        raise ValueError("Query plan params must be an object")
    unknown = set(params) - _PLAN_PARAMS[operation]
    if unknown:
        raise ValueError(f"Unsupported query plan parameter(s): {', '.join(sorted(unknown))}")
    if operation == "find" and not isinstance(params.get("text"), str):
        raise ValueError("Find query plans require text")
    if operation == "neighbors":
        if not isinstance(params.get("start"), str) or not params.get("start"):
            raise ValueError("Neighbors query plans require start")
        _validate_positive_int(params, "depth")
        if "relation_types" in params:
            relation_types = params["relation_types"]
            if not isinstance(relation_types, list) or not all(
                isinstance(item, str) and item in RELATION_TYPES for item in relation_types
            ):
                raise ValueError("relation_types must be known relation type strings")
        for key in ("undirected", "include_weak"):
            if key in params and not isinstance(params[key], bool):
                raise ValueError(f"Query plan parameter {key} must be boolean")
    if operation == "path":
        for key in ("start", "end"):
            if not isinstance(params.get(key), str) or not params.get(key):
                raise ValueError(f"Path query plans require {key}")
        _validate_positive_int(params, "max_hops")
        for key in ("all", "include_weak"):
            if key in params and not isinstance(params[key], bool):
                raise ValueError(f"Query plan parameter {key} must be boolean")
    if operation == "filter" and "type" in params and params["type"] not in ENTITY_TYPES:
        raise ValueError("Filter query plan type must be a known entity type")
    entity_refs = data.get("entity_refs") or []
    if not isinstance(entity_refs, list) or not all(isinstance(item, dict) for item in entity_refs):
        raise ValueError("entity_refs must be a list of objects")
    return {"operation": operation, "params": params, "entity_refs": entity_refs}


validate_plan = validate_query_plan
check_query_plan = validate_query_plan


def resolve_entity_refs(
    refs: str | list[str],
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    root = vault_path if vault_path is not None else vault
    reindex(root)
    conn = connect(root)
    try:
        ref_list = [refs] if isinstance(refs, str) else refs
        results: list[dict[str, Any]] = []
        for ref in ref_list:
            results.extend(resolve_entity_ref(conn, ref))
        return results
    finally:
        conn.close()
