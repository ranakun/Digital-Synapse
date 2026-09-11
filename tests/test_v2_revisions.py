from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from synapse.knowledge import encode_record, record_descriptor
from synapse.revisions import RevisionStore, is_v2, require_legacy
from synapse.source_store import prepare_source
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, hash_bytes

ROOT = Path(__file__).parents[1]
EXAMPLE_RECORD = json.loads(
    (ROOT / "docs/v2/contracts/example-knowledge_record.json").read_text(encoding="utf-8")
)["payload"]

LEGACY_ID = "me"
LEGACY_PATH = "entities/people/me.md"
LEGACY_RAW = (
    b"---\r\n"
    b"id: me\r\n"
    b"type: person\r\n"
    b"name: Owner\r\n"
    b"review_status: proposed\r\n"
    b"---\r\n"
    b"\r\nOriginal legacy bytes.\r\n"
)


def _ids() -> tuple[str, str]:
    return generate_ulid(), generate_ulid()


def _payload_hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _seed_legacy(tmp_path: Path) -> tuple[RevisionStore, str, dict]:
    vault = tmp_path / "vault"
    legacy_path = vault / LEGACY_PATH
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(LEGACY_RAW)
    row = record_descriptor(LEGACY_RAW, path=LEGACY_PATH)
    store = RevisionStore(vault)
    operation_id, request_id = _ids()

    def mutate(manifest: dict, _read: object) -> None:
        manifest["records"][LEGACY_ID] = copy.deepcopy(row)

    receipt = store.transact(
        operation_id=operation_id,
        request_id=request_id,
        kind="capture",
        payload_hash=_payload_hash("baseline capture"),
        mutate=mutate,
        objects={row["version"]: LEGACY_RAW},
        initialize=True,
    )
    return store, receipt["knowledge_revision"], row


def _v2_record(identity: str, *, statement: str = "A synthetic claim.") -> tuple[dict, bytes]:
    payload = copy.deepcopy(EXAMPLE_RECORD)
    payload["id"] = identity
    payload["statement"] = statement
    raw = encode_record(payload, name=f"Synthetic {identity}")
    return record_descriptor(raw, path=f"entities/insights/{identity}.md"), raw


def _commit_record(
    store: RevisionStore,
    row: dict,
    raw: bytes,
    *,
    label: str,
    operation_id: str | None = None,
    request_id: str | None = None,
    fault=None,
) -> dict:
    operation_id = operation_id or generate_ulid()
    request_id = request_id or generate_ulid()

    def mutate(manifest: dict, _read: object) -> None:
        manifest["records"][row["id"]] = copy.deepcopy(row)

    return store.transact(
        operation_id=operation_id,
        request_id=request_id,
        kind="suggestion-admission",
        payload_hash=_payload_hash(label),
        mutate=mutate,
        objects={row["version"]: raw},
        fault=fault,
    )


def _crash_child(callable_) -> int:
    pid = os.fork()
    if pid == 0:
        try:
            callable_()
        except BaseException:
            os._exit(121)
        os._exit(122)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status)
    return os.WEXITSTATUS(status)


def test_initialize_preserves_legacy_bytes_and_records_capture_baseline(tmp_path: Path) -> None:
    store, revision, row = _seed_legacy(tmp_path)

    assert is_v2(store.vault)
    assert store.head() == revision
    assert store.read_object(row["version"]) == LEGACY_RAW
    assert store.read_record(LEGACY_ID)["version"] == hash_bytes(LEGACY_RAW)
    assert store.read_record(LEGACY_ID)["availability"] == "accepted"
    assert store.manifest()["records"][LEGACY_ID] == row

    operation_id = next(iter(store.manifest()["local_receipts"]))
    receipt = store.receipt(operation_id)
    assert receipt is not None
    assert receipt["kind"] == "capture"
    assert receipt["knowledge_revision"] == revision
    assert receipt["durable_outcome"] == "committed"


@pytest.mark.parametrize("boundary", ["objects", "manifest", "head"])
def test_process_crash_boundaries_leave_recoverable_state(tmp_path: Path, boundary: str) -> None:
    store, previous_revision, _ = _seed_legacy(tmp_path)
    row, raw = _v2_record(generate_ulid())
    operation_id, request_id = _ids()
    payload_hash = _payload_hash(f"fault {boundary}")

    def attempt() -> None:
        def mutate(manifest: dict, _read: object) -> None:
            manifest["records"][row["id"]] = copy.deepcopy(row)

        def fault(phase: str) -> None:
            if phase == boundary:
                os._exit(77)

        store.transact(
            operation_id=operation_id,
            request_id=request_id,
            kind="suggestion-admission",
            payload_hash=payload_hash,
            mutate=mutate,
            objects={row["version"]: raw},
            fault=fault,
        )

    assert _crash_child(attempt) == 77
    if boundary == "head":
        assert store.head() != previous_revision
    else:
        assert store.head() == previous_revision
    assert store.checkout_path(row["path"]).exists() is False

    if boundary == "objects":
        assert (store.root / "objects" / row["version"]).is_file()
    if boundary == "manifest":
        assert (store.root / "objects" / row["version"]).is_file()
        prepared = [
            path
            for path in (store.root / "revisions").glob("*.json")
            if path.stem != previous_revision
        ]
        assert len(prepared) == 1
        prepared_revision = prepared[0].stem
        with pytest.raises(V2Error, match="not part of committed history"):
            store.manifest(prepared_revision)
        assert store.manifest()["sequence"] == 0
    if boundary == "head":
        committed = store.head()
        assert store.manifest(committed)["records"][row["id"]] == row

    retry = store.transact(
        operation_id=operation_id,
        request_id=request_id,
        kind="suggestion-admission",
        payload_hash=payload_hash,
        mutate=lambda manifest, _read: manifest["records"].update({row["id"]: copy.deepcopy(row)}),
        objects={row["version"]: raw},
    )
    assert retry["durable_outcome"] == "committed"
    assert store.read_record(row["id"])["version"] == row["version"]
    with store.writer_lock():
        pass


def test_receipt_origin_survives_unrelated_commit_and_payload_conflict(tmp_path: Path) -> None:
    store, _, _ = _seed_legacy(tmp_path)
    first_row, first_raw = _v2_record(generate_ulid(), statement="First claim.")
    operation_id, request_id = _ids()
    first = _commit_record(
        store,
        first_row,
        first_raw,
        label="first",
        operation_id=operation_id,
        request_id=request_id,
    )
    unrelated_row, unrelated_raw = _v2_record(generate_ulid(), statement="Unrelated claim.")
    _commit_record(store, unrelated_row, unrelated_raw, label="unrelated")

    recovered = store.receipt(operation_id)
    assert recovered == first
    assert recovered["knowledge_revision"] != store.head()

    retry = store.transact(
        operation_id=operation_id,
        request_id=request_id,
        kind="suggestion-admission",
        payload_hash=_payload_hash("first"),
        mutate=lambda manifest, _read: manifest["records"].update(
            {first_row["id"]: copy.deepcopy(first_row)}
        ),
        objects={first_row["version"]: first_raw},
    )
    assert retry == first

    with pytest.raises(V2Error) as caught:
        store.transact(
            operation_id=operation_id,
            request_id=request_id,
            kind="suggestion-admission",
            payload_hash=_payload_hash("changed payload"),
            mutate=lambda manifest, _read: None,
        )
    assert caught.value.code == "idempotency-conflict"


def test_pinned_revision_retains_old_record_and_source_bytes(tmp_path: Path) -> None:
    store, _, _ = _seed_legacy(tmp_path)
    first_row, first_raw = _v2_record(generate_ulid(), statement="Version one.")
    first = _commit_record(store, first_row, first_raw, label="record one")
    old_revision = first["knowledge_revision"]

    old_source = b"old evidence\r\nqualified by the original source\r\n"
    source_descriptor, source_objects = prepare_source(
        old_source,
        origin="synthetic old source",
        source_id=generate_ulid(),
        captured_at="2026-09-11T00:00:00Z",
    )
    source_id = source_descriptor["id"]
    source_version = source_descriptor["version"]

    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=_payload_hash("old source"),
        mutate=lambda manifest, _read: (
            manifest["source_versions"].update({source_version: source_descriptor}),
            manifest["sources"].update({source_id: source_version}),
        ),
        objects=source_objects,
    )
    source_revision = store.head()

    second_row, second_raw = _v2_record(
        first_row["id"], statement="Version two changed after the source capture."
    )
    second = _commit_record(store, second_row, second_raw, label="record two")

    assert store.read_record(first_row["id"], revision=old_revision)["statement"] == "Version one."
    assert store.read_object(source_descriptor["original_hash"]) == old_source
    assert store.read_record(first_row["id"], revision=second["knowledge_revision"])[
        "statement"
    ] == ("Version two changed after the source capture.")
    assert store.manifest(source_revision)["source_versions"][source_version] == source_descriptor


def test_competing_processes_serialize_without_lost_records(tmp_path: Path) -> None:
    store, _, _ = _seed_legacy(tmp_path)
    rows = [_v2_record(generate_ulid(), statement=f"Writer {index}.") for index in (1, 2)]
    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()
    pids: list[int] = []

    for row, raw in rows:
        pid = os.fork()
        if pid == 0:
            os.close(ready_r)
            os.close(go_w)
            try:
                os.write(ready_w, b"r")
                os.read(go_r, 1)
                _commit_record(store, row, raw, label=f"writer {row['id']}")
            except BaseException:
                os._exit(131)
            os._exit(0)
        pids.append(pid)

    os.close(ready_w)
    os.close(go_r)
    ready = b""
    while len(ready) < 2:
        ready += os.read(ready_r, 2 - len(ready))
    assert ready == b"rr"
    os.close(ready_r)
    os.write(go_w, b"gg")
    os.close(go_w)
    statuses = [os.waitpid(pid, 0)[1] for pid in pids]
    assert statuses == [0, 0]

    current = store.manifest()
    assert all(row["id"] in current["records"] for row, _raw in rows)
    assert current["sequence"] == 2
    with store.writer_lock():
        pass


@pytest.mark.parametrize("corruption", ["object", "manifest"])
def test_corrupt_retained_bytes_are_rejected(tmp_path: Path, corruption: str) -> None:
    store, _, _ = _seed_legacy(tmp_path)
    row, raw = _v2_record(generate_ulid())
    _commit_record(store, row, raw, label=f"corrupt {corruption}")
    head = store.head()

    if corruption == "object":
        object_path = store.root / "objects" / row["version"]
        object_path.write_bytes(b"tampered canonical object")
        with pytest.raises(V2Error, match="object.*hash"):
            store.read_object(row["version"])
        with pytest.raises(V2Error, match="object.*hash"):
            store.read_record(row["id"])
    else:
        manifest_path = store.root / "revisions" / f"{head}.json"
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
        with pytest.raises(V2Error) as caught:
            store.manifest()
        assert caught.value.code == "manifest-integrity"


def test_invalid_head_keeps_v2_mode_and_blocks_legacy_writes(tmp_path: Path) -> None:
    store, _, _ = _seed_legacy(tmp_path)
    (store.root / "HEAD").write_text("definitely-not-a-sha", encoding="ascii")

    assert is_v2(store.vault)
    with pytest.raises(V2Error):
        store.head()
    with pytest.raises(V2Error, match="cannot write a v2 vault"):
        require_legacy(store.vault, "legacy import")


def test_external_checkout_is_preserved_then_recovered(tmp_path: Path) -> None:
    store, baseline_revision, _ = _seed_legacy(tmp_path)
    row, raw = _v2_record(generate_ulid(), statement="Published version one.")
    first = _commit_record(store, row, raw, label="checkout one")
    assert store.refresh_checkout(previous_revision=baseline_revision) == []
    checkout = store.checkout_path(row["path"])
    assert checkout.read_bytes() == raw

    checkout.write_bytes(b"editor changed this checkout")
    with pytest.raises(V2Error, match="Editable Markdown differs"):
        store.check_checkout(row)

    changed_row, changed_raw = _v2_record(row["id"], statement="Published version two.")
    _commit_record(store, changed_row, changed_raw, label="checkout two")
    conflicts = store.refresh_checkout(previous_revision=first["knowledge_revision"])
    assert conflicts == [{"id": row["id"], "path": row["path"], "reason": "external-edit"}]
    assert checkout.read_bytes() == b"editor changed this checkout"

    checkout.unlink()
    assert store.refresh_checkout(previous_revision=first["knowledge_revision"]) == []
    assert checkout.read_bytes() == changed_raw


def test_path_collision_rejects_transaction_without_advancing_head(tmp_path: Path) -> None:
    store, _, _ = _seed_legacy(tmp_path)
    first_row, first_raw = _v2_record(generate_ulid())
    _commit_record(store, first_row, first_raw, label="path owner")
    before = store.head()
    second_row, second_raw = _v2_record(generate_ulid())
    second_row["path"] = first_row["path"]

    with pytest.raises(V2Error, match="Two records cannot occupy"):
        _commit_record(store, second_row, second_raw, label="path collision")

    assert store.head() == before
    assert set(store.manifest()["records"]) == {LEGACY_ID, first_row["id"]}
    assert not (store.root / "objects" / second_row["version"]).exists()
