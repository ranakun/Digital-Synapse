"""Pipeline A: ingestion and human verification gate."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from synapse.config import load_config, resolve_vault
from synapse.index import connect, reindex, resolve_entity_ref
from synapse.parser import entity_files
from synapse.providers import CompletionRequest, Generator, OpenAICompatibleGenerator
from synapse.util import (
    generate_ulid,
    normalize_name,
    read_frontmatter,
    slugify,
    unique_path,
    utc_now,
    write_frontmatter,
)

_CONTEXT_STOP_WORDS = {
    "about",
    "after",
    "also",
    "and",
    "any",
    "are",
    "but",
    "can",
    "for",
    "from",
    "has",
    "have",
    "into",
    "not",
    "our",
    "that",
    "the",
    "their",
    "this",
    "was",
    "with",
    "you",
}

def _source_terms(text: str, limit: int = 20) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", text):
        term = token.casefold()
        if term in _CONTEXT_STOP_WORDS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def _fts_candidate_ids(conn, source_text: str, limit: int) -> list[str]:
    terms = _source_terms(source_text)
    if not terms:
        return []
    query = " OR ".join(f'"{term.replace(chr(34), chr(34) + chr(34))}"' for term in terms)
    try:
        rows = conn.execute(
            """
            SELECT e.id
            FROM entities_fts f
            JOIN entities e ON e.rowid = f.rowid
            WHERE entities_fts MATCH ?
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
    except Exception:
        return []
    return [str(row["id"]) for row in rows]


def candidate_context(
    vault: Path, source_text: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    conn = connect(vault)
    try:
        rows = conn.execute("SELECT id, type, name, frontmatter FROM entities ORDER BY name").fetchall()
        normalized_source = normalize_name(source_text or "")
        fts_ids = _fts_candidate_ids(conn, source_text or "", limit * 4) if source_text else []
        fts_scores = {entity_id: max(limit * 4 - index, 1) for index, entity_id in enumerate(fts_ids)}

        ranked: list[tuple[int, str, dict[str, Any]]] = []
        for row in rows:
            frontmatter = json.loads(row["frontmatter"] or "{}")
            aliases = frontmatter.get("aliases") or []
            if not isinstance(aliases, list):
                aliases = []
            labels = [str(row["name"]), *[str(alias) for alias in aliases]]
            score = fts_scores.get(str(row["id"]), 0)
            if normalized_source:
                for label in labels:
                    normalized_label = normalize_name(label)
                    if normalized_label and normalized_label in normalized_source:
                        score += 1000 + len(normalized_label)
            candidate = {
                "id": row["id"],
                "type": row["type"],
                "name": row["name"],
                "aliases": aliases,
            }
            ranked.append((score, normalize_name(str(row["name"])), candidate))
        ranked.sort(key=lambda item: (-item[0], item[1], item[2]["id"]))
        if source_text and any(score for score, _, _ in ranked):
            return [candidate for score, _, candidate in ranked if score > 0][:limit]
        return [candidate for _, _, candidate in ranked[:limit]]
    finally:
        conn.close()


def extraction_prompt(text: str, format_hint: str, candidates: list[dict[str, Any]]) -> str:
    return f"""
Extract Digital Synapse entity changes from this source.

Return only JSON:
{{
  "changeset": [
    {{
      "op": "create | update",
      "type": "person | company | project | goal | finance",
      "matched_existing": "entity-id | null",
      "name": "string",
      "confidence": 0.0,
      "properties": {{}},
      "relations": [{{"type": "works_at", "target": "entity-id-or-name", "properties": {{}}}}],
      "body_append": "string | null"
    }}
  ],
  "ambiguities": ["notes for reviewer"]
}}

Existing candidate entities:
{json.dumps(candidates, ensure_ascii=False)}

Format: {format_hint}

Source:
{text}
""".strip()


def validate_changeset(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    changes = data.get("changeset")
    if not isinstance(changes, list):
        raise ValueError("changeset must be a list")
    ambiguities = data.get("ambiguities") or []
    if not isinstance(ambiguities, list):
        ambiguities = [str(ambiguities)]
    normalized: list[dict[str, Any]] = []
    for item in changes:
        if not isinstance(item, dict):
            continue
        op = item.get("op")
        entity_type = item.get("type")
        name = item.get("name")
        if op not in {"create", "update"} or not entity_type or not name:
            raise ValueError("Each changeset item requires op, type, and name")
        normalized.append(
            {
                "op": op,
                "type": str(entity_type),
                "matched_existing": item.get("matched_existing"),
                "name": str(name),
                "confidence": float(item.get("confidence") or 0),
                "properties": item.get("properties")
                if isinstance(item.get("properties"), dict)
                else {},
                "relations": item.get("relations")
                if isinstance(item.get("relations"), list)
                else [],
                "body_append": item.get("body_append"),
            }
        )
    return normalized, [str(item) for item in ambiguities]


def _entity_dir(vault: Path, entity_type: str) -> Path:
    folder = {
        "person": "people",
        "company": "companies",
        "project": "projects",
        "goal": "goals",
        "finance": "finance",
    }.get(entity_type, entity_type)
    return vault / "entities" / folder


def _resolve_target(vault: Path, target: str) -> str | None:
    conn = connect(vault)
    try:
        matches = resolve_entity_ref(conn, target)
        if len(matches) == 1:
            return matches[0]["id"]
        return None
    finally:
        conn.close()


def _materialize_create(
    vault: Path, change: dict[str, Any], source_path: Path, model: str
) -> dict[str, Any]:
    now = utc_now()
    entity_id = generate_ulid()
    relations = []
    ambiguities = []
    for rel in change["relations"]:
        if not isinstance(rel, dict):
            continue
        target = str(rel.get("target") or "").strip()
        to_id = _resolve_target(vault, target) if target else None
        if not to_id:
            ambiguities.append(f"Unresolved relation target for {change['name']}: {target}")
            continue
        relations.append(
            {
                "type": str(rel.get("type") or "related_to"),
                "target": to_id,
                "properties": rel.get("properties")
                if isinstance(rel.get("properties"), dict)
                else {},
                "source": source_path.name,
            }
        )
    metadata = {
        "id": entity_id,
        "type": change["type"],
        "name": change["name"],
        "aliases": [],
        "review_status": "proposed",
        "tags": ["low-confidence"] if change["confidence"] < 0.6 else [],
        "relations": relations,
        "properties": change["properties"],
        "created_at": now,
        "updated_at": now,
        "provenance": {
            "source_file": source_path.name,
            "extracted_by": model,
            "extracted_at": now,
        },
    }
    body = f"# {change['name']}\n\n{change.get('body_append') or ''}".strip()
    path = unique_path(_entity_dir(vault, change["type"]) / f"{slugify(change['name'])}.md")
    write_frontmatter(path, metadata, body)
    return {"id": entity_id, "file_path": str(path.relative_to(vault)), "ambiguities": ambiguities}


def _materialize_update(
    vault: Path, change: dict[str, Any], source_path: Path, model: str
) -> dict[str, Any]:
    target_id = change.get("matched_existing") or _resolve_target(vault, change["name"])
    if not target_id:
        return _materialize_create(vault, change, source_path, model)
    conn = connect(vault)
    try:
        row = conn.execute("SELECT file_path FROM entities WHERE id = ?", (target_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return _materialize_create(vault, change, source_path, model)
    path = vault / row["file_path"]
    metadata, body = read_frontmatter(path)
    metadata["review_status"] = "proposed"
    metadata["updated_at"] = utc_now()
    metadata.setdefault("properties", {}).update(change["properties"])
    metadata.setdefault("provenance", {}).update(
        {"source_file": source_path.name, "extracted_by": model, "extracted_at": utc_now()}
    )
    metadata.setdefault("relations", [])
    ambiguities = []
    for rel in change["relations"]:
        if not isinstance(rel, dict):
            continue
        target = str(rel.get("target") or "").strip()
        to_id = _resolve_target(vault, target) if target else None
        if not to_id:
            ambiguities.append(f"Unresolved relation target for {change['name']}: {target}")
            continue
        metadata["relations"].append(
            {
                "type": str(rel.get("type") or "related_to"),
                "target": to_id,
                "properties": rel.get("properties")
                if isinstance(rel.get("properties"), dict)
                else {},
                "source": source_path.name,
            }
        )
    append = change.get("body_append")
    if append:
        body = f"{body.rstrip()}\n\n{append.strip()}\n"
    write_frontmatter(path, metadata, body)
    return {"id": target_id, "file_path": str(path.relative_to(vault)), "ambiguities": ambiguities}


def ingest_file(
    source: str | Path,
    *,
    vault: str | Path | None = None,
    vault_path: str | Path | None = None,
    generator: Generator | None = None,
    provider: Generator | None = None,
) -> dict[str, Any]:
    root = resolve_vault(vault_path if vault_path is not None else vault)
    reindex(root)
    from synapse.extractors import extract_text

    source_path = Path(source).resolve()
    extracted = extract_text(source_path)
    if not extracted.text.strip():
        return {
            "written": [],
            "ambiguities": ["no extractable entities"],
            "source": str(source_path),
        }

    gen = generator or provider or OpenAICompatibleGenerator(root)
    result = gen.complete(
        CompletionRequest(
            task="ingest",
            schema_name="changeset",
            prompt=extraction_prompt(
                extracted.text, extracted.format_hint, candidate_context(root, extracted.text)
            ),
        )
    )
    if isinstance(result, dict):
        result_data = result
        result_model = "fake-provider"
    else:
        result_data = result.data
        result_model = result.model
    changes, ambiguities = validate_changeset(result_data)
    written = []
    for change in changes:
        if change["op"] == "update" or change.get("matched_existing"):
            outcome = _materialize_update(root, change, source_path, result_model)
        else:
            outcome = _materialize_create(root, change, source_path, result_model)
        written.append(outcome)
        ambiguities.extend(outcome["ambiguities"])
    reindex(root, full=True)
    return {"written": written, "ambiguities": ambiguities, "source": str(source_path)}


def proposed_entities(vault: str | Path | None = None) -> list[dict[str, Any]]:
    root = resolve_vault(vault)
    results = []
    for path in entity_files(root):
        metadata, _ = read_frontmatter(path)
        if metadata.get("review_status") == "proposed":
            results.append(
                {
                    "id": metadata.get("id"),
                    "name": metadata.get("name"),
                    "file_path": str(path.relative_to(root)),
                }
            )
    return results


def commit_proposed(vault: str | Path | None = None, message: str | None = None) -> dict[str, Any]:
    root = resolve_vault(vault)
    changed = []
    for path in entity_files(root):
        metadata, body = read_frontmatter(path)
        if metadata.get("review_status") == "proposed":
            metadata["review_status"] = "verified"
            for rel in metadata.get("relations") or []:
                if isinstance(rel, dict) and rel.get("review_status") == "proposed":
                    rel["review_status"] = "verified"
            write_frontmatter(path, metadata, body)
            changed.append(str(path.relative_to(root)))
    cfg = load_config(root)
    inbox = root / cfg["ingestion"]["inbox_dir"]
    processed = root / cfg["ingestion"]["processed_dir"]
    processed.mkdir(parents=True, exist_ok=True)
    moved = []
    if inbox.exists():
        for path in inbox.iterdir():
            if path.is_file():
                target = processed / path.name
                if target.exists():
                    target = processed / f"{path.stem}-{utc_now().replace(':', '')}{path.suffix}"
                shutil.move(str(path), str(target))
                moved.append(str(target.relative_to(root)))
    reindex(root, full=True)
    if not (root / ".git").exists():
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
    commit_message = message or "Update Digital Synapse vault"
    commit = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Digital Synapse",
            "-c",
            "user.email=synapse@example.local",
            "commit",
            "-m",
            commit_message,
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return {
        "verified": changed,
        "moved_sources": moved,
        "committed": commit.returncode == 0,
        "git_output": (commit.stdout + commit.stderr).strip(),
    }


def commit_vault(
    vault: str | Path | None = None,
    *,
    vault_path: str | Path | None = None,
    message: str | None = None,
    msg: str | None = None,
) -> dict[str, Any]:
    return commit_proposed(vault_path if vault_path is not None else vault, message or msg)


commit = commit_vault
finalize_commit = commit_vault
ingest = ingest_file
run_ingest = ingest_file
