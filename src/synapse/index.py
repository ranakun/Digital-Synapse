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
from synapse.parser import entity_files, parse_vault
from synapse.util import normalize_name, sha256_file, stable_json, utc_now

SCHEMA_VERSION = "1"


def connect(
    vault: str | Path | None = None,
    *,
    check_same_thread: bool = True,
    _skip_v2_refresh: bool = False,
) -> sqlite3.Connection:
    if not _skip_v2_refresh:
        from synapse.revisions import is_v2
        root = resolve_vault(vault)
        if is_v2(root):
            from synapse.v2_compat import reindex_v2
            reindex_v2(root)
    path = db_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
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
          entity_id TEXT PRIMARY KEY,
          model TEXT NOT NULL,
          dim INTEGER NOT NULL,
          vector BLOB NOT NULL
        );

        CREATE TABLE IF NOT EXISTS files (
          path TEXT PRIMARY KEY,
          mtime_ns INTEGER NOT NULL,
          size INTEGER NOT NULL,
          content_hash TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS parser_issues (
          file_path TEXT,
          severity TEXT NOT NULL,
          message TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(embeddings)").fetchall()}
    if "text_hash" not in cols:
        conn.execute("ALTER TABLE embeddings ADD COLUMN text_hash TEXT")
    conn.commit()


def reset_index(conn: sqlite3.Connection) -> None:
    # The embeddings table has `entity_id REFERENCES entities(id) ON DELETE
    # CASCADE`. With `PRAGMA foreign_keys=ON` (set by connect()), DROP TABLE
    # entities performs an implicit DELETE of every row, which cascades and
    # WIPES the entire embeddings table. Because the vault's ULIDs are stable
    # across a rebuild, the very same embeddings would still be valid after the
    # entities are re-inserted, so cascading them away silently breaks semantic
    # search on every reindex that detects a change (until the next `embed`).
    # Disable FK enforcement for the drops so embeddings survive the rebuild;
    # `embed`'s orphan-prune (FIX-11) removes any left referencing a since-
    # deleted entity. commit() first so the PRAGMA is not a no-op inside a txn.
    conn.commit()
    fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(
        """
        DROP TABLE IF EXISTS relations;
        DROP TABLE IF EXISTS entities_fts;
        DROP TABLE IF EXISTS entities;
        DROP TABLE IF EXISTS meta;
        DROP TABLE IF EXISTS parser_issues;
        DROP TABLE IF EXISTS files;
        """
    )
    ensure_schema(conn)
    if fk_on:
        conn.execute("PRAGMA foreign_keys=ON")


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
    # Wikilinks are written as [[file-slug|Display]] (Obsidian resolves by file
    # basename), so resolve weak links by slug too, not only by entity name.
    by_slug = {entity.file_path.stem.casefold(): entity.id for entity in entities}
    
    # Sort entities by ID to ensure deterministic ordering of duplicate specs
    sorted_entities = sorted(entities, key=lambda e: e.id)
    
    typed_relations: dict[tuple[str, str, str], Relation] = {}
    for entity in sorted_entities:
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
            
            direction = spec.get("direction", "outgoing")
            if direction not in ("incoming", "outgoing"):
                issues.append(
                    Issue("error", f"Invalid relation direction: {direction}", entity.file_path)
                )
                continue
                
            to_id = target if target in by_id else by_name.get(normalize_name(target))
            if not to_id:
                issues.append(
                    Issue("error", f"Unresolved typed relation target: {target}", entity.file_path)
                )
                continue
                
            if direction == "incoming":
                from_id = to_id
                rel_to_id = entity.id
            else:
                from_id = entity.id
                rel_to_id = to_id
                
            key = (from_id, rel_to_id, rel_type)
            props = spec.get("properties") if isinstance(spec.get("properties"), dict) else {}
            source_file = str(
                spec.get("source")
                or entity.frontmatter.get("provenance", {}).get("source_file")
                or ""
            )
            created_at = str(spec.get("created_at") or entity.created_at or "")
            
            new_rel = Relation(
                from_id=from_id,
                to_id=rel_to_id,
                type=rel_type,
                weak=False,
                properties=dict(props),
                source_file=source_file,
                review_status=entity.review_status,
                created_at=created_at,
            )
            
            if key in typed_relations:
                existing = typed_relations[key]
                existing.properties.update(new_rel.properties)
                if not (existing.created_at and existing.created_at.strip()) and (new_rel.created_at and new_rel.created_at.strip()):
                    existing.created_at = new_rel.created_at
                if not (existing.source_file and existing.source_file.strip()) and (new_rel.source_file and new_rel.source_file.strip()):
                    existing.source_file = new_rel.source_file
                existing.weak = False
            else:
                typed_relations[key] = new_rel

    relations: list[Relation] = list(typed_relations.values())
    for entity in sorted_entities:
        for ref in entity.weak_refs:
            key = normalize_name(ref)
            if key in ambiguous:
                issues.append(Issue("warning", f"Ambiguous weak link: {ref}", entity.file_path))
                continue
            to_id = by_name.get(key) or by_slug.get(ref.strip().casefold())
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
    from synapse.revisions import is_v2
    if is_v2(root):
        from synapse.v2_compat import reindex_v2
        return reindex_v2(root, full=full)
    conn = connect(root)
    try:
        ensure_schema(conn)

        # 1. Load existing files info from the files table
        existing_files = {
            row["path"]: (row["mtime_ns"], row["size"], row["content_hash"])
            for row in conn.execute("SELECT path, mtime_ns, size, content_hash FROM files").fetchall()
        }

        # 2. Stat-scan all files in the vault
        all_paths = entity_files(root)
        current_files: dict[str, tuple[int, int, str]] = {}

        # Per-file Path.resolve() costs ~0.4ms each on Windows; root is already
        # resolved (resolve_vault), so the fast path avoids resolving entirely.
        root_resolved = root.resolve()
        for p in all_paths:
            try:
                rel_path = str(p.relative_to(root_resolved))
            except ValueError:
                rel_path = str(p.resolve().relative_to(root_resolved))
            stat = p.stat()
            size = stat.st_size
            mtime_ns = stat.st_mtime_ns
            
            # Check cached hash if not full and mtime/size matches
            if not full and rel_path in existing_files:
                cached_mtime, cached_size, cached_hash = existing_files[rel_path]
                if cached_mtime == mtime_ns and cached_size == size:
                    current_files[rel_path] = (mtime_ns, size, cached_hash)
                    continue
            
            # Compute new hash
            content_hash = sha256_file(p)
            current_files[rel_path] = (mtime_ns, size, content_hash)

        # 3. Compare the complete source tree with the complete cached file set.
        # The entities table intentionally excludes archived and merged
        # tombstones, so it cannot be the authority for this gate.
        existing_hashes = {path: info[2] for path, info in existing_files.items()}
        current_hashes = {path: info[2] for path, info in current_files.items()}

        changed = full or existing_hashes != current_hashes

        if not changed:
            # Load cached issues
            cached_issues = []
            for row in conn.execute("SELECT file_path, severity, message FROM parser_issues").fetchall():
                fp = Path(row["file_path"]) if row["file_path"] else None
                cached_issues.append(Issue(row["severity"], row["message"], fp))
                
            return ReindexResult(
                entities=conn.execute("SELECT COUNT(*) AS c FROM entities").fetchone()["c"],
                relations=conn.execute("SELECT COUNT(*) AS c FROM relations").fetchone()["c"],
                changed_files=0,
                issues=cached_issues,
            )

        # 4. If changed, we do a full parse and rebuild
        entities, issues = parse_vault(root)
        relations, relation_issues = _relations_for_entities(entities)
        issues.extend(relation_issues)

        reset_index(conn)
        with conn:
            for entity in entities:
                _insert_entity(conn, entity)
            for relation in relations:
                _insert_relation(conn, relation)
            
            # Save files stats and hashes
            for path, (mtime_ns, size, content_hash) in current_files.items():
                conn.execute(
                    "INSERT INTO files(path, mtime_ns, size, content_hash) VALUES (?, ?, ?, ?)",
                    (path, mtime_ns, size, content_hash),
                )
                
            # Save parser issues
            for issue in issues:
                conn.execute(
                    "INSERT INTO parser_issues(file_path, severity, message) VALUES (?, ?, ?)",
                    (str(issue.file_path) if issue.file_path else None, issue.severity, issue.message),
                )
                
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
            changed_files=len(current_files),
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
    rel = {
        "id": row["id"],
        "from_id": row["from_id"],
        "to_id": row["to_id"],
        "type": row["type"],
        "weak": bool(row["weak"]),
        "properties": json.loads(row["properties"] or "{}"),
        "source_file": row["source_file"],
        "review_status": row["review_status"],
    }
    if "from_name" in row.keys():
        rel["from_name"] = row["from_name"]
    if "to_name" in row.keys():
        rel["to_name"] = row["to_name"]
    return rel


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
    sql = (
        "SELECT r.*, ef.name AS from_name, et.name AS to_name "
        "FROM relations r "
        "JOIN entities ef ON ef.id = r.from_id "
        "JOIN entities et ON et.id = r.to_id"
    )
    params: list[Any] = []
    if not include_weak:
        sql += " WHERE r.weak = 0"
    sql += " ORDER BY r.from_id, r.to_id, r.type"
    return [row_to_relation(row) for row in conn.execute(sql, params).fetchall()]
