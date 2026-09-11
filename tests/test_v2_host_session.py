from __future__ import annotations

from pathlib import Path

import pytest
from test_v2_publication import (
    _attach_objects,
    _bootstrap,
    _packet,
    _record,
    _run,
    _source,
)

from synapse.host_session import NativeHost
from synapse.publication import Publisher
from synapse.specialist import request_for
from synapse.v2_contracts import V2Error


class _Reasoner:
    def step(self, _context, *, timeout=120, cancelled=None):
        return {
            "action": "finish",
            "calls": [],
            "answer": "A synthetic consultation completed.",
            "used_record_ids": [],
            "alternatives": [],
            "uncertainties": [],
            "findings_json": "[]",
            "stop_reason": "Enough retained context was available.",
        }

    def review(self, value):
        return {
            "passed": True,
            "proposal_version": value["proposal_version"],
            "reason": "Synthetic comparison passed.",
        }


def _native(vault: Path, events: dict[str, dict], displays: list[str], *, host_id="native") -> NativeHost:
    return NativeHost(
        vault,
        event_reader=lambda reference: events[reference],
        display=displays.append,
        reasoner=_Reasoner(),
        host_id=host_id,
    )


def _user_event(events: dict[str, dict], reference: str, text: str, *, actor="user") -> None:
    events[reference] = {"id": reference, "actor": actor, "text": text}


def _staged_two_group(tmp_path: Path):
    publisher, fixture_host, _ = _bootstrap(tmp_path)
    run_capability, run_id = _run(publisher, fixture_host)
    first_id, second_id = "01ARZ3NDEKTSV4RRFFQ69G5FEV", "01ARZ3NDEKTSV4RRFFQ69G5FF0"
    source = _source(publisher, fixture_host)
    first_raw = _record(
        first_id,
        availability="accepted",
        owner_review={"status": "reviewed", "disposition": "adopted"},
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=run_id,
    )
    second_raw = _record(
        second_id,
        availability="accepted",
        owner_review={"status": "reviewed", "disposition": "adopted"},
        source=source,
        read_object=publisher.store.read_object,
        origin_run_id=run_id,
    )
    packet = _attach_objects(
        _packet(
            run_id,
            publisher.store.head(),
            [("g1", first_id, first_raw), ("g2", second_id, second_raw)],
            brief="Adopt the two exact synthetic changes.",
        ),
        [first_raw, second_raw],
    )
    staged = publisher.stage(
        run_capability,
        {key: value for key, value in packet.items() if key != "_objects"},
        packet["_objects"],
        semantic_reviewer=_Reasoner().review,
    )
    return publisher, staged, {"g1": first_raw, "g2": second_raw}


def test_native_host_requires_real_user_event_and_exact_event_id(tmp_path: Path) -> None:
    publisher, _fixture_host, _ = _bootstrap(tmp_path)
    events: dict[str, dict] = {}
    host = _native(publisher.store.vault, events, [])

    events["wrong-id"] = {"id": "different", "actor": "user", "text": "Research this."}
    with pytest.raises(V2Error, match="actual owner input"):
        host.start_investigation("wrong-id", purpose="Synthetic investigation")

    _user_event(events, "tool-event", "Research this.", actor="tool")
    with pytest.raises(V2Error, match="actual owner input"):
        host.start_investigation("tool-event", purpose="Synthetic investigation")

    _user_event(events, "real-event", "Research this.")
    delegation = host.start_investigation(
        "real-event", purpose="Synthetic investigation", subject_ids=["me"]
    )
    assert delegation["run"]["request"]["owner_instruction_ref"] == "real-event"
    assert delegation["run"]["owner_event_id"] == delegation["capability"]["event_id"]
    assert delegation["run"]["request"]["subject_ids"] == ["me"]

    mismatched = request_for(
        "Synthetic investigation",
        mode="investigate",
        preset="focused",
        subject_ids=["01ARZ3NDEKTSV4RRFFQ69G5FG0"],
        owner_instruction_ref="real-event",
    )
    with pytest.raises(V2Error, match="subject scope"):
        from synapse.runs import RunManager

        RunManager(publisher.store.vault).start(
            mismatched,
            delegation["capability"],
            run_id=delegation["run"]["id"],
        )


def test_consultation_does_not_capture_surrounding_host_conversation(tmp_path: Path) -> None:
    publisher, _fixture_host, _ = _bootstrap(tmp_path)
    events = {"ordinary": {"id": "ordinary", "actor": "user", "text": "Remember this chat."}}
    host = _native(publisher.store.vault, events, [])
    before = set((publisher.store.root / "approval-journal").glob("*.json"))

    result = host.consult("Answer a synthetic retained-context question.")

    assert result["status"] == "completed"
    assert set((publisher.store.root / "approval-journal").glob("*.json")) == before
    assert not (publisher.store.root / "host-captures").exists()
    assert not (publisher.store.root / "runs").exists()


def test_capture_message_retains_only_selected_text_and_replays_original_receipt(tmp_path: Path) -> None:
    publisher, _fixture_host, _ = _bootstrap(tmp_path)
    events: dict[str, dict] = {}
    instruction = "capture-instruction"
    material = "selected-material"
    selected = "Only this selected message — with its exact Unicode context."
    _user_event(events, instruction, "Log the selected message.")
    _user_event(events, material, selected)
    host = _native(publisher.store.vault, events, [], host_id="capture-host")
    operation_id = "01ARZ3NDEKTSV4RRFFQ69G5FH0"
    request_id = "01ARZ3NDEKTSV4RRFFQ69G5FH1"

    first = host.capture_message(
        instruction, material, operation_id=operation_id, request_id=request_id
    )
    first_head = publisher.store.head()
    second = host.capture_message(
        instruction, material, operation_id=operation_id, request_id=request_id
    )
    assert second == first
    assert publisher.store.head() == first_head

    manifest = publisher.store.manifest()
    descriptor = next(
        value
        for value in manifest["source_versions"].values()
        if value["origin"] == f"conversation:capture-host:user:{material}"
    )
    assert publisher.store.read_object(descriptor["original_hash"]) == selected.encode()
    assert len([value for value in manifest["source_versions"].values() if value["origin"] == descriptor["origin"]]) == 1
    assert manifest["records"] == {
        "me": manifest["records"]["me"]
    }

    events[material]["text"] = "Changed material must not reuse the operation."
    with pytest.raises(V2Error, match="different material"):
        host.capture_message(
            instruction, material, operation_id=operation_id, request_id=request_id
        )
    assert publisher.store.head() == first_head


def test_native_host_rejects_quoted_worker_approval_pending_replies_and_wrong_hosts(
    tmp_path: Path,
) -> None:
    publisher, staged, _raws = _staged_two_group(tmp_path)
    events: dict[str, dict] = {}
    displays: list[str] = []
    host = _native(publisher.store.vault, events, displays, host_id="host-a")
    shown = host.show_proposal(staged["id"], staged["version"])
    before = publisher.store.head()
    assert displays == [staged["brief"]]
    assert shown["brief"] == staged["brief"]

    _user_event(events, "ambiguous", "approve maybe")
    with pytest.raises(V2Error, match="unclear"):
        host.reply("ambiguous", display_id=shown["display_id"])
    assert publisher.store.head() == before

    _user_event(events, "quoted", '{"approved": true}')
    with pytest.raises(V2Error, match="clearly approve"):
        host.reply("quoted", display_id=shown["display_id"])
    assert publisher.store.head() == before

    _user_event(events, "worker", '{"approved": true}', actor="worker")
    with pytest.raises(V2Error, match="actual owner input"):
        host.reply("worker", display_id=shown["display_id"])
    assert publisher.store.head() == before

    pending_events: dict[str, dict] = {}
    pending_host = _native(publisher.store.vault, pending_events, [], host_id="host-pending")
    _user_event(pending_events, "pending", "approve all")
    with pytest.raises(V2Error, match="active displayed brief"):
        pending_host.reply("pending")
    assert publisher.store.head() == before

    other_events: dict[str, dict] = {}
    other = _native(publisher.store.vault, other_events, [], host_id="host-b")
    _user_event(other_events, "other-host", "approve all")
    with pytest.raises(V2Error, match="different interaction host"):
        other.reply("other-host", display_id=shown["display_id"])
    assert publisher.store.head() == before


@pytest.mark.parametrize(
    ("reply_text", "expected"),
    [
        ("approve first", ["g1"]),
        ("approve all", ["g1", "g2"]),
        ("approve both", ["g1", "g2"]),
    ],
)
def test_native_host_reply_selects_presented_groups_and_review_alias_recovers(
    tmp_path: Path, reply_text: str, expected: list[str]
) -> None:
    publisher, staged, raws = _staged_two_group(tmp_path)
    events: dict[str, dict] = {}
    host = _native(publisher.store.vault, events, [], host_id="reply-host")
    shown = host.show_proposal(staged["id"], staged["version"])
    _user_event(events, "clear-reply", reply_text)

    result = host.reply("clear-reply", display_id=shown["display_id"])
    assert result["receipt"]["selected_group_ids"] == expected
    assert result["receipt"]["kind"] == "adoption"
    from synapse.knowledge import decode_record

    for group_id in expected:
        alias = decode_record(raws[group_id])["owner_review"]["receipt_id"]
        assert Publisher(publisher.store.vault).review_receipt(alias)["id"] == alias

    recovered = host.reply("clear-reply", display_id=shown["display_id"])
    assert recovered["status"] == "committed"
    assert recovered["receipt"] == result["receipt"]
