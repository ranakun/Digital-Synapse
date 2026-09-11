"""Accepted, qualified compatibility projection for existing deterministic tools."""

from __future__ import annotations

from pathlib import Path

from synapse.gateway import Gateway
from synapse.models import Issue, ReindexResult, Relation
from synapse.parser import _parse_entity_file_internal
from synapse.revisions import RevisionStore
from synapse.util import utc_now


def projection_entities(vault: Path, revision=None):
    gateway = Gateway(vault, revision=revision)
    entities, issues = [], []
    for identity, row in gateway.view.manifest["records"].items():
        if row["availability"] != "accepted" or not row["active"]:
            continue
        unit = gateway._unit(identity, closure_limit=128, supports_qualifications=False)
        if unit.get("withheld") or not unit["complete"]:
            issues.append(Issue("warning", f"V2 qualifications for {identity} require the context gateway; assertion withheld from this legacy surface.", Path(row["path"])))
            # Identity still exists; preserving it does not assert its body.
            # Do not carry its outgoing semantic claims into an incapable client.
            entity, errors = _parse_entity_file_internal(vault / row["path"], vault, retained_bytes=gateway.view.store.read_object(row["version"]))
            issues.extend(errors)
            if entity:
                entity.body = "Context requires qualifications. Read this identity through synapse v2 context."
                entity.properties = {}
                entity.frontmatter = {"id": entity.id, "type": entity.type, "name": entity.name, "review_status": entity.review_status}
                entity.relation_specs, entity.weak_refs = [], []
                entities.append(entity)
            continue
        entity, errors = _parse_entity_file_internal(vault / row["path"], vault, retained_bytes=gateway.view.store.read_object(row["version"]))
        issues.extend(errors)
        if entity:
            entities.append(entity)
    return entities, issues


def reindex_v2(vault: Path, *, full=False):
    from synapse.index import _insert_entity, _insert_relation, connect, ensure_schema, reset_index

    store = RevisionStore(vault)
    revision = store.head()
    conn = connect(vault, _skip_v2_refresh=True)
    try:
        ensure_schema(conn)
        previous = conn.execute("SELECT value FROM meta WHERE key='knowledge_revision'").fetchone()
        if not full and previous and previous[0] == revision:
            issues = [Issue(row["severity"], row["message"], Path(row["file_path"]) if row["file_path"] else None) for row in conn.execute("SELECT * FROM parser_issues")]
            return ReindexResult(entities=conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], relations=conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0], changed_files=0, issues=issues)
        gateway = Gateway(vault, revision=revision)
        entities, issues = projection_entities(vault, revision)
        eligible = {entity.id for entity in entities}
        relations = []
        for edge in gateway.view.relationships(include_suggestions=False):
            if edge["from_id"] not in eligible or edge["to_id"] not in eligible:
                continue
            record_id = edge.get("record_id")
            if record_id:
                unit = gateway._unit(record_id, closure_limit=128, supports_qualifications=False)
                if unit.get("withheld") or not unit["complete"]:
                    continue
            relations.append(Relation(from_id=edge["from_id"], to_id=edge["to_id"], type=edge.get("relation", edge.get("relation_type", "related_to")), weak=edge.get("weak", False), properties=edge.get("properties", {}) | {"v2_edge_id": edge["id"]}, source_file=edge.get("source_file", ""), review_status=edge.get("review_status", "proposed"), created_at=edge.get("created_at")))
        reset_index(conn)
        with conn:
            for entity in entities:
                _insert_entity(conn, entity)
            for relation in relations:
                _insert_relation(conn, relation)
            for issue in issues:
                conn.execute("INSERT INTO parser_issues(file_path,severity,message) VALUES(?,?,?)", (str(issue.file_path) if issue.file_path else None, issue.severity, issue.message))
            for key, value in {"knowledge_revision": revision, "last_full_index_at": utc_now(), "vault_path": str(vault)}.items():
                conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))
        return ReindexResult(entities=len(entities), relations=len(relations), changed_files=len(entities), issues=issues)
    finally:
        conn.close()
