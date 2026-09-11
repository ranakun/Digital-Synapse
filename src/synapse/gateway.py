"""Evidence-preserving read composition over one retained knowledge revision.

The specialist chooses its queries. This gateway makes each chosen read
repeatable and keeps corrections, dependencies and transport limits intact.
"""

from __future__ import annotations

import copy
import json
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from synapse.read_view import ReadView
from synapse.source_store import evidence_ref, read_passage
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes

CORRECTIONS = {"qualifies", "revises", "contradicts"}
UNAVAILABLE = {"dismissed", "disputed"}


def serialized(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def transport(value: Any, representation: str = "json") -> dict | str:
    text = serialized(value)
    if representation == "json":
        return text
    if representation == "mcp":
        # One representation, not the same context in text and structuredContent.
        return {"content": [{"type": "text", "text": text}], "isError": False}
    raise V2Error("invalid-request", "Supported representations are json and mcp")


def transport_size(value: Any, representation: str = "json") -> int:
    result = transport(value, representation)
    return len(result if isinstance(result, str) else serialized(result))


def budget_response(value: dict, *, budget_chars: int = 8000, representation: str = "json", units_key: str = "items") -> dict:
    """Fit whole units including wrapper/metadata; never trim assertion text."""
    if isinstance(budget_chars, bool) or not isinstance(budget_chars, int) or not 256 <= budget_chars <= 32000:
        raise V2Error("invalid-request", "Response budget must be 256–32000 characters")
    output = copy.deepcopy(value)
    units = output.setdefault(units_key, [])
    removed = 0
    while True:
        if removed and units_key == "items" and "offset" in output:
            output["next_offset"] = output["offset"] + len(units)
        output["budget"] = {"unit": "characters", "limit": budget_chars, "representation": representation, "truncated": bool(removed), "omitted_units": removed}
        if transport_size(output, representation) <= budget_chars:
            return output
        if units:
            units.pop()
            removed += 1
            continue
        fallback = {"error": "insufficient-budget", "knowledge_revision": value.get("knowledge_revision"), "expansion": "Repeat with a larger response budget; no absence conclusion is supported."}
        if transport_size(fallback, representation) > budget_chars:
            fallback = {"error": "insufficient-budget", "minimum_characters": transport_size(fallback, representation)}
        return fallback


def compact_area(area: dict, organization_revision: str) -> dict:
    """Keep discovery metadata bounded; full memberships have their own page."""
    result = copy.deepcopy({key: value for key, value in area.items() if key not in {"member_ids", "summary"}})
    summary = area.get("summary", {})
    member_ids = area.get("member_ids", [])
    result["summary"] = {key: value for key, value in summary.items() if key != "member_ids"}
    result["summary"]["member_ids"] = list(member_ids[:3])
    result["summary"]["member_ids_total"] = len(member_ids)
    result["expansion"] = {"method": "area", "area_id": area["id"], "organization_revision": organization_revision}
    return result


def compact_organization_coverage(value: dict) -> dict:
    return {key: item for key, item in value.items() if isinstance(item, (int, str, bool)) or item is None}


def workspace_binding(vault: Path) -> dict:
    """Identify the local connection target, independent of its current revision.

    Copies/restores at another location intentionally have another binding.
    This is routing identity, not a credential or a portable knowledge identity.
    """
    path = str(Path(vault).expanduser().resolve())
    return {"id": hash_bytes(("synapse-workspace/1:" + path).encode()), "path": path}


class Gateway:
    def __init__(self, vault: Path, *, revision: str | None = None, known_at: str | None = None, timezone: str | None = None):
        self.view = ReadView(vault, revision=revision, known_at=known_at, timezone=timezone)
        self.revision = self.view.revision
        self._cache: dict[str, dict] = {}

    def _base(self) -> dict:
        return {"knowledge_revision": self.revision, "index_state": "current", "semantic_search": "unused", "limitations": []}

    def describe(self) -> dict:
        from synapse.semantic_capability import semantic_capability
        from synapse.v2_protocol import operation_contracts
        manifest = self.view.manifest
        return self._base() | {
            "protocol_version": "synapse-v2/1",
            "workspace": workspace_binding(self.view.store.vault),
            "semantic_capability": semantic_capability(self.view),
            "source_policy": self.view.source_purpose_policy().metadata(),
            "argument_contracts": operation_contracts(),
            "modes": ["consult", "investigate", "capture", "review", "continue"],
            "counts": {"records": len(manifest["records"]), "sources": len(manifest["sources"]), "retained_source_versions": len(manifest["source_versions"])},
            "methods": {
                "catalog": "Enumerate a scoped collection before paging. kind=records or sources; filter by subject_id, facet, query or availability.",
                "context": "Search or pass exact ids; returns whole records with mandatory incoming corrections and material premises. Try synonyms or narrower scope when useful.",
                "record": "Read complete retained Markdown in pages; use the same revision and next_offset.",
                "source": "Read exact retained source text; enumerate sources even when no claims have been extracted.",
                "search_sources": "Search retained text before reading more; returns exact evidence spans and page offsets, including unprocessed material.",
                "passage": "Expand a pinned UTF-8 evidence span with its surrounding context.",
                "neighbors": "Page a qualified local neighborhood; filter relation/direction/type, inspect relation_counts and follow next_offset. A connection is not endorsement.",
                "path": "Find a bounded path through accepted relationships by default.",
                "semantic": "Semantic candidates when explicitly warmed and indexed; returns correction-aware context with honest lexical fallback. Arguments: query, subject_id, facet, knowledge_policy, limit.",
                "overview": "Inspect retained coverage, source processing and domains before choosing an inquiry.",
                "suggestions": "Browse useful scoped possibilities; include_parked=true inspects prior dispositions, not an overdue review queue.",
                "areas": "Discover derived overlapping areas and coverage; organization is optional routing, not evidence or a fixed taxonomy.",
                "area": "Page qualified record/source members using the returned organization_revision and area_id.",
                "leads": "Find useful unreviewed questions from prior requested work or explicit saves; no investigation starts automatically.",
            },
            "time": {"valid_at": "Inclusive applicability date, not formulation date.", "known_at": "Committed history only, beginning at baseline migration."},
            "bounds": {"root_candidates": 20, "closure_nodes": 128, "default_characters": 8000, "max_characters": 32000, "minimum_characters": 256, "source_page_characters": 4000, "neighbors": 150, "edges": 500},
            "authority": "Consultation cannot capture, start investigations or approve changes. The trusted host delegates a requested run; accepted changes require its exact displayed selection.",
        }

    def catalog(self, *, budget_chars=8000, representation="json", **query) -> dict:
        result = self.view.catalog(**query)
        fitted = budget_response(result, budget_chars=budget_chars, representation=representation)
        if "items" in fitted and len(fitted["items"]) < len(result["items"]):
            fitted["next_offset"] = query.get("offset", 0) + len(fitted["items"])
        return fitted

    def _unit(self, identity: str, *, closure_limit: int, supports_qualifications: bool) -> dict:
        manifest = self.view.manifest
        if identity not in manifest["records"]:
            raise V2Error("ambiguous-identity", "Record identity does not resolve in this revision", details={"id": identity})
        pending = deque([identity])
        included: dict[str, dict] = {}
        notices, provisional = [], []
        invalid_premise = False
        correction_present = False
        source_changed = False
        while pending:
            current = pending.popleft()
            if current in included:
                continue
            if len(included) >= closure_limit:
                return {"root_id": identity, "complete": False, "records": [], "reason": "Mandatory correction/premise closure exceeds this response group.", "expansion": {"method": "context", "ids": [identity], "revision": self.revision, "closure_limit": 128}}
            row = manifest["records"].get(current)
            if row is None:
                notices.append({"id": current, "reason": "Referenced identity unavailable."})
                invalid_premise = True
                continue
            record = self.view.records(ids=[current])[0]
            included[current] = record
            for evidence in record.get("evidence", []):
                current_version = manifest["sources"].get(evidence["source_id"])
                if current_version == evidence["source_version"]:
                    continue
                old_source = manifest["source_versions"].get(evidence["source_version"], {})
                current_source = manifest["source_versions"].get(current_version, {})
                same_original = bool(old_source) and old_source.get("original_hash") == current_source.get("original_hash")
                source_changed = True
                invalid_premise |= not same_original
                notices.append({"source_id": evidence["source_id"], "evidence_version": evidence["source_version"], "current_source_version": current_version, "source_change": "extraction-revised" if same_original else "original-replaced-or-unavailable", "reason": "The exact cited passage remains historical evidence. A newer extraction does not independently corroborate it." if same_original else "The source original changed or is unavailable; revalidate before treating this as current support."})
            if row["availability"] == "suggestion":
                for decision in self.view.prior_decisions(current):
                    previous = decision["record"]
                    if decision["exact_duplicate"]:
                        if current == identity:
                            return {"root_id": identity, "complete": True, "records": [], "withheld": True, "reason": "An exact claim with the same scope and evidence was already dismissed or disputed.", "decision": {"id": previous["id"], "version": previous["version"]}}
                        invalid_premise = True
                    notices.append({"prior_decision": {"id": previous["id"], "version": previous["version"], "disposition": previous["owner_review"]["disposition"], "statement": previous["statement"], "conditions_and_limits": previous["conditions_and_limits"]}, "scope": "Same declared subject and claim key. Compare meaning and evidence before resurfacing; similarity alone does not dismiss an independent alternative."})
            if current == identity and (not row["active"] or record.get("lifecycle", "current") != "current" or row["availability"] == "draft" or row["disposition"] in UNAVAILABLE):
                return {"root_id": identity, "complete": True, "records": [], "withheld": True, "reason": "This record is withdrawn, draft, dismissed or disputed; inspect its retained Markdown for history."}
            for ref in record.get("dependencies", []):
                premise = manifest["records"].get(ref["id"])
                if not premise or not premise["active"] or premise["version"] != ref["version"] or premise["disposition"] in UNAVAILABLE or premise["availability"] == "draft":
                    invalid_premise = True
                    notices.append({"dependency": ref, "current_version": premise["version"] if premise else None, "reason": "A relied-on premise changed or is unavailable; the dependent assertion requires revalidation."})
                if premise:
                    if premise["availability"] == "suggestion":
                        provisional.append({"id": premise["id"], "version": premise["version"], "role": "premise"})
                    pending.append(premise["id"])
            for ref in self.view.incoming(current, roles=sorted(CORRECTIONS)):
                declaring = manifest["records"].get(ref["id"])
                if not declaring or not declaring["active"] or declaring["availability"] == "draft" or declaring["disposition"] in UNAVAILABLE:
                    continue
                correction_present = True
                notices.append({"correction": {key: ref[key] for key in ("id", "version", "role")}, "target_id": current, "scope": ref.get("scope", "See the correcting record."), "target_version": ref.get("target_version")})
                pending.append(ref["id"])
            # Context refs discuss or refute a target; they are not premises.
            # The complete ref remains in the record but cannot invalidate a rebuttal.
        qualified = invalid_premise or bool(provisional) or correction_present or source_changed
        if qualified and not supports_qualifications:
            return {"root_id": identity, "complete": True, "records": [], "withheld": True, "reason": "This assertion needs qualifications the caller cannot preserve.", "expansion": {"method": "context", "ids": [identity], "supports_qualifications": True, "revision": self.revision}}
        records = list(included.values())
        source_policy = self.view.source_purpose_policy()
        purpose_notes = {record["id"]: source_policy.record_evidence_policy(record) for record in records}
        legacy = any("record_kind" not in record for record in records)
        return {"root_id": identity, "complete": True, "records": records, "source_evidence_policy": purpose_notes, "qualified": qualified, "requires_revalidation": invalid_premise, "provisional_dependencies": provisional, "notices": notices, "limitations": ["Legacy premise/correction coverage is incomplete."] if legacy else []}

    def context(self, *, ids: list[str] | None = None, query: str = "", subject_id: str | None = None, facet: str | None = None, knowledge_policy: str = "mixed", valid_at: str | None = None, offset: int = 0, limit: int = 20, closure_limit: int = 128, supports_qualifications: bool = True, budget_chars: int = 8000, representation: str = "json") -> dict:
        if knowledge_policy == "current-state" and valid_at is None:
            valid_at = datetime.now(ZoneInfo(self.view.timezone)).date().isoformat()
        if knowledge_policy in {"accepted-only", "current-state"}:
            knowledge_policy = "accepted_only"
        if knowledge_policy not in {"mixed", "accepted_only"} or not 1 <= closure_limit <= 128 or not 1 <= limit <= 20:
            raise V2Error("invalid-request", "Unsupported policy or context bounds")
        arguments = {"ids": ids, "query": query, "subject_id": subject_id, "facet": facet, "knowledge_policy": knowledge_policy, "valid_at": valid_at, "offset": offset, "limit": limit, "closure_limit": closure_limit, "supports_qualifications": supports_qualifications, "budget_chars": budget_chars, "representation": representation}
        key = hash_bytes(canonical_json({"revision": self.revision, "operation": "context", "arguments": arguments, "configuration": "gateway/2:lexical", "source_policy": self.view.source_purpose_policy().snapshot_hash}))
        if key in self._cache:
            return copy.deepcopy(self._cache[key])
        if ids is None:
            candidates = self.view.candidates(query=query, subject_id=subject_id, facet=facet, availability="accepted" if knowledge_policy == "accepted_only" else None, valid_at=valid_at, offset=offset, limit=limit)
            selected = [item["id"] for item in candidates["items"]]
            total = candidates["total"]
        else:
            if len(ids) != len(set(ids)) or len(ids) > 20:
                raise V2Error("invalid-request", "Batch context takes up to 20 unique identities")
            selected, total = list(ids), len(ids)
        if valid_at is not None:
            from datetime import date
            try:
                date.fromisoformat(valid_at)
            except (TypeError, ValueError) as exc:
                raise V2Error("invalid-request", "valid_at must be an ISO calendar date") from exc
        units = []
        for identity in selected:
            row = self.view.manifest["records"].get(identity)
            if knowledge_policy == "accepted_only" and row and row["availability"] != "accepted":
                continue
            if ids is not None and valid_at is not None and row:
                record = self.view.records(ids=[identity])[0]
                start, end = record.get("applies_from"), record.get("applies_until")
                if (start and start > valid_at) or (end and end < valid_at):
                    units.append({"root_id": identity, "complete": True, "records": [], "withheld": True, "reason": "This record does not apply at the requested date; inspect retained history or choose another valid-at date.", "valid_at": valid_at})
                    continue
            units.append(self._unit(identity, closure_limit=closure_limit, supports_qualifications=supports_qualifications))
        value = self._base() | {"knowledge_policy": knowledge_policy, "items": units, "total_candidates": total, "offset": offset, "next_offset": offset + len(selected) if offset + len(selected) < total else None, "expansion": {"method": "context", "revision": self.revision, "ids": selected}, "coverage": "Selected candidates plus declared premise/correction closure; not exhaustive discovery of every relevant connection."}
        result = budget_response(value, budget_chars=budget_chars, representation=representation)
        if "items" in result and len(result["items"]) < len(units):
            result["next_offset"] = offset + len(result["items"])
        self._cache[key] = copy.deepcopy(result)
        return result

    def record(self, identity: str, *, offset=0, limit=4000) -> dict:
        row = self.view.manifest["records"].get(identity)
        if row is None:
            raise V2Error("ambiguous-identity", "Record identity is absent from the pinned revision")
        if not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or not 1 <= limit <= 16000:
            raise V2Error("invalid-request", "Invalid record page")
        text = self.view.store.read_object(row["version"]).decode("utf-8")
        stop = min(len(text), offset + limit)
        if offset > len(text):
            raise V2Error("invalid-request", "Record offset is beyond retained text")
        return self._base() | {"id": identity, "version": row["version"], "availability": row["availability"], "disposition": row["disposition"], "text": text[offset:stop], "offset": offset, "total_characters": len(text), "next_offset": stop if stop < len(text) else None, "complete": offset == 0 and stop == len(text), "interpretation": "Raw retained content; use context to inspect current qualifications before relying on claims."}

    def source(self, source_id: str, *, version=None, offset=0, limit=4000) -> dict:
        page = self.view.source(source_id, version=version, offset=offset, limit=limit)
        if page["text"]:
            descriptor = self.view.manifest["source_versions"][page["source_version"]]
            text = self.view.store.read_object(page["text_version"]).decode("utf-8")
            start = len(text[:offset].encode())
            page["evidence"] = evidence_ref(descriptor, self.view.store.read_object, start, start + len(page["text"].encode()))
        return self._base() | page

    def search_sources(self, query: str, *, offset=0, limit=20, source_scope="ordinary", budget_chars=8000, representation="json") -> dict:
        return budget_response(self.view.search_sources(query, offset=offset, limit=limit, source_scope=source_scope), budget_chars=budget_chars, representation=representation)

    def overview(self) -> dict:
        from synapse.suggestion_library import overview
        return overview(self.view.vault, revision=self.revision)

    def suggestions(self, **arguments) -> dict:
        from synapse.suggestion_library import library
        return library(self.view.vault, revision=self.revision, **arguments)

    def areas(self, *, query="", organization_revision=None, offset=0, limit=24, budget_chars=8000, representation="json") -> dict:
        from synapse.organization import Organization

        result = Organization(self).areas(query=query, organization_revision=organization_revision, offset=offset, limit=limit)
        result["items"] = [compact_area(area, result["organization_revision"]) for area in result["items"]]
        result["coverage"] = compact_organization_coverage(result["coverage"])
        return budget_response(result, budget_chars=budget_chars, representation=representation)

    def area(self, area_id: str, *, organization_revision: str, offset=0, limit=30, budget_chars=8000, representation="json") -> dict:
        from synapse.organization import Organization

        result = Organization(self).area(area_id, organization_revision=organization_revision, offset=offset, limit=limit)
        result["area"] = compact_area(result["area"], result["organization_revision"])
        result["coverage"] = compact_organization_coverage(result["coverage"])
        return budget_response(result, budget_chars=budget_chars, representation=representation)

    def leads(self, *, query="", subject_id=None, source_id=None, offset=0, limit=20, budget_chars=8000, representation="json") -> dict:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise V2Error("invalid-request", "Invalid lead page")
        if source_id is not None and source_id not in self.view.manifest["sources"]:
            raise V2Error("source-unavailable", "Lead source is unavailable in this revision")
        matches, page_offset = [], 0
        while True:
            page = self.view.candidates(query=query, subject_id=subject_id, offset=page_offset, limit=200)
            for item in page["items"]:
                record = item["record"]
                if record.get("record_kind") != "question" or record.get("owner_review", {}).get("disposition") == "deferred":
                    continue
                if source_id and not any(ref["source_id"] == source_id for ref in record.get("evidence", [])):
                    continue
                unit = self._unit(record["id"], closure_limit=128, supports_qualifications=True)
                if unit.get("withheld"):
                    continue
                matches.append(unit)
            if page["next_offset"] is None:
                break
            page_offset = page["next_offset"]
        result = self._base() | {"items": matches[offset:offset + limit], "offset": offset, "total": len(matches), "next_offset": offset + limit if offset + limit < len(matches) else None, "review_required": False, "guidance": "Questions are possible directions, not established premises or permission to begin work."}
        return budget_response(result, budget_chars=budget_chars, representation=representation)

    def semantic(self, query: str, *, subject_id=None, facet=None, knowledge_policy="mixed", limit=10, source_scope="ordinary", budget_chars=8000, representation="json") -> dict:
        from synapse.v2_semantic import query as semantic_query
        if knowledge_policy not in {"mixed", "accepted-only", "current-state"} or not 1 <= limit <= 20:
            raise V2Error("invalid-request", "Invalid semantic policy or limit")
        ranked = semantic_query(self.view, query, subject_id=subject_id, facet=facet, availability=None if knowledge_policy == "mixed" else "accepted", limit=limit, include_sources=True, source_scope=source_scope)
        options = {"knowledge_policy": knowledge_policy, "budget_chars": 32000}
        units, omissions = [], list(ranked.get("omissions", []))
        if ranked["semantic"] == "ok":
            for candidate in ranked["candidates"]:
                if candidate.get("kind") == "source":
                    evidence = candidate.get("evidence", [])
                    if not evidence:
                        omissions.append("A source candidate lacked an exact passage and was omitted.")
                        continue
                    try:
                        passage = self.passage(evidence[0], context_characters=0)
                    except V2Error as exc:
                        omissions.append(f"A source candidate could not be verified ({exc.code}).")
                        continue
                    units.append({"kind": "source", "source_id": candidate["source_id"], "source_version": candidate["source_version"], "text_version": candidate["text_version"], "chunk_id": candidate["chunk_id"], "evidence": evidence, "passage": passage, "purpose": candidate.get("purpose", "unknown"), "interpretation": "Retained source material; similarity is not factual support or owner endorsement."})
                else:
                    context = self.context(ids=[candidate["id"]], **options)
                    units.extend(dict(unit, kind="record") for unit in context.get("items", []))
                    if context.get("error"):
                        omissions.append(f"Record {candidate['id']} requires a larger context read.")
            result = self._base() | {"knowledge_policy": knowledge_policy, "items": units, "total_candidates": len(ranked["candidates"]), "semantic_search": "ready"}
        else:
            result = self.context(query=query, subject_id=subject_id, facet=facet, limit=limit, **options)
            result["items"] = [dict(unit, kind="record") for unit in result.get("items", [])]
            if subject_id is None and facet is None and knowledge_policy == "mixed":
                sources = self.view.search_sources(query, limit=limit, source_scope=source_scope)
                result["items"].extend(dict(item, kind="source", interpretation="Retained source material; lexical match is not factual support.") for item in sources.get("items", []))
                result["source_coverage"] = {key: sources.get(key) for key in ("total", "next_offset", "limitations")}
            else:
                omissions.append("Scoped lexical fallback covers records; use search_sources explicitly for source-only material.")
            result["semantic_search"] = "stale" if "stale" in ranked["semantic"] or "mismatch" in ranked["semantic"] else "unavailable"
        result["source_scope"] = source_scope
        result["source_policy"] = ranked.get("source_policy", self.view.source_purpose_policy().metadata())
        result["semantic_detail"] = ranked["semantic"]
        result["retrieval_method"] = "semantic" if ranked["semantic"] == "ok" else "lexical-fallback"
        result["limitations"] = result.get("limitations", []) + omissions + ranked.get("limitations", []) + ["Candidate similarity is not support for a claim; evidence and corrections remain attached."]
        return budget_response(result, budget_chars=budget_chars, representation=representation)

    def _node(self, identity):
        row = copy.deepcopy(self.view.manifest["records"][identity])
        if row["profile"] == "knowledge-v2":
            record = self.view.records(ids=[identity])[0]
            for name in ("owner_review", "owner_position", "lifecycle", "record_kind", "support", "as_of", "applies_from", "applies_until", "epistemic_basis"):
                if name in record:
                    row[name] = record[name]
        return row

    def _qualified_edges(self, identity, include_suggestions):
        edges, withheld = [], 0
        policy_hash = self.view.source_purpose_policy().snapshot_hash
        for edge in self.view.relationships(identity=identity, include_suggestions=include_suggestions):
            # relationships() decodes fresh dictionaries from SQLite per read.
            # Re-copying every high-degree legacy edge adds no isolation.
            assertion = edge.get("record_id")
            if assertion:
                key = f"edge-context:{policy_hash}:{assertion}"
                if key not in self._cache:
                    self._cache[key] = self._unit(assertion, closure_limit=128, supports_qualifications=True)
                unit = self._cache[key]
                assertion_record = next((record for record in unit.get("records", []) if record["id"] == assertion), {})
                for name in ("owner_review", "owner_position", "lifecycle", "record_kind", "support", "as_of", "applies_from", "applies_until", "epistemic_basis"):
                    if name in assertion_record:
                        edge[name] = assertion_record[name]
                if unit.get("withheld") or not unit.get("complete"):
                    withheld += 1
                    continue
                for name in ("qualified", "requires_revalidation", "notices", "provisional_dependencies", "limitations", "source_evidence_policy"):
                    edge[name] = unit.get(name)
                if not include_suggestions and (unit.get("requires_revalidation") or unit.get("provisional_dependencies")):
                    withheld += 1
                    continue
                edge["context_expansion"] = {"method": "context", "ids": [assertion], "revision": self.revision}
            else:
                edge["limitations"] = ["Legacy relationship: passage-level provenance and declared dependency coverage may be incomplete."]
            edges.append(edge)
        return edges, withheld

    def passage(self, evidence: dict, *, context_characters=300) -> dict:
        source = self.view.manifest["source_versions"].get(evidence.get("source_version"))
        if not source:
            raise V2Error("source-unavailable", "Evidence source is not retained in this revision")
        if not 0 <= context_characters <= 4000:
            raise V2Error("invalid-request", "Passage context must be 0–4000 characters")
        return self._base() | read_passage(source, evidence, self.view.store.read_object, context_characters=context_characters)

    def neighbors(self, identity: str, *, include_suggestions=False, limit=150, offset=0,
                  relation=None, direction="both", node_type=None, budget_chars=32000, representation="json") -> dict:
        if (identity not in self.view.manifest["records"] or isinstance(limit, bool)
                or not isinstance(limit, int) or not 1 <= limit <= 150
                or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
                or direction not in {"both", "in", "out"}
                or (relation is not None and not isinstance(relation, str))
                or (node_type is not None and not isinstance(node_type, str))):
            raise V2Error("invalid-request", "Neighborhood requires a known identity, valid filters and bounded page")
        edges, withheld = self._qualified_edges(identity, include_suggestions)
        counts, matched = {}, []
        for edge in edges:
            directions = ({"out"} if edge["from_id"] == identity else set()) | ({"in"} if edge["to_id"] == identity else set())
            peer = edge["to_id"] if edge["from_id"] == identity else edge["from_id"]
            for way in directions:
                key = (edge["relation"], way)
                counts[key] = counts.get(key, 0) + 1
            if relation is not None and edge["relation"] != relation:
                continue
            if direction != "both" and direction not in directions:
                continue
            if node_type is not None and self.view.manifest["records"][peer]["type"] != node_type:
                continue
            matched.append(edge)
        # Page relationships, not an arbitrary prefix of neighbor identities.
        # Parallel relationships to one busy peer can therefore also be completed.
        matched.sort(key=lambda e: (e["from_id"], e["to_id"], e["relation"], e["id"]))
        peers = {e[k] for e in matched for k in ("from_id", "to_id")} - {identity}
        selected = matched[offset:offset + limit]
        metadata = self._base() | {
            "total_neighbors": len(peers), "total_relationships": len(matched),
            "offset": offset, "page_unit": "relationships", "withheld_edges": withheld,
            "filters": {"relation": relation, "direction": direction, "node_type": node_type},
            "relation_counts": [{"relation": key[0], "direction": key[1], "count": count}
                                for key, count in sorted(counts.items())],
        }
        while True:
            identities = {identity} | {e[k] for e in selected for k in ("from_id", "to_id")}
            next_offset = offset + len(selected) if offset + len(selected) < len(matched) else None
            value = metadata | {
                "nodes": [self._node(item) for item in sorted(identities)], "edges": selected,
                "next_offset": next_offset, "truncated": next_offset is not None,
                "budget": {"limit": budget_chars, "unit": "characters", "representation": representation},
            }
            if transport_size(value, representation) <= budget_chars:
                if not selected and offset < len(matched):
                    raise V2Error("coverage-limited", "A complete relationship and its endpoints do not fit; increase the response budget")
                return value
            if not selected:
                raise V2Error("coverage-limited", "Neighborhood metadata does not fit; increase the response budget")
            selected = selected[:-1]

    def path(self, start: str, end: str, *, include_suggestions=False, max_hops=4, max_nodes=500) -> dict:
        if not 1 <= max_hops <= 8 or not 2 <= max_nodes <= 500:
            raise V2Error("invalid-request", "Path bounds exceed advertised limits")
        if start not in self.view.manifest["records"] or end not in self.view.manifest["records"]:
            raise V2Error("ambiguous-identity", "Path endpoints must resolve exactly")
        pending = deque([(start, [])])
        visited = {start}
        limited = False
        while pending:
            identity, route = pending.popleft()
            if identity == end:
                return self._base() | {"found": True, "edges": route, "visited": len(visited), "truncated": limited, "knowledge_policy": "mixed" if include_suggestions else "accepted_only"}
            if len(route) >= max_hops:
                limited = True
                continue
            edges, withheld = self._qualified_edges(identity, include_suggestions)
            limited = limited or bool(withheld)
            for edge in edges:
                other = edge["to_id"] if edge["from_id"] == identity else edge["from_id"]
                if other in visited:
                    continue
                if len(visited) >= max_nodes:
                    limited = True
                    break
                visited.add(other)
                pending.append((other, route + [edge]))
        return self._base() | {"found": False, "edges": [], "visited": len(visited), "truncated": limited, "limitation": "No path within these bounds does not establish absence of a useful connection."}
