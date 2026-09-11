"""Additive v1 inventory, isolated migration trial, and restore verification.

No implicit cutover. Preparing a snapshot reads the specified vault; activation
requires the publisher's exact owner-bound bootstrap capability.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from synapse.knowledge import record_descriptor
from synapse.owner_host import OwnerHost
from synapse.publication import Publisher, snapshot_fingerprint
from synapse.revisions import RevisionStore, is_v2
from synapse.source_extractors import prepare_source_file
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes, version_for


def _allowed(path: Path) -> bool:
    return not any(part == ".env" or part.startswith(".env.") for part in path.parts)


@dataclass
class Snapshot:
    records: dict[str, bytes]
    sources: list[dict]
    objects: dict[str, bytes]
    legacy_edges: list[dict]
    report: dict

    @property
    def fingerprint(self):
        return snapshot_fingerprint(self.records, self.sources, self.legacy_edges)

    @property
    def review_fingerprint(self):
        """Stable review of input bytes and extraction, before capture assigns IDs.

        Fresh preparation allocates source IDs and capture timestamps. Those
        bookkeeping values must not invalidate a review of unchanged material.
        Preserve family membership by its member origins instead of temporary
        IDs. Publication still binds the exact prepared descriptors through
        ``fingerprint``; this projection is only for preview-to-activation review.
        """
        families = {}
        for source in self.sources:
            families.setdefault(source["source_family_id"], []).append(source["origin"])
        sources = []
        for source in self.sources:
            row = {
                key: value for key, value in source.items()
                if key not in {"id", "version", "captured_at", "source_family_id"}
            }
            row["family_origins"] = sorted(families[source["source_family_id"]])
            sources.append(row)
        return snapshot_fingerprint(
            self.records, sorted(sources, key=canonical_json), self.legacy_edges
        )


def _stable_edges(relations):
    occurrences, output = Counter(), []
    for relation in relations:
        key = (relation.from_id, relation.to_id, relation.type, bool(relation.weak))
        ordinal = occurrences[key]
        occurrences[key] += 1
        output.append(
            relation.to_dict()
            | {"id": "legacy:" + hashlib.sha256(canonical_json([*key, ordinal])).hexdigest()}
        )
    # Existing YAML source metadata can contain date scalars. The old index
    # serialized them as strings; this is a relation projection, not a rewrite.
    return json.loads(json.dumps(output, default=str))


def prepare(vault: Path) -> Snapshot:
    vault = Path(vault).resolve()
    if is_v2(vault):
        raise V2Error(
            "invalid-request", "This vault already has a v2 revision; use status or recovery"
        )
    if not (vault / "entities").is_dir():
        raise V2Error("invalid-request", "Migration requires an existing entity vault")
    records, descriptors, objects, sources = {}, {}, {}, []
    for path in sorted((vault / "entities").rglob("*.md")):
        if path.is_symlink() or not _allowed(path.relative_to(vault)):
            raise V2Error("invalid-path", "Migration requires ordinary canonical entity files")
        relative = path.relative_to(vault).as_posix()
        raw = path.read_bytes()
        descriptor = record_descriptor(raw, path=relative)
        if descriptor["id"] in descriptors:
            raise V2Error(
                "ambiguous-identity",
                "Migration found duplicate canonical identities",
                details={"id": descriptor["id"]},
            )
        records[relative], descriptors[descriptor["id"]] = raw, descriptor
    omitted_links = []
    source_families = {}
    for directory in ("inbox", "sources"):
        root = vault / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(vault)
            if not _allowed(relative) or path.name.startswith("."):
                continue
            if path.is_symlink():
                omitted_links.append(relative.as_posix())
                continue
            if not path.is_file():
                continue
            source, prepared = prepare_source_file(path, origin=relative.as_posix())
            family = source_families.setdefault(source["original_hash"], source["source_family_id"])
            source["source_family_id"] = family
            source["version"] = version_for("source_version", source)
            sources.append(source)
            objects.update(prepared)
    from synapse.index import _relations_for_entities
    from synapse.parser import _parse_entity_file_internal

    entities = []
    for identity, descriptor in descriptors.items():
        if descriptor["active"]:
            entity, issues = _parse_entity_file_internal(
                vault / descriptor["path"], vault, retained_bytes=records[descriptor["path"]]
            )
            if entity is None or any(issue.severity == "error" for issue in issues):
                raise V2Error(
                    "invalid-record",
                    "Existing entity cannot be parsed without loss",
                    details={"id": identity},
                )
            entities.append(entity)
    relations, issues = _relations_for_entities(entities)
    edges = _stable_edges(relations)
    report = {
        "entities": len(records),
        "active_entities": sum(row["active"] for row in descriptors.values()),
        "excluded_entities": sum(not row["active"] for row in descriptors.values()),
        "trust": dict(Counter(row["review_status"] for row in descriptors.values())),
        "relations": len(edges),
        "sources": len(sources),
        "source_bytes": sum(len(objects[source["original_hash"]]) for source in sources),
        "extraction": dict(Counter(source["extraction"]["completeness"] for source in sources)),
        "unresolved_relation_notices": len(issues),
        "unfollowed_source_links": omitted_links,
        "limitations": [
            "Existing locators and owner labels are retained, not newly verified. Legacy statements do not gain invented passage anchors or pre-baseline knowledge history.",
            "Source retention covers ordinary files under inbox/ and sources/. External or absent source locations remain explicitly outside this snapshot.",
        ],
    }
    result = Snapshot(records, sources, objects, edges, report)
    report["snapshot_hash"] = result.review_fingerprint
    return result


def activate(
    vault: Path, snapshot: Snapshot, capability: dict, *, operation_id: str, request_id: str
) -> dict:
    # Recheck the editable baseline immediately before entering the publisher.
    # Actual cutover requires a coordinated writer boundary as documented.
    current = {
        path.relative_to(vault).as_posix(): hash_bytes(path.read_bytes())
        for path in (vault / "entities").rglob("*.md")
        if path.is_file() and _allowed(path.relative_to(vault))
    }
    expected = {path: hash_bytes(raw) for path, raw in snapshot.records.items()}
    if current != expected:
        raise V2Error("external-edit-conflict", "The migration baseline changed before activation")
    for source in snapshot.sources:
        original = vault / source["origin"]
        if not original.is_file() or hash_bytes(original.read_bytes()) != source["original_hash"]:
            raise V2Error(
                "external-edit-conflict", "An original source changed before migration activation"
            )
    return Publisher(vault).bootstrap(
        capability,
        operation_id=operation_id,
        request_id=request_id,
        records=snapshot.records,
        sources=snapshot.sources,
        objects=snapshot.objects,
        legacy_edges=snapshot.legacy_edges,
    )


def _copy_paths(vault: Path):
    for path in sorted(vault.rglob("*")):
        relative = path.relative_to(vault)
        if not _allowed(relative) or path.is_symlink() or not path.is_file():
            continue
        if relative.parts[0] == ".synapse":
            # Reviewed source-purpose decisions are local policy, not a
            # reproducible index. A restore must preserve those decisions.
            purpose_snapshot = (len(relative.parts) == 3
                                and relative.parts[1] == "source-purpose"
                                and relative.suffix == ".json")
            if relative.as_posix() != ".synapse/config.yaml" and not purpose_snapshot:
                continue
        yield path, relative


def copy_stable(vault: Path, destination: Path) -> dict:
    vault, destination = Path(vault).resolve(), Path(destination).resolve()
    if destination == vault or vault in destination.parents or destination.exists():
        raise V2Error("invalid-path", "A snapshot requires a new directory outside the live vault")
    before = {
        relative.as_posix(): hash_bytes(path.read_bytes()) for path, relative in _copy_paths(vault)
    }
    destination.mkdir(parents=True)
    for path, relative in _copy_paths(vault):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    after = {
        relative.as_posix(): hash_bytes(path.read_bytes()) for path, relative in _copy_paths(vault)
    }
    copied = {
        relative.as_posix(): hash_bytes(path.read_bytes())
        for path, relative in _copy_paths(destination)
    }
    if before != after or before != copied:
        raise V2Error(
            "external-edit-conflict",
            "The source changed during the copy; the original remains untouched",
        )
    return {
        "files": len(copied),
        "content_hash": hash_bytes(canonical_json(copied)),
        "file_hashes": copied,
    }


def trial(vault: Path, destination: Path) -> dict:
    """Explicit engineering trial on a new private copy; never live cutover."""
    from synapse.gateway import Gateway
    from synapse.index import reindex

    copy_report = copy_stable(vault, destination)
    snapshot = prepare(destination)
    store = RevisionStore(destination)
    capability = OwnerHost(store, host_id="isolated-engineering-trial").record_instruction(
        "explicit-migration-trial",
        actions=["capture"],
        scope={"bootstrap": True, "snapshot_hash": snapshot.fingerprint},
    )
    receipt = activate(
        destination, snapshot, capability, operation_id=generate_ulid(), request_id=generate_ulid()
    )
    gateway = Gateway(destination)
    manifest = gateway.view.manifest
    preserved = all(
        store.read_object(row["version"]) == snapshot.records[row["path"]]
        for row in manifest["records"].values()
    )
    source_preserved = all(
        store.read_object(source["original_hash"]) == snapshot.objects[source["original_hash"]]
        for source in snapshot.sources
    )
    index = reindex(destination, full=True)
    current_edges = gateway.view.relationships(include_suggestions=False)
    expected_edges = {
        edge["id"]: {
            key: edge[key]
            for key in (
                "from_id",
                "to_id",
                "type",
                "weak",
                "properties",
                "source_file",
                "review_status",
                "created_at",
            )
        }
        for edge in snapshot.legacy_edges
    }
    actual_edges = {
        edge["id"]: {
            key: edge[key]
            for key in (
                "from_id",
                "to_id",
                "type",
                "weak",
                "properties",
                "source_file",
                "review_status",
                "created_at",
            )
        }
        for edge in current_edges
    }
    retained_before = {
        identity: (row["version"], row["review_status"], row["active"])
        for identity, row in manifest["records"].items()
    }
    for path in (destination / ".synapse" / "v2-indexes").glob("*.db"):
        path.unlink()
    for suffix in ("", "-wal", "-shm"):
        (destination / ".synapse" / f"index.db{suffix}").unlink(missing_ok=True)
    reindex(destination, full=True)
    rebuilt = Gateway(destination)
    preserved_after = retained_before == {
        identity: (row["version"], row["review_status"], row["active"])
        for identity, row in rebuilt.view.manifest["records"].items()
    }
    return {
        "source_vault_unchanged": copy_report["file_hashes"]
        == {
            relative.as_posix(): hash_bytes(path.read_bytes())
            for path, relative in _copy_paths(Path(vault))
        },
        "copy_files": copy_report["files"],
        "copy_hash": copy_report["content_hash"],
        "baseline": snapshot.report,
        "knowledge_revision": receipt["knowledge_revision"],
        "exact_markdown_preserved": preserved,
        "exact_source_bytes_preserved": source_preserved,
        "normalized_legacy_edges_preserved": expected_edges == actual_edges,
        "cold_rebuild_preserved": preserved_after,
        "compatibility_index_entities": index.entities,
        "compatibility_notices": len(index.issues),
        "trial_directory": str(destination),
        "live_cutover": False,
    }
