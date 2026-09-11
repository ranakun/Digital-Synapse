"""Deterministic, disposable organization over one pinned v2 Gateway.

The projection is deliberately small and explainable.  It uses sparse lexical
features and bounded inverted-index candidates; it never writes a knowledge
record or turns proximity into a relationship assertion.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter, OrderedDict, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import networkx as nx

from synapse.util import vector_to_blob
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes
from synapse.v2_semantic import _index_path, get_runtime, prepared_vectors, source_passages

_ALGORITHM = "organization-5-louvain-nx3.6.1-text4"
_PROJECTION_SCHEMA = "organization-projection/6"
_CHUNKER = "paragraph-900-v2"
_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
_STOPWORDS = frozenset(
    ("a an and are as at be by for from has in is it of on or that the this to with "
     "i you your my me we our its was were have had do does true false null none "
     "not yes no he she his her they them their him who what when where how why "
     "would could should can will been being than then also only more much some "
     "all any each other very just there here these those such which out into "
     "about up so if but because through after before while want wanted needs "
     "need wants says said done did doing still even get got like one two new "
     "original source sources retained record records evidence user owner primary "
     "verbatim statement review derived sha body version revisions context path id "
     "name type availability support conditions limits status text processing json "
     "yaml csv txt md html http https www com linkedin entities inbox imported "
     "import current associated generated entity synthesis discovery").split()
)
_ADJACENCY_WORDS = frozenset(
    "adjacency imported import owner owns member-of source-of generated-from".split()
)
_HEX = frozenset("0123456789abcdef")
_SNAPSHOT_CACHE_LIMIT = 8
_SNAPSHOT_CACHE: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
_LATEST_SNAPSHOT: dict[tuple[Any, ...], tuple[str, Path, tuple[int, int, int, int]]] = {}


class _FrozenDict(dict):
    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("Organization snapshots are immutable")

    __delitem__ = __ior__ = __setitem__ = clear = pop = popitem = setdefault = update = _immutable


class _FrozenList(list):
    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("Organization snapshots are immutable")

    __delitem__ = __iadd__ = __imul__ = __setitem__ = append = clear = extend = insert = pop = remove = reverse = sort = _immutable


@dataclass(frozen=True)
class OrganizationConfig:
    """The frozen initial scoring configuration.

    A mapping can be supplied for calibration, but all values are copied into
    the snapshot identity so a changed threshold can never masquerade as the
    previous projection.
    """

    algorithm: str = _ALGORITHM
    max_neighbors: int = 24
    lexical_cosine_min: float = 0.22
    local_vector_cosine_min: float = 0.72
    community_resolution: float = 1.0
    min_units: int = 3
    max_memberships: int = 3
    secondary_membership_ratio: float = 0.75
    use_local_vectors: bool = False
    chunker: str = _CHUNKER

    def __post_init__(self) -> None:
        if self.algorithm != _ALGORITHM or self.chunker != _CHUNKER:
            raise V2Error("invalid-request", "Unsupported organization algorithm or chunker")
        for name, minimum, maximum in (
            ("max_neighbors", 1, 24),
            ("min_units", 3, 2**53 - 1),
            ("max_memberships", 1, 3),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise V2Error("invalid-request", f"{name} must be an integer between {minimum} and {maximum}")
        for name in (
            "lexical_cosine_min", "local_vector_cosine_min",
            "community_resolution", "secondary_membership_ratio",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not 0 < value <= 1 or not math.isfinite(value)
            ):
                raise V2Error("invalid-request", f"{name} must be a finite number greater than zero and at most one")
        if not isinstance(self.use_local_vectors, bool):
            raise V2Error("invalid-request", "use_local_vectors must be boolean")

    @classmethod
    def from_value(cls, value: Any) -> OrganizationConfig:
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise V2Error("invalid-request", "Organization config must be an object")
        fields = asdict(cls())
        unknown = set(value) - set(fields)
        if unknown:
            raise V2Error("invalid-request", "Organization config contains unknown fields")
        fields.update(value)
        return cls(**fields)


def _tokens(text: str) -> list[str]:
    # Structural serialization fields must not become the subject of a life
    # area. This changes navigation features only; exact originals, full-text
    # search and passage offsets retain every byte and every qualification.
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"https?://[^\s<>\]\)\"']+", " ", text)
    text = re.sub(r"\[\[[^\]|]+\|([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[\[[^\]]+\]\]", " ", text)
    text = re.sub(r"\b[\w./-]+\.(?:md|json|yaml|txt|csv|png|jpg|pdf)\b", " ", text)
    text = re.sub(r'"(?:id|version|source_id|source_version|text_version|hash|sha|path)"\s*:\s*"[^"\n]*"', " ", text)
    text = re.sub(r'"(?:[^"\\\r\n]|\\.)*"\s*:', " ", text)
    text = re.sub(r'\\(?:u[0-9a-fA-F]{4}|[nrt])', " ", text)
    return [token for token in _TOKEN_RE.findall(text.casefold()) if token not in _STOPWORDS and len(token) > 1 and "_" not in token and not token.isdecimal() and not (len(token) >= 20 and any(char.isdecimal() for char in token)) and not re.fullmatch(r"l\d+", token) and not (len(token) >= 32 and all(char in _HEX for char in token))]



def display_title(label: str) -> str:
    """Readable navigation text only. Exact paths remain in source references."""
    label = str(label).replace("\\", "/").rsplit("/", 1)[-1]
    if " — " in label:
        label = label.split(" — ", 1)[1]
    label = re.sub(r"\.(?:md|txt|csv|jsonl?|ya?ml|pdf|png|jpe?g)$", "", label, flags=re.I)
    label = re.sub(r"\b[0-9A-HJKMNP-TV-Z]{26}\b", "", label)
    label = re.sub(r"\b\d{4}-\d{2}(?:-\d{2})?\b", "", label)
    label = re.sub(r"^\d+[-_ ]+", "", label)
    label = re.sub(r"[-_]+", " ", label)
    label = " ".join(label.split()).strip(" —·")
    return label[:1].upper() + label[1:] if label else "Untitled material"


def _area_label(members: list[dict[str, Any]]) -> str:
    # Each canonical source gets one vote, however many passages it contains.
    unique = {m.get("source_id", m["id"]): m for m in members}
    records = [m for m in unique.values() if m["kind"] == "record"]
    kinds = Counter(m.get("entity_type", "") for m in records)
    if len(records) >= 3 and (kinds["person"] + kinds["company"]) / len(records) >= 0.72:
        return "People & organizations"
    counts: Counter[str] = Counter()
    excluded = {"article", "articles", "proposed", "proposal",
                "candidates", "manifest", "check", "integration", "coverage", "byte",
                "curated", "episode", "september", "august", "july", "june", "complete"}
    # Title prefixes before an em dash are attribution, not subjects.
    for member in unique.values():
        title = display_title(member["label"])
        if " — " in title:
            title = title.split(" — ", 1)[1]
        tokens = set(_tokens(title)) - excluded
        counts.update({token: 1.0 if member["kind"] == "record" else 0.35 for token in tokens})
    ranked = sorted(counts, key=lambda token: (-counts[token], token))
    if len(ranked) >= 2:
        return " & ".join(token.capitalize() for token in ranked[:2])
    if ranked:
        return ranked[0].capitalize()
    return "Import & review material"

def _features(text: str) -> Counter[str]:
    words = _tokens(text)
    counts: Counter[str] = Counter(words)
    counts.update(f"{left} {right}" for left, right in zip(words, words[1:], strict=False))
    return counts


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    small, large = (left, right) if len(left) <= len(right) else (right, left)
    dot = sum(value * large.get(key, 0.0) for key, value in small.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _safe_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _HEX for char in value)


def _hashable(value: Any) -> Any:
    if isinstance(value, float):
        return format(value, ".12g")
    if isinstance(value, list):
        return [_hashable(item) for item in value]
    if isinstance(value, dict):
        return {key: _hashable(item) for key, item in value.items()}
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze(item) for item in value)
    return value


def _file_stat(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns), int(stat.st_ctime_ns))


def _cache_integrity(value: dict[str, Any]) -> str:
    return hash_bytes(canonical_json(_hashable({key: item for key, item in value.items() if key != "integrity"})))


def _projection_integrity(value: dict[str, Any]) -> str:
    return hash_bytes(canonical_json(_hashable({key: item for key, item in value.items() if key != "projection_hash"})))


def _valid_projection(value: Any, *, organization_revision: str, knowledge_revision: str) -> bool:
    if (
        not isinstance(value, dict)
        or not _is_sha256(organization_revision)
        or value.get("organization_revision") != organization_revision
        or value.get("knowledge_revision") != knowledge_revision
        or value.get("projection_schema") != _PROJECTION_SCHEMA
        or value.get("projection_state") != "current"
        or not isinstance(value.get("configuration"), dict)
        or not isinstance(value.get("semantic"), dict)
        or not isinstance(value.get("areas"), list)
        or not _is_sha256(value.get("projection_hash"))
    ):
        return False
    try:
        return value["projection_hash"] == _projection_integrity(value)
    except (V2Error, TypeError, ValueError, OverflowError):
        return False


def _relation_is_thematic(edge: dict[str, Any]) -> bool:
    relation = str(edge.get("relation") or edge.get("type") or "").casefold()
    if any(word in relation for word in _ADJACENCY_WORDS):
        return False
    return edge.get("from_id") != "me" and edge.get("to_id") != "me"


def _record_family(record: dict[str, Any], identity: str) -> str:
    evidence = record.get("evidence")
    if isinstance(evidence, list):
        families = sorted(
            str(item["source_family_id"])
            for item in evidence
            if isinstance(item, dict) and item.get("source_family_id")
        )
        if families:
            return families[0]
    value = record.get("source_family_id") or (record.get("properties") or {}).get("source_family_id")
    return str(value) if isinstance(value, str) and value else identity


def _v2_duplicate_key(record: dict[str, Any]) -> bytes | None:
    if not record.get("record_kind") or not record.get("claim_key"):
        return None
    return canonical_json(
        {
            "subject_id": record.get("subject_id"),
            "claim_key": record.get("claim_key"),
            "record_kind": record.get("record_kind"),
            "statement": record.get("statement"),
            "conditions_and_limits": record.get("conditions_and_limits"),
            "support": record.get("support"),
            "counterevidence": record.get("counterevidence"),
            "alternatives": record.get("alternatives"),
            "would_change_with": record.get("would_change_with"),
            "owner_position": record.get("owner_position"),
            "epistemic_basis": record.get("epistemic_basis"),
            "as_of": record.get("as_of"),
            "applies_from": record.get("applies_from"),
            "applies_until": record.get("applies_until"),
        }
    )


def _round_robin(collections, limit, *, exclude=None):
    result, seen = [], {exclude}
    for offset in range(max((len(values) for values in collections), default=0)):
        for values in collections:
            if offset < len(values) and values[offset] not in seen:
                result.append(values[offset])
                seen.add(values[offset])
                if len(result) == limit:
                    return result
    return result


def _balanced_representatives(values, members, limit):
    families = defaultdict(list)
    for index in sorted(values, key=lambda item: members[item]["id"]):
        families[members[index]["family"]].append(index)
    return _round_robin([families[key] for key in sorted(families)], limit)


class Organization:
    """Build and page one revision-pinned organization projection."""

    def __init__(self, gateway: Any, *, config: OrganizationConfig | dict[str, Any] | None = None):
        if gateway is None or not hasattr(gateway, "view") or not hasattr(gateway, "revision"):
            raise V2Error("invalid-request", "Organization requires a pinned Gateway")
        self.gateway = gateway
        self.view = gateway.view
        if config is None:
            config = {"use_local_vectors": get_runtime(self.view.vault) is not None}
        self.config = OrganizationConfig.from_value(config)
        self._memory: dict[str, dict[str, Any]] = {}

    @property
    def _directory(self) -> Path:
        return self.view.vault / ".synapse" / "organization"

    def _path(self, revision: str) -> Path:
        return self._directory / f"{revision}.json"

    def _all_records(self) -> tuple[list[dict[str, Any]], int]:
        records: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.view.candidates(offset=offset, limit=200)
            records.extend(page["items"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        active = sum(1 for row in self.view.manifest["records"].values() if row.get("active"))
        return records, max(0, active - len(records))

    def _qualification(self, identity: str, *, policy=None) -> dict[str, Any]:
        policy_hash = (policy or self.view.source_purpose_policy()).snapshot_hash
        key = f"organization-qualification:{self.revision}:{policy_hash}"
        cache = self.gateway._cache.setdefault(key, {})
        if identity in cache:
            return _safe_json(cache[identity])
        try:
            result = self.gateway._unit(identity, closure_limit=128, supports_qualifications=True)
        except (AttributeError, V2Error) as exc:
            result = {
                "qualified": False,
                "complete": False,
                "withheld": True,
                "requires_revalidation": True,
                "notices": [f"Qualification expansion unavailable: {type(exc).__name__}"],
                "expansion": {"method": "context", "ids": [identity], "revision": self.revision},
            }
        purpose_notes = result.get("source_evidence_policy", {})
        needs_inspection = any(note.get("source_purpose_exclusion_candidate") for note in purpose_notes.values())
        qualification = {
            "state": "qualified" if result.get("qualified") or needs_inspection else "clear",
            "source_evidence_policy": purpose_notes,
            "withheld": bool(result.get("withheld") or result.get("complete") is False),
            "reason": result.get("reason"),
            "requires_revalidation": bool(result.get("requires_revalidation")),
            "provisional_dependencies": result.get("provisional_dependencies", []),
            "notices": result.get("notices", []),
            "limitations": result.get("limitations", []),
            "expansion": {"method": "context", "ids": [identity], "revision": self.revision},
        }
        cache[identity] = qualification
        return _safe_json(qualification)

    def _members(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        policy = self.view.source_purpose_policy()
        rows, suppressed = self._all_records()
        qualifications: dict[str, dict[str, Any]] = {}
        withheld = 0
        withheld_keys: Counter[bytes] = Counter()
        eligible = []
        for item in rows:
            identity = str(item["id"])
            qualification = self._qualification(identity, policy=policy)
            if qualification["withheld"]:
                withheld += 1
                key = _v2_duplicate_key(item["record"])
                if key is not None:
                    withheld_keys[key] += 1
                continue
            qualifications[identity] = qualification
            eligible.append(item)

        # Only qualified candidates can represent an exact-meaning group.
        # Sorting first keeps every duplicate ref pointed at the final winner,
        # including groups with more than two accepted/suggestion candidates.
        eligible.sort(key=lambda item: (
            0 if item["record"].get("availability", item.get("availability")) == "accepted" else 1,
            str(item["id"]),
        ))
        selected: dict[bytes, dict[str, Any]] = {}
        duplicate_record_refs: list[dict[str, str]] = []
        for item in eligible:
            key = _v2_duplicate_key(item["record"])
            if key is None:
                selected[b"legacy:" + str(item["id"]).encode("utf-8")] = item
                continue
            current = selected.get(key)
            if current is None:
                selected[key] = item
                continue
            duplicate_record_refs.append({"duplicate_id": str(item["id"]), "representative_id": str(current["id"])})
        rows = sorted(selected.values(), key=lambda item: str(item["id"]))
        members: list[dict[str, Any]] = []
        for item in rows:
            record = item["record"]
            identity = str(item["id"])
            qualification = qualifications[identity]
            for field in (
                "availability",
                "owner_review",
                "owner_position",
                "review_status",
                "record_kind",
                "epistemic_basis",
                "lifecycle",
                "support",
                "as_of",
                "applies_from",
                "applies_until",
            ):
                value = record.get(field, item.get(field))
                if value is not None:
                    qualification[field] = _safe_json(value)
            body = record.get("body", "")
            if "record_kind" in record:
                body = " ".join(
                    str(record.get(name, ""))
                    for name in (
                        "statement",
                        "conditions_and_limits",
                        "support",
                        "counterevidence",
                        "alternatives",
                        "would_change_with",
                    )
                )
            members.append(
                {
                    "id": f"record:{identity}",
                    "kind": "record",
                    "label": str(item.get("name") or identity),
                    "entity_type": record.get("type", item.get("type", "")),
                    "ref": {"record_id": identity, "record_version": item["version"]},
                    "text": f"{item.get('name', '')} {body}",
                    "family": _record_family(record, identity),
                    "record_id": identity,
                    "record_evidence": record.get("evidence", []) if isinstance(record.get("evidence", []), list) else [],
                    "qualification": qualification,
                    "material": record.get("record_kind") != "navigation",
                }
            )

        current = self.view.manifest.get("sources") or {}
        versions = self.view.manifest.get("source_versions") or {}
        excluded_sources = sum(not policy.visible(source_id, version) for source_id, version in current.items())
        passages = [p for p in source_passages(self.view) if policy.visible(p["source_id"], p["source_version"])]
        readable_versions = {str(passage["source_version"]) for passage in passages}
        unreadable = sum(1 for source_id, version in current.items() if policy.visible(source_id, version) and str(version) not in readable_versions)
        source_states: Counter[tuple[str, str, str]] = Counter()
        for source_id, version in current.items():
            if not policy.visible(source_id, version):
                continue
            descriptor = versions[version]
            text_state = "readable" if version in readable_versions else (
                "unavailable" if descriptor.get("text_version") else "no-text"
            )
            source_states[(descriptor["extraction"]["completeness"], descriptor["processing"], text_state)] += 1
        evidence_by_source: dict[tuple[str, str], list[tuple[int, int, str]]] = defaultdict(list)
        for record_member in members:
            for record_ref in record_member.get("record_evidence", []):
                if isinstance(record_ref, dict) and isinstance(record_ref.get("source_id"), str) and isinstance(record_ref.get("source_version"), str):
                    evidence_by_source[(record_ref["source_id"], record_ref["source_version"])].append(
                        (int(record_ref.get("byte_start", 0)), int(record_ref.get("byte_end", 0)), record_member["record_id"])
                    )
        duplicate_source_passage_refs: list[dict[str, str]] = []
        duplicate_source_refs: dict[tuple[str, str], dict[str, str]] = {}
        source_content_representatives: dict[str, tuple[str, str, str]] = {}
        for passage in passages:
            content_key = hash_bytes(str(passage["text"]).encode("utf-8"))
            previous = source_content_representatives.get(content_key)
            if previous is not None:
                duplicate_source_passage_refs.append(
                    {"duplicate_id": passage["identity"], "representative_id": previous[2]}
                )
                source_key = (passage["source_id"], passage["source_version"])
                previous_key = (previous[0], previous[1])
                if source_key != previous_key:
                    duplicate_source_refs.setdefault(
                        source_key,
                        {
                            "duplicate_source_id": passage["source_id"],
                            "duplicate_source_version": passage["source_version"],
                            "representative_id": previous[2],
                        },
                    )
                continue
            source_content_representatives[content_key] = (
                passage["source_id"],
                passage["source_version"],
                passage["identity"],
            )
            supporting_record_ids = []
            passage_ref = passage["evidence"]
            for start, end, record_id in evidence_by_source.get(
                (passage_ref["source_id"], passage_ref["source_version"]), [],
            ):
                if start < passage_ref["byte_end"] and passage_ref["byte_start"] < end:
                    supporting_record_ids.append(record_id)
            descriptor = versions[passage["source_version"]]
            extraction_state = descriptor["extraction"]["completeness"]
            members.append(
                {
                    "id": passage["identity"],
                    "kind": "source",
                    "label": str(passage["label"]),
                    "ref": {
                        "source_id": passage["source_id"],
                        "source_version": passage["source_version"],
                        "text_version": passage["text_version"],
                        "evidence": [passage["evidence"]],
                    },
                    "text": passage["text"],
                    "family": passage["source_family_id"],
                    "source_id": passage["source_id"],
                    "material": True,
                    "supporting_record_ids": sorted(set(supporting_record_ids)),
                    "qualification": {
                        "state": "source-material",
                        "basis": "retained-source-passage",
                        "assertion": False,
                        "availability": "source-material",
                        "review_status": "not-applicable",
                        "extraction": descriptor["extraction"],
                        "processing": descriptor["processing"],
                        "extraction_state": extraction_state,
                        "processing_state": descriptor["processing"],
                        "limitations": ["Extraction is partial; unextracted material is not represented."] if extraction_state == "partial" else [],
                    },
                }
            )
        coverage = {
            "suppressed_records": suppressed,
            "inactive_records": sum(not row.get("active") for row in self.view.manifest["records"].values()),
            "withheld_records": withheld,
            "withheld_duplicate_groups": sum(count > 1 and key not in selected for key, count in withheld_keys.items()),
            "duplicate_records": len(duplicate_record_refs),
            "duplicate_record_refs": sorted(duplicate_record_refs, key=lambda item: item["duplicate_id"]),
            "duplicate_sources": len(duplicate_source_refs),
            "duplicate_source_passages": len(duplicate_source_passage_refs),
            "duplicate_source_refs": [
                duplicate_source_refs[key] for key in sorted(duplicate_source_refs)
            ],
            "unreadable_sources": unreadable,
            "source_states": [
                {"extraction_state": extraction, "processing_state": processing, "text_state": text, "count": count}
                for (extraction, processing, text), count in sorted(source_states.items())
            ],
            "input_records": len([item for item in members if item["kind"] == "record" and item["material"]]),
            "navigation_hints": len([item for item in members if item["kind"] == "record" and not item["material"]]),
            "input_sources": len(current),
            "excluded_internal_sources": excluded_sources,
            "source_policy": policy.metadata(),
        }
        return members, coverage

    def _config_identity(self) -> dict[str, Any]:
        return {
            key: (format(value, ".12g") if isinstance(value, float) else value)
            for key, value in asdict(self.config).items()
        }

    def _cache_path(self, namespace: str, key: str) -> Path:
        return self._directory / namespace / f"{key}.json"

    def _read_cache(self, namespace: str, key: str) -> dict[str, Any] | None:
        path = self._cache_path(namespace, key)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(value, dict)
            or value.get("key") != key
            or not _is_sha256(value.get("integrity"))
            or value["integrity"] != _cache_integrity(value)
        ):
            return None
        return value

    def _write_cache(self, namespace: str, key: str, value: dict[str, Any]) -> None:
        directory = self._cache_path(namespace, key).parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".feature-", suffix=".json", dir=directory)
        os.close(fd)
        temporary = Path(temporary_name)
        payload = dict(value)
        payload["integrity"] = _cache_integrity(payload)
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, self._cache_path(namespace, key))
        finally:
            temporary.unlink(missing_ok=True)

    def _feature_key(self, member: dict[str, Any], *, model: str) -> str:
        return hash_bytes(
            canonical_json(
                {
                    "content": hash_bytes(str(member["text"]).encode("utf-8")),
                    "member": member["id"],
                    "ref": member["ref"],
                    "chunker": self.config.chunker,
                    "model": model,
                    "configuration": self._config_identity(),
                }
            )
        )

    def _cached_features(self, member: dict[str, Any], *, model: str) -> dict[str, int]:
        key = self._feature_key(member, model=model)
        cached = self._read_cache("features", key)
        counts = cached.get("counts") if cached else None
        if isinstance(counts, dict) and all(
            isinstance(token, str) and isinstance(value, int) and value >= 0
            for token, value in counts.items()
        ):
            return Counter(counts)
        counts = dict(_features(str(member["text"])))
        self._write_cache("features", key, {"key": key, "counts": counts})
        return Counter(counts)

    def _local_neighbors(self, members, runtime, model):
        loaded = prepared_vectors(self.view, model=model, runtime=runtime, include_sources=True)
        if loaded["semantic"] != "ok":
            return {}, loaded["semantic"], None
        by_id = {item["id"]: index for index, item in enumerate(members)}
        vectors, hashes = {}, []
        for identity, version, vector, kind, _ref in loaded["vectors"]:
            hashes.append([identity, version, hash_bytes(vector_to_blob(vector))])
            index = by_id.get(identity if kind == "source" else f"record:{identity}")
            if index is None:
                continue
            norm = math.sqrt(sum(value * value for value in vector))
            if norm:
                vectors[index] = [value / norm for value in vector]
        input_hash = hash_bytes(canonical_json(sorted(hashes)))
        if not vectors:
            return {}, "no-eligible-local-vectors", input_hash
        dimension = len(next(iter(vectors.values())))
        # Fixed random-sign projections supply approximate, bounded candidates.
        # Six bands of four bits: no all-pairs matrix or model call on map open.
        planes = [[1.0 if value & 1 else -1.0 for value in hashlib.shake_256(f"synapse-lsh-1:{plane}".encode()).digest(dimension)] for plane in range(24)]
        buckets, signatures = defaultdict(list), {}
        for index, vector in vectors.items():
            bits = [int(sum(a * b for a, b in zip(vector, plane, strict=True)) >= 0) for plane in planes]
            signature = [(band, tuple(bits[band * 4:band * 4 + 4])) for band in range(6)]
            signatures[index] = signature
            for key in signature:
                buckets[key].append(index)
        nominees = {key: _balanced_representatives(values, members, self.config.max_neighbors + 1) for key, values in buckets.items()}
        neighbors = defaultdict(list)
        for index, signature in signatures.items():
            selected = _round_robin([nominees[key] for key in signature], self.config.max_neighbors, exclude=index)
            for other in selected:
                score = sum(a * b for a, b in zip(vectors[index], vectors[other], strict=True))
                if score >= self.config.local_vector_cosine_min:
                    neighbors[index].append((other, score, "vector"))
        return neighbors, "ok", input_hash

    def _derive(self) -> dict[str, Any]:
        members, input_coverage = self._members()
        runtime = get_runtime(self.view.vault) if self.config.use_local_vectors else None
        local_model = getattr(runtime, "model", None) if runtime is not None else None
        semantic_state = {
            "requested": self.config.use_local_vectors,
            "model": local_model,
            "state": "available" if runtime is not None else ("unavailable" if self.config.use_local_vectors else "disabled"),
        }
        limitations = [
            "Lexical fallback does not claim semantic equivalence for paraphrases.",
            "Readable source passages are candidates; binary-only and failed extraction remain catalogued but unavailable here.",
        ]
        vector_neighbors: dict[int, list[tuple[int, float, str]]] = defaultdict(list)
        if self.config.use_local_vectors and runtime is None:
            limitations.append("Local vector blending was requested but no registered warm semantic runtime is available; no model was loaded.")
        elif runtime is not None and local_model:
            vector_neighbors, vector_state, vector_input_hash = self._local_neighbors(members, runtime, str(local_model))
            semantic_state["input_hash"] = vector_input_hash
            if vector_state != "ok":
                semantic_state["state"] = "unavailable"
                semantic_state["reason"] = vector_state
                limitations.append(f"Local vector blending is unavailable for this revision: {vector_state}.")
            else:
                semantic_state["state"] = "ready"
        input_rows = [
            {
                "id": item["id"],
                "kind": item["kind"],
                "ref": item["ref"],
                "qualification": item["qualification"],
                "text": item["text"],
            }
            for item in members
        ]
        config_identity = self._config_identity()
        identity = hash_bytes(
            canonical_json(
                {
                    "knowledge_revision": self.revision,
                    "projection_schema": _PROJECTION_SCHEMA,
                    "source_policy_hash": input_coverage["source_policy"]["snapshot_hash"],
                    "algorithm": config_identity,
                    "semantic": semantic_state,
                    "inputs": input_rows,
                }
            )
        )
        if identity in self._memory:
            return _safe_json(self._memory[identity])

        vectors: list[dict[str, float]] = []
        document_frequency: Counter[str] = Counter()
        for item in members:
            counts = self._cached_features(item, model="lexical")
            document_frequency.update(counts)
            vectors.append({key: float(value) for key, value in counts.items()})
        total = len(members)
        for vector in vectors:
            for key in list(vector):
                vector[key] *= math.log((total + 1) / (document_frequency[key] + 1)) + 1

        postings: dict[str, list[int]] = defaultdict(list)
        for index, vector in enumerate(vectors):
            for token in vector:
                postings[token].append(index)
        # Representatives are computed once per token, not sorted repeatedly
        # for every member of a large import. Candidate scoring is capped first.
        nominees = {token: _balanced_representatives(values, members, self.config.max_neighbors + 1) for token, values in postings.items()}
        neighbor_map: dict[int, list[tuple[int, float, str]]] = defaultdict(list)
        for index, vector in enumerate(vectors):
            terms = sorted((term for term in vector if 1 < len(postings[term]) <= max(3, total * .80)), key=lambda term: (len(postings[term]), -vector[term], term))[:8]
            if not terms:
                terms = sorted((term for term in vector if len(postings[term]) > 1), key=lambda term: (len(postings[term]), -vector[term], term))[:4]
            candidates = _round_robin([nominees[term] for term in terms], self.config.max_neighbors, exclude=index)
            scored = []
            for other in candidates:
                score = _cosine(vector, vectors[other])
                if score >= self.config.lexical_cosine_min:
                    scored.append((other, score, "lexical"))
            neighbor_map[index] = sorted(scored, key=lambda value: (-value[1], members[value[0]]["id"]))

        for index, values in vector_neighbors.items():
            neighbor_map[index].extend(values)

        by_record = {item.get("record_id"): index for index, item in enumerate(members) if item["kind"] == "record"}
        recorded: list[dict[str, Any]] = []
        try:
            edges = self.view.relationships(include_suggestions=True)
        except (V2Error, OSError):
            edges = []
        relation_degrees: Counter[str] = Counter()
        for edge in edges:
            if not _relation_is_thematic(edge):
                continue
            left, right = by_record.get(edge.get("from_id")), by_record.get(edge.get("to_id"))
            if left is None or right is None:
                continue
            assertion_id = edge.get("record_id")
            if isinstance(assertion_id, str):
                try:
                    assertion = self.gateway._unit(assertion_id, closure_limit=128, supports_qualifications=True)
                except (AttributeError, V2Error):
                    continue
                if assertion.get("withheld") or assertion.get("complete") is False:
                    continue
            recorded.append(edge)
            relation_degrees[edge["from_id"]] += 1
            relation_degrees[edge["to_id"]] += 1
        for edge in recorded:
            degree = math.sqrt(
                relation_degrees[edge["from_id"]] * relation_degrees[edge["to_id"]]
            )
            score = 1.0 / degree if degree else 0.0
            left, right = by_record[edge["from_id"]], by_record[edge["to_id"]]
            neighbor_map[left].append((right, score, "recorded"))
            neighbor_map[right].append((left, score, "recorded"))

        # Keep the recorded channel inside the same bounded candidate budget.
        # A high-degree relation remains represented in the coverage/read
        # surfaces; it cannot make organization quadratic.
        for index, values in list(neighbor_map.items()):
            best: dict[int, tuple[float, str]] = {}
            for other, score, channel in values:
                previous = best.get(other)
                if previous is None or score > previous[0] or (score == previous[0] and channel < previous[1]):
                    best[other] = (score, channel)
            ordered = sorted(
                ((score, members[other]["id"], other, channel) for other, (score, channel) in best.items()),
                key=lambda value: (-value[0], value[1], value[3]),
            )[: self.config.max_neighbors]
            neighbor_map[index] = [(other, score, channel) for score, _id, other, channel in ordered]

        # Community detection groups the bounded similarity/relation graph.
        # Pairwise seed-neighborhood merging fragmented realistic source text
        # into thousands of tiny overlapping areas; lowering its threshold
        # instead collapsed most material into one import-shaped component.
        # A pinned library, stable insertion order and fixed seed make this
        # disposable navigation derivation reproducible without model calls.
        graph = nx.Graph()
        graph.add_nodes_from(range(len(members)))
        for index in sorted(neighbor_map):
            for other, score, _channel in neighbor_map[index]:
                if index == other or score <= 0:
                    continue
                prior = graph.get_edge_data(index, other, {}).get("weight", 0.0)
                graph.add_edge(index, other, weight=max(prior, score))
        neighborhoods = []
        if graph.number_of_edges():
            communities = nx.community.louvain_communities(
                graph, resolution=self.config.community_resolution,
                threshold=1e-7, max_level=10, seed=0,
            )
            for community in communities:
                neighborhoods.extend(set(component) for component in nx.connected_components(graph.subgraph(community)))
        neighborhoods.sort(key=lambda group: tuple(sorted(group)))

        def material_count(neighborhood):
            record_ids = {
                members[index]["record_id"]
                for index in neighborhood
                if members[index]["kind"] == "record" and members[index]["material"]
            }
            supporting_sources = {
                members[index]["source_id"]
                for index in neighborhood
                if members[index]["kind"] == "source"
                and set(members[index].get("supporting_record_ids", [])) & record_ids
            }
            return len(record_ids) + len({
                members[index]["source_id"] for index in neighborhood
                if members[index]["kind"] == "source" and members[index]["source_id"] not in supporting_sources
            })

        raw_groups: list[set[int]] = []
        seen_groups: set[tuple[int, ...]] = set()
        for neighborhood in neighborhoods:
            key = tuple(sorted(neighborhood))
            independent_count = material_count(neighborhood)
            if independent_count < self.config.min_units or key in seen_groups:
                continue
            seen_groups.add(key)
            raw_groups.append(set(neighborhood))

        area_ids = [
            "area:" + hash_bytes(canonical_json(sorted(members[index]["id"] for index in group)))
            for group in raw_groups
        ]
        memberships: dict[int, list[tuple[float, str, int]]] = defaultdict(list)
        incoming_neighbors: dict[int, set[int]] = defaultdict(set)
        for index, values in neighbor_map.items():
            for other, _score, _channel in values:
                incoming_neighbors[other].add(index)
        for group_index, group in enumerate(raw_groups):
            candidates = set(group)
            for member_index in group:
                candidates.update(incoming_neighbors[member_index])
            for index in sorted(candidates):
                supports = [
                    (score, channel)
                    for other, score, channel in neighbor_map[index]
                    if other in group
                ]
                support, support_channel = max(supports, default=(0.0, "lexical"))
                if support >= self.config.lexical_cosine_min or support_channel == "recorded":
                    memberships[index].append((support, area_ids[group_index], group_index))
        for index in memberships:
            choices = sorted(memberships[index], key=lambda value: (-value[0], value[1]))
            best = choices[0][0]
            memberships[index] = [
                choice
                for choice in choices
                if choice[0] >= best * self.config.secondary_membership_ratio
            ][: self.config.max_memberships]

        area_members: list[set[int]] = [set() for _ in raw_groups]
        for index, choices in memberships.items():
            for _score, _area_id, group_index in choices:
                area_members[group_index].add(index)

        member_by_id = {item["id"]: item for item in members}
        area_values: list[dict[str, Any]] = []
        for group_index, group in enumerate(area_members):
            if material_count(group) < self.config.min_units:
                continue
            label = _area_label([members[index] for index in sorted(group)])
            member_ids = sorted(members[index]["id"] for index in group)
            area_values.append(
                {
                    "id": area_ids[group_index],
                    "label": label,
                    "summary": {
                        "text": f"Material grouped around {label}; see the retained members for exact evidence.",
                        "member_ids": member_ids,
                        "limitations": ["Area labels and summaries are derived navigation metadata, not evidence."],
                    },
                    "member_ids": member_ids,
                    "coverage": self._coverage_for(member_ids, member_by_id, len(member_ids)),
                }
            )

        member_values: list[dict[str, Any]] = []
        valid_area_ids = {area["id"] for area in area_values}
        recorded_incident_ids = {
            endpoint
            for edge in recorded
            for endpoint in (edge.get("from_id"), edge.get("to_id"))
            if isinstance(endpoint, str)
        }
        for index, item in enumerate(members):
            area_ids_for_member = sorted(
                area_id
                for _score, area_id, _group in memberships.get(index, [])
                if area_id in valid_area_ids
            )
            reasons = sorted({"local vector similarity" if channel == "vector" else "lexical similarity" for _other, _score, channel in neighbor_map[index] if channel in {"lexical", "vector"}}) if area_ids_for_member else []
            if item.get("record_id") in recorded_incident_ids:
                reasons.append("recorded thematic relation")
            value = {
                key: item[key]
                for key in ("id", "kind", "label", "ref", "qualification")
            }
            value.update(area_ids=area_ids_for_member, reasons=reasons)
            member_values.append(value)
        loose = sorted(item["id"] for item in member_values if not item["area_ids"])

        links = self._links(area_values, members, recorded, neighbor_map)
        area_member_ids = {member_id for area in area_values for member_id in area["member_ids"]}
        grouped_members = [member_by_id[member_id] for member_id in area_member_ids]
        coverage = {
            **input_coverage,
            "unique_records": len({item["record_id"] for item in grouped_members if item["kind"] == "record" and item["material"]}),
            "unique_sources": len({item["source_id"] for item in grouped_members if item["kind"] == "source"}),
            "memberships": sum(len(item["area_ids"]) for item in member_values),
            "loose_records": sum(item["kind"] == "record" and item["material"] for item in members if item["id"] in set(loose)),
            "loose_sources": len({item["source_id"] for item in members if item["id"] in set(loose) and item["kind"] == "source"}),
        }
        method = "lexical-tfidf+local-vector" if semantic_state["state"] == "ready" else "lexical-tfidf"
        projection = {
            "knowledge_revision": self.revision,
            "organization_revision": identity,
            "projection_schema": _PROJECTION_SCHEMA,
            "source_policy_hash": input_coverage["source_policy"]["snapshot_hash"],
            "projection_state": "current",
            "method": method,
            "semantic": semantic_state,
            "configuration": asdict(self.config),
            "coverage": coverage,
            "limitations": limitations,
            "areas": sorted(area_values, key=lambda value: value["id"]),
            "members": sorted(member_values, key=lambda value: value["id"]),
            "links": links,
            "member_links": self._member_links(area_values, members, recorded, neighbor_map),
            "loose": loose,
            "lineage": self._lineage(area_values, method=method, semantic=semantic_state),
        }
        projection["projection_hash"] = _projection_integrity(projection)
        self._memory[identity] = _safe_json(projection)
        return projection

    def _lineage(self, areas: list[dict[str, Any]], *, method: str, semantic: dict[str, Any]) -> list[dict[str, Any]]:
        parent_revision = self.view.manifest.get("parent")
        if not parent_revision or not self._directory.is_dir():
            return []
        previous: dict[str, Any] | None = None
        # Vector content hashes change across knowledge revisions. The model,
        # method and availability/fallback mode must still match exactly.
        semantic_mode = {key: value for key, value in semantic.items() if key != "input_hash"}
        for path in sorted(self._directory.glob("*.json")):
            if not _is_sha256(path.stem):
                continue
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not _valid_projection(candidate, organization_revision=path.stem, knowledge_revision=parent_revision):
                continue
            try:
                candidate_config = OrganizationConfig.from_value(candidate["configuration"])
            except V2Error:
                continue
            candidate_semantic = candidate["semantic"]
            if (
                _hashable(asdict(candidate_config)) != self._config_identity()
                or candidate["configuration"] != asdict(candidate_config)
                or candidate.get("method") != method
                or {key: value for key, value in candidate_semantic.items() if key != "input_hash"} != semantic_mode
                or (candidate_semantic.get("state") == "ready" and not _is_sha256(candidate_semantic.get("input_hash")))
            ):
                continue
            if not all(
                isinstance(area, dict) and isinstance(area.get("id"), str)
                and isinstance(area.get("member_ids"), list)
                and all(isinstance(member_id, str) for member_id in area["member_ids"])
                for area in candidate["areas"]
            ):
                continue
            previous = candidate
            break
        if not previous:
            return []
        old_areas = {
            area["id"]: set(area.get("member_ids", []))
            for area in previous.get("areas", [])
            if isinstance(area, dict) and isinstance(area.get("member_ids"), list)
        }
        new_areas = {area["id"]: set(area["member_ids"]) for area in areas}
        lineage: list[dict[str, Any]] = []
        for area_id, members in sorted(new_areas.items()):
            overlaps = sorted(old_id for old_id, old_members in old_areas.items() if members & old_members)
            if len(overlaps) > 1:
                lineage.append({"kind": "merge", "area_id": area_id, "previous_area_ids": overlaps})
        for old_id, members in sorted(old_areas.items()):
            overlaps = sorted(area_id for area_id, new_members in new_areas.items() if members & new_members)
            if len(overlaps) > 1:
                lineage.append({"kind": "split", "previous_area_id": old_id, "area_ids": overlaps})
        return lineage

    @staticmethod
    def _coverage_for(member_ids: list[str], member_by_id: dict[str, dict[str, Any]], memberships: int) -> dict[str, int]:
        selected = [member_by_id[member_id] for member_id in member_ids if member_id in member_by_id]
        return {
            "unique_records": len({item["record_id"] for item in selected if item["kind"] == "record" and item["material"]}),
            "unique_sources": len({item["source_id"] for item in selected if item["kind"] == "source"}),
            "memberships": memberships,
        }

    def _links(
        self,
        areas: list[dict[str, Any]],
        members: list[dict[str, Any]],
        recorded: list[dict[str, Any]],
        neighbor_map: dict[int, list[tuple[int, float, str]]],
    ) -> list[dict[str, Any]]:
        area_sets = {area["id"]: set(area["member_ids"]) for area in areas}
        links: list[dict[str, Any]] = []
        for left_index, left in enumerate(areas):
            for right in areas[left_index + 1 :]:
                shared = sorted(area_sets[left["id"]] & area_sets[right["id"]])
                if shared:
                    links.append(self._link(left["id"], right["id"], "overlap", shared, [], "Areas share overlapping qualified material."))
        area_for_member = defaultdict(set)
        for area in areas:
            for member_id in area["member_ids"]:
                area_for_member[member_id].add(area["id"])
        similarity_pairs: dict[tuple[str, str], set[str]] = defaultdict(set)
        similarity_methods: dict[tuple[str, str], set[str]] = defaultdict(set)
        for index, values in neighbor_map.items():
            left_areas = area_for_member.get(members[index]["id"], [])
            for other, _score, channel in values:
                if channel not in {"lexical", "vector"}:
                    continue
                for left in left_areas:
                    for right in area_for_member.get(members[other]["id"], []):
                        if left != right:
                            pair = tuple(sorted((left, right)))
                            similarity_pairs[pair].update((members[index]["id"], members[other]["id"]))
                            similarity_methods[pair].add(channel)
        for (left, right), member_ids in sorted(similarity_pairs.items()):
            methods = " and ".join("local vector" if method == "vector" else method for method in sorted(similarity_methods[(left, right)]))
            links.append(self._link(left, right, "similarity", sorted(member_ids), [], f"Retained material has qualifying {methods} similarity across these areas."))
        by_record_area = defaultdict(set)
        for item in members:
            if item["kind"] == "record":
                by_record_area[item["record_id"]].update(area_for_member.get(item["id"], []))
        for edge in recorded:
            for left in by_record_area.get(edge.get("from_id"), set()):
                for right in by_record_area.get(edge.get("to_id"), set()):
                    if left != right:
                        links.append(self._link(left, right, "recorded", [], [edge.get("record_id") or edge.get("id")], "A retained thematic relation crosses these areas."))
        return sorted(links, key=lambda value: value["id"])

    def _member_links(
        self,
        areas: list[dict[str, Any]],
        members: list[dict[str, Any]],
        recorded: list[dict[str, Any]],
        neighbor_map: dict[int, list[tuple[int, float, str]]],
    ) -> list[dict[str, Any]]:
        """Return bounded, within-area edges for map rendering.

        These links are derived navigation data.  They never become canonical
        relationships and only connect members that already share an area.
        """

        area_for_member: dict[str, set[str]] = defaultdict(set)
        for area in areas:
            for member_id in area["member_ids"]:
                area_for_member[member_id].add(area["id"])
        similarity: dict[tuple[str, str], tuple[float, str]] = {}
        for index, values in neighbor_map.items():
            left_id = members[index]["id"]
            for other, score, channel in values:
                if channel not in {"lexical", "vector"}:
                    continue
                right_id = members[other]["id"]
                if not area_for_member.get(left_id, set()) & area_for_member.get(right_id, set()):
                    continue
                start, end = sorted((left_id, right_id))
                pair = (start, end)
                previous = similarity.get(pair)
                candidate = (float(score), channel)
                if previous is None or candidate[0] > previous[0] or (
                    candidate[0] == previous[0] and candidate[1] < previous[1]
                ):
                    similarity[pair] = candidate
        links = [
            {
                "id": "member-link:" + hash_bytes(canonical_json([left, right, "similarity", method])),
                "from": left,
                "to": right,
                "channel": "similarity",
                "explanation": "Retained members share a qualifying derived similarity within an area.",
                "method": method,
            }
            for (left, right), (_score, method) in sorted(similarity.items())
        ]
        by_record_member = {
            item["record_id"]: item["id"]
            for item in members
            if item["kind"] == "record"
        }
        recorded_pairs: dict[tuple[str, str, str], dict[str, Any]] = {}
        for edge in recorded:
            assertion_id = edge.get("record_id") if isinstance(edge.get("record_id"), str) else None
            legacy_edge_id = edge.get("id") if isinstance(edge.get("id"), str) else None
            left = by_record_member.get(edge.get("from_id"))
            right = by_record_member.get(edge.get("to_id"))
            if left is None or right is None or (assertion_id is None and legacy_edge_id is None):
                continue
            if not area_for_member.get(left, set()) & area_for_member.get(right, set()):
                continue
            identity = assertion_id or legacy_edge_id
            key = (left, right, identity)
            if key in recorded_pairs:
                continue
            link = {
                "id": "member-link:" + hash_bytes(canonical_json([left, right, "recorded", identity])),
                "from": left,
                "to": right,
                "channel": "recorded",
                "explanation": "A retained thematic assertion connects these members within an area.",
            }
            if assertion_id is not None:
                link["assertion_id"] = assertion_id
            else:
                link["assertion_id"] = None
                link["legacy_edge_id"] = legacy_edge_id
                link["metadata"] = _safe_json(edge)
                link["qualification"] = {
                    "state": "legacy-edge",
                    "withheld": False,
                    "complete": False,
                    "limitations": [
                        "Legacy relationship: passage-level provenance and declared dependency coverage may be incomplete."
                    ],
                }
            recorded_pairs[key] = link
        links.extend(recorded_pairs[key] for key in sorted(recorded_pairs))
        return sorted(links, key=lambda value: value["id"])

    @staticmethod
    def _link(left: str, right: str, channel: str, member_ids: list[str], assertion_ids: list[str], explanation: str) -> dict[str, Any]:
        start, end = sorted((left, right))
        return {
            "id": "link:" + hash_bytes(canonical_json([start, end, channel, member_ids, assertion_ids])),
            "from": start,
            "to": end,
            "channel": channel,
            "count": len(member_ids) if member_ids else len(assertion_ids),
            "member_ids": member_ids,
            "assertion_ids": assertion_ids,
            "explanation": explanation,
        }

    @property
    def revision(self) -> str:
        return self.gateway.revision

    def _load(self, organization_revision: str) -> dict[str, Any]:
        if not _is_sha256(organization_revision):
            raise V2Error("invalid-request", "organization_revision must be a SHA-256 content address")
        path = self._path(organization_revision)
        if not path.is_file():
            raise V2Error("revision-unavailable", "Requested organization projection is unavailable", details={"revision": organization_revision})
        if path.name != f"{organization_revision}.json":
            raise V2Error("revision-unavailable", "Requested organization projection is unavailable", details={"revision": organization_revision})
        try:
            stat = _file_stat(path)
        except OSError as exc:
            raise V2Error("revision-unavailable", "Requested organization projection is unavailable", details={"revision": organization_revision}) from exc
        cache_key = self._snapshot_cache_key(organization_revision, stat)
        cached = _SNAPSHOT_CACHE.get(cache_key)
        if cached is not None:
            _SNAPSHOT_CACHE.move_to_end(cache_key)
            return cached
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise V2Error("revision-unavailable", "Requested organization projection is unavailable", details={"revision": organization_revision}) from exc
        if not _valid_projection(value, organization_revision=organization_revision, knowledge_revision=self.revision):
            raise V2Error("revision-unavailable", "Organization projection identity is invalid", details={"revision": organization_revision})
        if value.get("source_policy_hash") != self.view.source_purpose_policy().snapshot_hash:
            raise V2Error("revision-unavailable", "Source discovery policy changed; rediscover the organization projection")
        frozen = _freeze(value)
        _SNAPSHOT_CACHE[cache_key] = frozen
        _SNAPSHOT_CACHE.move_to_end(cache_key)
        while len(_SNAPSHOT_CACHE) > _SNAPSHOT_CACHE_LIMIT:
            _SNAPSHOT_CACHE.popitem(last=False)
        return frozen

    def _remember_current(self, value, prefix):
        revision = value["organization_revision"]
        path = self._path(revision)
        _LATEST_SNAPSHOT[prefix] = (revision, path, _file_stat(path))
        return value

    def _runtime_binding(self):
        runtime = get_runtime(self.view.vault) if self.config.use_local_vectors else None
        path = _index_path(self.view.vault, self.revision)
        try:
            stamp = _file_stat(path) if runtime else None
        except OSError:
            stamp = None
        return (getattr(runtime, "model", None), stamp)

    def _current_vector_hash(self):
        runtime = get_runtime(self.view.vault) if self.config.use_local_vectors else None
        if runtime is None:
            return None
        loaded = prepared_vectors(self.view, model=runtime.model, runtime=runtime)
        if loaded["semantic"] != "ok":
            return None
        return hash_bytes(canonical_json(sorted([identity, version, hash_bytes(vector_to_blob(vector))] for identity, version, vector, _kind, _ref in loaded["vectors"])))

    def _snapshot_prefix(self) -> tuple[Any, ...]:
        return (
            str(self.view.vault.resolve()), self.revision,
            hash_bytes(canonical_json(self._config_identity())), self._runtime_binding(),
            self.view.source_purpose_policy().snapshot_hash,
        )

    def _snapshot_cache_key(self, organization_revision: str, stat: tuple[int, int, int, int]) -> tuple[Any, ...]:
        return (*self._snapshot_prefix(), organization_revision, stat)

    def _snapshot_view(self, *, organization_revision: str | None = None, rebuild: bool = False) -> dict[str, Any]:
        """Borrow the validated immutable projection for trusted read-only callers.

        The returned object is process-cached and must not be mutated.  Public
        ``snapshot`` returns a defensive JSON copy; page APIs copy only their
        emitted page and metadata.
        """

        if organization_revision is not None:
            if not _is_sha256(organization_revision):
                raise V2Error("invalid-request", "organization_revision must be a SHA-256 content address")
            return self._load(organization_revision)
        if not isinstance(rebuild, bool):
            raise V2Error("invalid-request", "rebuild must be boolean")
        prefix = self._snapshot_prefix()
        if not rebuild:
            latest = _LATEST_SNAPSHOT.get(prefix)
            if latest is not None:
                latest_revision, latest_path, latest_stat = latest
                try:
                    if latest_path.is_file() and _file_stat(latest_path) == latest_stat:
                        return self._load(latest_revision)
                except (OSError, V2Error):
                    _LATEST_SNAPSHOT.pop(prefix, None)
            vector_hash = self._current_vector_hash()
            for candidate in sorted(self._directory.glob("*.json")) if self._directory.is_dir() else []:
                try:
                    value = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                if not isinstance(value, dict) or not isinstance(value.get("semantic"), dict):
                    continue
                candidate_revision = value.get("organization_revision")
                if (
                    value.get("knowledge_revision") == self.revision
                    and value.get("configuration") == asdict(self.config)
                    and value.get("semantic", {}).get("model") == (getattr(get_runtime(self.view.vault), "model", None) if self.config.use_local_vectors else None)
                    and value.get("semantic", {}).get("input_hash") == vector_hash
                    and _is_sha256(candidate_revision)
                ):
                    try:
                        return self._remember_current(self._load(candidate_revision), prefix)
                    except V2Error:
                        continue
        value = self._derive()
        path = self._path(value["organization_revision"])
        # Reaching derivation means no valid current projection was reused.
        # Replace even an existing corrupt path; explicit version reads return
        # through _load above and never enter this repair path.
        self._directory.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".organization-", suffix=".json", dir=self._directory)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return self._remember_current(self._load(value["organization_revision"]), prefix)

    def snapshot(self, *, organization_revision: str | None = None, rebuild: bool = False) -> dict[str, Any]:
        return _safe_json(self._snapshot_view(organization_revision=organization_revision, rebuild=rebuild))

    @staticmethod
    def _page(items: list[dict[str, Any]], offset: int, limit: int) -> dict[str, Any]:
        total = len(items)
        selected = items[offset : offset + limit]
        return {"items": selected, "total": total, "offset": offset, "next_offset": offset + len(selected) if offset + len(selected) < total else None, "truncated": offset + len(selected) < total}

    def areas(self, *, query: str = "", organization_revision: str | None = None, offset: int = 0, limit: int = 24) -> dict[str, Any]:
        if not isinstance(query, str) or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise V2Error("invalid-request", "Invalid organization area page")
        value = self._snapshot_view(organization_revision=organization_revision)
        needle = query.casefold().strip()
        items = [area for area in value["areas"] if not needle or needle in f"{area['label']} {area['summary']['text']}".casefold()]
        result = {key: value[key] for key in ("knowledge_revision", "organization_revision", "projection_state", "method", "coverage", "limitations")} | self._page(items, offset, limit)
        return _safe_json(result)

    def area(self, area_id: str, *, organization_revision: str, offset: int = 0, limit: int = 30) -> dict[str, Any]:
        if not isinstance(area_id, str) or not area_id or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 150:
            raise V2Error("invalid-request", "Invalid organization area request")
        value = self._snapshot_view(organization_revision=organization_revision)
        area = next((item for item in value["areas"] if item["id"] == area_id), None)
        if area is None:
            raise V2Error("ambiguous-identity", "Area identity does not resolve in this projection")
        by_id = {item["id"]: item for item in value["members"]}
        page = self._page([by_id[item] for item in area["member_ids"]], offset, limit)
        result = {key: value[key] for key in ("knowledge_revision", "organization_revision", "projection_state", "method", "coverage", "limitations")} | {"area": area, **page}
        return _safe_json(result)
