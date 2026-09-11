"""Deterministic, revision-pinned reads for the Synapse v2 store.

The checkout is deliberately not a read authority in this module.  A
``ReadView`` selects one retained manifest and builds a disposable projection
from the objects named by that manifest.  This keeps an editor changing
``entities/`` from changing an in-flight consultation, and makes deleting the
projection a routine recovery operation.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import tempfile
import threading
from collections import Counter, OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from synapse.knowledge import decode_record, metadata_and_body, record_descriptor
from synapse.revisions import RevisionStore
from synapse.source_purpose import SourcePurposePolicy, load_source_purposes, validate_source_scope
from synapse.source_store import evidence_ref, read_source_page
from synapse.util import normalize_name
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes

_INDEX_SCHEMA = "read-view-7"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_BUILD_LOCK = threading.Lock()
_VALIDATED_INDEXES: OrderedDict[tuple, bool] = OrderedDict()
_VALIDATION_LOCK = threading.Lock()


def _index_fingerprint(path: Path) -> tuple:
    """Changed DB bytes or SQLite sidecars always invalidate warm validation."""
    stamps = []
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")):
        try:
            stat = candidate.stat()
            stamps.append((stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except FileNotFoundError:
            stamps.append(None)
    return tuple(stamps)


def _fail(code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
    raise V2Error(code, message, details=details)


def _json(value: Any) -> str:
    # Legacy YAML permits date scalars. Its existing index exposes them as
    # strings; canonical Markdown bytes remain untouched by this projection.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _date_value(value: Any, field: str, timezone: ZoneInfo | None = None) -> date:
    if isinstance(value, datetime):
        if timezone is not None and value.tzinfo is not None:
            value = value.astimezone(timezone)
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        if _DATE_RE.fullmatch(value):
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if timezone is not None and parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone)
            return parsed.date()
    _fail("invalid-request", f"{field} must be an ISO date or date-time")


def _validate_page(offset: Any, limit: Any) -> tuple[int, int]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        _fail("invalid-request", "offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        _fail("invalid-request", "limit must be an integer between 1 and 200")
    return offset, limit


def _validated_timezone(value: str) -> ZoneInfo:
    if not isinstance(value, str) or not value.strip():
        _fail("invalid-request", "timezone must be a valid IANA timezone")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        _fail("invalid-request", f"Unknown timezone: {value}")
        raise AssertionError from exc


def _known_at_value(value: Any, timezone: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            if _DATE_RE.fullmatch(value):
                try:
                    parsed = datetime.combine(date.fromisoformat(value), datetime.min.time())
                except ValueError:
                    _fail("invalid-request", "known_at must be an ISO date or date-time")
            else:
                _fail("invalid-request", "known_at must be an ISO date or date-time")
    else:
        _fail("invalid-request", "known_at must be an ISO date or date-time")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(ZoneInfo("UTC"))


def _committed_at(manifest: Mapping[str, Any]) -> datetime:
    value = manifest.get("committed_at")
    if not isinstance(value, str):
        _fail("unsupported-history", "Revision history has no committed timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        _fail("unsupported-history", "Revision history has an invalid committed timestamp")
        raise AssertionError from exc
    if parsed.tzinfo is None:
        _fail("unsupported-history", "Revision history has an unzoned committed timestamp")
    return parsed


def _fts_query(value: str) -> str:
    # Quoting each token avoids FTS5 operators supplied by a caller while
    # retaining useful token matching for punctuation and Unicode text.
    tokens = re.findall(r"[^\s]+", value.strip(), flags=re.UNICODE)
    return " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)


class ReadView:
    """A read-only view over exactly one retained knowledge revision."""

    def __init__(
        self,
        vault: Path,
        *,
        revision: str | None = None,
        known_at: Any = None,
        timezone: str | None = None,
    ) -> None:
        self.vault = Path(vault).resolve()
        self.store = RevisionStore(self.vault)
        from synapse.config import resolve_timezone
        try:
            timezone = resolve_timezone(self.vault, timezone)
        except ValueError as exc:
            raise V2Error("invalid-request", str(exc)) from exc
        self.timezone = timezone
        self._timezone = _validated_timezone(timezone)
        if revision is not None and known_at is not None:
            _fail("invalid-request", "revision and known_at cannot be supplied together")
        if revision is not None and (not isinstance(revision, str) or not _HASH_RE.fullmatch(revision)):
            _fail("invalid-request", "revision must be a SHA-256 content address")

        if known_at is not None:
            target = _known_at_value(known_at, self._timezone)
            self.revision, self.manifest = self._revision_at(target)
        else:
            self.revision = revision or self.store.head()
            # manifest() already returns independently decoded, hash-verified
            # data; copying the full vault again adds no isolation.
            self.manifest = self.store.manifest(self.revision)

        self._db_path = self.vault / ".synapse" / "v2-indexes" / f"{self.revision}.db"
        self._ensure_index()

    @property
    def index_path(self) -> Path:
        return self._db_path

    def _revision_at(self, target: datetime) -> tuple[str, dict[str, Any]]:
        current = self.store.head()
        seen: set[str] = set()
        baseline: tuple[str, dict[str, Any]] | None = None
        while current:
            if current in seen:
                _fail("unsupported-history", "Revision ancestry contains a cycle")
            seen.add(current)
            manifest = self.store.manifest(current)
            baseline = (current, manifest)
            if _committed_at(manifest) <= target:
                return current, _copy(manifest)
            current = manifest.get("parent")
        _fail(
            "unsupported-history",
            "known_at predates the retained baseline history",
            details={"baseline_revision": baseline[0] if baseline else None},
        )

    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        connection = sqlite3.connect(str(path or self._db_path))
        connection.row_factory = sqlite3.Row
        return connection

    def _index_is_valid(self) -> bool:
        if not self._db_path.is_file():
            return False
        try:
            fingerprint = _index_fingerprint(self._db_path)
            cache_key = (str(self._db_path.resolve()), self.revision, _INDEX_SCHEMA, fingerprint)
            with _VALIDATION_LOCK:
                if cache_key in _VALIDATED_INDEXES:
                    _VALIDATED_INDEXES.move_to_end(cache_key)
                    return True
            conn = self._connect()
            row = conn.execute("SELECT value FROM meta WHERE key = 'knowledge_revision'").fetchone()
            schema = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {
                item[0]
                for item in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
            }
            required = {"meta", "records", "sources", "source_versions", "premise_refs", "context_refs", "relationships", "records_fts", "sources_fts", "decision_duplicates"}
            conn.close()
            valid = (
                row is not None
                and row[0] == self.revision
                and schema is not None
                and schema[0] == _INDEX_SCHEMA
                and integrity == "ok"
                and required <= tables
            )
            if valid and _index_fingerprint(self._db_path) == fingerprint:
                with _VALIDATION_LOCK:
                    _VALIDATED_INDEXES[cache_key] = True
                    while len(_VALIDATED_INDEXES) > 32:
                        _VALIDATED_INDEXES.popitem(last=False)
                return True
            return False
        except (OSError, sqlite3.Error, TypeError):
            try:
                conn.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            return False

    def _ensure_index(self) -> None:
        if self._index_is_valid():
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # A process-level mutex avoids two local callers doing the same
        # expensive build.  It is not the publication lock and never covers
        # source/record preparation or a canonical write.
        with _BUILD_LOCK:
            if self._index_is_valid():
                return
            fd, name = tempfile.mkstemp(prefix=f".{self.revision}.", suffix=".db", dir=self._db_path.parent)
            os.close(fd)
            temporary = Path(name)
            try:
                self._build_index(temporary)
                os.replace(temporary, self._db_path)
            except Exception:
                temporary.unlink(missing_ok=True)
                for sidecar in (Path(f"{temporary}-wal"), Path(f"{temporary}-shm")):
                    sidecar.unlink(missing_ok=True)
                raise

    def _build_index(self, path: Path) -> None:
        records: list[dict[str, Any]] = []
        record_map = self.manifest.get("records")
        if not isinstance(record_map, Mapping):
            _fail("recovery-required", "Pinned manifest has no record mapping")
        for identity, descriptor in record_map.items():
            if not isinstance(descriptor, Mapping) or descriptor.get("id") != identity:
                _fail("recovery-required", "Pinned manifest contains an invalid record descriptor")
            raw = self.store.read_object(str(descriptor["version"]))
            checked = record_descriptor(raw, path=str(descriptor["path"]))
            if dict(descriptor) != checked:
                _fail("recovery-required", "Record descriptor disagrees with retained Markdown", details={"id": identity})
            payload = decode_record(raw, path=str(descriptor["path"]))
            metadata, body = metadata_and_body(raw)
            records.append(self._record_row(checked, payload, metadata, body))

        sources = self.manifest.get("source_versions") or {}
        source_rows: list[dict[str, Any]] = []
        if not isinstance(sources, Mapping):
            _fail("recovery-required", "Pinned manifest has no source version mapping")
        for version, descriptor in sources.items():
            if not isinstance(descriptor, Mapping) or descriptor.get("version") != version:
                _fail("recovery-required", "Pinned manifest contains an invalid source descriptor")
            source_rows.append({"version": version, "source_id": descriptor.get("id"), "descriptor": dict(descriptor)})

        conn = self._connect(path)
        try:
            self._create_schema(conn)
            conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", ("schema", _INDEX_SCHEMA))
            conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", ("knowledge_revision", self.revision))
            conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", ("timezone", self.timezone))
            from synapse.suggestion_policy import meaning_key
            decided = {}
            for row in records:
                if row["disposition"] in {"dismissed", "disputed"}:
                    decided.setdefault(meaning_key(row["payload"]), []).append(row["id"])
            for row in records:
                if row["availability"] == "suggestion" and row["disposition"] not in {"dismissed", "disputed"}:
                    for decision_id in decided.get(meaning_key(row["payload"]), []):
                        conn.execute("INSERT INTO decision_duplicates(record_id,decision_id) VALUES (?,?)", (row["id"], decision_id))
            for row in records:
                conn.execute(
                    """INSERT INTO records
                    (id,version,path,profile,type,name,availability,review_status,active,merged_into,
                     disposition,subject_id,claim_key,facets,as_of,applies_from,applies_until,record_json,metadata_json,body)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row["id"], row["version"], row["path"], row["profile"], row["type"], row["name"],
                        row["availability"], row["review_status"], int(row["active"]), row["merged_into"],
                        row["disposition"], row["subject_id"], row["claim_key"], _json(row["facets"]),
                        row["as_of"], row["applies_from"], row["applies_until"], _json(row["payload"]),
                        _json(row["metadata"]), row["body"],
                    ),
                )
                for reference in row["payload"].get("dependencies", []) if isinstance(row["payload"], Mapping) else []:
                    if isinstance(reference, Mapping):
                        conn.execute(
                            "INSERT INTO premise_refs(record_id,record_version,ref_id,ref_version,role) VALUES(?,?,?,?,?)",
                            (row["id"], row["version"], reference.get("id"), reference.get("version"), reference.get("role", "premise")),
                        )
                for reference in row["payload"].get("context_refs", []) if isinstance(row["payload"], Mapping) else []:
                    if isinstance(reference, Mapping):
                        conn.execute(
                            "INSERT INTO context_refs(record_id,record_version,ref_id,ref_version,role,scope) VALUES(?,?,?,?,?,?)",
                            (row["id"], row["version"], reference.get("id"), reference.get("version"), reference.get("role"), reference.get("scope")),
                        )
                text = " ".join(
                    [row["body"], row["name"], _json(row["metadata"]), _json(row["payload"])]
                )
                conn.execute("INSERT INTO records_fts(record_id,text,name,metadata) VALUES(?,?,?,?)", (row["id"], text, row["name"], _json(row["metadata"])))
            for row in source_rows:
                conn.execute("INSERT INTO source_versions(version,source_id,descriptor_json) VALUES(?,?,?)", (row["version"], row["source_id"], _json(row["descriptor"])))
            for source_id, version in (self.manifest.get("sources") or {}).items():
                conn.execute("INSERT INTO sources(source_id,version) VALUES(?,?)", (source_id, version))
                descriptor = sources[version]
                if descriptor.get("text_version"):
                    text = self.store.read_object(descriptor["text_version"]).decode("utf-8")
                    conn.execute("INSERT INTO sources_fts(source_id,version,origin,text) VALUES(?,?,?,?)", (source_id, version, descriptor["origin"], text))
            record_versions = {str(row["id"]): str(row["version"]) for row in records}
            for relation in self._relationship_rows(records):
                conn.execute(
                    """INSERT INTO relationships
                    (public_id,from_id,to_id,relation,availability,disposition,record_id,record_version,metadata_json)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (relation["id"], relation["from_id"], relation["to_id"], relation["relation"], relation["availability"], relation.get("disposition", "none"), relation.get("record_id"), relation.get("record_version"), _json(relation)),
                )
            # v1 owner-knowledge corrections were represented as related_to
            # roles.  Preserve those scoped references when the target is an
            # explicit retained ID; a name cannot safely establish identity.
            for row in records:
                metadata = row["metadata"]
                props = metadata.get("properties") if isinstance(metadata.get("properties"), Mapping) else {}
                if props.get("knowledge_profile") != "owner-knowledge-v1":
                    continue
                relation_specs = metadata.get("relations")
                if not isinstance(relation_specs, list):
                    continue
                for spec in relation_specs:
                    if not isinstance(spec, Mapping) or spec.get("type") != "related_to":
                        continue
                    target = spec.get("target")
                    relation_props = spec.get("properties") if isinstance(spec.get("properties"), Mapping) else {}
                    relation_roles = relation_props.get("roles")
                    resolved_target = self._legacy_context_target(target, records)
                    if resolved_target is None or not isinstance(relation_roles, list):
                        continue
                    scope = str(relation_props.get("scope") or relation_props.get("note") or "legacy scoped relation")
                    for role in relation_roles:
                        if role not in {"about", "qualifies", "revises", "contradicts"}:
                            continue
                        conn.execute(
                            "INSERT INTO context_refs(record_id,record_version,ref_id,ref_version,role,scope) VALUES(?,?,?,?,?,?)",
                            (row["id"], row["version"], resolved_target, record_versions[resolved_target], role, scope),
                        )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(FULL)")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            PRAGMA journal_mode = DELETE;
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE records (
              id TEXT PRIMARY KEY, version TEXT NOT NULL, path TEXT NOT NULL,
              profile TEXT NOT NULL, type TEXT NOT NULL, name TEXT NOT NULL,
              availability TEXT NOT NULL, review_status TEXT NOT NULL,
              active INTEGER NOT NULL, merged_into TEXT, disposition TEXT NOT NULL,
              subject_id TEXT, claim_key TEXT, facets TEXT NOT NULL,
              as_of TEXT, applies_from TEXT, applies_until TEXT,
              record_json TEXT NOT NULL, metadata_json TEXT NOT NULL, body TEXT NOT NULL
            );
            CREATE INDEX records_subject ON records(subject_id);
            CREATE INDEX records_availability ON records(availability);
            CREATE INDEX records_claim_key ON records(claim_key);
            CREATE TABLE decision_duplicates(record_id TEXT NOT NULL, decision_id TEXT NOT NULL);
            CREATE INDEX duplicate_record ON decision_duplicates(record_id);
            CREATE TABLE sources(source_id TEXT PRIMARY KEY, version TEXT NOT NULL);
            CREATE TABLE source_versions(version TEXT PRIMARY KEY, source_id TEXT NOT NULL, descriptor_json TEXT NOT NULL);
            CREATE TABLE premise_refs(record_id TEXT NOT NULL, record_version TEXT NOT NULL, ref_id TEXT NOT NULL, ref_version TEXT NOT NULL, role TEXT NOT NULL);
            CREATE INDEX premise_refs_ref ON premise_refs(ref_id);
            CREATE TABLE context_refs(record_id TEXT NOT NULL, record_version TEXT NOT NULL, ref_id TEXT NOT NULL, ref_version TEXT NOT NULL, role TEXT NOT NULL, scope TEXT NOT NULL);
            CREATE INDEX context_refs_ref ON context_refs(ref_id);
            CREATE TABLE relationships(
              row_id INTEGER PRIMARY KEY, public_id TEXT NOT NULL, from_id TEXT NOT NULL,
              to_id TEXT NOT NULL, relation TEXT NOT NULL, availability TEXT NOT NULL,
              disposition TEXT NOT NULL,
              record_id TEXT, record_version TEXT, metadata_json TEXT NOT NULL
            );
            CREATE INDEX relationships_from ON relationships(from_id);
            CREATE INDEX relationships_to ON relationships(to_id);
            CREATE VIRTUAL TABLE records_fts USING fts5(record_id UNINDEXED, text, name, metadata);
            CREATE VIRTUAL TABLE sources_fts USING fts5(source_id UNINDEXED, version UNINDEXED, origin, text);
            """
        )

    @staticmethod
    def _record_row(descriptor: Mapping[str, Any], payload: Mapping[str, Any], metadata: Mapping[str, Any], body: str) -> dict[str, Any]:
        props = metadata.get("properties") if isinstance(metadata.get("properties"), Mapping) else {}
        values: Mapping[str, Any] = payload if "subject_id" in payload else props
        facets = values.get("facets", [])
        if not isinstance(facets, list):
            facets = []
        return {
            "id": descriptor["id"], "version": descriptor["version"], "path": descriptor["path"],
            "profile": descriptor["profile"], "type": descriptor["type"], "name": descriptor["name"],
            "availability": descriptor["availability"], "review_status": descriptor["review_status"],
            "active": bool(descriptor["active"]), "merged_into": descriptor.get("merged_into"),
            "disposition": descriptor.get("disposition", "none"), "subject_id": values.get("subject_id"),
            "claim_key": values.get("claim_key"), "facets": [str(item) for item in facets],
            "as_of": values.get("as_of"), "applies_from": values.get("applies_from"),
            "applies_until": values.get("applies_until"), "payload": dict(payload),
            "metadata": dict(metadata), "body": body,
        }

    def _relationship_rows(self, records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        by_id = {str(row["id"]): row for row in records}
        rows: list[dict[str, Any]] = []
        # These are retained bytes, rather than checkout files.  The local
        # imports keep the legacy parser/index compatibility seam out of the
        # module import graph.
        from synapse.index import _relations_for_entities
        from synapse.parser import _parse_entity_file_internal

        entities = []
        for row in records:
            if not row["active"]:
                continue
            payload = row["payload"]
            if row["profile"] == "knowledge-v2" and isinstance(payload, Mapping) and isinstance(payload.get("relationship"), Mapping):
                relation = payload["relationship"]
                from_id, to_id = relation.get("from_id"), relation.get("to_id")
                if from_id in by_id and to_id in by_id and by_id[from_id]["active"] and by_id[to_id]["active"]:
                    rows.append({
                        "id": row["id"], "from_id": from_id, "to_id": to_id,
                        "relation": relation.get("relation_type"), "scope": relation.get("scope"),
                        "availability": row["availability"], "record_id": row["id"], "record_version": row["version"],
                        "review_status": row["review_status"], "version": row["version"], "disposition": row.get("disposition", "none"),
                    })
            entity, _issues = _parse_entity_file_internal(
                self.vault / row["path"], self.vault, retained_bytes=self.store.read_object(str(row["version"]))
            )
            if entity is not None:
                if row["profile"] == "knowledge-v2":
                    # Typed assertions have their own availability and IDs.
                    # Their readable Markdown must not mint accepted weak links.
                    entity.relation_specs = []
                    entity.weak_refs = []
                entities.append(entity)

        legacy_relations, _issues = _relations_for_entities(entities)
        entity_by_id = {entity.id: entity for entity in entities}
        occurrences = Counter()
        for relation in legacy_relations:
            if relation.from_id not in by_id or relation.to_id not in by_id:
                continue
            if not by_id[relation.from_id]["active"] or not by_id[relation.to_id]["active"]:
                continue
            public = relation.to_dict()
            key = (relation.from_id, relation.to_id, relation.type, bool(relation.weak))
            ordinal = occurrences[key]
            occurrences[key] += 1
            stable_id = "legacy:" + hash_bytes(canonical_json([*key, ordinal]))
            public.update(
                id=stable_id,
                relation=relation.type,
                availability="accepted",
                record_id=None,
                record_version=None,
                from_name=entity_by_id[relation.from_id].name,
                to_name=entity_by_id[relation.to_id].name,
            )
            rows.append(public)
        return rows

    @staticmethod
    def _legacy_context_target(target: Any, records: Iterable[Mapping[str, Any]]) -> str | None:
        """Resolve one legacy scoped ref only through an unambiguous ID/name/slug."""
        if not isinstance(target, str) or not target.strip():
            return None
        rows = list(records)
        by_id = {str(row["id"]): str(row["id"]) for row in rows}
        if target in by_id:
            return target
        names: dict[str, set[str]] = {}
        slugs: dict[str, set[str]] = {}
        for row in rows:
            labels = [row["name"]]
            metadata = row["metadata"]
            aliases = metadata.get("aliases") if isinstance(metadata.get("aliases"), list) else []
            labels.extend(alias for alias in aliases if isinstance(alias, str))
            for label in labels:
                key = normalize_name(label)
                if key:
                    names.setdefault(key, set()).add(str(row["id"]))
            slug = Path(str(row["path"])).stem.casefold()
            if slug:
                slugs.setdefault(slug, set()).add(str(row["id"]))
        name_matches = names.get(normalize_name(target), set())
        if len(name_matches) == 1:
            return next(iter(name_matches))
        slug_matches = slugs.get(target.strip().casefold(), set())
        if len(slug_matches) == 1:
            return next(iter(slug_matches))
        return None

    def _base_result(self, items: list[dict[str, Any]], total: int, offset: int) -> dict[str, Any]:
        return {
            "items": items, "total": total, "offset": offset,
            "next_offset": offset + len(items) if offset + len(items) < total else None,
            "knowledge_revision": self.revision, "index_state": "current",
            "semantic_search": "unused", "limitations": [],
        }

    @staticmethod
    def _descriptor_item(row: sqlite3.Row) -> dict[str, Any]:
        item = {
            "id": row["id"], "version": row["version"], "path": row["path"], "profile": row["profile"],
            "type": row["type"], "name": row["name"], "availability": row["availability"],
            "review_status": row["review_status"], "active": bool(row["active"]), "merged_into": row["merged_into"],
            "disposition": row["disposition"],
        }
        if row["subject_id"] is not None:
            item["subject_id"] = row["subject_id"]
        if row["claim_key"] is not None:
            item["claim_key"] = row["claim_key"]
        item["facets"] = json.loads(row["facets"])
        return item

    def _record_where(self, *, subject_id: str | None, facet: str | None, availability: str | None, query: str | None, valid_at: date | None = None, include_excluded: bool = False) -> tuple[str, list[Any], str | None]:
        clauses = ["r.active = 1"]
        if not include_excluded:
            clauses.extend(["r.availability != 'draft'", "r.disposition NOT IN ('dismissed','disputed')"])
            clauses.append("r.id NOT IN (SELECT record_id FROM decision_duplicates)")
        params: list[Any] = []
        if subject_id is not None:
            clauses.append("r.subject_id = ?")
            params.append(subject_id)
        if facet is not None:
            clauses.append("EXISTS (SELECT 1 FROM json_each(r.facets) WHERE json_each.value = ?)")
            params.append(facet)
        if availability is not None:
            clauses.append("r.availability = ?")
            params.append(availability)
        match = _fts_query(query or "") if query else None
        if match:
            clauses.append("r.id IN (SELECT record_id FROM records_fts WHERE records_fts MATCH ?)")
            params.append(match)
        if valid_at is not None:
            iso = valid_at.isoformat()
            clauses.append("(r.applies_from IS NULL OR r.applies_from <= ?)")
            params.append(iso)
            clauses.append("(r.applies_until IS NULL OR r.applies_until >= ?)")
            params.append(iso)
        return " AND ".join(clauses), params, match

    def source_purpose_policy(self) -> SourcePurposePolicy:
        """Get a validated local purpose snapshot for this pinned revision."""
        return load_source_purposes(self.vault, revision=self.revision, manifest=self.manifest)

    def catalog(self, *, kind: str = "records", subject_id: str | None = None, facet: str | None = None, availability: str | None = None, query: str | None = None, offset: int = 0, limit: int = 20, source_scope: str = "ordinary") -> dict[str, Any]:
        offset, limit = _validate_page(offset, limit)
        validate_source_scope(source_scope)
        if kind not in {"records", "sources"}:
            _fail("invalid-request", "catalog kind must be records or sources")
        for field, value in (("subject_id", subject_id), ("facet", facet), ("availability", availability), ("query", query)):
            if value is not None and not isinstance(value, str):
                _fail("invalid-request", f"{field} must be a string")
        conn = self._connect()
        try:
            if kind == "sources":
                if subject_id is not None or facet is not None or availability is not None:
                    _fail(
                        "invalid-request",
                        "subject_id, facet and availability filters do not apply to source catalogues",
                    )
                policy = self.source_purpose_policy()
                conn.create_function("source_purpose_visible", 2, lambda identity, version: policy.visible(identity, version, source_scope))
                clauses, params = ["source_purpose_visible(source_id,version) = 1"], []
                if query:
                    clauses.append("descriptor_json LIKE ?")
                    params.append(f"%{query}%")
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                total = conn.execute(f"SELECT COUNT(*) FROM source_versions{where}", params).fetchone()[0]
                rows = conn.execute(f"SELECT descriptor_json FROM source_versions{where} ORDER BY source_id,version LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
                items = []
                for row in rows:
                    item = json.loads(row[0])
                    item.update(policy.labels(item["id"], item["version"]))
                    items.append(item)
                result = self._base_result(items, total, offset)
                result.update(source_scope=source_scope, source_policy=policy.metadata())
                return result
            where, params, _ = self._record_where(subject_id=subject_id, facet=facet, availability=availability, query=query)
            total = conn.execute(f"SELECT COUNT(*) FROM records r WHERE {where}", params).fetchone()[0]
            rows = conn.execute(f"SELECT * FROM records r WHERE {where} ORDER BY r.name COLLATE NOCASE, r.id LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
            policy = self.source_purpose_policy()
            items = []
            for row in rows:
                item = self._descriptor_item(row)
                item.update(policy.record_evidence_policy(json.loads(row["record_json"])))
                items.append(item)
            return self._base_result(items, total, offset) | {"source_policy": policy.metadata()}
        finally:
            conn.close()

    def records(self, *, ids: list[str]) -> list[dict[str, Any]]:
        if not isinstance(ids, list) or any(not isinstance(identity, str) or not identity for identity in ids):
            _fail("invalid-request", "ids must be a list of non-empty strings")
        conn = self._connect()
        try:
            result = []
            for identity in ids:
                row = conn.execute("SELECT record_json FROM records WHERE id = ?", (identity,)).fetchone()
                if row is None:
                    _fail("record-unavailable", "Record is absent from this revision", details={"id": identity})
                # Verify selected canonical bytes even when the disposable
                # index already exists; a corrupt object cannot be masked by it.
                descriptor = self.manifest["records"][identity]
                raw = self.store.read_object(descriptor["version"])
                if record_descriptor(raw, path=descriptor["path"]) != descriptor:
                    _fail("recovery-required", "Record descriptor disagrees with retained Markdown")
                result.append(decode_record(raw, path=descriptor["path"]))
            return result
        finally:
            conn.close()

    def markdown(self, identity: str, *, offset: int = 0, limit: int = 4000) -> dict[str, Any]:
        """Return a Unicode-character page of the exact retained Markdown."""
        if not isinstance(identity, str) or not identity:
            _fail("invalid-request", "id must be a non-empty string")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 4000
        ):
            _fail("invalid-request", "markdown offset must be non-negative and limit between 1 and 4000")
        row = self.manifest.get("records", {}).get(identity)
        if not isinstance(row, Mapping):
            _fail("record-unavailable", "Record is absent from this revision", details={"id": identity})
        raw = self.store.read_object(str(row["version"]))
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            _fail("record-unavailable", "Retained Markdown is not valid UTF-8", details={"id": identity})
        total = len(text)
        if offset > total:
            _fail(
                "invalid-request",
                "offset is beyond the end of the retained Markdown",
                details={"offset": offset, "total_characters": total},
            )
        end = min(offset + limit, total)
        next_offset = end if end < total else None
        return {
            "id": identity,
            "version": row["version"],
            "path": row["path"],
            "markdown": text[offset:end],
            "offset": offset,
            "next_offset": next_offset,
            "total_characters": total,
            "truncated": next_offset is not None,
            "end_of_record": next_offset is None,
            "complete": next_offset is None and offset == 0,
            "limitations": [],
        }

    def metadata(self, identity: str) -> dict[str, Any]:
        if not isinstance(identity, str) or not identity:
            _fail("invalid-request", "id must be a non-empty string")
        conn = self._connect()
        try:
            row = conn.execute("SELECT metadata_json FROM records WHERE id = ?", (identity,)).fetchone()
            if row is None:
                _fail("record-unavailable", "Record is absent from this revision", details={"id": identity})
            return json.loads(row[0])
        finally:
            conn.close()

    def candidates(self, *, query: str = "", subject_id: str | None = None, facet: str | None = None, availability: str | None = None, valid_at: Any = None, offset: int = 0, limit: int = 20, include_excluded: bool = False) -> dict[str, Any]:
        offset, limit = _validate_page(offset, limit)
        if not isinstance(query, str):
            _fail("invalid-request", "query must be a string")
        target_date = _date_value(valid_at, "valid_at", self._timezone) if valid_at is not None else None
        conn = self._connect()
        try:
            where, params, match = self._record_where(subject_id=subject_id, facet=facet, availability=availability, query=query, valid_at=target_date, include_excluded=include_excluded)
            total = conn.execute(f"SELECT COUNT(*) FROM records r WHERE {where}", params).fetchone()[0]
            # Join the FTS table in the ranked read.  A MATCH-only subquery
            # would lose bm25's per-hit score, causing alphabetical results.
            if match:
                rows = conn.execute(
                    f"WITH ranked AS (SELECT record_id, bm25(records_fts) AS score "
                    f"FROM records_fts WHERE records_fts MATCH ?) "
                    f"SELECT r.* FROM records r JOIN ranked ON ranked.record_id = r.id "
                    f"WHERE {where} ORDER BY ranked.score, r.name COLLATE NOCASE, r.id LIMIT ? OFFSET ?",
                    [match, *params, limit, offset],
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT r.* FROM records r WHERE {where} ORDER BY r.name COLLATE NOCASE, r.id LIMIT ? OFFSET ?",
                    [*params, limit, offset],
                ).fetchall()
            items = []
            for row in rows:
                item = self._descriptor_item(row)
                item["record"] = json.loads(row["record_json"])
                item["temporal"] = {key: row[key] for key in ("as_of", "applies_from", "applies_until") if row[key] is not None}
                if target_date is not None and not row["applies_from"] and not row["applies_until"]:
                    item["limitations"] = ["Applicability dates are unspecified for this record; valid_at cannot promise that it applies."]
                items.append(item)
            result = self._base_result(items, total, offset)
            if target_date is not None and any("limitations" in item for item in items):
                result["limitations"] = ["Some candidates have unspecified applicability dates; valid_at cannot promise that they apply."]
            return result
        except sqlite3.Error as exc:
            _fail("invalid-request", "The candidate query is invalid", details={"error": str(exc)})
        finally:
            conn.close()

    def source(self, source_id: str, *, version: str | None = None, offset: int = 0, limit: int = 4000) -> dict[str, Any]:
        if not isinstance(source_id, str) or not source_id:
            _fail("invalid-request", "source_id must be a non-empty string")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4000:
            _fail("invalid-request", "source offset must be non-negative and source limit between 1 and 4000")
        manifest_sources = self.manifest.get("sources") or {}
        manifest_versions = self.manifest.get("source_versions") or {}
        selected = version if version is not None else manifest_sources.get(source_id)
        if not isinstance(selected, str) or selected not in manifest_versions:
            _fail("source-unavailable", "Source version is absent from this revision", details={"source_id": source_id, "version": version})
        descriptor = manifest_versions[selected]
        if not isinstance(descriptor, Mapping) or descriptor.get("id") != source_id or descriptor.get("version") != selected:
            _fail("source-unavailable", "Requested source version does not belong to this source", details={"source_id": source_id, "version": selected})
        return read_source_page(dict(descriptor), self.store.read_object, offset=offset, limit=limit)

    def search_sources(self, query: str, *, offset=0, limit=20, source_scope: str = "ordinary") -> dict[str, Any]:
        """Search retained text even when no assertion was extracted from it."""
        offset, limit = _validate_page(offset, limit)
        validate_source_scope(source_scope)
        if not isinstance(query, str) or not query.strip():
            _fail("invalid-request", "Source search needs a meaningful query")
        match = _fts_query(query)
        policy = self.source_purpose_policy()
        conn = self._connect()
        try:
            conn.create_function("source_purpose_visible", 2, lambda identity, version: policy.visible(identity, version, source_scope))
            where = "sources_fts MATCH ? AND source_purpose_visible(source_id,version) = 1"
            total = conn.execute(f"SELECT COUNT(*) FROM sources_fts WHERE {where}", (match,)).fetchone()[0]
            hits = conn.execute(f"SELECT source_id,version FROM sources_fts WHERE {where} ORDER BY bm25(sources_fts),source_id,version LIMIT ? OFFSET ?", (match, limit, offset)).fetchall()
            items = []
            for hit in hits:
                descriptor = self.manifest["source_versions"][hit["version"]]
                text = self.store.read_object(descriptor["text_version"]).decode("utf-8")
                words = re.findall(r"\w+", query, flags=re.UNICODE)
                pattern = "|".join(re.escape(word) for word in words)
                found = re.search(pattern, text, re.IGNORECASE) if pattern else None
                start = max(0, found.start() - 180) if found else 0
                stop = min(len(text), start + 800)
                item = {"source_id": descriptor["id"], "source_version": descriptor["version"], "text_version": descriptor["text_version"], "origin": descriptor["origin"], "excerpt": text[start:stop], "offset": start, "next_offset": stop if stop < len(text) else None, "total_characters": len(text), "extraction": descriptor["extraction"], "claim_extraction": "Not implied by text search."}
                item.update(policy.labels(descriptor["id"], descriptor["version"]))
                if stop > start:
                    item["evidence"] = evidence_ref(descriptor, self.store.read_object, len(text[:start].encode()), len(text[:stop].encode()))
                items.append(item)
            result = self._base_result(items, total, offset)
            result.update(source_scope=source_scope, source_policy=policy.metadata())
            result["limitations"] = ["Search covers current retained extracted text. Failed/binary-only extraction and unretained sources are not searchable; inspect the source catalog for coverage."]
            return result
        except sqlite3.Error as exc:
            _fail("invalid-request", "Source query could not be evaluated", details={"error": str(exc)})
        finally:
            conn.close()

    def incoming(self, identity: str, *, roles: Sequence[str] | None = None) -> list[dict[str, Any]]:
        if not isinstance(identity, str) or not identity:
            _fail("invalid-request", "identity must be a non-empty string")
        if roles is not None and (isinstance(roles, (str, bytes)) or not isinstance(roles, Sequence) or any(not isinstance(role, str) for role in roles)):
            _fail("invalid-request", "roles must be a sequence of strings")
        role_set = set(roles or [])
        conn = self._connect()
        try:
            refs: list[dict[str, Any]] = []
            for row in conn.execute("SELECT p.*, r.active FROM premise_refs p JOIN records r ON r.id=p.record_id WHERE p.ref_id=? AND r.active=1 ORDER BY p.record_id,p.ref_version", (identity,)).fetchall():
                if role_set and row["role"] not in role_set:
                    continue
                refs.append({"id": row["record_id"], "version": row["record_version"], "role": row["role"], "target_id": row["ref_id"], "target_version": row["ref_version"]})
            for row in conn.execute("SELECT c.*, r.active FROM context_refs c JOIN records r ON r.id=c.record_id WHERE c.ref_id=? AND r.active=1 ORDER BY c.record_id,c.role,c.ref_version", (identity,)).fetchall():
                if role_set and row["role"] not in role_set:
                    continue
                refs.append({"id": row["record_id"], "version": row["record_version"], "role": row["role"], "target_id": row["ref_id"], "target_version": row["ref_version"], "scope": row["scope"]})
            return refs
        finally:
            conn.close()

    def prior_decisions(self, identity: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT d.record_json, EXISTS(SELECT 1 FROM decision_duplicates x WHERE x.record_id=r.id AND x.decision_id=d.id) AS exact_duplicate FROM records r JOIN records d ON d.subject_id=r.subject_id AND d.claim_key=r.claim_key WHERE r.id=? AND d.id!=r.id AND d.disposition IN ('dismissed','disputed') ORDER BY d.id", (identity,)).fetchall()
            return [{"record": json.loads(row["record_json"]), "exact_duplicate": bool(row["exact_duplicate"])} for row in rows]
        finally:
            conn.close()

    def relationships(self, *, identity: str | None = None, include_suggestions: bool = False) -> list[dict[str, Any]]:
        if identity is not None and (not isinstance(identity, str) or not identity):
            _fail("invalid-request", "identity must be a non-empty string")
        if not isinstance(include_suggestions, bool):
            _fail("invalid-request", "include_suggestions must be boolean")
        conn = self._connect()
        try:
            clauses = []
            params: list[Any] = []
            if identity is not None:
                clauses.append("(from_id=? OR to_id=?)")
                params.extend([identity, identity])
            if not include_suggestions:
                clauses.append("availability='accepted'")
            else:
                clauses.append(
                    "(availability='accepted' OR "
                    "(availability='suggestion' AND disposition NOT IN "
                    "('dismissed','disputed')))"
                )
            where = " WHERE " + " AND ".join(clauses)
            rows = conn.execute(f"SELECT metadata_json FROM relationships{where} ORDER BY from_id,to_id,relation,public_id", params).fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            conn.close()
