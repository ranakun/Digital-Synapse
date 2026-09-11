from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from synapse.knowledge import record_descriptor
from synapse.owner_host import OwnerHost
from synapse.publication import Publisher
from synapse.revisions import RevisionStore
from synapse.runs import RunManager
from synapse.source_store import prepare_source
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes


def _seed_vault(tmp_path: Path) -> RevisionStore:
    vault = tmp_path / "vault"
    raw = (
        b"---\n"
        b"id: me\n"
        b"type: person\n"
        b"name: Owner\n"
        b"review_status: proposed\n"
        b"---\n\n"
        b"Owner.\n"
    )
    row = record_descriptor(raw, path="entities/people/me.md")
    store = RevisionStore(vault)
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"baseline"),
        mutate=lambda manifest, _read: manifest["records"].update({"me": copy.deepcopy(row)}),
        objects={row["version"]: raw},
        initialize=True,
    )
    return store


def _request(
    owner_ref: str,
    *,
    mode: str = "investigate",
    run_id: str | None = None,
    pinned_revision: str | None = None,
) -> dict:
    value = {
        "id": generate_ulid(),
        "mode": mode,
        "purpose": "Investigate a bounded synthetic question.",
        "context": "Synthetic test context.",
        "subject_ids": ["me"],
        "owner_instruction_ref": owner_ref,
        "knowledge_policy": "mixed",
        "budget": {
            "preset": "focused",
            "max_minutes": 10,
            "max_source_expansions": 8,
            "max_result_characters": 8_000,
        },
    }
    if mode == "continue":
        value["continuation_id"] = run_id or generate_ulid()
    if pinned_revision is not None:
        value["pinned_revision"] = pinned_revision
    return value


def _capability(store: RevisionStore, run_id: str, owner_ref: str, *, actions=None) -> tuple[OwnerHost, dict[str, str]]:
    host = OwnerHost(store)
    capability = host.record_instruction(
        owner_ref,
        actions=actions or ["investigate", "admit", "stage"],
        scope={"run_id": run_id, "subject_ids": ["me"]},
    )
    return host, capability


def test_start_binds_real_owner_origin_and_persists_public_state(tmp_path: Path) -> None:
    store = _seed_vault(tmp_path)
    manager = RunManager(store.vault)
    run_id = generate_ulid()
    host, capability = _capability(store, run_id, "owner-message-1")

    run = manager.start(_request("owner-message-1"), capability, run_id=run_id)

    assert run["status"] == "running"
    assert run["owner_event_id"] == capability["event_id"]
    assert run["knowledge_revision"] == store.head()
    assert run["request"]["subject_ids"] == ["me"]
    assert (store.root / "runs" / f"{run_id}.json").is_file()
    assert manager.public(run_id) == run
    assert Publisher(store.vault)._run(capability, run_id, "admit")["owner_event_id"] == capability["event_id"]

    with pytest.raises(V2Error, match="owner event"):
        manager.start(_request("other-owner-message"), capability, run_id=run_id)
    with pytest.raises(V2Error, match="could not be verified"):
        manager.checkpoint(run_id, {"event_id": capability["event_id"], "token": "spoofed"}, operations=1)
    host.revoke(capability)
    with pytest.raises(V2Error, match="revoked"):
        manager.checkpoint(run_id, capability, operations=1)


def test_parallel_owner_event_cannot_update_the_active_run_segment(tmp_path: Path) -> None:
    store = _seed_vault(tmp_path)
    manager = RunManager(store.vault)
    run_id = generate_ulid()
    host, capability = _capability(store, run_id, "owner-message-parallel")
    manager.start(_request("owner-message-parallel"), capability, run_id=run_id)
    parallel = host.record_instruction(
        "owner-message-parallel",
        actions=["investigate", "admit", "stage"],
        scope={"run_id": run_id, "subject_ids": ["me"]},
    )

    with pytest.raises(V2Error, match="active owner event"):
        manager.checkpoint(run_id, parallel, operations=1)
    assert manager.get(run_id)["usage"]["operations"] == 0


def test_start_honors_a_reachable_pinned_revision(tmp_path: Path) -> None:
    store = _seed_vault(tmp_path)
    pinned_revision = store.head()
    descriptor, objects = prepare_source(b"A later retained source.", origin="test")
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(canonical_json(descriptor)),
        mutate=lambda manifest, _read: (
            manifest["sources"].update({descriptor["id"]: descriptor["version"]}),
            manifest["source_versions"].update({descriptor["version"]: descriptor}),
        ),
        objects=objects,
    )
    manager = RunManager(store.vault)
    run_id = generate_ulid()
    _, capability = _capability(store, run_id, "owner-message-pinned")

    run = manager.start(
        _request("owner-message-pinned", pinned_revision=pinned_revision),
        capability,
        run_id=run_id,
    )

    assert run["knowledge_revision"] == pinned_revision
    assert run["knowledge_revision"] != store.head()


def test_ordinary_consultation_never_creates_run_state(tmp_path: Path) -> None:
    store = _seed_vault(tmp_path)
    manager = RunManager(store.vault)
    run_id = generate_ulid()
    host, capability = _capability(store, run_id, "owner-message-2")
    request = _request("owner-message-2", mode="consult")

    with pytest.raises(V2Error, match="investigate request"):
        manager.start(request, capability, run_id=run_id)
    assert not (store.root / "runs").exists()
    assert host is not None


def test_checkpoint_budget_is_shared_across_tool_types_and_runs_are_independent(tmp_path: Path) -> None:
    store = _seed_vault(tmp_path)
    manager = RunManager(store.vault)
    first_id, second_id = generate_ulid(), generate_ulid()
    _, first_capability = _capability(store, first_id, "owner-message-3")
    _, second_capability = _capability(store, second_id, "owner-message-4")
    manager.start(_request("owner-message-3"), first_capability, run_id=first_id)
    manager.start(_request("owner-message-4"), second_capability, run_id=second_id)

    manager.checkpoint(first_id, first_capability, operations=4, source_expansions=2)
    exhausted = manager.checkpoint(first_id, first_capability, operations=4, source_expansions=1)

    assert exhausted["status"] == "partial"
    assert exhausted["stop_reason"] == "Operation or source-expansion budget exhausted."
    assert exhausted["usage"]["operations"] == 8
    assert exhausted["usage"]["source_expansions"] == 3
    assert manager.get(second_id)["usage"]["operations"] == 0


def test_duplicate_request_is_idempotent_and_changed_request_conflicts(tmp_path: Path) -> None:
    store = _seed_vault(tmp_path)
    manager = RunManager(store.vault)
    run_id = generate_ulid()
    _, capability = _capability(store, run_id, "owner-message-5")
    request = _request("owner-message-5")
    first = manager.start(request, capability, run_id=run_id)
    second = manager.start(copy.deepcopy(request), capability, run_id=run_id)

    assert second == first
    changed = copy.deepcopy(request)
    changed["purpose"] = "A different bounded question."
    with pytest.raises(V2Error, match="different request"):
        manager.start(changed, capability, run_id=run_id)


def test_cancel_resume_requires_explicit_event_and_revision_revalidation(tmp_path: Path) -> None:
    store = _seed_vault(tmp_path)
    manager = RunManager(store.vault)
    run_id = generate_ulid()
    _, capability = _capability(store, run_id, "owner-message-6")
    manager.start(_request("owner-message-6"), capability, run_id=run_id)
    cancelled = manager.cancel(run_id, capability)
    assert cancelled["status"] == "cancelled"
    assert cancelled["checkpoint"]["operations"] == 0

    descriptor, objects = prepare_source(b"A retained source.", origin="test")
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(canonical_json(descriptor)),
        mutate=lambda manifest, _read: (
            manifest["sources"].update({descriptor["id"]: descriptor["version"]}),
            manifest["source_versions"].update({descriptor["version"]: descriptor}),
        ),
        objects=objects,
    )
    resume_ref = "owner-message-7"
    _, resume_capability = _capability(
        store,
        run_id,
        resume_ref,
        actions=["resume", "investigate", "admit", "stage"],
    )
    continuation = _request(resume_ref, mode="continue", run_id=run_id)

    with pytest.raises(V2Error, match="revision changed"):
        manager.resume(run_id, continuation, resume_capability)
    resumed = manager.resume(run_id, continuation, resume_capability, accept_current_revision=True)
    assert resumed["status"] == "running"
    assert resumed["owner_event_id"] == resume_capability["event_id"]
    assert resumed["revalidation_required"] is True
    assert resumed["changed_refs"]
    assert resumed["usage"]["operations"] == 0
    assert len(resumed["segments"]) == 2


def test_resumed_segment_has_its_own_wall_clock_budget(tmp_path: Path) -> None:
    store = _seed_vault(tmp_path)
    current = [datetime(2026, 9, 11, 12, 0, tzinfo=UTC)]
    manager = RunManager(store.vault, clock=lambda: current[0])
    run_id = generate_ulid()
    _, capability = _capability(store, run_id, "owner-message-day-start")
    manager.start(_request("owner-message-day-start"), capability, run_id=run_id)
    manager.cancel(run_id, capability)

    current[0] += timedelta(days=1)
    resume_ref = "owner-message-day-resume"
    _, resume_capability = _capability(
        store,
        run_id,
        resume_ref,
        actions=["resume", "investigate", "admit", "stage"],
    )
    continuation = _request(resume_ref, mode="continue", run_id=run_id)
    resumed = manager.resume(run_id, continuation, resume_capability)
    assert resumed["usage"]["elapsed_seconds"] == 86_400

    current[0] += timedelta(minutes=9, seconds=59)
    active = manager.checkpoint(run_id, resume_capability, operations=1)
    assert active["status"] == "running"
    assert active["checkpoint"]["segment_elapsed_seconds"] == 599

    current[0] += timedelta(seconds=2)
    expired = manager.status(run_id)
    assert expired["status"] == "partial"
    assert expired["usage"]["elapsed_seconds"] == 87_001
    assert expired["checkpoint"]["segment_elapsed_seconds"] == 601


def test_finish_retains_supplied_stop_reason(tmp_path: Path) -> None:
    store = _seed_vault(tmp_path)
    manager = RunManager(store.vault)
    run_id = generate_ulid()
    _, capability = _capability(store, run_id, "owner-message-finish")
    manager.start(_request("owner-message-finish"), capability, run_id=run_id)

    finished = manager.finish(
        run_id,
        capability,
        status="completed",
        stop_reason="No sound connection survived the scoped challenge.",
    )

    assert finished["status"] == "completed"
    assert finished["stop_reason"] == "No sound connection survived the scoped challenge."
    assert manager.get(run_id)["stop_reason"] == finished["stop_reason"]


def test_status_persists_elapsed_wall_clock_budget(tmp_path: Path) -> None:
    store = _seed_vault(tmp_path)
    current = [datetime(2026, 9, 11, 12, 0, tzinfo=UTC)]
    manager = RunManager(store.vault, clock=lambda: current[0])
    run_id = generate_ulid()
    _, capability = _capability(store, run_id, "owner-message-8")
    manager.start(_request("owner-message-8"), capability, run_id=run_id)

    current[0] += timedelta(minutes=10, seconds=1)
    status = manager.status(run_id)

    assert status["status"] == "partial"
    assert status["wire_status"] == "partial"
    assert status["usage"]["elapsed_seconds"] == 601
    assert manager.get(run_id)["status"] == "partial"
