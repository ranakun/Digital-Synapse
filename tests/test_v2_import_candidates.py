from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from synapse.import_candidates import prepare_import
from synapse.knowledge import record_descriptor
from synapse.revisions import RevisionStore
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, hash_bytes

FIXTURE = Path(__file__).parent / "fixtures/linkedin/connections.csv"


def _legacy_store(tmp_path: Path) -> tuple[RevisionStore, dict[str, bytes]]:
    vault = tmp_path / "vault"
    owner = b"---\nid: me\ntype: person\nname: Owner\nreview_status: proposed\naliases: []\nrelations: []\nproperties: {}\n---\n\nOwner\n"
    tombstone = b"---\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntype: person\nname: Gone\nreview_status: proposed\narchived: true\n---\n\nGone\n"
    owner_row = record_descriptor(owner, path="entities/people/me.md")
    tombstone_row = record_descriptor(tombstone, path="entities/people/gone.md")
    store = RevisionStore(vault)
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"import-candidate-baseline"),
        objects={owner_row["version"]: owner, tombstone_row["version"]: tombstone},
        initialize=True,
        mutate=lambda manifest, _read: manifest["records"].update(
            {"me": owner_row, tombstone_row["id"]: tombstone_row}
        ),
    )
    return store, {"me": owner, tombstone_row["id"]: tombstone}


def test_prepare_import_uses_retained_baseline_and_returns_exact_candidate_changes(tmp_path: Path) -> None:
    store, before = _legacy_store(tmp_path)
    source_hash = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    result = prepare_import(store.vault, FIXTURE, importer="import_linkedin_connections")
    assert result["base_revision"] == store.head()
    assert result["source_descriptor"]["original_hash"] == source_hash
    assert result["source_objects"][source_hash] == FIXTURE.read_bytes()
    assert result["summary"]["created"] >= 1
    assert all(change["kind"] == "create-record" for change in result["changes"])
    assert all(isinstance(change["raw"], bytes) for change in result["changes"])
    assert {"me", "01ARZ3NDEKTSV4RRFFQ69G5FAV"} == set(before)
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == source_hash
    assert store.manifest()["records"]["me"]["version"] == hashlib.sha256(before["me"]).hexdigest()


def test_prepare_import_preserves_tombstones_and_rejects_unexpected_options(tmp_path: Path) -> None:
    store, _before = _legacy_store(tmp_path)
    with pytest.raises(V2Error) as caught:
        prepare_import(
            store.vault,
            FIXTURE,
            importer="import_linkedin_connections",
            options={"trust": "verified"},
        )
    assert caught.value.code == "invalid-request"

    result = prepare_import(store.vault, FIXTURE, importer="import_linkedin_connections")
    assert not any(
        change.get("target_id") == "01ARZ3NDEKTSV4RRFFQ69G5FAV" for change in result["changes"]
    )
    assert result["summary"]["withdrawn"] == 0

