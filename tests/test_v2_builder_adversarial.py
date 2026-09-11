from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest
import yaml
from test_v2_publication import _bootstrap, _record, _run, _source

from synapse.host_session import NativeHost
from synapse.import_candidates import prepare_import
from synapse.knowledge import decode_record, record_descriptor
from synapse.proposal_builder import build_proposal
from synapse.publication import Publisher
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, hash_bytes

FIXTURE_LINKEDIN = Path(__file__).parent / "fixtures" / "linkedin" / "connections.csv"
FIXTURE_WHATSAPP = Path(__file__).parent / "fixtures" / "whatsapp" / "android-day-first.txt"

ID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ID_B = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
ID_OTHER = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
ID_SURVIVOR = "01ARZ3NDEKTSV4RRFFQ69G5FAY"
ID_ABSORBED = "01ARZ3NDEKTSV4RRFFQ69G5FAZ"
ID_REFERENCE = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
ID_VERIFIED = "01ARZ3NDEKTSV4RRFFQ69G5FB1"


class _Reasoner:
    def __init__(self) -> None:
        self.reviews: list[dict] = []

    def step(self, _context, *, timeout=120, cancelled=None):
        return {
            "action": "finish",
            "calls": [],
            "answer": "Synthetic investigation completed.",
            "used_record_ids": [],
            "alternatives": [],
            "uncertainties": [],
            "findings_json": "[]",
            "stop_reason": "The synthetic fixture is sufficient.",
        }

    def review(self, value):
        self.reviews.append(copy.deepcopy(value))
        return {
            "passed": True,
            "proposal_version": value["proposal_version"],
            "reason": "The exact synthetic before/after comparison passed.",
        }


def _group(
    group_id: str,
    brief: str,
    *,
    path: str,
    raw: bytes,
    target_id: str | None = None,
    requires: list[str] | None = None,
    identity_merge: dict | None = None,
) -> dict:
    change = {
        "kind": "replace-record" if target_id else "create-record",
        "path": path,
        "raw": raw,
    }
    if target_id:
        change["target_id"] = target_id
    group = {
        "id": group_id,
        "requires": requires or [],
        "effects": [
            {
                "id": f"effect-{group_id}",
                "kind": "mechanical",
                "meaning": f"Adopt the exact reviewed result for {group_id}.",
                "brief_span_start": 0,
                "brief_span_end": len(brief),
            }
        ],
        "changes": [change],
        "read_set": [],
        "source_preconditions": [],
    }
    if identity_merge is not None:
        group["identity_merge"] = identity_merge
    return group


def _multi_change_group(
    group_id: str,
    brief: str,
    changes: list[dict],
    *,
    identity_merge: dict | None = None,
) -> dict:
    effect = {
        "id": f"effect-{group_id}",
        "kind": "identity-merge" if identity_merge else "mechanical",
        "meaning": "Apply every exact operation in this reviewed group.",
        "brief_span_start": 0,
        "brief_span_end": len(brief),
    }
    group = {
        "id": group_id,
        "requires": [],
        "effects": [effect],
        "changes": changes,
        "read_set": [],
        "source_preconditions": [],
    }
    if identity_merge is not None:
        group["identity_merge"] = identity_merge
    return group


def _stage(
    publisher: Publisher,
    capability: dict[str, str],
    packet: dict,
    objects: dict[str, bytes],
    reasoner: _Reasoner,
) -> dict:
    return publisher.stage(
        capability,
        packet,
        objects,
        semantic_reviewer=reasoner.review,
    )


def _native_fixture(tmp_path: Path, *, group_count: int = 2):
    publisher, owner, _ = _bootstrap(tmp_path)
    source = _source(
        publisher,
        owner,
        raw=b"The retained builder fixture supports both exact synthetic claims.",
        origin="builder-source",
    )
    events = {
        "investigate": {
            "id": "investigate",
            "actor": "user",
            "text": "Investigate the bounded synthetic changes.",
        },
        "reply-first": {"id": "reply-first", "actor": "user", "text": "approve first"},
        "reply-all": {"id": "reply-all", "actor": "user", "text": "approve all"},
        "reply-both": {"id": "reply-both", "actor": "user", "text": "approve both"},
        "reply-second": {"id": "reply-second", "actor": "user", "text": "approve second"},
    }
    displays: list[str] = []
    reasoner = _Reasoner()
    host = NativeHost(
        publisher.store.vault,
        event_reader=lambda reference: events[reference],
        display=displays.append,
        reasoner=reasoner,
        host_id="builder-native",
    )
    delegation = host.start_investigation(
        "investigate",
        purpose="Prepare exact synthetic proposal changes.",
        subject_ids=["me"],
    )
    raws = [
        _record(
            identity,
            source=source,
            read_object=publisher.store.read_object,
            origin_run_id=delegation["run"]["id"],
        )
        for identity in ([ID_A, ID_B] if group_count == 2 else [ID_A])
    ]
    brief = "Adopt the exact reviewed synthetic changes."
    groups = [
        _group(
            "g1" if index == 0 else "g2",
            brief,
            path=f"entities/insights/{identity}.md",
            raw=raw,
        )
        for index, (identity, raw) in enumerate(zip([ID_A, ID_B], raws, strict=True))
    ]
    packet, objects = build_proposal(
        publisher.store,
        run_id=delegation["run"]["id"],
        brief=brief,
        groups=groups,
    )
    staged = host.stage(delegation, packet, objects)
    return publisher, owner, host, delegation, staged, objects, displays, events, reasoner


def test_builder_to_native_display_selected_publish_and_retained_readback(tmp_path: Path) -> None:
    publisher, owner, host, delegation, staged, objects, displays, events, reasoner = _native_fixture(
        tmp_path
    )
    assert staged["semantic_review"]["status"] == "passed"
    assert reasoner.reviews[0]["groups"]

    shown = host.show_proposal(staged["id"], staged["version"])
    assert displays == [staged["brief"]]
    events["reply-first"]["text"] = "approve first"
    first = host.reply("reply-first", display_id=shown["display_id"])
    first_revision = first["receipt"]["knowledge_revision"]
    assert publisher.store.read_record(ID_A, first_revision)["availability"] == "accepted"
    assert ID_B not in publisher.store.manifest(first_revision)["records"]

    retry = host.reply("reply-first", display_id=shown["display_id"])
    assert retry["status"] == "committed"
    assert retry["receipt"] == first["receipt"]

    events["reply-second"]["text"] = "approve second"
    shown_second = host.show_proposal(staged["id"], staged["version"])
    second = host.reply("reply-second", display_id=shown_second["display_id"])
    assert second["receipt"]["knowledge_revision"] != first_revision
    assert publisher.store.read_record(ID_B, second["receipt"]["knowledge_revision"])["availability"] == "accepted"

    alias_a = decode_record(objects[staged["groups"][0]["operations"][0]["after_hash"]])["owner_review"]["receipt_id"]
    alias_b = decode_record(objects[staged["groups"][1]["operations"][0]["after_hash"]])["owner_review"]["receipt_id"]
    assert alias_a != alias_b
    assert Publisher(publisher.store.vault).review_receipt(alias_a)["operation_id"] == first["receipt"]["operation_id"]
    assert Publisher(publisher.store.vault).review_receipt(alias_b)["operation_id"] == second["receipt"]["operation_id"]


@pytest.mark.parametrize("reply_ref", ["reply-first", "reply-all", "reply-both"])
def test_native_owner_selection_accepts_only_clear_first_all_or_both(
    tmp_path: Path, reply_ref: str
) -> None:
    publisher, _owner, host, _delegation, staged, _objects, displays, _events, _reasoner = _native_fixture(
        tmp_path
    )
    shown = host.show_proposal(staged["id"], staged["version"])
    result = host.reply(reply_ref, display_id=shown["display_id"])
    expected = ["g1"] if reply_ref == "reply-first" else ["g1", "g2"]
    assert result["receipt"]["selected_group_ids"] == expected
    assert displays == [staged["brief"]]


def test_builder_rejects_prerequisite_omission_and_dependency_cycles(tmp_path: Path) -> None:
    publisher, owner, _ = _bootstrap(tmp_path)
    source = _source(publisher, owner, origin="dependency-source")
    run_capability, run_id = _run(publisher, owner)
    brief = "Adopt the exact reviewed synthetic changes."
    first = _record(ID_A, source=source, read_object=publisher.store.read_object, origin_run_id=run_id)
    second = _record(ID_B, source=source, read_object=publisher.store.read_object, origin_run_id=run_id)

    dependent_packet, dependent_objects = build_proposal(
        publisher.store,
        run_id=run_id,
        brief=brief,
        groups=[
            _group("g1", brief, path=f"entities/insights/{ID_A}.md", raw=first),
            _group(
                "g2",
                brief,
                path=f"entities/insights/{ID_B}.md",
                raw=second,
                requires=["g1"],
            ),
        ],
    )
    staged = _stage(publisher, run_capability, dependent_packet, dependent_objects, _Reasoner())
    approval = owner.approve_displayed(
        staged,
        ["g2"],
        displayed_brief=staged["brief"],
        owner_message_ref="dependent-only-owner-reply",
    )
    before = publisher.store.head()
    with pytest.raises(V2Error, match="prerequisite"):
        publisher.publish(
            approval,
            staged["id"],
            staged["version"],
            ["g2"],
            operation_id=generate_ulid(),
            request_id=generate_ulid(),
        )
    assert publisher.store.head() == before

    with pytest.raises(V2Error, match="cycle"):
        build_proposal(
            publisher.store,
            run_id=run_id,
            brief=brief,
            groups=[
                _group("g1", brief, path=f"entities/insights/{ID_A}.md", raw=first, requires=["g2"]),
                _group("g2", brief, path=f"entities/insights/{ID_B}.md", raw=second, requires=["g1"]),
            ],
        )


def test_selected_group_is_not_blocked_by_independent_other_group_drift(tmp_path: Path) -> None:
    other_raw = _legacy_raw(ID_OTHER, "Other")
    publisher, owner, _ = _bootstrap(
        tmp_path,
        extra_records={"entities/people/other.md": other_raw},
    )
    source = _source(publisher, owner, origin="selected-read-set-source")
    run_capability, run_id = _run(publisher, owner)
    brief = "Adopt the exact reviewed synthetic changes."
    first = _record(ID_A, source=source, read_object=publisher.store.read_object, origin_run_id=run_id)
    second = _record(ID_OTHER, source=source, read_object=publisher.store.read_object, origin_run_id=run_id)
    packet, objects = build_proposal(
        publisher.store,
        run_id=run_id,
        brief=brief,
        groups=[
            _group("g1", brief, path=f"entities/insights/{ID_A}.md", raw=first),
            _group(
                "g2",
                brief,
                path="entities/people/other.md",
                raw=second,
                target_id=ID_OTHER,
            ),
        ],
    )
    staged = _stage(publisher, run_capability, packet, objects, _Reasoner())
    assert staged["read_set"] == []  # only genuinely common conditions belong here
    assert {ref["id"] for group in staged["groups"] for ref in group["read_set"]} == {"me", ID_OTHER}
    approval = owner.approve_displayed(
        staged,
        ["g1"],
        displayed_brief=staged["brief"],
        owner_message_ref="selected-group-owner-reply",
    )

    drifted = other_raw.replace(b"\nOther\n", b"\nOther changed independently.\n")
    assert drifted != other_raw
    drift_row = record_descriptor(drifted, path="entities/people/other.md")
    publisher.store.transact(
        operation_id="01ARZ3NDEKTSV4RRFFQ69G5GH0",
        request_id="01ARZ3NDEKTSV4RRFFQ69G5GH1",
        kind="capture",
        payload_hash=hash_bytes(b"independent other-group drift"),
        objects={drift_row["version"]: drifted},
        mutate=lambda manifest, _read: manifest["records"].update({ID_OTHER: drift_row}),
    )

    result = publisher.publish(
        approval,
        staged["id"],
        staged["version"],
        ["g1"],
        operation_id="01ARZ3NDEKTSV4RRFFQ69G5GH2",
        request_id="01ARZ3NDEKTSV4RRFFQ69G5GH3",
    )
    assert publisher.store.read_record(ID_A, result["receipt"]["knowledge_revision"])["availability"] == "accepted"
    assert publisher.store.manifest()["records"][ID_OTHER]["version"] == drift_row["version"]


def _legacy_raw(
    identity: str,
    name: str,
    *,
    archived: bool = False,
    merged_into: str | None = None,
    relations: list[dict] | None = None,
    review_status: str = "proposed",
) -> bytes:
    metadata = {
        "id": identity,
        "type": "person",
        "name": name,
        "review_status": review_status,
        "aliases": [],
        "relations": relations or [],
        "properties": {},
    }
    if archived:
        metadata["archived"] = True
    if merged_into:
        metadata["merged_into"] = merged_into
    rendered = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{rendered}\n---\n\n{name}\n".encode()


def _merge_fixture(tmp_path: Path):
    survivor = _legacy_raw(ID_SURVIVOR, "Survivor")
    absorbed = _legacy_raw(ID_ABSORBED, "Absorbed")
    reference = _legacy_raw(
        ID_REFERENCE,
        "Reference",
        relations=[{"type": "knows", "target_id": ID_ABSORBED}],
    )
    publisher, owner, _ = _bootstrap(
        tmp_path,
        extra_records={
            f"entities/people/{ID_SURVIVOR}.md": survivor,
            f"entities/people/{ID_ABSORBED}.md": absorbed,
            f"entities/people/{ID_REFERENCE}.md": reference,
        },
    )
    publisher.store.refresh_checkout()
    rows = publisher.store.manifest()["records"]
    return publisher, owner, survivor, absorbed, reference, rows


def test_typed_identity_merge_rewrites_active_references_and_preserves_redirect(
    tmp_path: Path,
) -> None:
    publisher, owner, survivor, absorbed, reference, rows = _merge_fixture(tmp_path)
    run_capability, run_id = _run(publisher, owner)
    brief = "Merge the duplicate identities and rewrite every active reference."
    survivor_after = _legacy_raw(ID_SURVIVOR, "Survivor Unified")
    absorbed_after = _legacy_raw(
        ID_ABSORBED,
        "Absorbed",
        archived=True,
        merged_into=ID_SURVIVOR,
    )
    reference_after = _legacy_raw(
        ID_REFERENCE,
        "Reference",
        relations=[{"type": "knows", "target_id": ID_SURVIVOR}],
    )
    merge = {
        "survivor": {"id": ID_SURVIVOR, "version": rows[ID_SURVIVOR]["version"]},
        "absorbed": [{"id": ID_ABSORBED, "version": rows[ID_ABSORBED]["version"]}],
        "rewritten_references": [{"id": ID_REFERENCE, "version": rows[ID_REFERENCE]["version"]}],
        "redirects": [{"from_id": ID_ABSORBED, "to_id": ID_SURVIVOR}],
    }
    changes = [
        {
            "kind": "replace-record",
            "path": f"entities/people/{ID_SURVIVOR}.md",
            "target_id": ID_SURVIVOR,
            "raw": survivor_after,
        },
        {
            "kind": "withdraw-record",
            "path": f"entities/people/{ID_ABSORBED}.md",
            "target_id": ID_ABSORBED,
            "raw": absorbed_after,
        },
        {
            "kind": "replace-record",
            "path": f"entities/people/{ID_REFERENCE}.md",
            "target_id": ID_REFERENCE,
            "raw": reference_after,
        },
    ]

    packet, _objects = build_proposal(
        publisher.store,
        run_id=run_id,
        brief=brief,
        groups=[_multi_change_group("merge", brief, changes, identity_merge=merge)],
    )
    staged = _stage(publisher, run_capability, packet, _objects, _Reasoner())
    approval = owner.approve_displayed(
        staged,
        ["merge"],
        displayed_brief=brief,
        owner_message_ref="merge-owner-reply",
    )
    result = publisher.publish(
        approval,
        staged["id"],
        staged["version"],
        ["merge"],
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )
    manifest = publisher.store.manifest(result["receipt"]["knowledge_revision"])
    assert manifest["records"][ID_ABSORBED]["active"] is False
    assert manifest["records"][ID_ABSORBED]["merged_into"] == ID_SURVIVOR
    assert f"target_id: {ID_SURVIVOR}" in publisher.store.read_object(
        manifest["records"][ID_REFERENCE]["version"]
    ).decode()


def test_identity_merge_rejects_false_redirect_without_side_effect(tmp_path: Path) -> None:
    publisher, owner, _survivor, _absorbed, _reference, rows = _merge_fixture(tmp_path)
    _run(publisher, owner)
    brief = "Merge the duplicate identities and rewrite every active reference."
    merge = {
        "survivor": {"id": ID_SURVIVOR, "version": rows[ID_SURVIVOR]["version"]},
        "absorbed": [{"id": ID_ABSORBED, "version": rows[ID_ABSORBED]["version"]}],
        "rewritten_references": [],
        "redirects": [{"from_id": ID_ABSORBED, "to_id": "me"}],
    }
    changes = [
        {
            "kind": "replace-record",
            "path": f"entities/people/{ID_SURVIVOR}.md",
            "target_id": ID_SURVIVOR,
            "raw": _legacy_raw(ID_SURVIVOR, "Survivor Unified"),
        },
        {
            "kind": "withdraw-record",
            "path": f"entities/people/{ID_ABSORBED}.md",
            "target_id": ID_ABSORBED,
            "raw": _legacy_raw(ID_ABSORBED, "Absorbed", archived=True, merged_into=ID_SURVIVOR),
        },
    ]
    before = publisher.store.head()
    with pytest.raises(V2Error, match="redirects"):
        build_proposal(
            publisher.store,
            run_id=generate_ulid(),
            brief=brief,
            groups=[_multi_change_group("merge", brief, changes, identity_merge=merge)],
        )
    assert publisher.store.head() == before


@pytest.mark.parametrize("target_kind", ["verified-after", "verified-target"])
def test_builder_cannot_demote_or_false_inherit_verified_records(
    tmp_path: Path, target_kind: str
) -> None:
    if target_kind == "verified-after":
        publisher, _owner, _ = _bootstrap(tmp_path)
        before_raw = publisher.store.read_object(publisher.store.manifest()["records"]["me"]["version"])
        after_raw = before_raw.replace(b"review_status: proposed", b"review_status: verified")
        target_id = "me"
        path = "entities/people/me.md"
    else:
        verified = _legacy_raw(ID_VERIFIED, "Verified", review_status="verified")
        publisher, _owner, _ = _bootstrap(
            tmp_path,
            extra_records={f"entities/people/{ID_VERIFIED}.md": verified},
        )
        before_raw = verified
        after_raw = _legacy_raw(ID_VERIFIED, "Verified changed")
        target_id = ID_VERIFIED
        path = f"entities/people/{ID_VERIFIED}.md"
    before = publisher.store.head()
    group = _group("g1", "Adopt the exact reviewed change.", path=path, raw=after_raw, target_id=target_id)
    with pytest.raises(V2Error, match="verified"):
        build_proposal(
            publisher.store,
            run_id=generate_ulid(),
            brief="Adopt the exact reviewed change.",
            groups=[group],
        )
    assert publisher.store.manifest()["records"][target_id]["version"] == hash_bytes(before_raw)
    assert publisher.store.head() == before


def _vault_snapshot(vault: Path) -> dict[str, bytes]:
    selected: dict[str, bytes] = {}
    for top in ("_synapse", "entities", "inbox", "proposals"):
        root = vault / top
        if not root.exists():
            continue
        for path in sorted(path for path in root.rglob("*") if path.is_file()):
            selected[path.relative_to(vault).as_posix()] = path.read_bytes()
    return selected


def test_prepare_import_executes_linkedin_candidate_without_mutating_retained_vault(
    tmp_path: Path,
) -> None:
    tombstone = _legacy_raw("01ARZ3NDEKTSV4RRFFQ69G5FH", "Archived Person", archived=True)
    publisher, _owner, _ = _bootstrap(
        tmp_path,
        extra_records={"entities/people/archived-person.md": tombstone},
    )
    before = _vault_snapshot(publisher.store.vault)
    source_bytes = FIXTURE_LINKEDIN.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    result = prepare_import(
        publisher.store.vault,
        FIXTURE_LINKEDIN,
        importer="import_linkedin_connections",
    )
    assert result["source_descriptor"]["original_hash"] == source_hash
    assert result["source_objects"][source_hash] == source_bytes
    assert result["summary"]["created"] >= 1
    assert all(change["kind"] == "create-record" for change in result["changes"])
    assert not any(change.get("target_id") == "01ARZ3NDEKTSV4RRFFQ69G5FH" for change in result["changes"])
    assert _vault_snapshot(publisher.store.vault) == before
    assert publisher.store.manifest()["records"]["01ARZ3NDEKTSV4RRFFQ69G5FH"]["active"] is False


def test_prepare_import_executes_whatsapp_candidate_and_preserves_original_bytes(
    tmp_path: Path,
) -> None:
    publisher, _owner, _ = _bootstrap(tmp_path)
    before = _vault_snapshot(publisher.store.vault)
    source_bytes = FIXTURE_WHATSAPP.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    result = prepare_import(
        publisher.store.vault,
        FIXTURE_WHATSAPP,
        importer="import_whatsapp_chat",
        options={"chat_key": "synthetic-chat", "date_order": "day-first", "min_messages": 2},
    )
    assert result["source_descriptor"]["original_hash"] == source_hash
    assert result["source_objects"][source_hash] == source_bytes
    assert result["summary"]["created"] >= 1
    assert result["summary"]["replaced"] == 0
    assert _vault_snapshot(publisher.store.vault) == before
    assert publisher.store.manifest()["records"].keys() == {"me"}


def test_prepare_import_rejects_unknown_importer_unsafe_options_and_deletion_requests(
    tmp_path: Path,
) -> None:
    publisher, _owner, _ = _bootstrap(tmp_path)
    before = _vault_snapshot(publisher.store.vault)
    cases = [
        ("unknown-importer", {}, "not an approved"),
        ("import_linkedin_connections", {"trust": "verified"}, "candidate options"),
        ("import_whatsapp_chat", {"chat_key": "synthetic-chat", "delete_source": True}, "WhatsApp"),
    ]
    for importer, options, message in cases:
        with pytest.raises(V2Error, match=message):
            prepare_import(
                publisher.store.vault,
                FIXTURE_LINKEDIN,
                importer=importer,
                options=options,
            )
    assert _vault_snapshot(publisher.store.vault) == before
