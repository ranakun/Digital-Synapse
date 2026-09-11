"""Retained canonical revisions with one recoverable publication boundary.

This module provides storage mechanics. Public source/admission/adoption
authority is enforced by publication.py; callers cannot approve via a record.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from synapse.knowledge import decode_record, record_descriptor, validate_record_path
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes, validate_payload

FORMAT = "synapse-v2/1"
HASH = re.compile(r"^[a-f0-9]{64}$")


def is_v2(vault: Path) -> bool:
    """Presence is the mode switch; an invalid HEAD must never enable v1 writes."""
    head = Path(vault) / "_synapse" / "HEAD"
    return head.exists() or head.is_symlink()


def require_legacy(vault: Path, operation: str) -> None:
    if is_v2(vault):
        raise V2Error(
            "legacy-write-blocked",
            f"{operation} cannot write a v2 vault; use the v2 capture/proposal interface.",
        )


def _hash(value: str) -> str:
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise V2Error("invalid-version", "Expected a SHA-256 content address")
    return value


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_write(path: Path, data: bytes) -> None:
    """Same-filesystem replacement; partial files never become the named object."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".prepared-", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


class RevisionStore:
    def __init__(self, vault: Path):
        self.vault = Path(vault).resolve()
        self.root = self.vault / "_synapse"

    @contextmanager
    def writer_lock(self) -> Iterator[None]:
        try:
            import fcntl
        except ImportError as exc:
            raise V2Error("unsupported-platform", "V2 writes require a tested POSIX lock") from exc
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "writer.lock").open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def head(self) -> str:
        try:
            return _hash((self.root / "HEAD").read_text(encoding="ascii").strip())
        except (OSError, UnicodeError) as exc:
            raise V2Error("revision-unavailable", "V2 HEAD is missing or unreadable") from exc

    @contextmanager
    def operation_lock(self, operation_id: str) -> Iterator[None]:
        """Serialize one recoverable host operation without blocking other writers."""
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", operation_id):
            raise V2Error("invalid-request", "Invalid host operation identity")
        try:
            import fcntl
        except ImportError as exc:
            raise V2Error("unsupported-platform", "V2 writes require a tested POSIX lock") from exc
        folder = self.root / "operation-locks"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f"{operation_id}.lock").open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def read_object(self, version: str) -> bytes:
        try:
            data = (self.root / "objects" / _hash(version)).read_bytes()
        except OSError as exc:
            raise V2Error(
                "source-unavailable", "Retained object is unavailable", details={"version": version}
            ) from exc
        if hash_bytes(data) != version:
            raise V2Error("object-integrity", "Retained object failed its content hash")
        return data

    def _load_manifest(self, revision: str) -> dict[str, Any]:
        try:
            raw = (self.root / "revisions" / f"{_hash(revision)}.json").read_bytes()
            if hash_bytes(raw) != revision:
                raise V2Error("manifest-integrity", "Manifest content hash does not match")
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("format") != FORMAT:
                raise ValueError("Unsupported manifest format")
            for field in (
                "records",
                "sources",
                "source_versions",
                "local_receipts",
                "receipt_origins",
            ):
                if not isinstance(value.get(field), dict):
                    raise ValueError(f"Missing manifest mapping: {field}")
            if not isinstance(value.get("sequence"), int) or value["sequence"] < 0:
                raise ValueError("Invalid manifest sequence")
            return value
        except V2Error:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise V2Error(
                "revision-unavailable",
                "Retained manifest is unavailable or invalid",
                details={"revision": revision},
            ) from exc

    def manifest(self, revision: str | None = None) -> dict[str, Any]:
        current = self.head()
        target = _hash(revision) if revision is not None else current
        seen: set[str] = set()
        while current:
            if current in seen:
                raise V2Error("manifest-integrity", "Revision ancestry contains a cycle")
            seen.add(current)
            value = self._load_manifest(current)
            if current == target:
                return value
            current = value.get("parent")
        raise V2Error(
            "revision-unavailable",
            "Revision is not part of committed history",
            details={"revision": target},
        )

    def read_record(self, identity: str, revision: str | None = None) -> dict[str, Any]:
        manifest = self.manifest(revision)
        row = manifest["records"].get(identity)
        if row is None:
            raise V2Error(
                "record-unavailable",
                "Record is absent from this revision",
                details={"id": identity},
            )
        raw = self.read_object(row["version"])
        checked = record_descriptor(raw, path=row["path"])
        for field in (
            "id",
            "version",
            "profile",
            "type",
            "name",
            "availability",
            "review_status",
            "active",
            "merged_into",
        ):
            if row.get(field) != checked[field]:
                raise V2Error(
                    "manifest-integrity",
                    "Record descriptor disagrees with Markdown",
                    details={"id": identity, "field": field},
                )
        return decode_record(raw, path=row["path"])

    def receipt(self, operation_id: str) -> dict[str, Any] | None:
        current = self.head()
        manifest = self.manifest(current)
        origin = (
            current
            if operation_id in manifest["local_receipts"]
            else manifest["receipt_origins"].get(operation_id)
        )
        if origin is None:
            return None
        core = self.manifest(origin)["local_receipts"].get(operation_id)
        if not isinstance(core, dict):
            raise V2Error("manifest-integrity", "Receipt origin does not contain the operation")
        return copy.deepcopy(core) | {"knowledge_revision": origin}

    def checkout_path(self, locator: str) -> Path:
        target = self.vault / validate_record_path(locator)
        if not target.resolve().is_relative_to(self.vault):
            raise V2Error("external-edit-conflict", "A record path resolves outside its vault")
        return target

    def check_checkout(self, row: dict[str, Any]) -> None:
        path = self.checkout_path(row["path"])
        try:
            matches = (
                path.is_file()
                and not path.is_symlink()
                and hash_bytes(path.read_bytes()) == row["version"]
            )
        except OSError:
            matches = False
        if not matches:
            raise V2Error(
                "external-edit-conflict",
                "Editable Markdown differs from the published record",
                details={"id": row["id"], "path": row["path"]},
            )

    def _persist_objects(self, objects: dict[str, bytes]) -> None:
        for version, raw in objects.items():
            if hash_bytes(raw) != _hash(version):
                raise V2Error("object-integrity", "Prepared object has the wrong hash")
            path = self.root / "objects" / version
            if path.exists():
                if self.read_object(version) != raw:
                    raise V2Error("object-integrity", "Immutable object cannot be overwritten")
            else:
                durable_write(path, raw)

    def _validate_records(
        self, before: dict[str, Any], after: dict[str, Any], read: Callable[[str], bytes]
    ) -> None:
        paths: set[str] = set()
        for identity, row in after["records"].items():
            if row.get("id") != identity:
                raise V2Error("invalid-record", "Manifest record key and ID disagree")
            path = validate_record_path(row["path"])
            if path in paths:
                raise V2Error("path-conflict", "Two records cannot occupy the same locator")
            paths.add(path)
            if row != before["records"].get(identity):
                actual = record_descriptor(read(row["version"]), path=path)
                if row != actual:
                    raise V2Error(
                        "invalid-record", "Prepared descriptor does not match its Markdown"
                    )
        for source_id, version in after["sources"].items():
            descriptor = after["source_versions"].get(version)
            if not descriptor or descriptor["id"] != source_id:
                raise V2Error(
                    "invalid-source", "Source pointer has no matching retained descriptor"
                )
        for version, descriptor in after["source_versions"].items():
            if version not in before["source_versions"]:
                validate_payload("source_version", descriptor)
                from synapse.v2_contracts import version_for

                if (
                    descriptor["version"] != version
                    or version_for("source_version", descriptor) != version
                ):
                    raise V2Error("invalid-source", "Source descriptor version is incorrect")
                read(descriptor["original_hash"])
                if descriptor.get("text_version"):
                    read(descriptor["text_version"]).decode("utf-8")
            elif descriptor != before["source_versions"][version]:
                raise V2Error("invalid-source", "Retained source descriptors are immutable")
        if not before["source_versions"].keys() <= after["source_versions"].keys():
            raise V2Error("invalid-source", "Source history cannot be removed by a transaction")

    def transact(
        self,
        *,
        operation_id: str,
        request_id: str,
        kind: str,
        payload_hash: str,
        mutate: Callable[[dict[str, Any], Callable[[str], bytes]], None],
        objects: dict[str, bytes] | None = None,
        receipt_fields: dict[str, Any] | None = None,
        initialize: bool = False,
        fault: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Execute one internal prepared mutation. Only services grant authority.

        `fault` is an explicitly supplied deterministic failure-test hook; no
        environment variable or model output enables it in normal operation.
        """
        pending = objects or {}
        _hash(payload_hash)
        for version, raw in pending.items():
            if hash_bytes(raw) != _hash(version):
                raise V2Error("object-integrity", "Prepared object hash is invalid")
        event = fault or (lambda _phase: None)
        with self.writer_lock():
            if is_v2(self.vault):
                prior = self.receipt(operation_id)
                if prior:
                    if (
                        prior["payload_hash"] != payload_hash
                        or prior["kind"] != kind
                        or prior["request_id"] != request_id
                    ):
                        raise V2Error(
                            "idempotency-conflict",
                            "Operation ID already has a different request/payload",
                        )
                    return prior
                if initialize:
                    raise V2Error("already-initialized", "Vault already uses v2")
                parent = self.head()
                before = self.manifest(parent)
            elif initialize:
                parent = None
                before = {
                    "format": FORMAT,
                    "parent": None,
                    "sequence": -1,
                    "records": {},
                    "sources": {},
                    "source_versions": {},
                    "local_receipts": {},
                    "receipt_origins": {},
                    "legacy_edges": [],
                }
            else:
                raise V2Error("revision-unavailable", "Vault has not been initialized for v2")
            manifest = copy.deepcopy(before)

            def read(version: str) -> bytes:
                return pending[version] if version in pending else self.read_object(version)

            mutate(manifest, read)
            self._validate_records(before, manifest, read)
            now = datetime.now(UTC)
            if before.get("committed_at"):
                last = datetime.fromisoformat(before["committed_at"].replace("Z", "+00:00"))
                now = max(now, last + timedelta(microseconds=1))
            timestamp = now.isoformat().replace("+00:00", "Z")
            core = {
                "id": generate_ulid(),
                "operation_id": operation_id,
                "request_id": request_id,
                "kind": kind,
                "payload_hash": payload_hash,
                "created_at": timestamp,
                "durable_outcome": "committed",
                "limitations": [],
            }
            if receipt_fields:
                if set(receipt_fields) & (set(core) - {"id", "limitations"}):
                    raise V2Error(
                        "invalid-receipt", "Receipt fields cannot overwrite operation identity"
                    )
                core.update(receipt_fields)
            validate_payload("receipt", core | {"knowledge_revision": "0" * 64})
            origins = copy.deepcopy(before["receipt_origins"])
            if parent:
                origins.update({key: parent for key in before["local_receipts"]})
            manifest.update(
                format=FORMAT,
                parent=parent,
                sequence=before["sequence"] + 1,
                committed_at=timestamp,
                local_receipts={operation_id: core},
                receipt_origins=origins,
            )
            self._persist_objects(pending)
            event("objects")
            raw = canonical_json(manifest)
            revision = hash_bytes(raw)
            durable_write(self.root / "revisions" / f"{revision}.json", raw)
            event("manifest")
            durable_write(self.root / "HEAD", revision.encode("ascii"))
            event("head")
            # The durable result is fixed. Projection/index recovery cannot undo it.
            return copy.deepcopy(core) | {"knowledge_revision": revision}

    def refresh_checkout(
        self, *, previous_revision: str | None = None, identities: list[str] | None = None
    ) -> list[dict[str, str]]:
        """Repair the editable projection, preserving edits which are not ours."""
        current = self.manifest()
        previous = self.manifest(previous_revision) if previous_revision else {"records": {}}
        conflicts = []
        for identity, row in current["records"].items():
            if identities is not None and identity not in identities:
                continue
            path = self.checkout_path(row["path"])
            old = previous["records"].get(identity)
            raw = self.read_object(row["version"])
            if path.is_file() and hash_bytes(path.read_bytes()) == row["version"]:
                continue
            allowed = not path.exists() or (
                old
                and old["path"] == row["path"]
                and path.is_file()
                and hash_bytes(path.read_bytes()) == old["version"]
            )
            if path.is_symlink() or not allowed:
                conflicts.append({"id": identity, "path": row["path"], "reason": "external-edit"})
                continue
            durable_write(path, raw)
            if old and old["path"] != row["path"]:
                previous_path = self.checkout_path(old["path"])
                if (
                    previous_path.is_file()
                    and not previous_path.is_symlink()
                    and hash_bytes(previous_path.read_bytes()) == old["version"]
                ):
                    previous_path.unlink()
                    _fsync_directory(previous_path.parent)
                elif previous_path.exists():
                    conflicts.append(
                        {"id": identity, "path": old["path"], "reason": "external-edit"}
                    )
        return conflicts
