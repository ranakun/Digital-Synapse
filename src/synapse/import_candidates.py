"""Prepare deterministic importer candidates in a disposable legacy vault."""

from __future__ import annotations

import copy
import hashlib
import inspect
import shutil
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from synapse.config import init_vault, load_config, save_config
from synapse.knowledge import record_descriptor
from synapse.parser import entity_files
from synapse.revisions import RevisionStore
from synapse.source_extractors import prepare_source_file
from synapse.v2_contracts import V2Error


def _fail(message: str, *, details: dict[str, Any] | None = None) -> None:
    raise V2Error("invalid-request", message, details=details)


def _importer(importer: str, options: dict[str, Any]) -> tuple[Callable[..., Any], dict[str, Any]]:
    if not isinstance(importer, str) or not importer:
        _fail("importer must be a nonempty allowlisted name")
    name = importer.replace("-", "_")

    # Imports stay local to candidate preparation so this module does not add
    # an import-time dependency edge to the legacy importer/index modules.
    import synapse.importers as linkedin_importers

    functions = {
        key: value
        for key, value in vars(linkedin_importers).items()
        if key.startswith("import_linkedin_") and callable(value)
    }
    if name in functions:
        if options:
            _fail("LinkedIn importers do not accept candidate options", details={"keys": sorted(options)})
        return functions[name], {}

    if name in {"import_whatsapp_chat", "whatsapp_chat"}:
        from synapse.whatsapp import import_whatsapp_chat

        allowed = {"chat_key", "since", "min_messages", "date_order", "chat_label", "participant_mappings"}
        unknown = set(options) - allowed
        if unknown:
            _fail("Unsupported WhatsApp candidate option", details={"keys": sorted(unknown)})
        if "chat_key" not in options:
            _fail("WhatsApp candidates require chat_key")
        return import_whatsapp_chat, copy.deepcopy(options)

    _fail("Importer is not an approved deterministic candidate adapter", details={"importer": importer})


def _baseline(vault: Path, revision: str | None) -> tuple[RevisionStore, str, dict[str, Any], dict[str, bytes]]:
    store = RevisionStore(vault)
    pinned = revision or store.head()
    manifest = store.manifest(pinned)
    raw_by_id: dict[str, bytes] = {}
    for identity, row in manifest["records"].items():
        raw_by_id[identity] = store.read_object(row["version"])
    return store, pinned, manifest, raw_by_id


def _copy_baseline(candidate: Path, manifest: dict[str, Any], raw_by_id: dict[str, bytes]) -> None:
    entities = candidate / "entities"
    if entities.exists():
        shutil.rmtree(entities)
    entities.mkdir(parents=True)
    paths: set[str] = set()
    for identity, row in manifest["records"].items():
        path = row["path"]
        if path in paths:
            _fail("Pinned manifest contains duplicate record paths")
        paths.add(path)
        destination = candidate / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw_by_id[identity])


def _snapshot(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, bytes]]:
    rows: dict[str, dict[str, Any]] = {}
    raw_by_id: dict[str, bytes] = {}
    for path in entity_files(root):
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        raw = path.read_bytes()
        row = record_descriptor(raw, path=relative)
        if row["id"] in rows:
            _fail("Candidate importer produced duplicate record IDs", details={"id": row["id"]})
        rows[row["id"]] = row
        raw_by_id[row["id"]] = raw
    return rows, raw_by_id


def _changes(
    baseline: dict[str, Any],
    before_raw: dict[str, bytes],
    after_rows: dict[str, dict[str, Any]],
    after_raw: dict[str, bytes],
) -> list[dict[str, Any]]:
    for identity, row in baseline["records"].items():
        after = after_rows.get(identity)
        if after is None:
            _fail("Importer deleted a retained baseline record", details={"id": identity})
        if not row["active"]:
            if after != row or after_raw[identity] != before_raw[identity]:
                _fail("Importer changed a retained tombstone", details={"id": identity})
        if row["review_status"] == "verified" and after_raw[identity] != before_raw[identity]:
            raise V2Error("approval-required", "Importer changed a verified record", details={"id": identity})
        for field in ("availability", "review_status", "active", "merged_into", "disposition"):
            if after[field] != row[field]:
                raise V2Error("approval-required", "Importer changed retained record trust state", details={"id": identity, "field": field})
        if after["active"] is False and row["active"] is True:
            _fail("Importer wrote a new tombstone")

    changes: list[dict[str, Any]] = []
    for identity in sorted(after_rows):
        row = after_rows[identity]
        if identity not in baseline["records"]:
            if not row["active"]:
                _fail("Importer wrote a new tombstone", details={"id": identity})
            changes.append({"kind": "create-record", "path": row["path"], "raw": after_raw[identity]})
            continue
        before = baseline["records"][identity]
        if after_raw[identity] == before_raw[identity] and row["path"] == before["path"]:
            continue
        kind = "relocate-record" if row["path"] != before["path"] else "replace-record"
        changes.append(
            {
                "kind": kind,
                "path": row["path"],
                "raw": after_raw[identity],
                "target_id": identity,
                "before_version": before["version"],
            }
        )
    return changes


def prepare_import(
    vault: Path,
    source: Path,
    *,
    importer: str,
    options: dict[str, Any] | None = None,
    revision: str | None = None,
) -> dict[str, Any]:
    """Run one allowlisted importer against a disposable retained baseline."""

    vault = Path(vault).resolve()
    source = Path(source).resolve()
    if not source.is_file():
        raise V2Error("source-unavailable", "The supplied importer source is not a file")
    if options is not None and not isinstance(options, dict):
        _fail("options must be a mapping")
    importer_fn, importer_options = _importer(importer, options or {})
    store, base_revision, manifest, before_raw = _baseline(vault, revision)
    source_descriptor, source_objects = prepare_source_file(source)
    source_bytes = source_objects[source_descriptor["original_hash"]]
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    config = load_config(vault)
    limitations = [
        "Candidate records are legacy importer output and require normal proposal review before adoption.",
    ]

    with TemporaryDirectory(prefix="synapse-import-candidate-") as temp_name:
        candidate = init_vault(Path(temp_name) / "vault", initialize_git=False)
        _copy_baseline(candidate, manifest, before_raw)
        save_config(candidate, copy.deepcopy(config))

        # The importer receives a candidate-local copy.  This keeps the
        # original source read-only even if a legacy adapter attempts a copy.
        candidate_source = candidate / "inbox" / ".candidate-source" / source.name
        candidate_source.parent.mkdir(parents=True, exist_ok=True)
        candidate_source.write_bytes(source_bytes)
        kwargs: dict[str, Any] = {"vault": candidate}
        kwargs.update(importer_options)
        try:
            signature = inspect.signature(importer_fn)
            accepted = set(signature.parameters)
            if "vault_path" in accepted and "vault" not in accepted:
                kwargs["vault_path"] = kwargs.pop("vault")
            result = importer_fn(candidate_source, **kwargs)
        except V2Error:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise V2Error("invalid-request", "Deterministic importer failed in candidate vault", details={"error": exc.__class__.__name__}) from exc

        after_rows, after_raw = _snapshot(candidate)
        changes = _changes(manifest, before_raw, after_rows, after_raw)
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
            raise V2Error("external-edit-conflict", "Importer changed the supplied source")

        counts = {
            "records_before": len(manifest["records"]),
            "records_after": len(after_rows),
            "created": sum(change["kind"] == "create-record" for change in changes),
            "replaced": sum(change["kind"] == "replace-record" for change in changes),
            "relocated": sum(change["kind"] == "relocate-record" for change in changes),
            "withdrawn": 0,
            "unchanged": len(manifest["records"]) - sum(
                change["kind"] != "create-record" for change in changes
            ),
        }
        if isinstance(result, dict):
            warnings = result.get("warnings")
            if isinstance(warnings, list) and warnings:
                limitations.append(f"Importer reported {len(warnings)} deterministic warning(s).")

    return {
        "base_revision": base_revision,
        "source_descriptor": source_descriptor,
        "source_objects": source_objects,
        "changes": changes,
        "summary": counts,
        "limitations": limitations,
    }
