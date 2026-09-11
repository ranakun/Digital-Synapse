"""Pipeline C maintenance and reconciliation."""

from __future__ import annotations

import json
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from synapse.embeddings import embed_entities as _embed_entities
from synapse.embeddings import nearest_duplicates
from synapse.index import all_relations, connect, reindex, resolve_entity_ref
from synapse.models import RELATION_TYPES
from synapse.owner_context import load_knowledge_entities, validate_knowledge_graph
from synapse.parser import entity_files
from synapse.util import (
    norm_url,
    normalize_name,
    read_frontmatter,
    slugify,
    unique_path,
    utc_now,
    write_frontmatter,
)


def _all_url_keyed_people(items: list[dict[str, str]]) -> bool:
    if not items or any(item.get("type") != "person" for item in items):
        return False
    urls = [norm_url(item.get("linkedin_url", "")) for item in items]
    return all(urls) and len(set(urls)) == len(urls)


def check_vault(
    vault: str | Path | None = None,
    *,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(vault_path if vault_path is not None else (vault or ".")).resolve()
    reindex_result = reindex(root)
    conn = connect(root)
    try:
        entities = conn.execute(
            "SELECT id, type, name, frontmatter, review_status FROM entities"
        ).fetchall()
        rels = all_relations(conn, include_weak=True)
        knowledge_issues = validate_knowledge_graph(load_knowledge_entities(conn))
        # Group by normalized name (for cross-type collision detection) and by
        # (type, normalized name) (for true duplicate detection). Two entities
        # that share a name but differ in type — e.g. the Ethereum *company* and
        # the Ethereum *skill* — are NOT duplicates; they are a name collision to
        # check weak-link resolution against. Grouping duplicates by name alone
        # (the old behaviour) false-positived that pair and sent a worker toward a
        # cross-type poison merge. The scorecard already groups by (type, name);
        # this matches it.
        names: dict[str, list[dict[str, str]]] = defaultdict(list)
        by_type_name: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
        proposed = []
        identity_unresolved_count = 0
        for row in entities:
            frontmatter = json.loads(row["frontmatter"] or "{}")
            properties = frontmatter.get("properties") or {}
            if not isinstance(properties, dict):
                properties = {}
            norm = normalize_name(row["name"])
            item = {
                "id": row["id"],
                "name": row["name"],
                "type": row["type"],
                "linkedin_url": str(properties.get("linkedin_url") or ""),
            }
            names[norm].append(item)
            by_type_name[(row["type"], norm)].append(item)
            if row["review_status"] == "proposed":
                proposed.append({"id": row["id"], "name": row["name"]})
            # Count entities tagged identity-unresolved
            tags = [str(t).casefold() for t in (frontmatter.get("tags") or [])]
            if "identity-unresolved" in tags:
                identity_unresolved_count += 1
        duplicates = [
            [{"id": item["id"], "name": item["name"]} for item in items]
            for (_etype, norm), items in by_type_name.items()
            if len(items) > 1 and norm and not _all_url_keyed_people(items)
        ]
        # Softer signal: same name, different types. Not a duplicate — flagged so
        # the owner can confirm any `[[weak link]]` to that name resolves to the
        # intended type (see FIX-09: `[[ethereum]]` → the company, not the skill).
        cross_type_name_collisions = [
            {
                "name": items[0]["name"],
                "types": sorted({item["type"] for item in items}),
                "entities": [
                    {"id": item["id"], "name": item["name"], "type": item["type"]}
                    for item in items
                ],
            }
            for norm, items in names.items()
            if norm and len({item["type"] for item in items}) > 1
        ]
        unknown_relations = [rel for rel in rels if rel["type"] not in RELATION_TYPES]
        related_to = [rel for rel in rels if rel["type"] == "related_to"]
        connected_ids = {
            entity_id
            for rel in rels
            for entity_id in (str(rel["from_id"]), str(rel["to_id"]))
        }
        oversized = []
        for path in entity_files(root):
            try:
                metadata, _ = read_frontmatter(path)
            except Exception:
                continue
            relations_list = metadata.get("relations") or []
            if not isinstance(relations_list, list):
                relations_list = []
            count = 0
            for r in relations_list:
                if isinstance(r, dict):
                    t = r.get("type")
                    if t not in ("works_at", "former_employee_of", "attended", "demonstrates_skill"):
                        count += 1
            if count > 75:
                oversized.append({
                    "id": str(metadata.get("id") or ""),
                    "name": str(metadata.get("name") or path.stem),
                    "count": count
                })

        orphans = [
            {"id": row["id"], "name": row["name"]}
            for row in entities
            if str(row["id"]) not in connected_ids
        ]
    finally:
        conn.close()
    try:
        semantic_duplicates = nearest_duplicates(root)
    except Exception:
        semantic_duplicates = []
    return {
        "issues": [
            issue.__dict__ | {"file_path": str(issue.file_path) if issue.file_path else None}
            for issue in [*reindex_result.issues, *knowledge_issues]
        ],
        "duplicates": duplicates,
        "cross_type_name_collisions": cross_type_name_collisions,
        "semantic_duplicates": semantic_duplicates,
        "unknown_relation_types": unknown_relations,
        "related_to_edges": related_to,
        "orphans": orphans,
        "proposed": proposed,
        "oversized_relation_files": oversized,
        "identity_unresolved_count": identity_unresolved_count,
        "summary": {
            "entities": len(entities),
            "relations": len(rels),
            "issue_count": len(reindex_result.issues) + len(knowledge_issues),
            "identity_unresolved": identity_unresolved_count,
        },
    }


def _entity_path_by_id(vault: Path, entity_id: str) -> Path:
    for path in entity_files(vault):
        metadata, _ = read_frontmatter(path)
        if metadata.get("id") == entity_id:
            return path
    raise ValueError(f"Unknown entity id: {entity_id}")


def _retarget_wikilinks(body: str, old_slug: str, new_slug: str) -> str:
    """Retarget exact wikilink destinations while preserving labels and headings."""
    return re.sub(
        rf"(?<=\[\[){re.escape(old_slug)}(?=(?:[#|]|\]\]))",
        new_slug,
        body,
        flags=re.IGNORECASE,
    )


def merge_entities(
    keep_id: str,
    merge_id: str,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault or vault_path, "merge_entities")
    root = Path(vault_path if vault_path is not None else (vault or ".")).resolve()
    keep_path = _entity_path_by_id(root, keep_id)
    merge_path = _entity_path_by_id(root, merge_id)
    keep_meta, keep_body = read_frontmatter(keep_path)
    merge_meta, merge_body = read_frontmatter(merge_path)

    aliases = list(
        dict.fromkeys(
            [
                *(keep_meta.get("aliases") or []),
                merge_meta.get("name"),
                *(merge_meta.get("aliases") or []),
            ]
        )
    )
    keep_meta["aliases"] = [alias for alias in aliases if alias and alias != keep_meta.get("name")]
    
    keep_props = keep_meta.setdefault("properties", {})
    if not isinstance(keep_props, dict):
        keep_props = {}
        keep_meta["properties"] = keep_props
        
    merge_props = merge_meta.get("properties") or {}
    if not isinstance(merge_props, dict):
        merge_props = {}
        
    differing_collisions = {}
    
    # Check linkedin_url collision
    keep_url = keep_props.get("linkedin_url")
    merge_url = merge_props.get("linkedin_url")
    if keep_url and merge_url:
        alt_list = keep_props.get("linkedin_urls_alt")
        if not isinstance(alt_list, list):
            alt_list = [alt_list] if alt_list else []
        if merge_url != keep_url and merge_url not in alt_list:
            alt_list.append(merge_url)
        if alt_list:
            keep_props["linkedin_urls_alt"] = alt_list
    elif merge_url and not keep_url:
        keep_props["linkedin_url"] = merge_url

    # Check other property collisions (keep wins)
    for key, merge_val in merge_props.items():
        if key == "linkedin_url":
            continue
        if key in keep_props:
            keep_val = keep_props[key]
            if keep_val != merge_val:
                differing_collisions[key] = merge_val
        else:
            keep_props[key] = merge_val

    keep_meta.setdefault("relations", [])
    for rel in merge_meta.get("relations") or []:
        if isinstance(rel, dict):
            keep_meta["relations"].append(rel)
            
    keep_meta["review_status"] = "proposed"
    
    collision_section = ""
    if differing_collisions:
        collision_section = "Collisions:\n" + "\n".join(
            f"  {k}: {v}" for k, v in sorted(differing_collisions.items())
        ) + "\n\n"
        
    keep_body = f"{keep_body.rstrip()}\n\n## Merged Notes From {merge_meta.get('name')}\n\n{collision_section}{merge_body.strip()}\n"
    write_frontmatter(keep_path, keep_meta, keep_body)

    rewritten = []
    for path in entity_files(root):
        if path == merge_path:
            continue
        metadata, body = read_frontmatter(path)
        touched = False
        for rel in metadata.get("relations") or []:
            if isinstance(rel, dict) and rel.get("target") == merge_id:
                rel["target"] = keep_id
                touched = True
        rewritten_body = _retarget_wikilinks(body, merge_path.stem, keep_path.stem)
        if rewritten_body != body:
            body = rewritten_body
            touched = True
        if touched:
            metadata["review_status"] = "proposed"
            write_frontmatter(path, metadata, body)
            rewritten.append(str(path.relative_to(root)))

    archive_dir = root / "entities" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    merge_meta["review_status"] = "proposed"
    merge_meta["merged_into"] = keep_id
    write_frontmatter(merge_path, merge_meta, merge_body)
    archived_path = unique_path(archive_dir / f"{slugify(merge_meta.get('name') or merge_id)}.md")
    shutil.move(str(merge_path), str(archived_path))
    reindex(root, full=True)
    return {
        "kept": str(keep_path.relative_to(root)),
        "archived": str(archived_path.relative_to(root)),
        "rewritten": rewritten,
    }


def archive_entity(
    entity_id: str,
    reason: str,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
    """Move an entity file to entities/archive/ — a recoverable tombstone, not
    a hard delete. Mirrors merge_entities' archival step exactly (locate by id,
    write tombstone frontmatter, `unique_path` + `shutil.move` into
    entities/archive/, full reindex) so QA has one reviewed removal mechanism
    instead of two.

    Dangling-edge policy (see docs/INBOX-RUNBOOK.md): unlike `merge_entities`,
    this does NOT rewrite other files' relations that target `entity_id` — they
    are deliberately left in place. This is the SAFER default: `synapse check`
    already reports relations pointing at ids missing from the live index, so a
    stale reference becomes a visible, reviewable issue instead of being pruned
    (and its edge-level evidence — properties, source — destroyed) before a
    human confirms the archival was correct.
    """
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault or vault_path, "archive_entity")
    if entity_id == "me":
        raise ValueError("Refusing to archive 'me' — the owner entity can never be archived")

    root = Path(vault_path if vault_path is not None else (vault or ".")).resolve()
    entity_path = _entity_path_by_id(root, entity_id)
    metadata, body = read_frontmatter(entity_path)

    metadata["review_status"] = "proposed"
    metadata["archived"] = True
    metadata["archived_reason"] = reason
    metadata["archived_at"] = utc_now()
    write_frontmatter(entity_path, metadata, body)

    archive_dir = root / "entities" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived_path = unique_path(archive_dir / f"{slugify(metadata.get('name') or entity_id)}.md")
    shutil.move(str(entity_path), str(archived_path))
    reindex(root, full=True)
    return {
        "id": entity_id,
        "archived": str(archived_path.relative_to(root)),
        "reason": reason,
    }


def embed_vault(
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
    embedder=None,
    all: bool = False,  # noqa: A002
    all_: bool = False,
) -> dict[str, Any]:
    _ = all, all_
    result = _embed_entities(vault_path if vault_path is not None else vault, embedder=embedder)
    return {"embeddings": result}


check = check_vault
run_check = check_vault
merge = merge_entities
run_merge = merge_entities
embed = embed_vault
run_archive = archive_entity


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


class AmbiguousRefError(ValueError):
    """Raised when a ref resolves to more than one entity."""

    def __init__(self, ref: str, candidates: list[dict[str, Any]]) -> None:
        self.ref = ref
        self.candidates = candidates
        super().__init__(f"Ambiguous ref '{ref}': {len(candidates)} candidates")


def _collect_by_batch(
    conn,
    *,
    type_filter: str | None,
    tag_filter: str | None,
) -> list[dict[str, Any]]:
    """Return entities matching type and/or tag batch filters."""
    rows = conn.execute("SELECT * FROM entities").fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        if type_filter and row["type"] != type_filter:
            continue
        if tag_filter:
            fm = json.loads(row["frontmatter"] or "{}")
            tags = [str(t).casefold() for t in (fm.get("tags") or [])]
            if tag_filter.casefold() not in tags:
                continue
        results.append({"id": row["id"], "name": row["name"], "file_path": row["file_path"]})
    return results


def _set_verified(vault_root: Path, entity: dict[str, Any]) -> bool:
    """Write review_status: verified + updated_at to the entity file. Returns True if changed."""
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault_root, "_set_verified")
    fp = Path(entity["file_path"])
    if not fp.is_absolute():
        fp = vault_root / fp
    if not fp.exists():
        return False
    metadata, body = read_frontmatter(fp)
    if metadata.get("review_status") == "verified":
        return False
    metadata["review_status"] = "verified"
    metadata["updated_at"] = utc_now()
    write_frontmatter(fp, metadata, body)
    return True


def _pending_recommend_verify(vault_root: Path) -> list[dict[str, Any]]:
    """Glob proposals/applied/*.yaml for recommend_verify advisories. Tolerates missing dir."""
    applied_dir = vault_root / "proposals" / "applied"
    if not applied_dir.exists():
        return []
    advisories: list[dict[str, Any]] = []
    for path in sorted(applied_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                continue
            if data.get("recommend_verify"):
                advisories.append(
                    {
                        "file": str(path.relative_to(vault_root)),
                        "recommend_verify": data["recommend_verify"],
                    }
                )
        except Exception:
            continue
    return advisories


def verify_entities(
    refs: list[str],
    vault: str | Path | None = None,
    *,
    type_filter: str | None = None,
    tag_filter: str | None = None,
    yes: bool = False,
) -> dict[str, Any]:
    """Set review_status: verified on resolved entities.

    - Refs resolved via resolve_entity_ref; ambiguous ref raises AmbiguousRefError.
    - With type_filter or tag_filter, collects all matching entities (batch mode).
      Batch mode requires yes=True or it returns the candidate list without writing.
    - Returns {verified: [...], already_verified: [...], not_found: [...], requires_yes: bool}.
    """
    from synapse.legacy_guard import guard_legacy
    guard_legacy(vault, "verify_entities")
    root = Path(vault or ".").resolve()
    reindex(root)
    conn = connect(root)
    try:
        # Resolve explicit refs
        targets: list[dict[str, Any]] = []
        not_found: list[str] = []
        for ref in refs:
            matches = resolve_entity_ref(conn, ref)
            if not matches:
                not_found.append(ref)
            elif len(matches) > 1:
                conn.close()
                raise AmbiguousRefError(ref, matches)
            else:
                row = conn.execute(
                    "SELECT id, name, file_path FROM entities WHERE id = ?", (matches[0]["id"],)
                ).fetchone()
                if row:
                    targets.append({"id": row["id"], "name": row["name"], "file_path": row["file_path"]})

        # Batch filter
        if type_filter or tag_filter:
            batch = _collect_by_batch(conn, type_filter=type_filter, tag_filter=tag_filter)
            seen_ids = {t["id"] for t in targets}
            for entity in batch:
                if entity["id"] not in seen_ids:
                    targets.append(entity)
                    seen_ids.add(entity["id"])

        if not targets and not not_found:
            return {"verified": [], "already_verified": [], "not_found": [], "requires_yes": False}

        # Batch requires --yes
        is_batch = bool(type_filter or tag_filter) or len(refs) > 1
        if is_batch and not yes:
            return {
                "verified": [],
                "already_verified": [],
                "not_found": not_found,
                "requires_yes": True,
                "candidates": [{"id": t["id"], "name": t["name"]} for t in targets],
            }

        verified: list[dict[str, Any]] = []
        already_verified: list[dict[str, Any]] = []
        for entity in targets:
            changed = _set_verified(root, entity)
            entry = {"id": entity["id"], "name": entity["name"]}
            if changed:
                verified.append(entry)
            else:
                already_verified.append(entry)

        return {
            "verified": verified,
            "already_verified": already_verified,
            "not_found": not_found,
            "requires_yes": False,
        }
    finally:
        conn.close()


def verify_report(vault: str | Path | None = None) -> dict[str, Any]:
    """Return counts by type and pending recommend_verify advisories."""
    root = Path(vault or ".").resolve()
    reindex(root)
    conn = connect(root)
    try:
        rows = conn.execute(
            "SELECT type, review_status, COUNT(*) AS c FROM entities GROUP BY type, review_status"
        ).fetchall()
    finally:
        conn.close()

    by_type: dict[str, dict[str, int]] = {}
    for row in rows:
        etype = row["type"]
        status = row["review_status"]
        by_type.setdefault(etype, {"verified": 0, "proposed": 0})
        by_type[etype][status] = row["c"]

    advisories = _pending_recommend_verify(root)

    return {
        "counts_by_type": by_type,
        "pending_recommend_verify": advisories,
    }
