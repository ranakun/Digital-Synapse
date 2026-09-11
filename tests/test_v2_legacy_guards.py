from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from synapse.config import init_vault, load_config, save_config
from synapse.enrichment import attach_linkedin_profile, import_linkedin_profile_pdf
from synapse.gateway import Gateway
from synapse.identity import emit_candidates_proposal
from synapse.importers import (
    import_linkedin_certifications,
    import_linkedin_connections,
    import_linkedin_education,
    import_linkedin_endorsements_given,
    import_linkedin_endorsements_received,
    import_linkedin_events,
    import_linkedin_invitations,
    import_linkedin_job_applications,
    import_linkedin_messages,
    import_linkedin_positions,
    import_linkedin_recommendations_given,
    import_linkedin_recommendations_received,
    import_linkedin_saved_jobs,
    import_linkedin_skills,
)
from synapse.index import connect, reindex
from synapse.ingest import commit_proposed, commit_vault, ingest_file
from synapse.knowledge import encode_record, record_descriptor
from synapse.maintenance import archive_entity, merge_entities, verify_entities
from synapse.parser import parse_entity_file
from synapse.proposals import apply_proposal
from synapse.read_view import ReadView
from synapse.revisions import RevisionStore, is_v2, require_legacy
from synapse.source_store import evidence_ref, prepare_source
from synapse.util import write_frontmatter
from synapse.v2_contracts import V2Error, hash_bytes
from synapse.whatsapp import import_whatsapp_chat

ROOT = Path(__file__).parents[1]
EXAMPLE = json.loads(
    (ROOT / "docs/v2/contracts/example-knowledge_record.json").read_text(encoding="utf-8")
)["payload"]

# Stable fixture identities make a failed route or projection easy to inspect.
TARGET_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
CORRECTION_ID = "01ARZ3NDEKTSV4RRFFQ69G5FC0"
SUGGESTION_ID = "01ARZ3NDEKTSV4RRFFQ69G5FD0"
SOURCE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def _legacy_raw(
    identity: str,
    *,
    entity_type: str,
    name: str,
    review_status: str,
    body: str,
    properties: dict | None = None,
    relations: list[dict] | None = None,
) -> bytes:
    metadata = {
        "id": identity,
        "type": entity_type,
        "name": name,
        "review_status": review_status,
    }
    if properties is not None:
        metadata["properties"] = properties
    if relations is not None:
        metadata["relations"] = relations
    rendered = json.dumps(metadata, ensure_ascii=False, indent=2)
    return f"---\n{rendered}\n---\n\n{body}".encode()


def _seed_v2_vault(tmp_path: Path) -> tuple[Path, RevisionStore, dict[str, bytes]]:
    vault = tmp_path / "v2-vault"
    me_raw = (
        b"---\r\n"
        b"id: me\r\n"
        b"type: person\r\n"
        b"name: Owner\r\n"
        b"review_status: verified\r\n"
        b"---\r\n"
        b"\r\nRetained owner baseline.\r\n"
    )
    target_raw = _legacy_raw(
        TARGET_ID,
        entity_type="person",
        name="Qualified Target",
        review_status="verified",
        body="Retained target assertion.\n",
    )
    correction_raw = _legacy_raw(
        CORRECTION_ID,
        entity_type="insight",
        name="Legacy qualification",
        review_status="proposed",
        body="This legacy record qualifies the target.\n",
        properties={
            "knowledge_profile": "owner-knowledge-v1",
            "subject_id": TARGET_ID,
            "claim_key": "qualification",
        },
        relations=[
            {
                "type": "related_to",
                "target": TARGET_ID,
                "properties": {
                    "roles": ["qualifies"],
                    "scope": "The retained target needs this qualification.",
                },
            }
        ],
    )
    source_descriptor, source_objects = prepare_source(
        b"Retained source passage with exact context.",
        origin="legacy-guard-fixture.txt",
        source_id=SOURCE_ID,
        source_family_id=SOURCE_ID,
        captured_at="2026-09-11T00:00:00Z",
    )
    evidence = evidence_ref(source_descriptor, source_objects.__getitem__, 0, 20)
    suggestion_payload = copy.deepcopy(EXAMPLE)
    suggestion_payload.update(
        id=SUGGESTION_ID,
        subject_id="me",
        statement="Unreviewed suggestion must not become a legacy fact.",
        evidence=[evidence],
        availability="suggestion",
        review_status="proposed",
    )
    suggestion_raw = encode_record(suggestion_payload, name="Unreviewed suggestion")
    raws = {
        "me": me_raw,
        TARGET_ID: target_raw,
        CORRECTION_ID: correction_raw,
        SUGGESTION_ID: suggestion_raw,
    }
    paths = {
        "me": "entities/people/me.md",
        TARGET_ID: f"entities/people/{TARGET_ID}.md",
        CORRECTION_ID: f"entities/insights/{CORRECTION_ID}.md",
        SUGGESTION_ID: f"entities/insights/{SUGGESTION_ID}.md",
    }
    rows = {
        identity: record_descriptor(raw, path=paths[identity])
        for identity, raw in raws.items()
    }
    objects = dict(source_objects)
    objects.update({hash_bytes(raw): raw for raw in raws.values()})
    store = RevisionStore(vault)

    def seed(manifest: dict, _read) -> None:
        manifest["records"].update(rows)
        manifest["sources"][SOURCE_ID] = source_descriptor["version"]
        manifest["source_versions"][source_descriptor["version"]] = source_descriptor

    store.transact(
        operation_id="01ARZ3NDEKTSV4RRFFQ69G5FE0",
        request_id="01ARZ3NDEKTSV4RRFFQ69G5FE1",
        kind="capture",
        payload_hash=hash_bytes(b"explicit v2 legacy guard fixture"),
        mutate=seed,
        objects=objects,
        initialize=True,
    )
    store.refresh_checkout()
    (vault / "inbox").mkdir(parents=True, exist_ok=True)
    (vault / "inbox" / "retained-source.txt").write_text("pending source", encoding="utf-8")
    (vault / "proposals" / "pending").mkdir(parents=True, exist_ok=True)
    (vault / "proposals" / "pending" / "existing.yaml").write_text("pending: true\n", encoding="utf-8")
    return vault, store, raws


def _tree_snapshot(vault: Path) -> dict[str, tuple[str, bytes]]:
    roots = [
        vault / "_synapse" / "HEAD",
        vault / "_synapse" / "objects",
        vault / "_synapse" / "revisions",
        vault / "entities",
        vault / "inbox",
        vault / "proposals" / "pending",
    ]
    snapshot: dict[str, tuple[str, bytes]] = {}
    for root in roots:
        if not root.exists():
            continue
        paths = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
        for path in paths:
            relative = path.relative_to(vault).as_posix()
            snapshot[relative] = ("symlink" if path.is_symlink() else "file", path.read_bytes())
    return snapshot


# Public canonical legacy writers exercised below, including every LinkedIn route.
LEGACY_WRITERS = [
    "import_linkedin_connections",
    "import_linkedin_certifications",
    "import_linkedin_positions",
    "import_linkedin_education",
    "import_linkedin_skills",
    "import_linkedin_recommendations_received",
    "import_linkedin_recommendations_given",
    "import_linkedin_saved_jobs",
    "import_linkedin_job_applications",
    "import_linkedin_invitations",
    "import_linkedin_messages",
    "import_linkedin_endorsements_received",
    "import_linkedin_endorsements_given",
    "import_linkedin_events",
    "import_whatsapp_chat",
    "ingest_file",
    "commit_proposed",
    "commit_vault",
    "apply_proposal",
    "merge_entities",
    "archive_entity",
    "verify_entities",
    "emit_candidates_proposal",
    "attach_linkedin_profile",
    "import_linkedin_profile_pdf",
    "save_config",
    "init_vault",
    "write_frontmatter",
]


def test_all_public_legacy_writers_block_before_first_side_effect(tmp_path: Path) -> None:
    vault, _store, _raws = _seed_v2_vault(tmp_path)
    missing = vault / "does-not-exist.input"
    calls = [
        ("import_linkedin_connections", lambda: import_linkedin_connections(missing, vault=vault)),
        ("import_linkedin_certifications", lambda: import_linkedin_certifications(missing, vault=vault)),
        ("import_linkedin_positions", lambda: import_linkedin_positions(missing, vault=vault)),
        ("import_linkedin_education", lambda: import_linkedin_education(missing, vault=vault)),
        ("import_linkedin_skills", lambda: import_linkedin_skills(missing, vault=vault)),
        ("import_linkedin_recommendations_received", lambda: import_linkedin_recommendations_received(missing, vault=vault)),
        ("import_linkedin_recommendations_given", lambda: import_linkedin_recommendations_given(missing, vault=vault)),
        ("import_linkedin_saved_jobs", lambda: import_linkedin_saved_jobs(missing, vault=vault)),
        ("import_linkedin_job_applications", lambda: import_linkedin_job_applications(missing, vault=vault)),
        ("import_linkedin_invitations", lambda: import_linkedin_invitations(missing, vault=vault)),
        ("import_linkedin_messages", lambda: import_linkedin_messages(missing, vault=vault)),
        ("import_linkedin_endorsements_received", lambda: import_linkedin_endorsements_received(missing, vault=vault)),
        ("import_linkedin_endorsements_given", lambda: import_linkedin_endorsements_given(missing, vault=vault)),
        ("import_linkedin_events", lambda: import_linkedin_events(missing, vault=vault)),
        ("import_whatsapp_chat", lambda: import_whatsapp_chat(missing, chat_key="fixture", vault=vault)),
        ("ingest_file", lambda: ingest_file(missing, vault=vault)),
        ("commit_proposed", lambda: commit_proposed(vault)),
        ("commit_vault", lambda: commit_vault(vault)),
        ("apply_proposal dry-run", lambda: apply_proposal(vault, missing, execute=False)),
        ("apply_proposal execute", lambda: apply_proposal(vault, missing, execute=True, allow_verify=True)),
        ("merge_entities", lambda: merge_entities(TARGET_ID, "missing", vault=vault)),
        ("archive_entity", lambda: archive_entity("missing", "fixture", vault=vault)),
        ("verify_entities", lambda: verify_entities(["missing"], vault=vault, yes=True)),
        ("emit_candidates_proposal", lambda: emit_candidates_proposal(vault, [{"name": "Unknown", "candidates": [TARGET_ID]}])),
        (
            "attach_linkedin_profile",
            lambda: attach_linkedin_profile(missing, person_id=TARGET_ID, captured_at="2026-09-11", vault=vault),
        ),
        (
            "import_linkedin_profile_pdf",
            lambda: import_linkedin_profile_pdf(missing, person_id=TARGET_ID, captured_at="2026-09-11", vault=vault),
        ),
        ("save_config", lambda: save_config(vault, {"unexpected": True})),
        ("init_vault", lambda: init_vault(vault, initialize_git=False)),
        (
            "write_frontmatter",
            lambda: write_frontmatter(
                vault / "entities" / "people" / "new.md",
                {"id": "new", "type": "person", "name": "New", "review_status": "proposed"},
                "must not be written",
            ),
        ),
    ]
    call_names = {name.removesuffix(" dry-run").removesuffix(" execute") for name, _call in calls}
    assert call_names == set(LEGACY_WRITERS)

    for name, call in calls:
        before = _tree_snapshot(vault)
        with pytest.raises(V2Error) as raised:
            call()
        assert raised.value.code == "legacy-write-blocked", name
        assert _tree_snapshot(vault) == before, f"{name} changed canonical or pending files"

    assert load_config(vault)["vault_path"] == str(vault.resolve())
    assert is_v2(vault)


def test_corrupt_head_still_blocks_legacy_writes(tmp_path: Path) -> None:
    vault, store, _raws = _seed_v2_vault(tmp_path)
    head = store.root / "HEAD"
    head.write_text("corrupt-head", encoding="ascii")
    before = _tree_snapshot(vault)

    with pytest.raises(V2Error) as raised:
        import_linkedin_connections(vault / "missing.csv", vault=vault)
    assert raised.value.code == "legacy-write-blocked"
    with pytest.raises(V2Error) as raised:
        write_frontmatter(
            vault / "entities" / "people" / "blocked.md",
            {"id": "blocked", "type": "person", "name": "Blocked", "review_status": "proposed"},
            "blocked",
        )
    assert raised.value.code == "legacy-write-blocked"
    assert _tree_snapshot(vault) == before
    assert is_v2(vault)
    with pytest.raises(V2Error, match="v2 vault"):
        require_legacy(vault, "legacy route")


def test_retained_reads_projection_rebuild_and_qualification_limits(tmp_path: Path) -> None:
    vault, store, raws = _seed_v2_vault(tmp_path)
    me_path = vault / "entities" / "people" / "me.md"
    target_path = vault / "entities" / "people" / f"{TARGET_ID}.md"
    me_path.write_text("editable checkout fabrication\n", encoding="utf-8")
    target_path.write_text("editable target fabrication\n", encoding="utf-8")

    view = ReadView(vault)
    gateway = Gateway(vault)
    assert view.records(ids=["me"])[0]["body"] == "Retained owner baseline.\n"
    assert gateway.record("me")["text"] == raws["me"].decode("utf-8")
    assert parse_entity_file(me_path, vault).body == "Retained owner baseline.\n"

    reindex(vault, full=True)
    with connect(vault) as conn:
        indexed_me = conn.execute(
            "SELECT body, content_hash, review_status FROM entities WHERE id='me'"
        ).fetchone()
        indexed_target = conn.execute(
            "SELECT body, content_hash, review_status, frontmatter FROM entities WHERE id=?",
            (TARGET_ID,),
        ).fetchone()
        suggestion = conn.execute("SELECT 1 FROM entities WHERE id=?", (SUGGESTION_ID,)).fetchone()
    assert indexed_me["body"] == "Retained owner baseline.\n"
    assert indexed_me["content_hash"] == hash_bytes(raws["me"])
    assert indexed_me["review_status"] == "verified"
    assert suggestion is None
    assert indexed_target["body"] == "Context requires qualifications. Read this identity through synapse v2 context."
    assert indexed_target["content_hash"] == hash_bytes(raws[TARGET_ID])
    assert indexed_target["review_status"] == "verified"
    assert json.loads(indexed_target["frontmatter"]) == {
        "id": TARGET_ID,
        "type": "person",
        "name": "Qualified Target",
        "review_status": "verified",
    }

    accepted_only = gateway.context(ids=[SUGGESTION_ID], knowledge_policy="accepted_only")
    assert accepted_only["items"] == []
    mixed = gateway.context(ids=[SUGGESTION_ID], knowledge_policy="mixed")
    assert mixed["items"][0]["records"][0]["statement"].startswith("Unreviewed suggestion")
    withheld = gateway.context(ids=[TARGET_ID], supports_qualifications=False)["items"][0]
    assert withheld["withheld"] is True
    from synapse.v2_compat import projection_entities

    projected, _issues = projection_entities(vault)
    projected_target = next(entity for entity in projected if entity.id == TARGET_ID)
    assert projected_target.review_status == "verified"
    assert projected_target.properties == {}
    assert projected_target.body.startswith("Context requires qualifications")

    before_rebuild = {
        "me": (indexed_me["body"], indexed_me["content_hash"], indexed_me["review_status"]),
        "target": (indexed_target["body"], indexed_target["content_hash"], indexed_target["review_status"]),
    }
    for path in (vault / ".synapse" / "index.db", *((vault / ".synapse" / "v2-indexes").glob("*.db"))):
        path.unlink(missing_ok=True)
    reindex(vault, full=True)
    with connect(vault) as conn:
        rebuilt_me = conn.execute("SELECT body, content_hash, review_status FROM entities WHERE id='me'").fetchone()
        rebuilt_target = conn.execute("SELECT body, content_hash, review_status FROM entities WHERE id=?", (TARGET_ID,)).fetchone()
    assert (rebuilt_me["body"], rebuilt_me["content_hash"], rebuilt_me["review_status"]) == before_rebuild["me"]
    assert (rebuilt_target["body"], rebuilt_target["content_hash"], rebuilt_target["review_status"]) == before_rebuild["target"]


def test_corrupt_selected_retained_object_cannot_be_hidden_by_cached_view(tmp_path: Path) -> None:
    vault, store, _raws = _seed_v2_vault(tmp_path)
    view = ReadView(vault)
    object_path = store.root / "objects" / view.manifest["records"]["me"]["version"]
    object_path.write_bytes(b"corrupt selected canonical object")

    with pytest.raises(V2Error, match="content hash"):
        view.records(ids=["me"])
