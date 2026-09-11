"""Revision-pinned read helpers for the v2 graph explorer."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from synapse.gateway import Gateway, compact_organization_coverage, serialized, transport_size
from synapse.revisions import RevisionStore
from synapse.v2_protocol import dispatch

MAX_GRAPH_NODES = 151
MAX_GRAPH_EDGES = 500


def build_v2_session(vault: Path) -> dict[str, Any]:
    """Detect retained mode without deriving an index or organization map."""
    vault = Path(vault)
    if not (vault / "_synapse" / "HEAD").exists():
        return {"mode": "legacy"}
    revision = RevisionStore(vault).head()
    return {"mode": "v2", "knowledge_revision": revision, "session": _head_state(vault, revision)}


def _head_state(vault: Path, revision: str) -> dict[str, Any]:
    head = RevisionStore(vault).head()
    return {
        "revision": revision,
        "head_revision": head,
        "changed": head != revision,
        "notice": "HEAD changed; refresh deliberately to inspect the newer revision." if head != revision else None,
    }


def _freshness(gateway: Gateway) -> dict[str, Any]:
    sources = [
        gateway.view.manifest["source_versions"][version]
        for version in (gateway.view.manifest.get("sources") or {}).values()
    ]
    capture_times = []
    for source in sources:
        captured = source.get("captured_at")
        if isinstance(captured, str):
            try:
                capture_times.append(datetime.fromisoformat(captured.replace("Z", "+00:00")))
            except ValueError:
                continue
    today = datetime.now(UTC).date()
    record_map = gateway.view.manifest.get("records") or {}
    record_ids = list(record_map) if isinstance(record_map, dict) else []
    records = gateway.view.records(ids=record_ids) if record_ids else []
    applicable = [
        row for row in records
        if row.get("applies_from") or row.get("applies_until")
    ]
    expired = [
        row for row in applicable
        if isinstance(row.get("applies_until"), str) and row["applies_until"] < today.isoformat()
    ]
    return {
        "sources_with_capture_time": len(capture_times),
        "sources_without_capture_time": len(sources) - len(capture_times),
        "latest_capture_at": max(capture_times).isoformat() if capture_times else None,
        "records_with_applicability": len(applicable),
        "records_past_applicability": len(expired),
        "meaning": "Freshness is a retained timestamp and applicability signal; it does not prove that the knowledge is complete or current.",
    }


def build_v2_overview(vault: Path, *, revision: str | None = None) -> dict[str, Any]:
    vault = Path(vault)
    gateway = Gateway(vault, revision=revision)
    summary = gateway.overview()
    suggestions = gateway.suggestions(limit=20, budget_chars=4000)
    summary.update(
        {
            "session": _head_state(vault, gateway.revision),
            "freshness": _freshness(gateway),
            "suggestions": {
                "available_groups": suggestions.get("total_groups", 0),
                "sample": suggestions.get("items", []),
                "review_required": False,
                "guidance": suggestions.get("guidance"),
            },
            "limitations": [
                summary["meaning"],
                "Only current source pointers are counted in the overview; retained historical versions remain available through source reads.",
                "Suggestions are relevant provisional material, not an overdue review queue.",
            ],
        }
    )
    return summary


def build_v2_read(
    vault: Path,
    operation: str,
    arguments: dict[str, Any] | None = None,
    *,
    revision: str | None = None,
    budget_chars: int = 8000,
) -> dict[str, Any]:
    if isinstance(budget_chars, bool) or not isinstance(budget_chars, int) or not 256 <= budget_chars <= 32000:
        return {"error": {"code": "invalid-request", "message": "Response budget must be 256–32000 characters"}}
    gateway = Gateway(Path(vault), revision=revision)
    session = _head_state(Path(vault), gateway.revision)
    overhead = len(serialized({"session": session})) + 2
    inner_budget = budget_chars - overhead
    if inner_budget < 256:
        return {"error": {"code": "coverage-limited", "message": "The complete read and session do not fit; increase the budget."}}
    payload = dispatch(
        Path(vault),
        operation,
        arguments or {},
        revision=gateway.revision,
        gateway=gateway,
        budget_chars=inner_budget,
    )
    if isinstance(payload, dict) and isinstance(payload.get("knowledge_revision"), str):
        payload["session"] = session
    if "budget" in payload:
        payload["budget"]["limit"] = budget_chars
    if transport_size(payload) > budget_chars:
        return {"error": {"code": "coverage-limited", "message": "The complete read does not fit; increase the budget."}}
    return payload


def build_v2_catalog(
    vault: Path,
    *,
    revision: str | None = None,
    kind: str = "records",
    subject_id: str | None = None,
    facet: str | None = None,
    availability: str | None = None,
    query: str | None = None,
    offset: int = 0,
    limit: int = 20,
    budget_chars: int = 8000,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"kind": kind, "offset": offset, "limit": limit}
    for key, value in {
        "subject_id": subject_id,
        "facet": facet,
        "availability": availability,
        "query": query,
    }.items():
        if value is not None:
            arguments[key] = value
    return build_v2_read(vault, "catalog", arguments, revision=revision, budget_chars=budget_chars)


def _edge_key(edge: Mapping[str, Any]) -> str:
    return str(edge.get("id") or f"{edge.get('from_id')}:{edge.get('to_id')}:{edge.get('relation', edge.get('type'))}")


def build_v2_graph(
    vault: Path,
    focus_ids: Iterable[str],
    *,
    revision: str | None = None,
    include_suggestions: bool = False,
    limit: int = 150,
    organization_revision: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic aggregate of selected neighborhoods under hard caps."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 150:
        return {"error": {"code": "invalid-request", "message": "Graph limit must be between 1 and 150."}}
    selected = list(dict.fromkeys(item for item in focus_ids if isinstance(item, str) and item))
    if not selected:
        selected = ["me"]
    if len(selected) > MAX_GRAPH_NODES:
        return {"error": {"code": "invalid-request", "message": "Select at most 151 focus records per graph request."}}
    gateway = Gateway(Path(vault), revision=revision)
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    truncated = False
    withheld_edges = 0
    for identity in selected:
        result = gateway.neighbors(identity, include_suggestions=include_suggestions, limit=limit)
        if result.get("truncated"):
            truncated = True
        withheld_edges += int(result.get("withheld_edges", 0))
        for node in result.get("nodes", []):
            if isinstance(node, Mapping) and isinstance(node.get("id"), str):
                nodes.setdefault(node["id"], dict(node))
        for edge in result.get("edges", []):
            if isinstance(edge, Mapping):
                item = dict(edge)
                edges.setdefault(_edge_key(item), item)

    if organization_revision:
        from synapse.organization import Organization, display_title
        snapshot = Organization(gateway)._snapshot_view(organization_revision=organization_revision)
        membership = {member["ref"].get("record_id"): member["area_ids"]
                      for member in snapshot["members"] if member["kind"] == "record"}
        for identity, node in nodes.items():
            node["area_ids"] = membership.get(identity, [])
            node["display_label"] = display_title(node.get("name") or node.get("label") or identity)
    focus_set = set(selected)
    ordered_ids = [identity for identity in selected if identity in nodes]
    ordered_ids.extend(
        identity for identity in sorted(nodes, key=lambda value: (str(nodes[value].get("name", "")).casefold(), value))
        if identity not in focus_set
    )
    if len(ordered_ids) > MAX_GRAPH_NODES:
        truncated = True
    kept_ids = set(ordered_ids[:MAX_GRAPH_NODES])
    ordered_edges = sorted(
        edges.values(), key=lambda item: (str(item.get("from_id")), str(item.get("to_id")), str(item.get("relation", item.get("type"))), _edge_key(item))
    )
    kept_edges = [
        edge for edge in ordered_edges
        if edge.get("from_id") in kept_ids and edge.get("to_id") in kept_ids
    ]
    if len(kept_edges) > MAX_GRAPH_EDGES:
        truncated = True
        kept_edges = kept_edges[:MAX_GRAPH_EDGES]
    if len(kept_edges) < len(ordered_edges):
        truncated = True
    return {
        "knowledge_revision": gateway.revision,
        "session": _head_state(Path(vault), gateway.revision),
        "knowledge_policy": "mixed" if include_suggestions else "accepted-only",
        "include_suggestions": include_suggestions,
        "focus_ids": [identity for identity in selected if identity in kept_ids],
        "nodes": [nodes[identity] for identity in ordered_ids[:MAX_GRAPH_NODES] if identity in kept_ids],
        "edges": kept_edges,
        "withheld_edges": withheld_edges,
        "truncated": truncated,
        "limits": {"nodes": MAX_GRAPH_NODES, "edges": MAX_GRAPH_EDGES},
        "limitations": [
            "The map is a selected neighborhood, not the whole graph.",
            "Suggested connections are provisional and remain labeled by availability and review state.",
        ] + (["This map is capped; expand a narrower focus to inspect more."] if truncated else []),
    }


__all__ = ["build_v2_catalog", "build_v2_graph", "build_v2_overview", "build_v2_read"]


def _map_member(member: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(serialized({key: member[key] for key in ("id", "kind", "label", "ref", "area_ids", "reasons", "qualification")}))


def _map_area(area: Mapping[str, Any], organization_revision: str) -> dict[str, Any]:
    return {
        "id": area["id"], "kind": "area", "label": area["label"],
        "coverage": area["coverage"],
        "summary": {"text": area["summary"]["text"], "limitations": area["summary"]["limitations"]},
        "expansion": {"area_id": area["id"], "organization_revision": organization_revision},
    }


def _map_selection(snapshot, selected, nodes):
    if not selected:
        return None
    area = next((item for item in snapshot["areas"] if item["id"] == selected), None)
    if area is not None:
        return {"id": selected, "state": "available" if any(node["id"] == selected for node in nodes) else "outside-view", "member": _map_area(area, snapshot["organization_revision"])}
    if selected.startswith("area:"):
        return {"id": selected, "state": "outside-view", "explanation": "The grouping has changed. Explore the current areas; this does not mean the underlying material was removed."}
    member = next((item for item in snapshot["members"] if selected in {item["id"], item["ref"].get("record_id"), item["ref"].get("source_id")}), None)
    if member is None:
        return {"id": selected, "state": "unavailable", "explanation": "This selection is no longer eligible in the pinned view; retained history may still be readable."}
    visible = next((node for node in nodes if node["id"] == member["id"] or (member["kind"] == "source" and node.get("ref", {}).get("source_id") == member["ref"]["source_id"])), None)
    return {"id": visible["id"] if visible else member["id"], "state": "available" if visible else "outside-view", "member": visible or _map_member(member)}


def _fit_map(payload, *, budget_chars):
    """Bound the whole scene without trimming evidence or qualification units."""
    if isinstance(budget_chars, bool) or not isinstance(budget_chars, int) or not 256 <= budget_chars <= 32000:
        return {"error": {"code": "invalid-request", "message": "Response budget must be 256–32000 characters"}}
    payload = json.loads(serialized(payload))
    nodes, edges = payload["nodes"], payload["edges"]
    removed_nodes = removed_edges = 0
    while True:
        visible = {node["id"] for node in nodes}
        old_edges = len(edges)
        edges[:] = [edge for edge in edges if edge["from"] in visible and edge["to"] in visible]
        removed_edges += old_edges - len(edges)
        total = payload["page"]["total"]
        payload["page"]["next_offset"] = payload["page"]["offset"] + len(nodes) if payload["page"]["offset"] + len(nodes) < total else None
        payload["omissions"].update(visible_nodes=len(nodes), visible_edges=len(edges), budget_nodes=removed_nodes, budget_edges=removed_edges)
        payload["truncated"] = bool(payload["page"]["next_offset"] is not None or removed_edges or payload["omissions"]["edge_cap"])
        payload["budget"] = {"unit": "characters", "limit": budget_chars, "truncated": bool(removed_nodes or removed_edges)}
        selection = payload.get("selection")
        if selection and selection.get("member"):
            in_focus = payload.get("area", {}).get("id") == selection["id"]
            selection["state"] = "available" if selection["id"] in visible or in_focus else "outside-view"
        size = transport_size(payload)
        if size <= budget_chars:
            return payload
        if edges:
            while edges and size > budget_chars - 128:
                size -= len(serialized(edges.pop())) + 1
                removed_edges += 1
        elif nodes:
            while nodes and size > budget_chars - 128:
                size -= len(serialized(nodes.pop())) + 1
                removed_nodes += 1
        else:
            return {"error": {"code": "coverage-limited", "message": "The complete scene metadata/selection does not fit. Increase the budget or read the selection separately."}}


def _map_base(vault, gateway, snapshot, *, scene):
    return {
        "knowledge_revision": gateway.revision,
        "organization_revision": snapshot["organization_revision"],
        "session": _head_state(Path(vault), gateway.revision),
        "projection_state": snapshot["projection_state"],
        "method": snapshot["method"], "scene": scene,
        "coverage": compact_organization_coverage(snapshot["coverage"]), "limitations": snapshot["limitations"],
        "loose": {"count": len(snapshot["loose"]), "expansion": {"area_id": "loose", "organization_revision": snapshot["organization_revision"]}},
    }


def _map_channels(channels):
    from synapse.v2_contracts import V2Error
    result = set(channels if channels is not None else ("overlap", "recorded", "similarity"))
    if result - {"overlap", "recorded", "similarity"}:
        raise V2Error("invalid-request", "Unknown map connection channel")
    return result


def build_v2_map(vault: Path, *, revision=None, organization_revision=None, query="", offset=0, limit=24, selected=None, channels=None, budget_chars=32000):
    """Balanced area overview; volumes remain explicit rather than node area."""
    from synapse.organization import Organization
    from synapse.v2_contracts import V2Error
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 24:
        raise V2Error("invalid-request", "Map overview accepts 1–24 areas")
    gateway = Gateway(Path(vault), revision=revision)
    organization = Organization(gateway)
    snapshot = organization._snapshot_view(organization_revision=organization_revision)
    page = organization.areas(query=query, organization_revision=snapshot["organization_revision"], offset=offset, limit=limit)
    nodes = [_map_area(area, snapshot["organization_revision"]) for area in page["items"]]
    visible, selected_channels = {node["id"] for node in nodes}, _map_channels(channels)
    all_edges = [edge for edge in snapshot["links"] if edge["from"] in visible and edge["to"] in visible and edge["channel"] in selected_channels]
    edges = []
    for edge in all_edges[:80]:
        value = dict(edge)
        if edge["channel"] == "recorded":
            counts = {"accepted": 0, "suggestion": 0, "legacy": 0}
            qualified_ids = []
            for assertion_id in edge.get("assertion_ids", []):
                if assertion_id not in gateway.view.manifest["records"]:
                    counts["legacy"] += 1
                    qualified_ids.append(assertion_id)
                    continue
                unit = gateway._unit(assertion_id, closure_limit=128, supports_qualifications=True)
                if unit.get("withheld") or not unit.get("complete"):
                    continue
                availability = gateway.view.manifest["records"][assertion_id].get("availability", "suggestion")
                counts[availability if availability in counts else "suggestion"] += 1
                qualified_ids.append(assertion_id)
            if not qualified_ids:
                continue
            value["assertion_ids"] = qualified_ids
            value["qualification_summary"] = counts
            value["availability"] = "suggestion" if counts["suggestion"] else "accepted" if counts["accepted"] else "legacy"
            value["review_note"] = "This aggregates recorded links. Inspect each assertion for review, support and owner position; acceptance alone is not verification."
        for name in ("member_ids", "assertion_ids"):
            identifiers = value.get(name, [])
            value[name] = identifiers[:8]
            value[name + "_total"] = len(identifiers)
        value["expansion"] = {"area_id": edge["from"], "organization_revision": snapshot["organization_revision"], "related_area_id": edge["to"]}
        value["interpretation"] = "Derived navigation; shared material, recorded assertions and similarity are different channels. Inspect exact assertions before relying on a connection."
        edges.append(value)
    result = _map_base(vault, gateway, snapshot, scene="overview") | {
        "nodes": nodes, "edges": edges, "selection": _map_selection(snapshot, selected, nodes),
        "page": {"offset": offset, "total": page["total"], "next_offset": page["next_offset"]},
        "limits": {"nodes": 24, "edges": 80},
        "omissions": {"edge_cap": max(0, len(all_edges) - 80), "total_scene_edges": len(all_edges)},
    }
    return _fit_map(result, budget_chars=budget_chars)


def build_v2_area(vault: Path, area_id: str, *, organization_revision: str, revision=None, offset=0, limit=30, selected=None, channels=None, budget_chars=32000):
    """Qualified material scene, including a paged loose-material collection."""
    from synapse.organization import Organization
    from synapse.v2_contracts import V2Error
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_GRAPH_NODES or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise V2Error("invalid-request", "Area page requires a nonnegative offset and 1–151 members")
    gateway = Gateway(Path(vault), revision=revision)
    snapshot = Organization(gateway)._snapshot_view(organization_revision=organization_revision)
    by_id = {item["id"]: item for item in snapshot["members"]}
    if area_id == "loose":
        member_ids = snapshot["loose"]
        area = {"id": "loose", "kind": "area", "label": "Loose material", "summary": {"text": "Material without a sufficiently supported area. Search and read it normally.", "limitations": []}}
    else:
        raw_area = next((item for item in snapshot["areas"] if item["id"] == area_id), None)
        if raw_area is None:
            raise V2Error("ambiguous-identity", "Area is unavailable in this projection")
        member_ids = raw_area["member_ids"]
        area = _map_area(raw_area, snapshot["organization_revision"])
    # One visible mark per canonical source, with one exact initial passage and
    # a full-source expansion. Chunk counts are navigation coverage, not witnesses.
    grouped = {}
    for identity in member_ids:
        member = _map_member(by_id[identity])
        key = member["ref"].get("source_id") if member["kind"] == "source" else member["id"]
        if key in grouped:
            grouped[key]["passage_count"] += 1
        else:
            grouped[key] = copy.deepcopy(member)
            if member["kind"] == "source":
                grouped[key]["passage_count"] = 1
                grouped[key]["expansion"] = {"method": "source", "id": member["ref"]["source_id"], "version": member["ref"]["source_version"], "revision": gateway.revision}
    from synapse.organization import display_title
    candidates = list(grouped.values())
    for node in candidates:
        node["display_label"] = display_title(node["label"])
    # A focused material keeps a stable position in the page ordering. It
    # cannot disappear merely because another passage represented its source.
    candidates.sort(key=lambda node: (0 if selected in {node["id"], node["ref"].get("source_id"), node["ref"].get("record_id")} else 1,
                                      node["kind"] != "record",
                                      bool(node["kind"] == "source" and (node["label"].endswith((".json", ".jsonl", ".yaml", ".yml")) or "/review/" in node["label"] or "/integration-review/" in node["label"])),
                                      node["display_label"].casefold(), node["id"]))
    nodes = candidates[offset:offset + limit]
    visible = {node["id"] for node in nodes}
    aliases = {identity: grouped[by_id[identity]["ref"].get("source_id") if by_id[identity]["kind"] == "source" else identity]["id"] for identity in member_ids}
    selected_channels, edges, seen = _map_channels(channels), [], set()
    for raw_edge in snapshot.get("member_links", []):
        left, right = aliases.get(raw_edge["from"]), aliases.get(raw_edge["to"])
        if left not in visible or right not in visible or left == right or raw_edge["channel"] not in selected_channels:
            continue
        edge = dict(raw_edge, **{"from": left, "to": right})
        display_key = (tuple(sorted((left, right))), edge["channel"]) if edge["channel"] == "similarity" else edge["id"]
        if display_key in seen:
            continue
        seen.add(display_key)
        if edge["channel"] == "recorded" and edge.get("assertion_id"):
            unit = gateway._unit(edge["assertion_id"], closure_limit=128, supports_qualifications=True)
            if unit.get("withheld") or not unit.get("complete"):
                continue
            edge["qualification"] = unit
            assertion = gateway._node(edge["assertion_id"])
            for key in ("availability", "owner_review", "owner_position", "lifecycle", "record_kind", "support", "as_of", "epistemic_basis"):
                if key in assertion:
                    edge[key] = assertion[key]
            edge["expansion"] = {"method": "context", "ids": [edge["assertion_id"]], "revision": gateway.revision}
        edges.append(edge)
    result = _map_base(vault, gateway, snapshot, scene="area") | {
        "area": area, "nodes": nodes, "edges": edges[:MAX_GRAPH_EDGES],
        "selection": _map_selection(snapshot, selected, nodes),
        "page": {"offset": offset, "total": len(candidates), "next_offset": None},
        "limits": {"nodes": MAX_GRAPH_NODES, "edges": MAX_GRAPH_EDGES},
        "omissions": {"edge_cap": max(0, len(edges) - MAX_GRAPH_EDGES), "total_scene_edges": len(edges)},
    }
    return _fit_map(result, budget_chars=budget_chars)
