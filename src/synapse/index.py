"""Disposable SQLite index for canonical Markdown."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import pysqlite3 as sqlite3
except Exception:  # pragma: no cover - fallback for environments with FTS5 stdlib sqlite.
    import sqlite3  # type: ignore[no-redef]

from synapse.config import db_path, resolve_vault
from synapse.models import RELATION_TYPES, Entity, Issue, ReindexResult, Relation
from synapse.parser import parse_vault
from synapse.util import normalize_name, stable_json, utc_now

SCHEMA_VERSION = "1"


def connect(vault: str | Path | None = None) -> sqlite3.Connection:
    path = db_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def verify_fts5(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_check USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts5_check")
    except sqlite3.Error as exc:
        raise RuntimeError("SQLite FTS5 is unavailable. Install pysqlite3-binary.") from exc


def ensure_schema(conn: sqlite3.Connection) -> None:
    verify_fts5(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT
        );

        CREATE TABLE IF NOT EXISTS entities (
          id TEXT PRIMARY KEY,
          type TEXT NOT NULL,
          name TEXT NOT NULL,
          file_path TEXT NOT NULL,
          frontmatter TEXT,
          body TEXT,
          content_hash TEXT NOT NULL,
          review_status TEXT NOT NULL DEFAULT 'proposed',
          created_at TEXT,
          updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
        CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

        CREATE TABLE IF NOT EXISTS relations (
          id INTEGER PRIMARY KEY,
          from_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
          to_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
          type TEXT NOT NULL,
          weak INTEGER NOT NULL DEFAULT 0,
          properties TEXT,
          source_file TEXT,
          review_status TEXT NOT NULL DEFAULT 'proposed',
          created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_rel_from ON relations(from_id, type);
        CREATE INDEX IF NOT EXISTS idx_rel_to ON relations(to_id, type);

        CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
          name, body, aliases,
          content='entities',
          content_rowid='rowid'
        );

        CREATE TABLE IF NOT EXISTS embeddings (
          entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
          model TEXT NOT NULL,
          dim INTEGER NOT NULL,
          vector BLOB NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()


def reset_index(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS embeddings;
        DROP TABLE IF EXISTS relations;
        DROP TABLE IF EXISTS entities_fts;
        DROP TABLE IF EXISTS entities;
        DROP TABLE IF EXISTS meta;
        """
    )
    ensure_schema(conn)


def _name_index(entities: list[Entity]) -> tuple[dict[str, str], dict[str, list[str]]]:
    single: dict[str, str] = {}
    multi: dict[str, list[str]] = {}
    for entity in entities:
        for label in [entity.name, *entity.aliases]:
            key = normalize_name(label)
            if not key:
                continue
            if key in single and single[key] != entity.id:
                multi.setdefault(key, [single[key]]).append(entity.id)
                single.pop(key, None)
            elif key in multi:
                multi[key].append(entity.id)
            else:
                single[key] = entity.id
    return single, multi


def _relations_for_entities(entities: list[Entity]) -> tuple[list[Relation], list[Issue]]:
    issues: list[Issue] = []
    by_id = {entity.id: entity for entity in entities}
    by_name, ambiguous = _name_index(entities)
    relations: list[Relation] = []
    for entity in entities:
        for spec in entity.relation_specs:
            rel_type = str(spec.get("type", "")).strip()
            target = str(spec.get("target", "")).strip()
            if not rel_type or not target:
                issues.append(
                    Issue("error", "Typed relation requires type and target", entity.file_path)
                )
                continue
            if rel_type not in RELATION_TYPES:
                issues.append(
                    Issue("warning", f"Unknown relation type: {rel_type}", entity.file_path)
                )
            to_id = target if target in by_id else by_name.get(normalize_name(target))
            if not to_id:
                issues.append(
                    Issue("error", f"Unresolved typed relation target: {target}", entity.file_path)
                )
                continue
            relations.append(
                Relation(
                    from_id=entity.id,
                    to_id=to_id,
                    type=rel_type,
                    weak=False,
                    properties=spec.get("properties")
                    if isinstance(spec.get("properties"), dict)
                    else {},
                    source_file=str(
                        spec.get("source")
                        or entity.frontmatter.get("provenance", {}).get("source_file")
                        or ""
                    ),
                    review_status=entity.review_status,
                    created_at=str(spec.get("created_at") or entity.created_at or ""),
                )
            )
        for ref in entity.weak_refs:
            key = normalize_name(ref)
            if key in ambiguous:
                issues.append(Issue("warning", f"Ambiguous weak link: {ref}", entity.file_path))
                continue
            to_id = by_name.get(key)
            if not to_id:
                issues.append(Issue("warning", f"Unresolved weak link: {ref}", entity.file_path))
                continue
            if to_id == entity.id:
                continue
            relations.append(
                Relation(
                    from_id=entity.id,
                    to_id=to_id,
                    type="mentioned_in",
                    weak=True,
                    source_file=str(entity.file_path),
                    review_status=entity.review_status,
                )
            )
    return relations, issues


def _insert_entity(conn: sqlite3.Connection, entity: Entity) -> None:
    conn.execute(
        """
        INSERT INTO entities(
          id, type, name, file_path, frontmatter, body, content_hash,
          review_status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity.id,
            entity.type,
            entity.name,
            str(entity.file_path),
            stable_json(entity.frontmatter),
            entity.body,
            entity.content_hash,
            entity.review_status,
            entity.created_at,
            entity.updated_at,
        ),
    )
    rowid = conn.execute("SELECT rowid FROM entities WHERE id = ?", (entity.id,)).fetchone()[
        "rowid"
    ]
    conn.execute(
        "INSERT INTO entities_fts(rowid, name, body, aliases) VALUES (?, ?, ?, ?)",
        (rowid, entity.name, entity.body, " ".join(entity.aliases)),
    )


def _insert_relation(conn: sqlite3.Connection, relation: Relation) -> None:
    conn.execute(
        """
        INSERT INTO relations(
          from_id, to_id, type, weak, properties, source_file, review_status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            relation.from_id,
            relation.to_id,
            relation.type,
            1 if relation.weak else 0,
            json.dumps(relation.properties, ensure_ascii=False, sort_keys=True),
            relation.source_file,
            relation.review_status,
            relation.created_at,
        ),
    )


def reindex(vault: str | Path | None = None, *, full: bool = False) -> ReindexResult:
    root = resolve_vault(vault)
    conn = connect(root)
    try:
        ensure_schema(conn)
        entities, issues = parse_vault(root)
        relations, relation_issues = _relations_for_entities(entities)
        issues.extend(relation_issues)

        # Correctness-first incremental behavior: skip if hashes match; otherwise rebuild all derived data.
        existing = {
            row["file_path"]: row["content_hash"]
            for row in conn.execute("SELECT file_path, content_hash FROM entities").fetchall()
        }
        incoming = {str(entity.file_path): entity.content_hash for entity in entities}
        changed = full or existing != incoming
        if not changed:
            return ReindexResult(
                entities=len(entities),
                relations=conn.execute("SELECT COUNT(*) AS c FROM relations").fetchone()["c"],
                changed_files=0,
                issues=issues,
            )

        reset_index(conn)
        with conn:
            for entity in entities:
                _insert_entity(conn, entity)
            for relation in relations:
                _insert_relation(conn, relation)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('last_full_index_at', ?)",
                (utc_now(),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('vault_path', ?)", (str(root),)
            )
        return ReindexResult(
            entities=len(entities),
            relations=len(relations),
            changed_files=len(incoming),
            issues=issues,
        )
    finally:
        conn.close()


def row_to_entity(row: sqlite3.Row) -> dict[str, Any]:
    frontmatter = json.loads(row["frontmatter"] or "{}")
    return {
        "id": row["id"],
        "type": row["type"],
        "name": row["name"],
        "file_path": row["file_path"],
        "review_status": row["review_status"],
        "aliases": frontmatter.get("aliases") or [],
        "tags": frontmatter.get("tags") or [],
        "properties": frontmatter.get("properties") or {},
    }


def row_to_relation(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "from_id": row["from_id"],
        "to_id": row["to_id"],
        "type": row["type"],
        "weak": bool(row["weak"]),
        "properties": json.loads(row["properties"] or "{}"),
        "source_file": row["source_file"],
        "review_status": row["review_status"],
    }


def entity_by_id(conn: sqlite3.Connection, entity_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
    return row_to_entity(row) if row else None


def resolve_entity_ref(conn: sqlite3.Connection, ref: str) -> list[dict[str, Any]]:
    direct = entity_by_id(conn, ref)
    if direct:
        return [direct]
    normalized = normalize_name(ref)
    rows = conn.execute("SELECT * FROM entities").fetchall()
    exact = []
    partial = []
    for row in rows:
        entity = row_to_entity(row)
        labels = [entity["name"], *entity.get("aliases", [])]
        if any(normalize_name(label) == normalized for label in labels):
            exact.append(entity)
        elif normalized and any(normalized in normalize_name(label) for label in labels):
            partial.append(entity)
    return exact or partial[:10]


def all_entities(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        row_to_entity(row)
        for row in conn.execute("SELECT * FROM entities ORDER BY name").fetchall()
    ]


def all_relations(conn: sqlite3.Connection, *, include_weak: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM relations"
    params: list[Any] = []
    if not include_weak:
        sql += " WHERE weak = 0"
    sql += " ORDER BY from_id, to_id, type"
    return [row_to_relation(row) for row in conn.execute(sql, params).fetchall()]
