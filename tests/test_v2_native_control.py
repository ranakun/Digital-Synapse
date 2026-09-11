from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_v2_publication import _bootstrap, _record, _run, _source
from typer.testing import CliRunner

from synapse.codex_events import CodexEvents, CodexSessionHost
from synapse.host_control import HostControl
from synapse.proposal_builder import build_proposal
from synapse.v2_contracts import V2Error, hash_bytes
from synapse.v2_control_cli import app

THREAD_A = "11111111-1111-4111-8111-111111111111"
THREAD_B = "22222222-2222-4222-8222-222222222222"
ID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ID_B = "01ARZ3NDEKTSV4RRFFQ69G5FAW"


class _Reasoner:
    def step(self, _context, *, timeout=120, cancelled=None):
        return {
            "action": "finish",
            "calls": [],
            "answer": "Synthetic native-control run completed.",
            "used_record_ids": [],
            "alternatives": [],
            "uncertainties": [],
            "findings_json": "[]",
            "stop_reason": "The synthetic retained context was sufficient.",
        }

    def review(self, value):
        return {
            "passed": True,
            "proposal_version": value["proposal_version"],
            "reason": "The synthetic exact comparison passed.",
        }


def _jsonl_event(
    identity: str,
    role: str,
    text: str,
    timestamp: str,
    *,
    kinds: list[str] | None = None,
    content_type: str | None = None,
) -> dict:
    metadata = {"turn_id": f"turn-{identity}"}
    if kinds is not None:
        metadata["content_item_kinds"] = kinds
    payload = {
        "type": "message",
        "role": role,
        "content": [
            {
                "type": content_type or ("input_text" if role == "user" else "output_text"),
                "text": text,
            }
        ],
        "internal_chat_message_metadata_passthrough": metadata,
    }
    return {"timestamp": timestamp, "type": "response_item", "payload": payload | {"id": identity}}


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")


def _append_jsonl(path: Path, entry: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def _staged_proposal(tmp_path: Path, *, group_count: int = 2):
    publisher, owner, _ = _bootstrap(tmp_path)
    source = _source(publisher, owner, origin="native-control-source")
    run_capability, run_id = _run(publisher, owner)
    brief = "Adopt the exact synthetic changes shown to the owner."
    identities = [ID_A, ID_B][:group_count]
    raws = [
        _record(
            identity,
            source=source,
            read_object=publisher.store.read_object,
            origin_run_id=run_id,
        )
        for identity in identities
    ]
    groups = [
        {
            "id": f"g{index + 1}",
            "requires": [],
            "effects": [
                {
                    "id": f"effect-{index + 1}",
                    "kind": "mechanical",
                    "meaning": "Adopt the exact synthetic record bytes.",
                    "brief_span_start": 0,
                    "brief_span_end": len(brief),
                }
            ],
            "changes": [
                {
                    "kind": "create-record",
                    "path": f"entities/insights/{identity}.md",
                    "raw": raw,
                }
            ],
            "read_set": [],
            "source_preconditions": [],
        }
        for index, (identity, raw) in enumerate(zip(identities, raws, strict=True))
    ]
    packet, objects = build_proposal(
        publisher.store,
        run_id=run_id,
        brief=brief,
        groups=groups,
    )
    staged = publisher.stage(
        run_capability,
        packet,
        objects,
        semantic_reviewer=_Reasoner().review,
    )
    return publisher, staged, run_capability, run_id, brief


def test_codex_events_accept_only_exact_user_text_items(tmp_path: Path) -> None:
    path = tmp_path / "synthetic-session.jsonl"
    _write_jsonl(
        path,
        [
            _jsonl_event("owner", "user", "Owner instruction", "2026-09-11T10:00:00Z", kinds=["user.text"]),
            _jsonl_event("subagent", "user", "Synthetic subagent notification", "2026-09-11T10:00:01Z", kinds=["subagent.notification"]),
            _jsonl_event("environment", "user", "Synthetic environment context", "2026-09-11T10:00:02Z", kinds=["environment.context"]),
            _jsonl_event("duplicate-kind", "user", "Duplicate metadata must not grant", "2026-09-11T10:00:03Z", kinds=["user.text", "user.text"]),
            _jsonl_event("assistant", "assistant", "Assistant display", "2026-09-11T10:00:04Z"),
        ],
    )
    events = CodexEvents([path])
    assert events.read("owner")["actor"] == "user"
    assert events.read("assistant")["actor"] == "assistant"
    for rejected in ("subagent", "environment"):
        with pytest.raises(V2Error, match="No actual"):
            events.read(rejected)
    with pytest.raises(V2Error, match="No actual"):
        events.read("duplicate-kind")


def test_codex_session_binds_exact_assistant_brief_and_later_owner_reply(
    tmp_path: Path,
) -> None:
    publisher, staged, _capability, _run_id, brief = _staged_proposal(tmp_path)
    session = tmp_path / "session.jsonl"
    _write_jsonl(
        session,
        [
            _jsonl_event("earlier", "user", "approve first", "2026-09-11T09:59:00Z", kinds=["user.text"]),
            _jsonl_event("wrong-assistant", "assistant", "A different brief.", "2026-09-11T10:00:00Z"),
        ],
    )
    events = CodexEvents([session])
    host = CodexSessionHost(
        publisher.store.vault,
        events,
        reasoner=_Reasoner(),
        thread_id=THREAD_A,
    )
    with pytest.raises(V2Error, match="exact brief"):
        host.bind_display(staged["id"], staged["version"], "wrong-assistant")

    _append_jsonl(
        session,
        _jsonl_event(
            "assistant-brief",
            "assistant",
            f"I reviewed this proposal:\n{brief}",
            "2026-09-11T10:01:00Z",
        ),
    )
    shown = host.bind_display(staged["id"], staged["version"], "assistant-brief")
    assert shown["brief"] == brief

    with pytest.raises(V2Error, match="must follow"):
        host.reply("earlier", display_id=shown["display_id"])
    with pytest.raises(V2Error, match="could not resolve"):
        host.reply("invented-owner-ref", display_id=shown["display_id"])

    _append_jsonl(
        session,
        _jsonl_event(
            "reply",
            "user",
            "approve first",
            "2026-09-11T10:02:00Z",
            kinds=["user.text"],
        ),
    )
    result = host.reply("reply", display_id=shown["display_id"])
    assert result["receipt"]["selected_group_ids"] == ["g1"]
    assert publisher.store.read_record(ID_A, result["receipt"]["knowledge_revision"])["availability"] == "accepted"
    assert ID_B not in publisher.store.manifest()["records"]


def test_native_control_capture_is_explicit_and_consult_is_transient(tmp_path: Path) -> None:
    publisher, _owner, _ = _bootstrap(tmp_path)
    session = tmp_path / "capture-session.jsonl"
    selected = "Only the selected owner message is retained — exact bytes."
    _write_jsonl(
        session,
        [
            _jsonl_event("instruction", "user", "Capture the selected message.", "2026-09-11T11:00:00Z", kinds=["user.text"]),
            _jsonl_event("material", "user", selected, "2026-09-11T11:00:01Z", kinds=["user.text"]),
            _jsonl_event("ordinary", "user", "An ordinary question.", "2026-09-11T11:00:02Z", kinds=["user.text"]),
        ],
    )
    events = CodexEvents([session])
    host = CodexSessionHost(publisher.store.vault, events, reasoner=_Reasoner(), thread_id=THREAD_A)
    before_journal = set((publisher.store.root / "approval-journal").glob("*.json"))
    consult = host.consult("Answer the ordinary synthetic question.")
    assert consult["status"] == "completed"
    assert set((publisher.store.root / "approval-journal").glob("*.json")) == before_journal
    assert not (publisher.store.root / "runs").exists()

    receipt = HostControl(host).execute(
        "capture-message",
        {"material_ref": "material"},
        owner_event_ref="instruction",
    )
    descriptor = next(iter(publisher.store.manifest()["source_versions"].values()))
    assert publisher.store.read_object(descriptor["original_hash"]) == selected.encode()
    assert descriptor["original_hash"] == hash_bytes(selected.encode())
    assert receipt["kind"] == "capture"
    assert publisher.store.manifest()["records"].keys() == {"me"}


def test_cli_native_start_execute_status_and_returned_run_are_secret_free(
    tmp_path: Path, monkeypatch
) -> None:
    publisher, _owner, _ = _bootstrap(tmp_path)
    events = {
        "owner": {"id": "owner", "actor": "user", "text": "Run the synthetic investigation."},
    }

    class _FakeEvents:
        def read(self, reference):
            return dict(events[reference])

    class _FakeReasoner(_Reasoner):
        def __init__(self, *, model=None):
            self.model = model

    monkeypatch.setattr("synapse.v2_control_cli.CodexEvents.for_thread", lambda _thread: _FakeEvents())
    monkeypatch.setattr("synapse.v2_control_cli.CodexReasoner", _FakeReasoner)
    runner = CliRunner()
    start = runner.invoke(
        app,
        [
            "native",
            "start",
            "--arguments-json",
            json.dumps({"purpose": "A bounded synthetic investigation.", "subject_ids": ["me"]}),
            "--owner-event",
            "owner",
            "--thread",
            THREAD_A,
            "--vault",
            str(publisher.store.vault),
        ],
    )
    assert start.exit_code == 0, start.output
    public = json.loads(start.output)
    run_id = public["id"]
    assert "token" not in public
    assert "capability" not in public

    status = runner.invoke(
        app,
        [
            "native",
            "status",
            "--arguments-json",
            json.dumps({"run_id": run_id}),
            "--thread",
            THREAD_A,
            "--vault",
            str(publisher.store.vault),
        ],
    )
    assert status.exit_code == 0, status.output
    status_public = json.loads(status.stdout)
    assert status_public["id"] == run_id
    assert "token" not in status_public
    assert "capability" not in status_public

    execute = runner.invoke(
        app,
        [
            "native",
            "execute-run",
            "--arguments-json",
            json.dumps({"run_id": run_id}),
            "--thread",
            THREAD_A,
            "--vault",
            str(publisher.store.vault),
        ],
    )
    assert execute.exit_code == 0, execute.output
    executed_public = json.loads(execute.stdout)
    assert executed_public["status"] in {"completed", "partial"}
    assert "token" not in executed_public
    assert "capability" not in executed_public


def test_cross_host_delegation_requires_explicit_owner_continue(tmp_path: Path) -> None:
    publisher, _owner, _ = _bootstrap(tmp_path)
    events_a = {
        "owner-a": {"id": "owner-a", "actor": "user", "text": "Start the run."},
    }
    events_b = {
        "owner-b": {"id": "owner-b", "actor": "user", "text": "Continue the run here."},
    }
    host_a = CodexSessionHost(
        publisher.store.vault,
        type("Events", (), {"read": lambda _self, ref: dict(events_a[ref])})(),
        reasoner=_Reasoner(),
        thread_id=THREAD_A,
    )
    host_b = CodexSessionHost(
        publisher.store.vault,
        type("Events", (), {"read": lambda _self, ref: dict(events_b[ref])})(),
        reasoner=_Reasoner(),
        thread_id=THREAD_B,
    )
    control_a = HostControl(host_a)
    started = control_a.execute(
        "start",
        {"purpose": "A bounded cross-host synthetic run.", "subject_ids": ["me"]},
        owner_event_ref="owner-a",
    )
    run_id = started["id"]
    with pytest.raises(V2Error, match="another host"):
        HostControl(host_b).execute("execute-run", {"run_id": run_id})

    control_a.execute("cancel", {"run_id": run_id}, owner_event_ref="owner-a")

    continued = HostControl(host_b).execute(
        "continue",
        {"run_id": run_id},
        owner_event_ref="owner-b",
    )
    assert continued["id"] == run_id
    executed = HostControl(host_b).execute("execute-run", {"run_id": run_id})
    assert executed["status"] in {"completed", "partial"}
