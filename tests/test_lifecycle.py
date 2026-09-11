from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from synapse.knowledge import record_descriptor
from synapse.lifecycle import (
    LifecycleError,
    backup_workspace,
    restore_workspace,
    start_viewer,
    stop_viewer,
    viewer_status,
)
from synapse.revisions import RevisionStore
from synapse.source_store import prepare_source
from synapse.util import generate_ulid
from synapse.v2_contracts import hash_bytes


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "synthetic workspace with spaces"
    store = RevisionStore(vault)
    raw = b"---\nid: me\ntype: person\nname: Synthetic Owner\nreview_status: proposed\n---\n\nOwner.\n"
    record = record_descriptor(raw, path="entities/people/me.md")
    source, objects = prepare_source(b"synthetic retained source", origin="sources/synthetic.txt")
    objects[record["version"]] = raw
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"seed"),
        mutate=lambda manifest, _read: (
            manifest["records"].update({"me": copy.deepcopy(record)}),
            manifest["sources"].update({source["id"]: source["version"]}),
            manifest["source_versions"].update({source["version"]: source}),
        ),
        objects=objects,
        initialize=True,
    )
    (vault / ".synapse" / "source-purpose").mkdir(parents=True)
    (vault / ".synapse" / "source-purpose" / "policy.json").write_text('{"preserved":true}', encoding="utf-8")
    (vault / ".synapse" / "host-events.json").write_text('{"pending":true}', encoding="utf-8")
    return vault


def _advance_revision(vault: Path) -> str:
    store = RevisionStore(vault)
    source, objects = prepare_source(b"advanced retained source", origin="sources/advanced.txt")
    receipt = store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"advance"),
        mutate=lambda manifest, _read: (
            manifest["sources"].update({source["id"]: source["version"]}),
            manifest["source_versions"].update({source["version"]: source}),
        ),
        objects=objects,
    )
    return receipt["knowledge_revision"]


def test_viewer_lifecycle_owns_only_its_token_and_uses_a_random_port(tmp_path: Path) -> None:
    home = tmp_path / "home with spaces"
    vault = _vault(tmp_path)
    assert viewer_status(home) == {"status": "missing"}
    started = start_viewer(home, vault)
    try:
        assert started["status"] == "running"
        assert started["url"].startswith("http://127.0.0.1:")
        assert viewer_status(home)["status"] == "running"
        with urlopen(started["url"] + "/_synapse/lifecycle") as response:
            assert response.status == 403
    except HTTPError as exc:
        assert exc.code == 403
    finally:
        stopped = stop_viewer(home)
    assert stopped == {
        **{key: started[key] for key in ("url", "pid", "vault", "started_revision")},
        "status": "stopped",
        "stopped": True,
    }
    assert viewer_status(home) == {"status": "missing"}


def test_start_is_idempotent_and_refuses_live_unverified_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = _vault(tmp_path)
    started = start_viewer(home, vault)
    try:
        assert start_viewer(home, vault) == started
        state_path = home / "run" / "viewer.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        token = state["token"]
        state["token"] = "wrong-token"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        assert viewer_status(home)["status"] == "stale"
        with pytest.raises(LifecycleError, match="unverified"):
            start_viewer(home, vault)
    finally:
        state_path = home / "run" / "viewer.json"
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["token"] = token
            state_path.write_text(json.dumps(state), encoding="utf-8")
        stop_viewer(home)


def test_reopen_same_vault_after_a_revision_advance_keeps_the_owned_viewer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = _vault(tmp_path)
    started = start_viewer(home, vault)
    try:
        advanced = _advance_revision(vault)
        reopened = start_viewer(home, vault)
        assert reopened["url"] == started["url"]
        assert reopened["pid"] == started["pid"]
        assert reopened["started_revision"] == started["started_revision"]
        assert reopened["current_revision"] == advanced
        assert viewer_status(home)["current_revision"] == advanced
    finally:
        stop_viewer(home)


def test_concurrent_starts_share_one_owned_viewer_and_state_is_truthful(tmp_path: Path) -> None:
    home = tmp_path / "home"
    vault = _vault(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: start_viewer(home, vault), range(2)))
    try:
        assert first == second
    finally:
        assert stop_viewer(home)["stopped"] is True
    state_path = home / "run" / "viewer.json"
    state_path.write_text(
        json.dumps(
            {
                "status": "running",
                "url": "http://127.0.0.1:not-a-port",
                "pid": 999_999_999,
                "token": "synthetic",
                "vault": str(vault),
                "started_revision": RevisionStore(vault).head(),
            }
        ),
        encoding="utf-8",
    )
    assert viewer_status(home)["status"] == "stale"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["url"] = "http://127.0.0.1:9"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    assert viewer_status(home)["status"] == "crashed"


def test_backup_and_restore_preserve_durable_state_but_skip_env_and_symlinks(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / ".env").write_text("do-not-copy", encoding="utf-8")
    (vault / "link").symlink_to(vault / ".synapse" / "host-events.json")
    (vault / ".synapse" / "index.db").write_bytes(b"derived")
    (vault / ".synapse" / "index.db-wal").write_bytes(b"derived")
    (vault / ".synapse" / "v2-indexes").mkdir()
    (vault / ".synapse" / "v2-indexes" / "derived.db").write_bytes(b"derived")
    backup = tmp_path / "backup"
    restored = tmp_path / "restored"
    report = backup_workspace(vault, backup)
    restored_report = restore_workspace(backup, restored)
    assert report["status"] == "backed_up"
    assert restored_report["status"] == "restored"
    assert report["knowledge_revision"] == RevisionStore(vault).head()
    assert RevisionStore(restored).head() == RevisionStore(vault).head()
    assert (restored / ".synapse" / "source-purpose" / "policy.json").read_text() == '{"preserved":true}'
    assert (restored / ".synapse" / "host-events.json").read_text() == '{"pending":true}'
    assert not (restored / ".env").exists()
    assert not (restored / "link").exists()
    assert not (restored / ".synapse" / "index.db").exists()
    assert not (restored / ".synapse" / "index.db-wal").exists()
    assert not (restored / ".synapse" / "v2-indexes").exists()
    assert "cannot become approval" in restored_report["warnings"][0]


def test_restore_rejects_missing_referenced_retained_objects(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    backup = tmp_path / "backup"
    destination = tmp_path / "restored"
    backup_workspace(vault, backup)
    record = RevisionStore(backup).manifest()["records"]["me"]
    (backup / "_synapse" / "objects" / record["version"]).unlink()
    with pytest.raises(Exception, match="Retained object"):
        restore_workspace(backup, destination)
    assert not destination.exists()


def test_backup_requires_new_destination_and_a_valid_retained_head(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    with pytest.raises(LifecycleError, match="new directory"):
        backup_workspace(vault, vault)
    (vault / "_synapse" / "HEAD").write_text("not-a-revision", encoding="ascii")
    with pytest.raises(Exception, match="SHA-256"):
        backup_workspace(vault, tmp_path / "backup")
