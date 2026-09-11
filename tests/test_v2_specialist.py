# ruff: noqa: F811
import json

import pytest
from test_v2_gateway import corpus  # noqa: F401

from synapse.owner_host import OwnerHost
from synapse.runs import RunManager
from synapse.specialist import Specialist, request_for
from synapse.v2_contracts import V2Error, validate_payload


class ScriptedReasoner:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.seen = []

    def step(self, context, **_kwargs):
        self.seen.append(context)
        return next(self.steps)


def read(method, arguments):
    return {"action": "read", "calls": [{"method": method, "arguments_json": json.dumps(arguments)}], "answer": "", "used_record_ids": [], "alternatives": [], "uncertainties": [], "findings_json": "[]", "stop_reason": ""}


def finish(ids=None, answer="No sound new connection is established."):
    return {"action": "finish", "calls": [], "answer": answer, "used_record_ids": ids or [], "alternatives": [], "uncertainties": ["The available evidence does not establish a stable preference."], "findings_json": "[]", "stop_reason": "Available evidence assessed."}


def test_adaptive_miss_reformulation_then_full_source_read(corpus):
    vault, store, add = corpus
    finding = add(statement="A practical workshop was useful once.")
    source_id = next(iter(store.manifest()["sources"]))
    reasoner = ScriptedReasoner([read("context", {"query": "dailyjournaling"}), read("context", {"query": "workshop"}), read("source", {"source_id": source_id}), finish([finding["id"]], "A workshop is a possibility; the evidence is one episode.")])
    before = sorted(str(path.relative_to(vault)) for path in (vault / "_synapse").rglob("*"))
    result = Specialist(vault, reasoner).run(request_for("What learning approach might suit me?"))
    validate_payload("result", result)
    assert result["status"] == "completed"
    assert reasoner.seen[1]["observations"][0]["response"]["total_candidates"] == 0
    assert [entry["method"] for entry in reasoner.seen[-1]["observations"]] == ["context", "context", "source"]
    assert result["records"][0]["owner_review"]["status"] == "not-reviewed"
    after = sorted(str(path.relative_to(vault)) for path in (vault / "_synapse").rglob("*"))
    assert before == after  # ordinary consultation retains no run/conversation


def test_direct_read_can_finish_without_mandatory_extra_search(corpus):
    vault, _, _ = corpus
    reasoner = ScriptedReasoner([read("context", {"ids": ["me"]}), finish(["me"], "The saved owner history is available.")])
    result = Specialist(vault, reasoner).run(request_for("Show the saved owner history."))
    assert len(reasoner.seen) == 2
    assert result["records"][0]["id"] == "me"


def test_advertised_arguments_work_with_public_aliases_and_pinned_session(corpus):
    vault, store, _ = corpus
    reasoner = ScriptedReasoner([
        read("record", {"id": "me", "revision": store.head(), "limit": 500}),
        finish(["me"]),
    ])
    result = Specialist(vault, reasoner).run(request_for("Read the owner record."))
    assert result["status"] == "completed"
    assert reasoner.seen[0]["capabilities"]["argument_contracts"]["record"]["example"]["id"] == "me"
    assert "error" not in reasoner.seen[1]["observations"][0]["response"]


def test_retrieval_cannot_change_revision_and_unknown_args_are_actionable(corpus):
    vault, _, _ = corpus
    reasoner = ScriptedReasoner([
        read("record", {"id": "me", "revision": "f" * 64}),
        read("catalog", {"q": "not-the-argument-name"}),
        finish(),
    ])
    Specialist(vault, reasoner).run(request_for("Inspect available records."))
    observations = reasoner.seen[-1]["observations"]
    assert "pinned revision" in observations[0]["response"]["error"]["message"]
    assert "Unknown arguments for catalog: q" in observations[1]["response"]["error"]["message"]


def test_generated_approval_is_not_a_read_operation(corpus):
    vault, store, _ = corpus
    step = read("context", {})
    step["calls"][0]["method"] = "approve"
    old = store.head()
    result = Specialist(vault, ScriptedReasoner([step])).run(request_for("Find useful context."))
    assert result["status"] == "partial"
    assert store.head() == old


def test_agent_cannot_cite_undiscovered_record(corpus):
    vault, _, add = corpus
    finding = add()
    with pytest.raises(V2Error, match="did not discover"):
        Specialist(vault, ScriptedReasoner([finish([finding["id"]])])).run(request_for("Help think through a decision."))


def test_shared_budget_does_not_reset_when_method_changes(corpus):
    vault, _, _ = corpus
    steps = [read("catalog", {}) if index % 2 else read("context", {"query": "absent"}) for index in range(8)]
    steps.append(finish())
    reasoner = ScriptedReasoner(steps)
    result = Specialist(vault, reasoner).run(request_for("Consider this question."))
    assert result["status"] == "completed"
    assert reasoner.seen[-1]["remaining"]["operations"] == 0
    assert len(reasoner.seen[-1]["observations"]) == 8


def test_requested_no_change_run_finishes_without_queue(corpus):
    vault, store, _ = corpus
    from synapse.util import generate_ulid
    run_id = generate_ulid()
    request = request_for("Investigate whether these ideas support a connection.", mode="investigate", preset="focused", owner_instruction_ref="synthetic-owner-event")
    capability = OwnerHost(store).record_instruction("synthetic-owner-event", actions=["investigate", "admit", "stage"], scope={"run_id": run_id, "subject_ids": []})
    RunManager(vault).start(request, capability, run_id=run_id)
    original = store.head()
    result = Specialist(vault, ScriptedReasoner([read("catalog", {}), finish()])).run(request, capability=capability, run_id=run_id)
    assert result["status"] == "completed"
    assert not result["proposal_ids"] and not result["receipt_ids"]
    assert store.head() == original
    assert RunManager(vault).get(run_id)["status"] == "completed"
