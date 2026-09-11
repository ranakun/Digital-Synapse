"""Bounded constellation scenes, derived from a pinned organization snapshot.

Every dot is an eligible record or one canonical source. Layout membership is
navigation, never an assertion. No new store or model call is introduced here.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from pathlib import Path

from synapse.gateway import Gateway, serialized, transport_size
from synapse.organization import Organization, display_title
from synapse.v2_contracts import V2Error
from synapse.web_v2 import _map_area, _map_base, _map_channels, _map_member, _map_selection

MAX_AREAS = 6
MAX_DOTS_PER_AREA = 48
MAX_SCENE_CHARACTERS = 256000


def _canonical(member):
    return member["ref"].get("source_id") or member["ref"].get("record_id") or member["id"]


def _ordered_areas(snapshot):
    """Diversify the first page; import volume cannot fill all six places."""
    pending = list(snapshot["areas"])
    chosen, labels = [], Counter()
    previous_members = set()
    while pending:

        def score(area):
            coverage = area["coverage"]
            volume = coverage.get("unique_records", 0) + coverage.get("unique_sources", 0)
            terms = set(area["label"].casefold().split(" & "))
            repeated = sum(labels[term] for term in terms)
            overlap = len(previous_members.intersection(area["member_ids"])) / max(
                1, len(area["member_ids"])
            )
            # Log scale preserves small subjects; repetition/overlap is an
            # ordering penalty only. Every area remains in this pageable view.
            return (math.log2(1 + volume) - repeated * 1.5 - overlap, area["id"])

        area = max(pending, key=score)
        pending.remove(area)
        chosen.append(area)
        labels.update(area["label"].casefold().split(" & "))
        previous_members.update(area["member_ids"])
    return chosen


def _cloud(area, members, links):
    grouped = {}
    aliases = {}
    for identity in area["member_ids"]:
        member = members[identity]
        key = _canonical(member)
        if key not in grouped:
            value = _map_member(member)
            value["display_label"] = display_title(value["label"])
            value["passage_count"] = 0
            grouped[key] = value
        grouped[key]["passage_count"] += int(member["kind"] == "source")
        aliases[identity] = grouped[key]["id"]
    candidates = {m["id"]: m for m in grouped.values()}
    adjacent = defaultdict(set)
    for edge in links:
        left, right = aliases.get(edge["from"]), aliases.get(edge["to"])
        if left and right and left != right:
            adjacent[left].add(right)
            adjacent[right].add(left)
    # Start from connected material, then grow real neighborhoods. Alternate
    # source and record seeds so a large import does not hide either kind.
    selected = []

    def rank(identity):
        node = candidates[identity]
        return (
            -min(12, len(adjacent[identity])),
            node["display_label"].casefold(),
            identity,
        )

    by_kind = {
        kind: sorted(
            (identity for identity in candidates if candidates[identity]["kind"] == kind),
            key=rank,
        )
        for kind in ("record", "source")
    }
    seeds = [
        identity
        for offset in range(max((len(values) for values in by_kind.values()), default=0))
        for kind in ("record", "source")
        if offset < len(by_kind[kind])
        for identity in [by_kind[kind][offset]]
    ]
    for seed in seeds:
        if len(selected) >= MAX_DOTS_PER_AREA:
            break
        if seed not in selected:
            selected.append(seed)
        for neighbor in sorted(adjacent[seed], key=rank)[:3]:
            if len(selected) < MAX_DOTS_PER_AREA and neighbor not in selected:
                selected.append(neighbor)
    selected = selected[:MAX_DOTS_PER_AREA]
    visible = set(selected)
    edges, seen = [], set()
    for edge in links:
        left, right = aliases.get(edge["from"]), aliases.get(edge["to"])
        if left not in visible or right not in visible or left == right:
            continue
        # Overview texture is explicitly similarity, not a shortcut around
        # complete qualification of recorded assertions. Those open in focus.
        if edge["channel"] != "similarity":
            continue
        pair = tuple(sorted((left, right)))
        if pair not in seen:
            seen.add(pair)
            edges.append(dict(edge, **{"from": left, "to": right}))
        if len(edges) == 60:
            break
    return {
        "area_id": area["id"],
        "nodes": [candidates[x] for x in selected],
        "edges": edges,
        "total_material": len(candidates),
        "shown_material": len(selected),
    }


def build_v2_landscape(
    vault: Path,
    *,
    revision=None,
    organization_revision=None,
    offset=0,
    limit=MAX_AREAS,
    selected=None,
    channels=None,
):
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise V2Error("invalid-request", "Landscape offset must be nonnegative")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_AREAS:
        raise V2Error("invalid-request", "Landscape accepts 1–6 areas")
    gateway = Gateway(Path(vault), revision=revision)
    snapshot = Organization(gateway, config={"community_resolution": 0.2})._snapshot_view(
        organization_revision=organization_revision
    )
    ordered = _ordered_areas(snapshot)
    areas = ordered[offset : offset + limit]
    visible = {area["id"] for area in areas}
    members = {m["id"]: m for m in snapshot["members"]}
    channels = _map_channels(channels)
    clouds = [_cloud(area, members, snapshot["member_links"]) for area in areas]
    # Cross-area lines represent overlap or similarity. Recorded connections
    # remain individually inspectable in area/Threads; no aggregate truth badge.
    edges = []
    for edge in sorted(snapshot["links"], key=lambda e: (-e.get("count", 0), e["id"])):
        if (
            edge["from"] in visible
            and edge["to"] in visible
            and edge["channel"] in channels & {"overlap", "similarity"}
        ):
            if any({x["from"], x["to"]} == {edge["from"], edge["to"]} for x in edges):
                continue
            edges.append(
                {k: edge[k] for k in ("id", "from", "to", "channel", "count", "explanation")}
            )
            edges[-1]["member_ids"] = edge.get("member_ids", [])[:8]
            edges[-1]["member_ids_total"] = len(edge.get("member_ids", []))
            edges[-1]["expansion"] = {
                "area_id": edge["from"],
                "organization_revision": snapshot["organization_revision"],
            }
        if len(edges) >= 8:
            break
    payload = _map_base(vault, gateway, snapshot, scene="landscape") | {
        "nodes": [_map_area(area, snapshot["organization_revision"]) for area in areas],
        "clouds": clouds,
        "edges": edges,
        "selection": _map_selection(
            snapshot,
            selected,
            [
                _map_member(m)
                for m in members.values()
                if selected and selected in {m["id"], _canonical(m)}
            ],
        ),
        "page": {
            "offset": offset,
            "total": len(ordered),
            "next_offset": offset + len(areas) if offset + len(areas) < len(ordered) else None,
        },
        "directory": [
            {"id": a["id"], "label": a["label"], "coverage": a["coverage"]} for a in ordered[:256]
        ],
        "directory_total": len(ordered),
        "directory_truncated": len(ordered) > 256,
        "limits": {
            "areas": MAX_AREAS,
            "dots_per_area": MAX_DOTS_PER_AREA,
            "characters": MAX_SCENE_CHARACTERS,
        },
        "meaning": "Dots are real sampled material. Areas can overlap; positions and similarity are navigation, not evidence.",
    }
    # Round-robin whole-unit removal keeps sample footprints balanced while
    # retaining every qualification on every remaining dot.
    while transport_size(payload) > MAX_SCENE_CHARACTERS - 32 and any(c["nodes"] for c in clouds):
        cloud = max(clouds, key=lambda c: sum(len(serialized(n)) for n in c["nodes"]))
        cloud["nodes"].pop()
        ids = {n["id"] for n in cloud["nodes"]}
        cloud["edges"] = [e for e in cloud["edges"] if e["from"] in ids and e["to"] in ids]
        cloud["shown_material"] = len(cloud["nodes"])
    if transport_size(payload) > MAX_SCENE_CHARACTERS:
        raise V2Error(
            "coverage-limited",
            "Landscape metadata exceeds the bounded scene; inspect areas through the read gateway",
        )
    payload["truncated"] = (
        any(c["shown_material"] < c["total_material"] for c in clouds)
        or payload["page"]["next_offset"] is not None
    )
    return payload
