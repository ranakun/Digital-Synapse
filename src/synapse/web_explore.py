"""Adaptive, bounded viewer scenes over the existing qualified organization.

Groups and bundles are navigation containers, never new knowledge or evidence.
Every leaf keeps its retained identity, revision and complete qualification.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import networkx as nx

from synapse.gateway import Gateway, serialized, transport_size
from synapse.organization import Organization, _area_label, display_title
from synapse.v2_contracts import V2Error, hash_bytes
from synapse.web_v2 import _map_area, _map_base, _map_member

LEAF_SIZE = 48
GROUP_PAGE = 8
SCENE_BUDGET = 256000
EDGE_LIMIT = 160


def _id(node):
    return (
        node.get("ref", {}).get("source_id") or node.get("ref", {}).get("record_id") or node["id"]
    )


def _context(vault, revision, organization_revision):
    gateway = Gateway(Path(vault), revision=revision)
    snapshot = Organization(gateway)._snapshot_view(organization_revision=organization_revision)
    nodes, aliases = {}, {}
    for member in snapshot["members"]:
        identity = _id(member)
        aliases[member["id"]] = identity
        if identity not in nodes:
            node = _map_member(member)
            node["id"] = identity
            node["display_label"] = display_title(member["label"])
            node["type"] = gateway.view.manifest["records"].get(identity, {}).get("type", "source")
            node["entity_type"] = node["type"]
            nodes[identity] = node
    edges, seen = [], set()
    for raw in snapshot["member_links"]:
        if raw["channel"] != "similarity":
            continue
        left, right = aliases.get(raw["from"]), aliases.get(raw["to"])
        if not left or not right or left == right:
            continue
        key = (
            (tuple(sorted((left, right))), raw["channel"])
            if raw["channel"] == "similarity"
            else raw["id"]
        )
        if key in seen:
            continue
        seen.add(key)
        edges.append(dict(raw, **{"from": left, "to": right}))
    # The organization texture intentionally contains only within-area links.
    # Browsing affiliations and Threads needs the full retained relationship set,
    # filtered to eligible endpoints and qualified before it influences grouping.
    recorded = []
    for raw in gateway.view.relationships(include_suggestions=True):
        if (
            raw["from_id"] not in nodes
            or raw["to_id"] not in nodes
            or raw["from_id"] == raw["to_id"]
        ):
            continue
        edge = dict(
            raw,
            **{"from": raw["from_id"], "to": raw["to_id"], "channel": "recorded", "metadata": raw},
        )
        if raw.get("record_id"):
            edge["assertion_id"] = raw["record_id"]
        else:
            edge["legacy_edge_id"] = raw["id"]
            edge["qualification"] = {
                "state": "legacy-edge",
                "limitations": ["Legacy relationship: passage-level provenance may be incomplete."],
            }
        recorded.append(edge)
    edges.extend(_qualified_edges(gateway, recorded, set(nodes)))
    return gateway, snapshot, nodes, aliases, edges


def _qualified_edges(gateway, edges, visible):
    result = []
    for edge in edges:
        if edge["from"] not in visible or edge["to"] not in visible:
            continue
        edge = dict(edge)
        assertion = edge.get("assertion_id")
        if assertion:
            unit = gateway._unit(assertion, closure_limit=128, supports_qualifications=True)
            if unit.get("withheld") or not unit.get("complete"):
                continue
            edge["qualification"] = unit
            row = gateway._node(assertion)
            for key in (
                "availability",
                "owner_review",
                "owner_position",
                "lifecycle",
                "record_kind",
                "support",
                "as_of",
                "epistemic_basis",
            ):
                if key in row:
                    edge[key] = row[key]
            edge["expansion"] = {
                "method": "context",
                "ids": [assertion],
                "revision": gateway.revision,
            }
        result.append(edge)
    return result


def _graph(identities, edges):
    graph = nx.Graph()
    graph.add_nodes_from(sorted(identities))
    for edge in edges:
        a, b = edge["from"], edge["to"]
        if a in identities and b in identities:
            graph.add_edge(
                a,
                b,
                weight=max(
                    graph.get_edge_data(a, b, {}).get("weight", 0),
                    3 if edge["channel"] == "recorded" else 1,
                ),
            )
    return graph


def _groups(identities, nodes, edges):
    """Use explicit types/affiliations before derived topic neighborhoods."""
    names = {
        "person": "People",
        "company": "Organizations",
        "conversation": "Conversations",
        "source": "Sources",
        "insight": "Ideas & notes",
        "opportunity": "Opportunities",
    }
    types = defaultdict(set)
    for identity in sorted(identities):
        types[nodes[identity]["type"]].add(identity)
    groups = []
    anchors = {}
    if len(types) > 1:
        groups = [
            (names.get(t, t.replace("_", " ").capitalize() + "s"), ids, "Existing material type")
            for t, ids in types.items()
        ]
    elif next(iter(types), None) in {"person", "conversation"}:
        relation_groups = defaultdict(set)
        for edge in edges:
            meta = edge.get("metadata", {})
            relation = meta.get("relation") or edge.get("relation")
            a, b = edge["from"], edge["to"]
            if (
                a in identities
                and nodes[b]["type"] == "company"
                and relation in {"works_at", "former_employee_of", "founded", "recruits_for"}
            ):
                relation_groups[b].add(a)
            if b in identities and nodes[a]["type"] == "person" and relation == "participated_in":
                relation_groups[a].add(b)
        covered = set()
        for target, ids in relation_groups.items():
            if len(ids) >= 2 and len(ids) < len(identities):
                groups.append(
                    (
                        display_title(nodes[target]["label"]),
                        ids,
                        "Recorded affiliations; may include past links"
                        if nodes[target]["type"] == "company"
                        else "Recorded participant",
                    )
                )
                anchors[(display_title(nodes[target]["label"]), tuple(sorted(ids)))] = target
                covered.update(ids)
        remaining = identities - covered
        if groups and remaining:
            groups.append(
                (
                    "Other " + names.get(next(iter(types)), "material").lower(),
                    remaining,
                    "No shared affiliation group; search or browse normally",
                )
            )
    elif next(iter(types), None) not in {"company", "source"}:
        graph = _graph(identities, edges)
        connected = {n for n, degree in graph.degree() if degree}
        partitions = (
            nx.community.louvain_communities(
                graph.subgraph(sorted(connected)), weight="weight", resolution=1, seed=19
            )
            if connected
            else []
        )
        loose = set(identities) - connected
        for part in partitions:
            if len(part) < 3:
                loose.update(part)
            else:
                ordered = sorted(part, key=lambda n: (-graph.degree(n), n))
                # Titles name a navigation neighborhood, never a new assertion.
                name = _area_label([nodes[n] for n in ordered[:24]])
                groups.append((name, set(part), "Grouped from retained connections and similarity"))
        if groups and loose:
            groups.append(("Other material", loose, "Material without a larger connected group"))
    if len(groups) < 2:
        return []
    result = []
    for label, members, description in groups:
        identity = "group:" + hash_bytes(serialized([label, sorted(members)]).encode())[:24]
        result.append(
            {
                "id": identity,
                "kind": "group",
                "label": label,
                "count": len(members),
                "members": members,
                "description": description,
                "anchor_id": anchors.get((label, tuple(sorted(members)))),
            }
        )
    return sorted(
        result,
        key=lambda group: (
            group["label"].startswith("Other "),
            -group["count"],
            group["label"],
            group["id"],
        ),
    )


def _leaf_order(identities, nodes, edges, selected=None):
    graph = _graph(identities, edges)
    # Navigation priority is explicit connectivity, not inferred importance.
    return sorted(
        identities,
        key=lambda n: (
            n != selected,
            -graph.degree(n),
            nodes[n]["kind"] == "source",
            nodes[n]["label"].casefold(),
            n,
        ),
    )


def _fit(payload):
    """Drop whole units only, and advance by the material actually returned."""
    payload = json.loads(serialized(payload))
    payload["omissions"] = {"edges": max(0, len(payload["edges"]) - EDGE_LIMIT), "nodes": 0}
    payload["edges"] = sorted(
        payload["edges"], key=lambda e: (e["channel"] != "recorded", e["id"])
    )[:EDGE_LIMIT]
    while transport_size(payload) > SCENE_BUDGET - 256:
        if payload["edges"]:
            payload["edges"].pop()
            payload["omissions"]["edges"] += 1
        elif payload.get("clouds") and any(c["nodes"] for c in payload["clouds"]):
            c = max(payload["clouds"], key=lambda c: len(c["nodes"]))
            c["nodes"].pop()
            c["shown_material"] = len(c["nodes"])
            ids = {n["id"] for n in c["nodes"]}
            c["edges"] = [e for e in c["edges"] if e["from"] in ids and e["to"] in ids]
            payload["omissions"]["nodes"] += 1
        elif len(payload["nodes"]) > (1 if (payload.get("focus") or payload.get("anchor")) else 0):
            payload["nodes"].pop()
            payload["omissions"]["nodes"] += 1
        else:
            raise V2Error(
                "coverage-limited",
                "The complete selection exceeds the scene budget. Read it directly.",
            )
    page = payload["page"]
    count = len(payload["nodes"]) - (1 if (payload.get("focus") or payload.get("anchor")) else 0)
    page["next_offset"] = page["offset"] + count if page["offset"] + count < page["total"] else None
    if page["next_offset"] == page["offset"]:
        raise V2Error(
            "coverage-limited", "A complete item exceeds the scene budget. Refine this collection."
        )
    payload["truncated"] = bool(
        page["next_offset"] is not None or any(payload["omissions"].values())
    )
    payload["budget"] = {"limit": SCENE_BUDGET, "unit": "characters"}
    return payload


def _validate(offset, query):
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise V2Error("invalid-request", "Offset must be nonnegative")
    if not isinstance(query, str) or len(query) > 300:
        raise V2Error("invalid-request", "Search must be at most 300 characters")


def _matches(identities, nodes, edges, query):
    terms = query.casefold().split()
    if not terms:
        return identities
    names = {n: nodes[n]["label"] + " " + nodes[n]["type"] for n in identities}
    for edge in edges:
        if edge["channel"] != "recorded":
            continue
        for a, b in ((edge["from"], edge["to"]), (edge["to"], edge["from"])):
            if a in names:
                names[a] += " " + nodes[b]["label"]
    return {n for n in identities if all(t in names[n].casefold() for t in terms)}


def build_v2_browse(
    vault,
    area_id,
    *,
    organization_revision,
    revision=None,
    group_path="",
    query="",
    offset=0,
    selected=None,
):
    _validate(offset, query)
    if not isinstance(group_path, str) or len(group_path.split("/")) > 5:
        raise V2Error("invalid-request", "Group path is too deep")
    gateway, snapshot, nodes, aliases, edges = _context(vault, revision, organization_revision)
    if area_id == "loose":
        raw_ids = snapshot["loose"]
        area = {"id": "loose", "kind": "area", "label": "Ungrouped material"}
    else:
        raw = next((a for a in snapshot["areas"] if a["id"] == area_id), None)
        if raw is None:
            raise V2Error("revision-unavailable", "This area is unavailable in the pinned map")
        raw_ids = raw["member_ids"]
        area = _map_area(raw, snapshot["organization_revision"])
    identities = {aliases[n] for n in raw_ids}
    trail = [{"label": area["label"], "path": "", "count": len(identities)}]
    anchor = None
    parts = [p for p in group_path.split("/") if p]
    for index, part in enumerate(parts):
        groups = _groups(identities, nodes, edges)
        group = next((g for g in groups if g["id"] == part), None)
        if group is None:
            raise V2Error("revision-unavailable", "This group is unavailable; reopen the area")
        identities = group["members"]
        anchor = nodes.get(group.get("anchor_id"))
        trail.append(
            {
                "label": group["label"],
                "path": "/".join(parts[: index + 1]),
                "count": len(identities),
            }
        )
    identities = _matches(identities, nodes, edges, query)
    groups = (
        _groups(identities, nodes, edges)
        if len(identities) > LEAF_SIZE and not query.strip() and len(parts) < 5 and anchor is None
        else []
    )
    payload = _map_base(vault, gateway, snapshot, scene="browse") | {
        "area": area,
        "trail": trail,
        "group_path": group_path,
        "query": query,
        "total_material": len(identities),
        "anchor": anchor,
        "mode": "groups"
        if groups
        else "collection"
        if len(identities) > LEAF_SIZE and anchor is None
        else "map",
        "nodes": [],
        "edges": [],
        "clouds": [],
    }
    if groups:
        shown = groups[offset : offset + GROUP_PAGE]
        payload["nodes"] = [{k: v for k, v in g.items() if k != "members"} for g in shown]
        for g in shown:
            ids = _leaf_order(g["members"], nodes, edges)[:8]
            links = _qualified_edges(gateway, edges, set(ids))
            payload["clouds"].append(
                {
                    "area_id": g["id"],
                    "nodes": [nodes[n] for n in ids],
                    "edges": links[:16],
                    "total_material": g["count"],
                    "shown_material": len(ids),
                }
            )
        total, size = len(groups), GROUP_PAGE
    else:
        ordered = _leaf_order(identities, nodes, edges, selected)
        ids = ordered[offset : offset + LEAF_SIZE]
        payload["nodes"] = ([anchor] if anchor else []) + [nodes[n] for n in ids]
        payload["edges"] = _qualified_edges(
            gateway, edges, {*ids, *([anchor["id"]] if anchor else [])}
        )
        total, size = len(ordered), LEAF_SIZE
    payload["page"] = {
        "offset": offset,
        "total": total,
        "size": size,
        "unit": "groups" if groups else "items",
    }
    return _fit(payload)


def build_v2_threads(
    vault, focus_id, *, organization_revision, revision=None, bundle="", query="", offset=0
):
    _validate(offset, query)
    gateway, snapshot, nodes, aliases, all_edges = _context(vault, revision, organization_revision)
    focus_id = aliases.get(focus_id, focus_id)
    if focus_id not in nodes:
        raise V2Error("record-unavailable", "This item is not eligible in the pinned map")
    focus = nodes[focus_id]
    withheld = 0
    edges = [
        e
        for e in all_edges
        if focus_id in (e["from"], e["to"])
        and (focus["kind"] == "source" or e["channel"] == "recorded")
    ]
    if focus["kind"] == "record":
        withheld = max(
            0,
            len(gateway.view.relationships(identity=focus_id, include_suggestions=True))
            - len(edges),
        )
    bundles = defaultdict(set)
    for e in edges:
        other = e["to"] if e["from"] == focus_id else e["from"]
        relation = e.get("relation") or e.get("metadata", {}).get("relation") or "similar material"
        direction = "out" if e["from"] == focus_id else "in"
        key = relation + ":" + direction
        e["bundle_id"] = key
        bundles[key].add(other)
    all_peers = set().union(*bundles.values()) if bundles else set()
    if bundle and bundle not in bundles:
        raise V2Error("revision-unavailable", "This connection bundle is unavailable")
    peers = set(bundles[bundle] if bundle else all_peers)
    if query.strip():
        peers = {
            n
            for n in peers
            if all(
                t in (nodes[n]["label"] + " " + nodes[n]["type"]).casefold()
                for t in query.casefold().split()
            )
        }
    grouped = len(peers) > 20 and not bundle and not query.strip()
    payload = _map_base(vault, gateway, snapshot, scene="threads") | {
        "focus": focus,
        "bundle": bundle,
        "query": query,
        "total_neighbors": len(all_peers),
        "withheld_edges": withheld,
        "mode": "bundles" if grouped else "map",
        "nodes": [focus],
        "edges": [],
        "clouds": [],
    }
    if grouped:
        ordered = sorted(bundles, key=lambda k: (-len(bundles[k]), k))
        for key in ordered[offset : offset + GROUP_PAGE]:
            relation, direction = key.rsplit(":", 1)
            types = Counter(nodes[n]["type"] for n in bundles[key])
            noun = next(iter(types)) if len(types) == 1 else "item"
            plural = {"person": "people", "company": "organizations"}.get(noun, noun + "s")
            payload["nodes"].append(
                {
                    "id": key,
                    "kind": "bundle",
                    "label": relation.replace("_", " ").capitalize(),
                    "count": len(bundles[key]),
                    "description": f"{'From' if direction == 'in' else 'To'} {len(bundles[key])} {noun if len(bundles[key]) == 1 else plural}",
                    "direction": direction,
                }
            )
        total, size = len(ordered), GROUP_PAGE
    else:
        ordered = _leaf_order(peers, nodes, edges)
        ids = ordered[offset : offset + 20]
        visible = {focus_id, *ids}
        payload["nodes"].extend(nodes[n] for n in ids)
        payload["edges"] = [
            e
            for e in edges
            if e["from"] in visible
            and e["to"] in visible
            and (not bundle or e["bundle_id"] == bundle)
        ]
        total, size = len(ordered), 20
    payload["page"] = {
        "offset": offset,
        "total": total,
        "size": size,
        "unit": "bundles" if grouped else "connected items",
    }
    return _fit(payload)
