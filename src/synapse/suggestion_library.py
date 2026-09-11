"""A useful suggestion library, not an automatically growing review queue."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from synapse.gateway import Gateway, budget_response
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes


def library(vault: Path, *, revision=None, subject_id=None, facet=None, include_parked=False, offset=0, limit=20, budget_chars=8000, representation="json") -> dict:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or not 1 <= limit <= 100:
        raise V2Error("invalid-request", "Invalid suggestion library page")
    gateway = Gateway(vault, revision=revision)
    groups = defaultdict(list)
    dispositions = Counter()
    page_offset = 0
    while True:
        page = gateway.view.candidates(subject_id=subject_id, facet=facet, availability="suggestion", offset=page_offset, limit=200, include_excluded=include_parked)
        for item in page["items"]:
            record = item["record"]
            disposition = record["owner_review"]["disposition"]
            dispositions[disposition] += 1
            if not include_parked and (disposition in {"dismissed", "disputed", "deferred"} or record["lifecycle"] != "current"):
                continue
            # Group exact declared claim scope only. Similar words, source
            # families or embedding distance never establish equivalent truth.
            key = (record["subject_id"], record["claim_key"], record.get("applies_from"), record.get("applies_until"))
            groups[key].append(record)
        if page["next_offset"] is None:
            break
        page_offset = page["next_offset"]
    items = []
    for key in sorted(groups, key=lambda value: str(value)):
        variants = groups[key]
        semantic = defaultdict(list)
        for value in variants:
            fields = {name: value.get(name) for name in ("subject_id", "claim_key", "record_kind", "statement", "conditions_and_limits", "support", "counterevidence", "alternatives", "would_change_with", "evidence", "dependencies", "context_refs", "relationship", "applies_from", "applies_until")}
            semantic[hash_bytes(canonical_json(fields))].append({"id": value["id"], "version": value["version"]})
        items.append({"subject_id": key[0], "claim_key": key[1], "applies_from": key[2], "applies_until": key[3], "variants": [{"id": value["id"], "version": value["version"], "statement": value["statement"], "conditions_and_limits": value["conditions_and_limits"], "owner_review": value["owner_review"], "availability": value["availability"]} for value in variants], "exact_duplicate_sets": [refs for refs in semantic.values() if len(refs) > 1], "grouping": "Same declared subject, claim key and applicability; variants remain distinct assertions."})
    result = {"knowledge_revision": gateway.revision, "items": items[offset:offset + limit], "total_groups": len(items), "dispositions": dict(dispositions), "next_offset": offset + limit if offset + limit < len(items) else None, "review_required": False, "guidance": "Unreviewed suggestions can remain available indefinitely. Review selected changes when a current question makes adoption useful; keeping a possibility or declining adoption does not dismiss it."}
    fitted = budget_response(result, budget_chars=budget_chars, representation=representation)
    if "items" in fitted and len(fitted["items"]) < len(result["items"]):
        fitted["next_offset"] = offset + len(fitted["items"])
    return fitted


def overview(vault: Path, *, revision=None) -> dict:
    gateway = Gateway(vault, revision=revision)
    states, types, facets = Counter(), Counter(), Counter()
    for row in gateway.view.manifest["records"].values():
        if not row["active"]:
            continue
        states[row["availability"]] += 1
        types[row["type"]] += 1
        if row["profile"] == "knowledge-v2":
            record = gateway.view.records(ids=[row["id"]])[0]
            facets.update(set(record["facets"]))
    sources = [gateway.view.manifest["source_versions"][version] for version in gateway.view.manifest["sources"].values()]
    return {"knowledge_revision": gateway.revision, "availability": dict(states), "entity_types": dict(types), "facets": dict(facets), "sources": {"total": len(sources), "text_complete": sum(source["extraction"]["completeness"] == "complete" for source in sources), "text_partial": sum(source["extraction"]["completeness"] == "partial" for source in sources), "text_unavailable": sum(not source.get("text_version") for source in sources)}, "meaning": "Coverage counts describe retained information and processing, not how complete our understanding of your life is.", "review_required": False}
