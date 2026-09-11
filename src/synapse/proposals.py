from __future__ import annotations

import difflib
import json
import re
import sys
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from synapse.index import connect, reindex
from synapse.maintenance import archive_entity as archive_entity_impl
from synapse.maintenance import check_vault, merge_entities
from synapse.models import ENTITY_TYPE_FOLDERS, ENTITY_TYPES, RELATION_TYPES, Entity
from synapse.owner_context import load_knowledge_entities, validate_knowledge_graph
from synapse.util import (
    encode_for_console,
    generate_ulid,
    read_frontmatter,
    slugify,
    unique_path,
    utc_now,
    write_frontmatter,
)


def _echo(text: str) -> None:
    """Print dry-run plan text without UnicodeEncodeError on legacy consoles."""
    print(encode_for_console(text, getattr(sys.stdout, "encoding", None)))

ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$", re.IGNORECASE)

@dataclass(frozen=True)
class EvidenceItem:
    file: str
    note: str

@dataclass(frozen=True)
class BaseItem:
    id: str
    content_hash: str

@dataclass(frozen=True)
class ProposalOp:
    op: str
    type: str | None = None
    name: str | None = None
    tags: list[str] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    relations: list[dict[str, Any]] = field(default_factory=list)
    from_: str | None = None  # maps from 'from'
    to: str | None = None
    direction: str | None = None
    id: str | None = None
    alias: str | None = None
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    set: dict[str, Any] = field(default_factory=dict)
    unset: list[str] = field(default_factory=list)
    keep: str | None = None
    merge: str | None = None
    reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

@dataclass(frozen=True)
class Proposal:
    id: str  # maps from 'proposal'
    agent: str
    created_at: str
    rationale: str
    confidence: Literal["high", "medium", "low"]
    evidence: list[EvidenceItem]
    base: list[BaseItem]
    ops: list[ProposalOp]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

def load_proposal(path: str | Path) -> Proposal:
    p_path = Path(path)
    if not p_path.exists():
        raise FileNotFoundError(f"Proposal file not found: {path}")
    with p_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("Proposal YAML must be a dictionary")
        
    evidence_items = []
    for item in data.get("evidence") or []:
        if isinstance(item, dict):
            evidence_items.append(EvidenceItem(file=str(item.get("file") or ""), note=str(item.get("note") or "")))
            
    base_items = []
    for item in data.get("base") or []:
        if isinstance(item, dict):
            base_items.append(BaseItem(id=str(item.get("id") or ""), content_hash=str(item.get("content_hash") or "")))
            
    ops = []
    for op_data in data.get("ops") or []:
        if not isinstance(op_data, dict):
            continue
        op_kwargs = dict(op_data)
        if "from" in op_kwargs:
            op_kwargs["from_"] = op_kwargs.pop("from")
            
        tags = op_kwargs.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        op_kwargs["tags"] = [str(t) for t in tags]
        
        properties = op_kwargs.get("properties") or {}
        op_kwargs["properties"] = dict(properties)
        
        relations = op_kwargs.get("relations") or []
        op_kwargs["relations"] = [dict(r) for r in relations if isinstance(r, dict)]
        
        add = op_kwargs.get("add") or []
        if isinstance(add, str):
            add = [add]
        op_kwargs["add"] = [str(t) for t in add]
        
        remove = op_kwargs.get("remove") or []
        if isinstance(remove, str):
            remove = [remove]
        op_kwargs["remove"] = [str(t) for t in remove]
        
        set_val = op_kwargs.get("set") or {}
        op_kwargs["set"] = dict(set_val)
        
        unset = op_kwargs.get("unset") or []
        if isinstance(unset, str):
            unset = [unset]
        op_kwargs["unset"] = [str(t) for t in unset]
        
        op_kwargs["raw"] = op_data
        
        valid_fields = ProposalOp.__dataclass_fields__.keys()
        filtered_kwargs = {k: v for k, v in op_kwargs.items() if k in valid_fields}
        
        ops.append(ProposalOp(**filtered_kwargs))
        
    return Proposal(
        id=str(data.get("proposal") or ""),
        agent=str(data.get("agent") or ""),
        created_at=str(data.get("created_at") or ""),
        rationale=str(data.get("rationale") or ""),
        confidence=data.get("confidence") or "medium",
        evidence=evidence_items,
        base=base_items,
        ops=ops,
        raw=data
    )

def validate_proposal(conn, proposal: Proposal) -> list[str]:
    errors = []
    
    if not proposal.id or not ULID_PATTERN.match(proposal.id):
        errors.append(f"Proposal ID {proposal.id!r} is not a valid ULID")
        
    if not proposal.created_at:
        errors.append("Proposal created_at is missing")
        
    if proposal.confidence not in ("high", "medium", "low"):
        errors.append(f"Invalid proposal confidence {proposal.confidence!r}")
        
    VALID_OPS = {
        "create_entity", "add_relation", "remove_relation", "add_alias", "retag",
        "update_properties", "merge", "recommend_verify", "archive_entity"
    }
    
    def entity_exists(entity_id: str) -> bool:
        if entity_id == "me":
            return True
        row = conn.execute("SELECT 1 FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return row is not None

    create_entity_indices = {
        i for i, candidate in enumerate(proposal.ops) if candidate.op == "create_entity"
    }

    def validate_ref(ref: Any, op_index: int, field_name: str) -> bool:
        if not isinstance(ref, str):
            errors.append(f"Op {op_index}: {field_name} must be a string")
            return False
        if ref == "me":
            return True
        if ref.startswith("$new."):
            try:
                n = int(ref.split(".")[1])
                if not (0 <= n < op_index):
                    errors.append(f"Op {op_index}: {field_name} references invalid $new.{n} (must be index < {op_index})")
                    return False
                if n not in create_entity_indices:
                    errors.append(f"Op {op_index}: {field_name} references $new.{n}, but op {n} is not a create_entity op")
                    return False
                return True
            except (IndexError, ValueError):
                errors.append(f"Op {op_index}: {field_name} has invalid $new reference format {ref!r}")
                return False
        if not ULID_PATTERN.match(ref):
            errors.append(f"Op {op_index}: {field_name} {ref!r} is not a valid ULID or $new.N ref")
            return False
        return True

    base_ids = set()
    for base_idx, base_item in enumerate(proposal.base):
        if not base_item.id or (base_item.id != "me" and not ULID_PATTERN.match(base_item.id)):
            errors.append(f"Base entry {base_idx}: ID {base_item.id!r} is not a valid ULID or 'me'")
        elif not entity_exists(base_item.id):
            errors.append(f"Base entry {base_idx}: Entity {base_item.id!r} does not exist in the database")
        else:
            base_ids.add(base_item.id)
            
        if not base_item.content_hash:
            errors.append(f"Base entry {base_idx}: content_hash is missing")

    touched_existing_ids = set()

    def note_touched(ref: Any) -> None:
        # "me" is an existing entity like any other: writes to it must carry a
        # base entry so the stale check protects the owner file too.
        if isinstance(ref, str) and (ref == "me" or ULID_PATTERN.match(ref)):
            touched_existing_ids.add(ref)

    for idx, op in enumerate(proposal.ops):
        if op.op not in VALID_OPS:
            errors.append(f"Op {idx}: unknown op type {op.op!r}")
            continue
            
        if op.op == "create_entity":
            if op.type not in ENTITY_TYPES:
                errors.append(f"Op {idx}: unknown entity type {op.type!r}")
            if not op.name or not isinstance(op.name, str):
                errors.append(f"Op {idx}: create_entity must have a non-empty name string")
            if not isinstance(op.tags, list) or not all(isinstance(t, str) for t in op.tags):
                errors.append(f"Op {idx}: tags must be a list of strings")
            if not isinstance(op.properties, dict):
                errors.append(f"Op {idx}: properties must be a dictionary")
            else:
                for pk, pv in op.properties.items():
                    if not (pv is None or isinstance(pv, (str, int, float, bool)) or 
                            (isinstance(pv, list) and all(isinstance(x, (str, int, float, bool)) for x in pv))):
                        errors.append(f"Op {idx}: property {pk!r} has a non-flat value {pv!r}")
            
            if not isinstance(op.relations, list):
                errors.append(f"Op {idx}: relations must be a list")
            else:
                for rel_idx, r in enumerate(op.relations):
                    if not isinstance(r, dict):
                        errors.append(f"Op {idx} relation {rel_idx}: relation must be a dictionary")
                        continue
                    r_type = r.get("type")
                    if r_type not in RELATION_TYPES:
                        errors.append(f"Op {idx} relation {rel_idx}: unknown relation type {r_type!r}")
                    
                    r_dir = r.get("direction")
                    if r_dir and r_dir not in ("incoming", "outgoing"):
                        errors.append(f"Op {idx} relation {rel_idx}: invalid relation direction {r_dir!r}")
                        
                    # File-format relation specs carry only `target` (+ optional
                    # `direction`); the indexer silently has no use for from/to
                    # keys, so reject them loudly instead of dropping edges.
                    for bad_key in ("from", "to"):
                        if bad_key in r:
                            errors.append(
                                f"Op {idx} relation {rel_idx}: relation specs use 'target' "
                                f"(+ optional 'direction'), not {bad_key!r} — see design doc 01"
                            )
                    target_val = r.get("target")
                    if target_val is None:
                        errors.append(f"Op {idx} relation {rel_idx}: missing required 'target'")
                    elif validate_ref(target_val, idx, f"relation {rel_idx} target"):
                        note_touched(target_val)

        elif op.op in ("add_relation", "remove_relation"):
            if validate_ref(op.from_, idx, "from"):
                note_touched(op.from_)
            if validate_ref(op.to, idx, "to"):
                note_touched(op.to)
            if op.type not in RELATION_TYPES:
                errors.append(f"Op {idx}: unknown relation type {op.type!r}")
            if op.direction and op.direction not in ("incoming", "outgoing"):
                errors.append(f"Op {idx}: invalid relation direction {op.direction!r}")

        elif op.op == "add_alias":
            if validate_ref(op.id, idx, "id"):
                note_touched(op.id)
            if not op.alias or not isinstance(op.alias, str):
                errors.append(f"Op {idx}: add_alias must have a non-empty alias string")

        elif op.op == "retag":
            if validate_ref(op.id, idx, "id"):
                note_touched(op.id)
            if not isinstance(op.add, list) or not all(isinstance(t, str) for t in op.add):
                errors.append(f"Op {idx}: add tags must be a list of strings")
            if not isinstance(op.remove, list) or not all(isinstance(t, str) for t in op.remove):
                errors.append(f"Op {idx}: remove tags must be a list of strings")
            if not op.add and not op.remove:
                errors.append(f"Op {idx}: retag must specify at least one tag to add or remove")
                
        elif op.op == "update_properties":
            if validate_ref(op.id, idx, "id"):
                note_touched(op.id)
            if not isinstance(op.set, dict):
                errors.append(f"Op {idx}: set properties must be a dictionary")
            else:
                for pk, pv in op.set.items():
                    if not (pv is None or isinstance(pv, (str, int, float, bool)) or 
                            (isinstance(pv, list) and all(isinstance(x, (str, int, float, bool)) for x in pv))):
                        errors.append(f"Op {idx}: set property {pk!r} has a non-flat value {pv!r}")
            if not isinstance(op.unset, list) or not all(isinstance(t, str) for t in op.unset):
                errors.append(f"Op {idx}: unset properties must be a list of strings")
            if not op.set and not op.unset:
                errors.append(f"Op {idx}: update_properties must specify at least one property to set or unset")
                
        elif op.op == "merge":
            if validate_ref(op.keep, idx, "keep"):
                note_touched(op.keep)
            if validate_ref(op.merge, idx, "merge"):
                note_touched(op.merge)

        elif op.op == "archive_entity":
            # Same review bar as merge: a closed, recoverable op — never a hard
            # delete. Existence of `op.id` (and staleness of its base hash) is
            # enforced by the generic touched_existing_ids checks below, same as
            # every other op that carries an `id`.
            if validate_ref(op.id, idx, "id"):
                note_touched(op.id)
                if op.id == "me":
                    errors.append(
                        f"Op {idx}: archive_entity refuses to archive 'me' — "
                        "the owner entity can never be archived"
                    )
            if not op.reason or not isinstance(op.reason, str):
                errors.append(f"Op {idx}: archive_entity must specify a non-empty reason string")

        elif op.op == "recommend_verify":
            if validate_ref(op.id, idx, "id"):
                note_touched(op.id)
            if not op.reason or not isinstance(op.reason, str):
                errors.append(f"Op {idx}: recommend_verify must specify a non-empty reason string")

    for tid in touched_existing_ids:
        if tid not in base_ids:
            errors.append(f"Entity {tid} is touched by operations but is missing from base entries")
            
    for tid in touched_existing_ids:
        if not entity_exists(tid):
            errors.append(f"Entity {tid} referenced in operations does not exist in the database")

    if not errors:
        errors.extend(_validate_projected_knowledge(conn, proposal))
    return errors


def _validate_projected_knowledge(conn, proposal: Proposal) -> list[str]:
    """Validate the final graph without writing files or inventing committed IDs.

    Project the actual storage semantics, including incoming edge ownership and
    create provenance noops. Existing profile defects do not block unrelated
    maintenance; only introduced or worsened errors reject the whole proposal.
    """
    baseline = load_knowledge_entities(conn)
    projected = deepcopy(baseline)
    new_ids: dict[str, str] = {}
    errors: list[str] = []

    def resolve(ref: str | None) -> str | None:
        return new_ids.get(ref, ref)

    for idx, op in enumerate(proposal.ops):
        if op.op == "create_entity":
            placeholder = f"$new.{idx}"
            existing_id = find_entity_by_provenance(conn, proposal.id, op.name, op.type)
            new_ids[placeholder] = existing_id or placeholder
            if existing_id:
                continue
            relations = deepcopy(op.relations)
            for relation in relations:
                relation["target"] = resolve(relation.get("target"))
            metadata = {
                "id": placeholder,
                "type": op.type,
                "name": op.name,
                "review_status": "proposed",
                "tags": deepcopy(op.tags),
                "properties": deepcopy(op.properties),
                "relations": relations,
            }
            projected[placeholder] = Entity(
                id=placeholder,
                type=op.type,
                name=op.name,
                file_path=Path("entities") / ENTITY_TYPE_FOLDERS[op.type] / f"{slugify(op.name)}.md",
                frontmatter=metadata,
                body=op.body,
                content_hash="",
                properties=metadata["properties"],
                relation_specs=relations,
            )
        elif op.op == "update_properties":
            entity_id = resolve(op.id)
            entity = projected.get(entity_id)
            if entity is None:
                errors.append(f"Op {idx}: {entity_id} is unavailable after an earlier operation")
                continue
            had_profile = "knowledge_profile" in entity.properties
            entity.properties.update(deepcopy(op.set))
            for key in op.unset:
                entity.properties.pop(key, None)
            if had_profile and not entity.properties.get("knowledge_profile"):
                errors.append(
                    f"Op {idx}: owner knowledge profile cannot be removed by "
                    "update_properties; revise or archive the record instead"
                )
        elif op.op in {"add_relation", "remove_relation"}:
            from_id, to_id = resolve(op.from_), resolve(op.to)
            incoming = op.direction == "incoming"
            owner_id, target_id = (to_id, from_id) if incoming else (from_id, to_id)
            entity = projected.get(owner_id)
            if entity is None:
                errors.append(f"Op {idx}: {owner_id} is unavailable after an earlier operation")
                continue
            # add stores outgoing direction implicitly even when explicitly
            # requested; remove distinguishes explicit and implicit storage.
            direction = "incoming" if incoming else None
            if op.op == "remove_relation" and op.direction == "outgoing":
                direction = "outgoing"

            def matches(
                relation: Any,
                relation_type: str | None = op.type,
                target: str | None = target_id,
                stored_direction: str | None = direction,
                op_type: str = op.op,
                match_properties: dict[str, Any] = op.properties,
            ) -> bool:
                if not isinstance(relation, dict) or (
                    relation.get("type") != relation_type
                    or relation.get("target") != target
                    or relation.get("direction") != stored_direction
                ):
                    return False
                if op_type == "add_relation":
                    return True
                source = match_properties.get("source")
                if source is not None and relation.get("source") != source:
                    return False
                properties = relation.get("properties") or {}
                return all(
                    properties.get(key) == value
                    for key, value in match_properties.items() if key != "source"
                )

            if op.op == "remove_relation":
                entity.relation_specs = [r for r in entity.relation_specs if not matches(r)]
            elif not any(matches(r) for r in entity.relation_specs):
                relation = {"type": op.type, "target": target_id}
                if incoming:
                    relation["direction"] = "incoming"
                extra = deepcopy(op.properties)
                if "source" in extra:
                    relation["source"] = extra.pop("source")
                if extra:
                    relation["properties"] = extra
                entity.relation_specs.append(relation)
        elif op.op == "archive_entity":
            # Archive preserves other files' references so the profile checker
            # can reject newly dangling evidence/revision links before writes.
            projected.pop(resolve(op.id), None)
        elif op.op == "merge":
            keep_id, merge_id = resolve(op.keep), resolve(op.merge)
            if keep_id not in projected or merge_id not in projected:
                errors.append(f"Op {idx}: merge endpoint is unavailable after an earlier operation")
                continue
            keep, merged = projected[keep_id], projected[merge_id]
            marked_inputs = [
                entity for entity in (keep, merged) if "knowledge_profile" in entity.properties
            ]
            if len(marked_inputs) == 2 and any(
                keep.properties.get(key) != merged.properties.get(key)
                for key in ("subject_id", "claim_key", "record_kind")
            ):
                errors.append(
                    f"Op {idx}: cannot merge different owner knowledge claim identities; "
                    "use a scoped qualification or revision instead"
                )
            _project_knowledge_merge(keep, merged)
            if marked_inputs and not keep.properties.get("knowledge_profile"):
                errors.append(f"Op {idx}: merge cannot remove the owner knowledge profile")
            for entity in projected.values():
                for relation in entity.relation_specs:
                    if isinstance(relation, dict) and relation.get("target") == merge_id:
                        relation["target"] = keep_id
            projected.pop(merge_id)

    for entity in projected.values():
        entity.frontmatter["properties"] = entity.properties
        entity.frontmatter["relations"] = entity.relation_specs

    def issue_key(issue):
        return issue.severity, str(issue.file_path), issue.message

    prior = Counter(issue_key(issue) for issue in validate_knowledge_graph(baseline))
    for issue in validate_knowledge_graph(projected):
        key = issue_key(issue)
        if prior[key]:
            prior[key] -= 1
        elif issue.severity == "error":
            errors.append(f"{issue.message} ({issue.file_path})")
    return errors


def _project_knowledge_merge(keep: Entity, merged: Entity) -> None:
    """Mirror merge_entities' property/body/edge effects for profile preflight."""
    keep_props, merge_props = keep.properties, merged.properties
    keep_url, merge_url = keep_props.get("linkedin_url"), merge_props.get("linkedin_url")
    if keep_url and merge_url:
        alternatives = keep_props.get("linkedin_urls_alt")
        if not isinstance(alternatives, list):
            alternatives = [alternatives] if alternatives else []
        if merge_url != keep_url and merge_url not in alternatives:
            alternatives.append(merge_url)
        if alternatives:
            keep_props["linkedin_urls_alt"] = alternatives
    elif merge_url and not keep_url:
        keep_props["linkedin_url"] = merge_url
    collisions = {}
    for key, value in merge_props.items():
        if key == "linkedin_url":
            continue
        if key in keep_props:
            if keep_props[key] != value:
                collisions[key] = value
        else:
            keep_props[key] = value
    collision_section = ""
    if collisions:
        collision_section = "Collisions:\n" + "\n".join(
            f"  {key}: {value}" for key, value in sorted(collisions.items())
        ) + "\n\n"
    keep.body = (
        f"{keep.body.rstrip()}\n\n## Merged Notes From {merged.name}\n\n"
        f"{collision_section}{merged.body.strip()}\n"
    )
    keep.relation_specs.extend(deepcopy(merged.relation_specs))


def check_stale(conn, base: list[BaseItem]) -> list[str]:
    drifted = []
    for item in base:
        row = conn.execute("SELECT content_hash FROM entities WHERE id = ?", (item.id,)).fetchone()
        if not row:
            drifted.append(f"Entity {item.id} not found in database")
        elif row["content_hash"] != item.content_hash:
            drifted.append(f"drifted: {item.id} has drifted (hash {row['content_hash']} != {item.content_hash})")
    return drifted

def get_entity_path(conn, vault: Path, entity_id: str, created_paths: dict[str, Path]) -> Path:
    if entity_id in created_paths:
        return created_paths[entity_id]
    if entity_id == "me":
        return vault / "entities" / "people" / "me.md"
    row = conn.execute("SELECT file_path FROM entities WHERE id = ?", (entity_id,)).fetchone()
    if not row:
        raise ValueError(f"Entity {entity_id} not found in index")
    fp = Path(row["file_path"])
    return fp if fp.is_absolute() else vault / fp

def find_entity_by_provenance(conn, proposal_id: str, name: str, entity_type: str) -> str | None:
    rows = conn.execute(
        "SELECT id, frontmatter FROM entities WHERE type = ? AND name = ?",
        (entity_type, name)
    ).fetchall()
    for row in rows:
        fm = json.loads(row["frontmatter"] or "{}")
        prov = fm.get("provenance") or {}
        if prov.get("extracted_by") == f"proposal:{proposal_id}":
            return row["id"]
    return None

def apply_proposal(
    vault: str | Path,
    proposal_path: str | Path,
    *,
    execute: bool = False,
    allow_verify: bool = False,
) -> dict[str, Any]:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault, "apply_proposal")
    root = Path(vault).resolve()
    p_path = Path(proposal_path).resolve()
    proposal = load_proposal(p_path)
    # Preflight and stale checks must see current canonical bodies/properties.
    reindex(root)
    
    conn = connect(root)
    try:
        errors = validate_proposal(conn, proposal)
        if errors:
            return {"success": False, "errors": errors}
            
        drifted = check_stale(conn, proposal.base)
        if drifted:
            return {"success": False, "errors": drifted, "stale": True}
            
        new_ids = {}
        for idx, op in enumerate(proposal.ops):
            if op.op == "create_entity":
                new_ids[f"$new.{idx}"] = generate_ulid()
                
        def resolve_ref(ref: str | None) -> str | None:
            if not ref:
                return ref
            if ref in new_ids:
                return new_ids[ref]
            if ref.startswith("$new."):
                # Validation rejects these upfront; never write a literal
                # placeholder into a canonical file.
                raise ValueError(f"Unresolvable reference {ref!r}")
            return ref
            
        created_paths = {}
        op_results = []
        skipped_verify_count = 0

        for idx, op in enumerate(proposal.ops):
            op_result = "noop"
            
            if op.op == "create_entity":
                existing_id = find_entity_by_provenance(conn, proposal.id, op.name, op.type)
                entity_id = new_ids[f"$new.{idx}"]
                folder = ENTITY_TYPE_FOLDERS.get(op.type, op.type)
                cand_path = root / "entities" / folder / f"{slugify(op.name)}.md"
                
                if existing_id:
                    new_ids[f"$new.{idx}"] = existing_id
                    try:
                        cand_path = get_entity_path(conn, root, existing_id, created_paths)
                    except ValueError:
                        pass
                    op_result = "noop"
                    old_text = ""
                    new_text = ""
                    if not execute:
                        _echo(f"create_entity: {cand_path.relative_to(root)} [noop]")
                else:
                    op_result = "applied"
                    resolved_relations = []
                    for r in op.relations:
                        rc = dict(r)
                        for f_key in ("target", "from", "to"):
                            if f_key in rc:
                                rc[f_key] = resolve_ref(rc[f_key])
                        resolved_relations.append(rc)
                        
                    metadata = {
                        "id": entity_id,
                        "type": op.type,
                        "name": op.name,
                        "review_status": "proposed",
                        "tags": op.tags,
                        "relations": resolved_relations,
                        "properties": op.properties,
                        "provenance": {
                            "extracted_by": f"proposal:{proposal.id}",
                            "extracted_at": utc_now(),
                        },
                        "created_at": utc_now(),
                    }
                    
                    old_text = ""
                    yaml_rendered = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
                    new_text = f"---\n{yaml_rendered}\n---\n\n{op.body.strip()}\n"
                    
                    if not execute:
                        _echo(f"create_entity: {cand_path.relative_to(root)} [applied]")
                        diff = "".join(difflib.unified_diff(
                            old_text.splitlines(keepends=True),
                            new_text.splitlines(keepends=True),
                            fromfile=f"a/{cand_path.name}",
                            tofile=f"b/{cand_path.name}"
                        ))
                        if diff:
                            _echo(diff)
                    else:
                        actual_path = unique_path(cand_path)
                        write_frontmatter(actual_path, metadata, op.body)
                        created_paths[entity_id] = actual_path
                        
            elif op.op == "add_relation":
                from_id = resolve_ref(op.from_)
                to_id = resolve_ref(op.to)

                # The edge is always from_id -> to_id. `direction: incoming`
                # only picks the declaring side: the spec lives on the to-side
                # file and points back at from_id (edge ownership, design 01).
                if op.direction == "incoming":
                    from_path = get_entity_path(conn, root, to_id, created_paths)
                    rel_spec = {"type": op.type, "target": from_id, "direction": "incoming"}
                else:
                    from_path = get_entity_path(conn, root, from_id, created_paths)
                    rel_spec = {"type": op.type, "target": to_id}

                # Carry the op's edge metadata onto the relation spec. `source`
                # is a canonical sibling key (the indexer reads it as the edge's
                # source_file); everything else rides under `properties`.
                # Without this, add_relation silently dropped all edge metadata
                # (source/started_on/current/note), unlike create_entity.
                if op.properties:
                    extra = dict(op.properties)
                    if "source" in extra:
                        rel_spec["source"] = extra.pop("source")
                    if extra:
                        rel_spec["properties"] = extra
                metadata, body = read_frontmatter(from_path)

                # Render old content for diff
                yaml_old = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
                old_text = f"---\n{yaml_old}\n---\n\n{body.strip()}\n"

                rels = metadata.setdefault("relations", [])

                # Check noop
                relation_exists = False
                for r in rels:
                    if (
                        isinstance(r, dict)
                        and r.get("type") == op.type
                        and r.get("target") == rel_spec["target"]
                        and r.get("direction") == rel_spec.get("direction")
                    ):
                        relation_exists = True
                        break

                if relation_exists:
                    op_result = "noop"
                    if not execute:
                        _echo(f"add_relation: {from_path.relative_to(root)} [noop]")
                else:
                    op_result = "applied"
                    rels.append(rel_spec)
                    metadata["review_status"] = "proposed"
                    
                    yaml_new = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
                    new_text = f"---\n{yaml_new}\n---\n\n{body.strip()}\n"
                    
                    if not execute:
                        _echo(f"add_relation: {from_path.relative_to(root)} [applied]")
                        diff = "".join(difflib.unified_diff(
                            old_text.splitlines(keepends=True),
                            new_text.splitlines(keepends=True),
                            fromfile=f"a/{from_path.name}",
                            tofile=f"b/{from_path.name}"
                        ))
                        if diff:
                            _echo(diff)
                    else:
                        write_frontmatter(from_path, metadata, body)

            elif op.op == "remove_relation":
                from_id = resolve_ref(op.from_)
                to_id = resolve_ref(op.to)

                if op.direction == "incoming":
                    from_path = get_entity_path(conn, root, to_id, created_paths)
                    target_id = from_id
                    stored_direction = "incoming"
                else:
                    from_path = get_entity_path(conn, root, from_id, created_paths)
                    target_id = to_id
                    # Deterministic importers may preserve an explicit
                    # `direction: outgoing`; hand-authored/proposal edges
                    # usually omit it. Match whichever representation the
                    # reviewed removal requested.
                    stored_direction = "outgoing" if op.direction == "outgoing" else None

                metadata, body = read_frontmatter(from_path)
                yaml_old = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
                old_text = f"---\n{yaml_old}\n---\n\n{body.strip()}\n"
                source = op.properties.get("source")
                property_match = {
                    key: value for key, value in op.properties.items() if key != "source"
                }

                def matches(
                    relation: Any,
                    relation_type: str | None = op.type,
                    target: str | None = target_id,
                    direction: str | None = stored_direction,
                    source_match: Any = source,
                    properties_match: dict[str, Any] = property_match,
                ) -> bool:
                    if not isinstance(relation, dict):
                        return False
                    if (
                        relation.get("type") != relation_type
                        or relation.get("target") != target
                        or relation.get("direction") != direction
                    ):
                        return False
                    if source_match is not None and relation.get("source") != source_match:
                        return False
                    relation_properties = relation.get("properties") or {}
                    return all(
                        relation_properties.get(key) == value
                        for key, value in properties_match.items()
                    )

                relations = metadata.get("relations") or []
                remaining = [relation for relation in relations if not matches(relation)]
                if len(remaining) == len(relations):
                    op_result = "noop"
                    if not execute:
                        _echo(f"remove_relation: {from_path.relative_to(root)} [noop]")
                else:
                    op_result = "applied"
                    metadata["relations"] = remaining
                    metadata["review_status"] = "proposed"
                    yaml_new = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
                    new_text = f"---\n{yaml_new}\n---\n\n{body.strip()}\n"
                    if not execute:
                        _echo(f"remove_relation: {from_path.relative_to(root)} [applied]")
                        diff = "".join(difflib.unified_diff(
                            old_text.splitlines(keepends=True),
                            new_text.splitlines(keepends=True),
                            fromfile=f"a/{from_path.name}",
                            tofile=f"b/{from_path.name}",
                        ))
                        if diff:
                            _echo(diff)
                    else:
                        write_frontmatter(from_path, metadata, body)
                        
            elif op.op == "add_alias":
                entity_id = resolve_ref(op.id)
                entity_path = get_entity_path(conn, root, entity_id, created_paths)
                metadata, body = read_frontmatter(entity_path)
                
                yaml_old = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
                old_text = f"---\n{yaml_old}\n---\n\n{body.strip()}\n"
                
                aliases = metadata.setdefault("aliases", [])
                if op.alias in aliases:
                    op_result = "noop"
                    if not execute:
                        _echo(f"add_alias: {entity_path.relative_to(root)} [noop]")
                else:
                    op_result = "applied"
                    aliases.append(op.alias)
                    metadata["review_status"] = "proposed"
                    
                    yaml_new = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
                    new_text = f"---\n{yaml_new}\n---\n\n{body.strip()}\n"
                    
                    if not execute:
                        _echo(f"add_alias: {entity_path.relative_to(root)} [applied]")
                        diff = "".join(difflib.unified_diff(
                            old_text.splitlines(keepends=True),
                            new_text.splitlines(keepends=True),
                            fromfile=f"a/{entity_path.name}",
                            tofile=f"b/{entity_path.name}"
                        ))
                        if diff:
                            _echo(diff)
                    else:
                        write_frontmatter(entity_path, metadata, body)
                        
            elif op.op == "retag":
                entity_id = resolve_ref(op.id)
                entity_path = get_entity_path(conn, root, entity_id, created_paths)
                metadata, body = read_frontmatter(entity_path)
                
                yaml_old = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
                old_text = f"---\n{yaml_old}\n---\n\n{body.strip()}\n"
                
                tags = metadata.setdefault("tags", [])
                needs_add = [t for t in op.add if t not in tags]
                needs_remove = [t for t in op.remove if t in tags]
                
                if not needs_add and not needs_remove:
                    op_result = "noop"
                    if not execute:
                        _echo(f"retag: {entity_path.relative_to(root)} [noop]")
                else:
                    op_result = "applied"
                    for t in op.add:
                        if t not in tags:
                            tags.append(t)
                    for t in op.remove:
                        if t in tags:
                            tags.remove(t)
                    metadata["review_status"] = "proposed"
                    
                    yaml_new = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
                    new_text = f"---\n{yaml_new}\n---\n\n{body.strip()}\n"
                    
                    if not execute:
                        _echo(f"retag: {entity_path.relative_to(root)} [applied]")
                        diff = "".join(difflib.unified_diff(
                            old_text.splitlines(keepends=True),
                            new_text.splitlines(keepends=True),
                            fromfile=f"a/{entity_path.name}",
                            tofile=f"b/{entity_path.name}"
                        ))
                        if diff:
                            _echo(diff)
                    else:
                        write_frontmatter(entity_path, metadata, body)
                        
            elif op.op == "update_properties":
                entity_id = resolve_ref(op.id)
                entity_path = get_entity_path(conn, root, entity_id, created_paths)
                metadata, body = read_frontmatter(entity_path)
                
                yaml_old = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
                old_text = f"---\n{yaml_old}\n---\n\n{body.strip()}\n"
                
                props = metadata.setdefault("properties", {})
                needs_set = {}
                for k, v in op.set.items():
                    if k not in props or props[k] != v:
                        needs_set[k] = v
                needs_unset = [k for k in op.unset if k in props]
                
                if not needs_set and not needs_unset:
                    op_result = "noop"
                    if not execute:
                        _echo(f"update_properties: {entity_path.relative_to(root)} [noop]")
                else:
                    op_result = "applied"
                    for k, v in op.set.items():
                        props[k] = v
                    for k in op.unset:
                        if k in props:
                            props.pop(k)
                    metadata["review_status"] = "proposed"
                    
                    yaml_new = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
                    new_text = f"---\n{yaml_new}\n---\n\n{body.strip()}\n"
                    
                    if not execute:
                        _echo(f"update_properties: {entity_path.relative_to(root)} [applied]")
                        diff = "".join(difflib.unified_diff(
                            old_text.splitlines(keepends=True),
                            new_text.splitlines(keepends=True),
                            fromfile=f"a/{entity_path.name}",
                            tofile=f"b/{entity_path.name}"
                        ))
                        if diff:
                            _echo(diff)
                    else:
                        write_frontmatter(entity_path, metadata, body)
                        
            elif op.op == "merge":
                keep_id = resolve_ref(op.keep)
                merge_id = resolve_ref(op.merge)
                
                # Check noop
                is_noop = False
                try:
                    get_entity_path(conn, root, merge_id, created_paths)
                except ValueError:
                    archive_dir = root / "entities" / "archive"
                    if archive_dir.exists():
                        for path in archive_dir.glob("*.md"):
                            try:
                                meta, _ = read_frontmatter(path)
                                if meta.get("id") == merge_id and meta.get("merged_into") == keep_id:
                                    is_noop = True
                                    break
                            except Exception:
                                continue
                                
                if is_noop:
                    op_result = "noop"
                    if not execute:
                        _echo(f"merge: {keep_id} <- {merge_id} [noop]")
                else:
                    op_result = "applied"
                    if not execute:
                        # Dry run for merge just reports action
                        _echo(f"merge: {keep_id} <- {merge_id} [applied]")
                    else:
                        merge_entities(keep_id, merge_id, vault_path=root)

            elif op.op == "archive_entity":
                target_id = resolve_ref(op.id)

                # Noop check mirrors merge's: already archived == a tombstone in
                # entities/archive/ carrying this id and the `archived` marker.
                is_noop = False
                try:
                    get_entity_path(conn, root, target_id, created_paths)
                except ValueError:
                    archive_dir = root / "entities" / "archive"
                    if archive_dir.exists():
                        for path in archive_dir.glob("*.md"):
                            try:
                                meta, _ = read_frontmatter(path)
                                if meta.get("id") == target_id and meta.get("archived"):
                                    is_noop = True
                                    break
                            except Exception:
                                continue

                if is_noop:
                    op_result = "noop"
                    if not execute:
                        _echo(f"archive_entity: {target_id} [noop]")
                else:
                    op_result = "applied"
                    if not execute:
                        entity_path = get_entity_path(conn, root, target_id, created_paths)
                        # Dangling-edge policy (documented in docs/INBOX-RUNBOOK.md
                        # and DIRECTION/G4): the SAFER default is to leave any
                        # relation that references the archived entity in place,
                        # rather than silently pruning it. A dangling reference
                        # then shows up as a visible `synapse check` issue (an
                        # edge pointing at an id missing from the live index) that
                        # a human reviews before deciding to clean it up — pruning
                        # here would destroy edge-level evidence (properties,
                        # source) before anyone confirmed the archival was right.
                        dangling = conn.execute(
                            "SELECT r.from_id, r.to_id, r.type, "
                            "ef.name AS from_name, et.name AS to_name "
                            "FROM relations r "
                            "JOIN entities ef ON ef.id = r.from_id "
                            "JOIN entities et ON et.id = r.to_id "
                            "WHERE r.from_id = ? OR r.to_id = ?",
                            (target_id, target_id),
                        ).fetchall()
                        _echo(
                            f"archive_entity: {entity_path.relative_to(root)} "
                            "-> entities/archive/ [applied]"
                        )
                        _echo(f"  reason: {op.reason}")
                        if dangling:
                            _echo(
                                f"  {len(dangling)} edge(s) will dangle (left in "
                                "place; `synapse check` will surface them):"
                            )
                            for edge in dangling:
                                _echo(
                                    f"    {edge['from_name']} ({edge['from_id']}) "
                                    f"—{edge['type']}→ "
                                    f"{edge['to_name']} ({edge['to_id']})"
                                )
                        else:
                            _echo("  0 edges reference this entity; nothing will dangle")
                    else:
                        archive_entity_impl(target_id, op.reason, vault_path=root)

            elif op.op == "recommend_verify":
                op_result = "skipped:verify"
                skipped_verify_count += 1
                target_id = resolve_ref(op.id)
                if not execute:
                    _echo(f"recommend_verify: {target_id} [skipped:verify]")
                if allow_verify:
                    _echo(f"Suggested verify command: synapse verify {target_id} --vault {root}")
                    
            op_results.append(op_result)

        if skipped_verify_count > 0 and not allow_verify:
            _echo(f"note: {skipped_verify_count} recommend_verify advisory(ies) skipped; re-run with --allow-verify to see suggested commands.")

        if execute:
            # Reindex, check vault, archive proposal
            reindex(root, full=True)
            check_vault(root)
            
            # Archive the proposal file
            applied_dir = root / "proposals" / "applied"
            applied_dir.mkdir(parents=True, exist_ok=True)
            
            # Read original YAML
            with p_path.open("r", encoding="utf-8") as f:
                orig_data = yaml.safe_load(f) or {}
                
            orig_data["result"] = {
                "applied_at": utc_now(),
                "ops": op_results
            }
            
            archive_path = unique_path(applied_dir / p_path.name)
            with archive_path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(orig_data, f, sort_keys=False, allow_unicode=True)
                
            p_path.unlink()
            
            # Applying data does not grant authority to inspect or mutate vault Git.
            _echo(f"Applied proposal: {op_results.count('applied')} change(s), "
                  f"{op_results.count('noop')} already present.")
                
        return {"success": True, "results": op_results}
        
    finally:
        conn.close()
