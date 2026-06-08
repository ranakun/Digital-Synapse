"""Pipeline C maintenance and reconciliation."""

from __future__ import annotations

import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from synapse.embeddings import embed_entities as _embed_entities
from synapse.embeddings import nearest_duplicates
from synapse.index import all_relations, connect, reindex
from synapse.models import RELATION_TYPES
from synapse.parser import entity_files
from synapse.util import normalize_name, read_frontmatter, slugify, unique_path, write_frontmatter


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
            "SELECT id, name, frontmatter, review_status FROM entities"
        ).fetchall()
        rels = all_relations(conn, include_weak=True)
        names: dict[str, list[dict[str, str]]] = defaultdict(list)
        proposed = []
        for row in entities:
            names[normalize_name(row["name"])].append({"id": row["id"], "name": row["name"]})
            if row["review_status"] == "proposed":
                proposed.append({"id": row["id"], "name": row["name"]})
        duplicates = [
            items for items in names.values() if len(items) > 1 and normalize_name(items[0]["name"])
        ]
        unknown_relations = [rel for rel in rels if rel["type"] not in RELATION_TYPES]
        related_to = [rel for rel in rels if rel["type"] == "related_to"]
        connected_ids = {
            entity_id
            for rel in rels
            for entity_id in (str(rel["from_id"]), str(rel["to_id"]))
        }
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
            for issue in reindex_result.issues
        ],
        "duplicates": duplicates,
        "semantic_duplicates": semantic_duplicates,
        "unknown_relation_types": unknown_relations,
        "related_to_edges": related_to,
        "orphans": orphans,
        "proposed": proposed,
        "summary": {
            "entities": len(entities),
            "relations": len(rels),
            "issue_count": len(reindex_result.issues),
        },
    }


def _entity_path_by_id(vault: Path, entity_id: str) -> Path:
    for path in entity_files(vault):
        metadata, _ = read_frontmatter(path)
        if metadata.get("id") == entity_id:
            return path
    raise ValueError(f"Unknown entity id: {entity_id}")


def merge_entities(
    keep_id: str,
    merge_id: str,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
) -> dict[str, Any]:
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
    keep_meta.setdefault("properties", {}).update(merge_meta.get("properties") or {})
    keep_meta.setdefault("relations", [])
    for rel in merge_meta.get("relations") or []:
        if isinstance(rel, dict):
            keep_meta["relations"].append(rel)
    keep_meta["review_status"] = "proposed"
    keep_body = f"{keep_body.rstrip()}\n\n## Merged Notes From {merge_meta.get('name')}\n\n{merge_body.strip()}\n"
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
