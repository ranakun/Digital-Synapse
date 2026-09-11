from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.v2_workflow_rehearsal import (
    BRIEF,
    CORRECTION,
    NOTE,
    THREAD,
    SyntheticEvents,
    SyntheticReasoner,
    main,
    run_rehearsal,
)
from synapse.codex_events import CodexEvents, CodexSessionHost
from synapse.host_control import HostControl
from synapse.revisions import RevisionStore
from synapse.v2_contracts import V2Error


@pytest.fixture(autouse=True)
def no_live_host_or_model(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Synthetic rehearsal must not discover owner sessions or launch live Codex")

    monkeypatch.setattr("synapse.codex_host.CodexReasoner.__init__", forbidden)
    monkeypatch.setattr(CodexEvents, "for_thread", forbidden)


@pytest.fixture
def rehearsal(tmp_path):
    output = tmp_path / "synthetic-rehearsal"
    return output, run_rehearsal(output)


def test_composed_save_correction_investigation_and_selected_adoption(rehearsal):
    output, report = rehearsal
    assert report["synthetic"] and report["status"] == "passed"
    assert json.loads((output / "report.json").read_text()) == report
    checkpoints = report["checkpoints"]
    assert [item["step"] for item in checkpoints] == [
        "explicit-save", "fresh-attributed-read", "saved-correction-and-fresh-read",
        "requested-investigation-started", "unreviewed-suggestions-used",
        "exact-brief-displayed", "selected-adoption", "fresh-read-map-and-sources",
    ]
    revisions = [item["knowledge_revision"] for item in checkpoints]
    assert revisions[0] == revisions[1]
    assert revisions[1] != revisions[2] == revisions[3]
    assert revisions[3] != revisions[4] == revisions[5]
    assert revisions[5] != revisions[6] == revisions[7]
    assert [item["simulated_events"] for item in checkpoints] == [3, 3, 5, 6, 6, 7, 8, 8]
    assert "60 minutes" in report["first_read"]["answer"]
    assert "20 minutes" in report["correction_read"]["answer"]
    assert "assistant, not an owner preference" in report["correction_read"]["answer"]
    assert {page["text"] for page in report["source_pages"]} == {NOTE, CORRECTION}
    assert report["adoption_receipt"]["kind"] == "adoption"
    assert report["adoption_receipt"]["selected_group_ids"] == ["g1"]
    selected, unselected = report["investigation"]["suggestion_ids"]
    assert report["final_records"][unselected] == report["suggestions_before"][unselected]
    assert report["final_records"][selected]["owner_review"]["disposition"] == "adopted"
    for record in report["suggestion_use"]["records"]:
        assert record["availability"] == "suggestion"
        assert record["owner_review"] == {"status": "not-reviewed", "disposition": "none"}
    for record in report["final_read"]["records"]:
        assert record["review_status"] == "proposed"
        assert record["owner_position"] == "unreviewed"
        assert record["epistemic_basis"] == ["assistant-hypothesis"]
        assert {ref["source_id"] for ref in record["evidence"]} == {
            page["source_id"] for page in report["source_pages"]
        }
    store = RevisionStore(Path(report["vault"]))
    runs = [json.loads(path.read_text()) for path in (store.root / "runs").glob("*.json")]
    assert sorted(run["request"]["mode"] for run in runs) == ["investigate", "prepare", "prepare"]
    investigation = next(run for run in runs if run["request"]["mode"] == "investigate")
    assert investigation["request"]["owner_instruction_ref"] == "synthetic-investigate"
    assert len(store.manifest()["sources"]) == 2
    assert all(row["review_status"] == "proposed" for row in store.manifest()["records"].values())
    assert report["map"]["scene"]["knowledge_revision"] == revisions[-1]
    visible = report["map"]["material_nodes"]
    assert {selected, unselected} <= {node["ref"].get("record_id") for node in visible}
    assert {page["source_id"] for page in report["source_pages"]} <= {
        node["ref"].get("source_id") for node in visible
    }


def test_exact_display_and_later_simulated_reply_are_required(rehearsal):
    output, report = rehearsal
    path = output / "synthetic-events.jsonl"
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert entries[-2]["payload"]["content"][0]["text"] == BRIEF
    assert entries[-1]["payload"]["content"][0]["text"] == "approve first"
    assert entries[-2]["timestamp"] < entries[-1]["timestamp"]
    host = CodexSessionHost(
        Path(report["vault"]), CodexEvents([path]), reasoner=SyntheticReasoner(), thread_id=THREAD,
    )
    before = host.publisher.store.head()
    # Even the committed display cannot be replayed with a pre-display user event.
    with pytest.raises(V2Error, match="must follow"):
        host.reply("synthetic-save", display_id=report["display"]["display_id"])
    with pytest.raises(V2Error, match="could not resolve"):
        host.reply("invented-production-owner-approval", display_id=report["display"]["display_id"])
    log = SyntheticEvents(path)
    log.entries = entries
    log.append("synthetic-wrong-brief", "assistant", "SYNTHETIC different review text.")
    with pytest.raises(V2Error, match="exact brief"):
        host.bind_display(report["proposal"]["proposal_id"], report["proposal"]["version"], "synthetic-wrong-brief")
    with pytest.raises(V2Error, match="actual owner instruction"):
        HostControl(host).execute("capture-message", {"material_ref": "synthetic-note"})
    with pytest.raises(V2Error, match="actual owner instruction"):
        HostControl(host).execute("start", {"purpose": "Unrequested synthetic investigation"})
    assert host.publisher.store.head() == before


@pytest.mark.parametrize("kind", ["empty-directory", "file", "symlink", "dangling-symlink"])
def test_refuses_any_existing_output_without_writing(tmp_path, kind):
    target = tmp_path / "target"
    if kind == "empty-directory":
        target.mkdir()
    elif kind == "file":
        target.write_text("untouched")
    else:
        destination = tmp_path / "destination"
        if kind == "symlink":
            destination.mkdir()
        target.symlink_to(destination)
    with pytest.raises(ValueError, match="Refusing existing output"):
        run_rehearsal(target)
    if kind == "file":
        assert target.read_text() == "untouched"
    elif kind == "empty-directory":
        assert list(target.iterdir()) == []
    else:
        assert target.is_symlink()


@pytest.mark.parametrize("marker", ["_synapse", ".synapse", "entities"])
def test_refuses_nested_output_in_existing_vault_including_symlink_parent(tmp_path, marker):
    owner_vault = tmp_path / "owner-vault"
    (owner_vault / marker).mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(owner_vault, target_is_directory=True)
    with pytest.raises(ValueError, match="inside an existing vault"):
        run_rehearsal(alias / "new-rehearsal")
    assert sorted(path.name for path in owner_vault.iterdir()) == [marker]


def test_manual_cli_default_cleans_up_and_explicit_target_retains_report(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    assert main([]) == 0
    assert "temporary vault removed" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []
    output = tmp_path / "manual-synthetic"
    assert main(["--output", str(output)]) == 0
    text = capsys.readouterr().out
    assert "Simulated later reply: approve first" in text
    assert "no real owner comprehension or approval" in text
    assert (output / "report.json").exists()
    with pytest.raises(SystemExit) as error:
        main(["--output", str(output)])
    assert error.value.code == 2
