from __future__ import annotations

from pathlib import Path

import pytest

from synapse.config import init_vault
from synapse.gateway import Gateway
from synapse.owner_host import OwnerHost
from synapse.publication import Publisher, snapshot_fingerprint
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error


def test_missing_retained_object_never_falls_back_to_edited_checkout(tmp_path: Path) -> None:
    vault = init_vault(tmp_path / "synthetic-vault", initialize_git=False)
    publisher = Publisher(vault)
    store = publisher.store
    raw = (
        b"---\n"
        b"id: me\n"
        b"type: person\n"
        b"name: Synthetic Owner\n"
        b"review_status: verified\n"
        b"---\n\n"
        b"# Synthetic Owner\n\n"
        b"Synthetic retained content.\n"
    )
    owner = OwnerHost(store, host_id="synthetic-restore-test")
    capability = owner.record_instruction(
        "synthetic-bootstrap",
        actions=["capture"],
        scope={
            "bootstrap": True,
            "snapshot_hash": snapshot_fingerprint({"entities/people/me.md": raw}, []),
        },
    )
    publisher.bootstrap(
        capability,
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        records={"entities/people/me.md": raw},
        sources=[],
        objects={},
        legacy_edges=[],
    )

    row = store.manifest()["records"]["me"]
    checkout = vault / row["path"]
    retained_object = store.root / "objects" / row["version"]
    checkout.write_bytes(b"edited checkout must never become the read source")
    retained_object.unlink()

    with pytest.raises(V2Error, match="Retained object is unavailable"):
        store.read_record("me")
    with pytest.raises(V2Error, match="Retained object is unavailable"):
        Gateway(vault).record("me")
