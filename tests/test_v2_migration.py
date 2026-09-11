
import copy
import json

import pytest
from typer.testing import CliRunner

from synapse.config import init_vault
from synapse.gateway import Gateway
from synapse.host_control import HostControl
from synapse.host_session import NativeHost
from synapse.knowledge import encode_record
from synapse.owner_host import OwnerHost
from synapse.publication import Publisher
from synapse.revisions import RevisionStore, durable_write
from synapse.source_store import evidence_ref, prepare_source
from synapse.util import generate_ulid, write_frontmatter
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes, validate_payload
from synapse.v2_control_cli import app
from synapse.v2_migration import activate, copy_stable, prepare, trial


def legacy(tmp_path):
    vault = init_vault(tmp_path / "live", initialize_git=False)
    identity = generate_ulid()
    write_frontmatter(vault / "entities/people/friend.md", {"id": identity, "type": "person", "name": "Friend", "aliases": ["Example Friend"], "review_status": "proposed", "relations": [{"type": "knows", "target": "me"}]}, "A known person. [[Me]]")
    (vault / "inbox/note.txt").write_text("A study group might be helpful. It is only a possibility.\n", encoding="utf-8")
    (vault / "inbox/copy.txt").write_bytes((vault / "inbox/note.txt").read_bytes())
    return vault


def test_additive_migration_and_cold_restore_preserve_canonical_data(tmp_path):
    vault = legacy(tmp_path)
    before = {path.relative_to(vault).as_posix(): hash_bytes(path.read_bytes()) for path in vault.rglob("*.md")}
    report = trial(vault, tmp_path / "trial")
    assert report["source_vault_unchanged"]
    assert report["exact_markdown_preserved"]
    assert report["exact_source_bytes_preserved"]
    assert report["normalized_legacy_edges_preserved"]
    assert report["cold_rebuild_preserved"]
    assert not report["live_cutover"]
    assert not (vault / "_synapse").exists()
    assert before == {path.relative_to(vault).as_posix(): hash_bytes(path.read_bytes()) for path in vault.rglob("*.md")}
    copy_stable(tmp_path / "trial", tmp_path / "restored")
    restored = Gateway(tmp_path / "restored")
    assert restored.revision == report["knowledge_revision"]
    assert len({source["source_family_id"] for source in restored.view.manifest["source_versions"].values()}) == 1
    assert restored.search_sources("possibility")["total"] == 2


def test_activation_rechecks_exact_baseline(tmp_path):
    vault = legacy(tmp_path)
    snapshot = prepare(vault)
    store = RevisionStore(vault)
    capability = OwnerHost(store).record_instruction("synthetic-cutover-decision", actions=["capture"], scope={"bootstrap": True, "snapshot_hash": snapshot.fingerprint})
    (vault / "entities/people/friend.md").write_text("Changed externally.")
    with pytest.raises(V2Error, match="baseline changed"):
        activate(vault, snapshot, capability, operation_id=generate_ulid(), request_id=generate_ulid())
    assert not (vault / "_synapse/HEAD").exists()


def test_public_v1_migration_preserves_non_owner_legacy_identity_and_metadata(tmp_path):
    vault = init_vault(tmp_path / "legacy", initialize_git=False)
    (vault / "entities/people/me.md").unlink()
    identity = "legacy-public-person"
    raw = (
        b"---\n"
        b"id: legacy-public-person\n"
        b"type: person\n"
        b"name: Public Person\n"
        b"review_status: proposed\n"
        b"unknown_metadata:\n"
        b"  preserved: true\n"
        b"---\n\n"
        b"Original public legacy content.\n"
    )
    path = vault / "entities/people/public-person.md"
    path.write_bytes(raw)

    snapshot = prepare(vault)
    report = trial(vault, tmp_path / "trial")
    gateway = Gateway(tmp_path / "trial")
    retained = gateway.view.store.read_object(gateway.view.manifest["records"][identity]["version"])

    assert report["exact_markdown_preserved"]
    assert retained == raw
    legacy_record = gateway.view.records(ids=[identity])[0]
    assert legacy_record["id"] == identity
    validate_payload("legacy_record", legacy_record)
    assert snapshot.report["entities"] == 1


def test_new_strict_claim_can_reference_migrated_legacy_entities(tmp_path):
    vault = init_vault(tmp_path / "legacy", initialize_git=False)
    (vault / "entities/people/me.md").unlink()
    subject_id, related_id = "legacy-subject", "legacy-related"
    for identity in (subject_id, related_id):
        write_frontmatter(
            vault / f"entities/people/{identity}.md",
            {"id": identity, "type": "person", "name": identity, "review_status": "proposed"},
            f"Original {identity} content.",
        )
    trial(vault, tmp_path / "trial")
    store = RevisionStore(tmp_path / "trial")
    publisher, owner = Publisher(store.vault), OwnerHost(store)
    source, objects = prepare_source(
        b"The retained source supports this relationship.",
        origin="synthetic-public-source.txt",
        source_id=generate_ulid(),
        source_family_id=generate_ulid(),
        captured_at="2026-09-11T00:00:00Z",
    )
    capture = owner.record_instruction(
        "synthetic-source-capture",
        actions=["capture"],
        scope={"capture_targets": {source["origin"]: source["original_hash"]}},
    )
    publisher.capture(capture, source, objects, operation_id=generate_ulid(), request_id=generate_ulid())
    run_id = generate_ulid()
    capability = owner.record_instruction(
        "synthetic-claim-request", actions=["investigate", "admit", "stage"], scope={"run_id": run_id}
    )
    durable_write(
        store.root / "runs" / f"{run_id}.json",
        canonical_json({"id": run_id, "status": "running", "owner_event_id": capability["event_id"], "request": {"subject_ids": [subject_id]}}),
    )
    claim_id = generate_ulid()
    payload = {
        "id": claim_id,
        "version": "0" * 64,
        "subject_id": subject_id,
        "claim_key": "legacy-reference",
        "record_kind": "relationship",
        "availability": "suggestion",
        "review_status": "proposed",
        "owner_review": {"status": "not-reviewed", "disposition": "none"},
        "owner_position": "unreviewed",
        "lifecycle": "current",
        "epistemic_basis": ["artifact-evidence"],
        "facets": ["relationship"],
        "statement": "The source records a relationship between the retained legacy entities.",
        "conditions_and_limits": "The source is limited to the recorded interaction.",
        "support": "The retained source provides the supporting passage.",
        "counterevidence": [],
        "alternatives": [],
        "would_change_with": [],
        "evidence": [evidence_ref(source, objects.__getitem__, 0, 20)],
        "dependencies": [],
        "context_refs": [{"id": subject_id, "version": store.manifest()["records"][subject_id]["version"], "role": "about", "scope": "The claim concerns this retained entity."}],
        "relationship": {"from_id": subject_id, "to_id": related_id, "relation_type": "knows", "scope": "The retained interaction."},
        "as_of": "2026-09-11",
        "origin_run_id": run_id,
    }
    raw = encode_record(payload)
    receipt = publisher.admit(
        capability,
        {f"entities/insights/{claim_id}.md": raw},
        run_id=run_id,
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )

    assert receipt["knowledge_revision"] == store.head()
    assert Gateway(store.vault).view.records(ids=[claim_id])[0]["subject_id"] == subject_id
    assert Gateway(store.vault).view.relationships(identity=subject_id, include_suggestions=True)[0]["to_id"] == related_id


def test_late_unicode_source_search_returns_exact_expandable_evidence(tmp_path):
    vault = legacy(tmp_path)
    (vault / "inbox/long.txt").write_text("A preface.\n" * 7000 + "The workshop note includes café and a different cause.", encoding="utf-8")
    trial(vault, tmp_path / "trial")
    gateway = Gateway(tmp_path / "trial")
    item = gateway.search_sources("workshop")["items"][0]
    assert item["offset"] > 65000
    assert "café" in item["excerpt"]
    assert gateway.passage(item["evidence"])["excerpt"] == item["excerpt"]


def test_legacy_edge_ids_survive_unrelated_added_relationship(tmp_path):
    vault = legacy(tmp_path)
    trial(vault, tmp_path / "trial")
    gateway = Gateway(tmp_path / "trial")
    before = gateway.view.relationships()
    assert all(edge["id"].startswith("legacy:") for edge in before)
    # A renamed checkout cannot alter IDs, nor does a cache rebuild depend on
    # its path/stat order. Full canonical relocation is tested at publication.
    (tmp_path / "trial/entities/people/friend.md").rename(tmp_path / "trial/entities/people/moved.md")
    gateway.view.index_path.unlink()
    assert Gateway(tmp_path / "trial").view.relationships() == before


def _activation_host(vault):
    event = {"id": "owner", "actor": "user", "text": "Activate this reviewed baseline."}
    return HostControl(NativeHost(
        vault, event_reader={"owner": event}.__getitem__, display=lambda _: None, reasoner=None,
    ))


def test_preview_then_host_activation_with_sources(tmp_path):
    vault = legacy(tmp_path)
    runner = CliRunner()
    first = runner.invoke(app, ["migration-preview", "--vault", str(vault)])
    second = runner.invoke(app, ["migration-preview", "--vault", str(vault)])
    assert first.exit_code == second.exit_code == 0
    reviewed = json.loads(first.output)
    assert reviewed["snapshot_hash"] == json.loads(second.output)["snapshot_hash"]
    assert not (vault / "_synapse/HEAD").exists()
    receipt = _activation_host(vault).execute(
        "activate", {"snapshot_hash": reviewed["snapshot_hash"]}, owner_event_ref="owner",
    )
    gateway = Gateway(vault)
    assert gateway.revision == receipt["knowledge_revision"]
    assert gateway.search_sources("possibility")["total"] == 2
    sources = gateway.view.manifest["source_versions"].values()
    assert len({source["source_family_id"] for source in sources}) == 1


@pytest.mark.parametrize("change", ["edit", "add", "remove", "rename"])
def test_reviewed_source_changes_still_block_activation(tmp_path, change):
    vault = legacy(tmp_path)
    reviewed = prepare(vault).review_fingerprint
    source = vault / "inbox/note.txt"
    if change == "edit":
        source.write_text("A changed qualification.")
    elif change == "add":
        (vault / "inbox/added.txt").write_text("Another source.")
    elif change == "remove":
        source.unlink()
    else:
        source.rename(vault / "inbox/renamed.txt")
    with pytest.raises(V2Error) as raised:
        _activation_host(vault).execute(
            "activate", {"snapshot_hash": reviewed}, owner_event_ref="owner",
        )
    assert raised.value.code == "stale-selection"
    assert not (vault / "_synapse/HEAD").exists()


def test_review_digest_keeps_extraction_family_and_record_changes(tmp_path):
    vault = legacy(tmp_path)
    baseline = prepare(vault)
    another = prepare(vault)
    assert baseline.fingerprint != another.fingerprint
    assert baseline.review_fingerprint == another.review_fingerprint
    for field, value in [
        ("text_version", "a" * 64),
        ("media_type", "text/markdown"),
        ("extraction", {"method": "different", "version": "2", "completeness": "partial"}),
        ("source_family_id", "different-family"),
    ]:
        changed = copy.deepcopy(baseline)
        changed.sources[0][field] = value
        assert baseline.review_fingerprint != changed.review_fingerprint
    (vault / "entities/people/friend.md").write_bytes(
        (vault / "entities/people/friend.md").read_bytes() + b"\nNew context.\n"
    )
    assert baseline.review_fingerprint != prepare(vault).review_fingerprint
