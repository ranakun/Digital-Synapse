"""Explicit local discovery policy, independent of retained source provenance.

Snapshots are keyed by knowledge revision and exact (source ID, version) pairs.
They never change canonical descriptors, original bytes, or source hashes. A
missing snapshot inherits the nearest ancestor policy only for exactly retained
source versions and hashes. New versions remain unknown/visible. A malformed or
misbound snapshot refuses discovery instead of silently changing its scope.

The checksum detects accidental corruption; it is not an owner attestation or
an authenticity signature. Only an explicitly reviewed caller should write
classifications. Deleting this derived state loses those local decisions unless
an ancestor snapshot retains them; filenames never establish purpose.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from synapse.revisions import RevisionStore, durable_write
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes

_FORMAT = "synapse-source-purpose/1"
_PURPOSES = {"unknown", "knowledge", "internal"}
_INPUT_FIELDS = {"source_id", "source_version", "purpose", "reason"}
_BOUND_FIELDS = _INPUT_FIELDS | {"original_hash", "text_version"}


def validate_source_scope(source_scope: str) -> str:
    if not isinstance(source_scope, str) or source_scope not in {"ordinary", "all"}:
        raise V2Error("invalid-request", "source_scope must be ordinary or all")
    return source_scope


def _path(vault: Path, revision: str) -> Path:
    return vault / ".synapse" / "source-purpose" / f"{revision}.json"


def _validate_revision(revision: str) -> None:
    if not isinstance(revision, str) or len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
        raise V2Error("invalid-request", "Source purpose revision must be a SHA-256 address")


@dataclass(frozen=True)
class SourcePurposePolicy:
    knowledge_revision: str
    snapshot_hash: str | None
    classifications: Mapping[tuple[str, str], Mapping[str, Any]]
    inherited_from_revision: str | None = None

    def purpose(self, source_id: str, source_version: str) -> str:
        return self.classifications.get((source_id, source_version), {}).get("purpose", "unknown")

    def labels(self, source_id: str, source_version: str) -> dict[str, Any]:
        reviewed = (source_id, source_version) in self.classifications
        return {
            "purpose": self.purpose(source_id, source_version),
            "purpose_basis": "reviewed-local-policy" if reviewed else "unclassified",
        }

    def visible(self, source_id: str, source_version: str, source_scope: str = "ordinary") -> bool:
        validate_source_scope(source_scope)
        return source_scope == "all" or self.purpose(source_id, source_version) != "internal"

    def metadata(self) -> dict[str, Any]:
        return {
            "knowledge_revision": self.knowledge_revision,
            "snapshot_hash": self.snapshot_hash,
            "state": "reviewed-local-policy" if self.snapshot_hash else "unclassified",
            "default_purpose": "unknown",
            "unknown_visible": True,
            "classified_versions": len(self.classifications),
            "inherited_from_revision": self.inherited_from_revision,
        }

    def record_evidence_policy(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Nominate, never suppress, records with exclusively internal anchors.

        This examines explicit evidence only; it does not infer provenance from
        names, origin strings, weak links, or a transitive dependency closure.
        Independently retained owner assertions still need caller review.
        """
        evidence = record.get("evidence")
        purposes = []
        if isinstance(evidence, list):
            for ref in evidence:
                if not isinstance(ref, Mapping) or not isinstance(ref.get("source_id"), str) or not isinstance(ref.get("source_version"), str):
                    purposes.append("unknown")
                else:
                    purposes.append(self.purpose(ref["source_id"], ref["source_version"]))
        only_internal = bool(purposes) and all(value == "internal" for value in purposes)
        state = "unlinked" if not purposes else "internal-only" if only_internal else "mixed" if "internal" in purposes else "ordinary-or-unknown"
        return {"source_evidence_purpose": state, "source_purpose_exclusion_candidate": only_internal}


def _bound_row(row: Any, manifest: Mapping[str, Any], *, stored: bool = False) -> dict[str, Any]:
    code = "recovery-required" if stored else "invalid-request"
    if not isinstance(row, Mapping) or set(row) != (_BOUND_FIELDS if stored else _INPUT_FIELDS):
        raise V2Error(code, "Source purpose classification has invalid fields")
    if any(not isinstance(row[key], str) or not row[key].strip() for key in _INPUT_FIELDS):
        raise V2Error(code, "Source purpose classification fields must be nonempty strings")
    if row["purpose"] not in _PURPOSES:
        raise V2Error(code, "Source purpose must be unknown, knowledge or internal")
    descriptor = manifest.get("source_versions", {}).get(row["source_version"])
    if not isinstance(descriptor, Mapping) or descriptor.get("id") != row["source_id"] or descriptor.get("version") != row["source_version"]:
        raise V2Error(code, "Source purpose classification is not bound to a retained source version")
    bound = {key: row[key] for key in _INPUT_FIELDS}
    bound.update(original_hash=descriptor["original_hash"], text_version=descriptor.get("text_version"))
    if stored and bound != row:
        raise V2Error(code, "Source purpose classification disagrees with retained source hashes")
    return bound


def _read_snapshot(
    vault: Path, revision: str, manifest: Mapping[str, Any],
) -> SourcePurposePolicy | None:
    try:
        raw = _path(vault, revision).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise V2Error("recovery-required", "Source purpose snapshot cannot be read") from exc
    try:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or set(envelope) != {"payload", "snapshot_hash"}:
            raise ValueError("invalid envelope")
        payload = envelope["payload"]
        checksum = hash_bytes(canonical_json(payload))
        if checksum != envelope["snapshot_hash"]:
            raise ValueError("checksum mismatch")
        if not isinstance(payload, dict) or set(payload) != {"format", "knowledge_revision", "classifications"} or payload["format"] != _FORMAT or payload["knowledge_revision"] != revision:
            raise ValueError("revision or format mismatch")
        if not isinstance(payload["classifications"], list):
            raise ValueError("invalid classifications")
        classifications = {}
        for row in payload["classifications"]:
            checked = _bound_row(row, manifest, stored=True)
            key = (checked["source_id"], checked["source_version"])
            if key in classifications:
                raise ValueError("duplicate classification")
            classifications[key] = MappingProxyType(checked)
        return SourcePurposePolicy(revision, checksum, MappingProxyType(classifications))
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise V2Error("recovery-required", "Source purpose snapshot is corrupt or bound to another revision") from exc


def _policy_payload(revision: str, classifications: Mapping) -> dict[str, Any]:
    return {
        "format": _FORMAT,
        "knowledge_revision": revision,
        "classifications": [dict(classifications[key]) for key in sorted(classifications)],
    }


def load_source_purposes(
    vault: Path, *, revision: str, manifest: Mapping[str, Any] | None = None,
) -> SourcePurposePolicy:
    """Load a policy or inherit unchanged exact pairs from its nearest ancestor.

    Optional manifest is a verified pinned view. Each parent manifest is hash
    checked before use. The nearest snapshot is a complete set of decisions;
    explicit unknown overrides are retained, and corrupt snapshots never permit
    fallback to an older one. Historical versions retain their own labels while
    changed/new versions are unknown. Reads never materialize inherited state.

    The effective checksum always binds the requested revision, not the ancestor
    revision. It can therefore also key that revision's derived organization.
    There is no process cache, so policy edits invalidate the next discovery.
    """
    vault = Path(vault).resolve()
    _validate_revision(revision)
    store = RevisionStore(vault)
    if manifest is None:
        manifest = store.manifest(revision)
    current_revision, current_manifest = revision, manifest
    seen = set()
    while True:
        if current_revision in seen:
            raise V2Error("recovery-required", "Source purpose ancestry contains a cycle")
        seen.add(current_revision)
        policy = _read_snapshot(vault, current_revision, current_manifest)
        if policy is not None:
            if current_revision == revision:
                return policy
            inherited = {}
            for key, row in policy.classifications.items():
                descriptor = manifest.get("source_versions", {}).get(key[1])
                if (
                    isinstance(descriptor, Mapping)
                    and descriptor.get("id") == key[0]
                    and descriptor.get("version") == key[1]
                    and descriptor.get("original_hash") == row["original_hash"]
                    and descriptor.get("text_version") == row["text_version"]
                ):
                    inherited[key] = row
            checksum = hash_bytes(canonical_json(_policy_payload(revision, inherited)))
            return SourcePurposePolicy(
                revision, checksum, MappingProxyType(inherited), current_revision,
            )
        parent = current_manifest.get("parent")
        if parent is None:
            return SourcePurposePolicy(revision, None, MappingProxyType({}))
        _validate_revision(parent)
        # This parent address came from a verified committed manifest; loading
        # it directly avoids repeatedly traversing HEAD for every ancestor.
        current_revision, current_manifest = parent, store._load_manifest(parent)


def write_source_purposes(
    vault: Path, *, revision: str, classifications: Sequence[Mapping[str, Any]],
) -> SourcePurposePolicy:
    """Merge explicitly reviewed classifications for one retained revision.

    Each row requires source_id, source_version, purpose, and a review reason.
    Existing and inherited pairs are replaced only when explicitly supplied;
    other decisions survive. Reclassify as unknown to clear an exclusion. This writes only local
    derived policy, never HEAD, canonical objects, descriptors or attestations.
    """
    _validate_revision(revision)
    store = RevisionStore(Path(vault))
    manifest = store.manifest(revision)
    if not isinstance(classifications, Sequence) or isinstance(classifications, (str, bytes)):
        raise V2Error("invalid-request", "classifications must be a sequence of reviewed rows")
    updates = {}
    for row in classifications:
        checked = _bound_row(row, manifest)
        key = (checked["source_id"], checked["source_version"])
        if key in updates:
            raise V2Error("invalid-request", "Duplicate source purpose classification")
        updates[key] = checked
    try:
        import fcntl
    except ImportError as exc:
        raise V2Error("unsupported-platform", "Source purpose writes require POSIX locking") from exc
    path = _path(store.vault, revision)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            current = load_source_purposes(store.vault, revision=revision, manifest=manifest)
            if not updates:
                return current
            merged = {key: dict(row) for key, row in current.classifications.items()}
            merged.update(updates)
            payload = _policy_payload(revision, merged)
            checksum = hash_bytes(canonical_json(payload))
            if checksum == current.snapshot_hash and current.inherited_from_revision is None:
                return current
            durable_write(path, canonical_json({"payload": payload, "snapshot_hash": checksum}))
            return load_source_purposes(store.vault, revision=revision, manifest=manifest)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
