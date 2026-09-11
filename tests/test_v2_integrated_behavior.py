# ruff: noqa: F811
import copy

import pytest
from test_v2_gateway import corpus  # noqa: F401
from test_v2_specialist import ScriptedReasoner, finish, read

from synapse.gateway import Gateway, transport_size
from synapse.host_session import NativeHost
from synapse.knowledge import encode_record, record_descriptor
from synapse.proposal_builder import build_proposal
from synapse.suggestion_library import library
from synapse.util import generate_ulid
from synapse.v2_contracts import hash_bytes, validate_payload


def test_thousand_suggestions_do_not_create_review_debt_or_reopen_exact_dismissal(corpus):
    vault, store, add = corpus
    dismissed = add(disposition="dismissed", statement="A project may help learning.")
    template = copy.deepcopy(dismissed)
    template["owner_review"] = {"status": "not-reviewed", "disposition": "none"}
    objects, rows = {}, {}
    for index in range(1000):
        value = copy.deepcopy(template)
        value["id"] = generate_ulid()
        if index >= 900:
            value["statement"] = f"A different independent project question {index}."
        raw = encode_record(value)
        row = record_descriptor(raw, path=f"entities/insights/{value['id']}.md")
        objects[row["version"]], rows[row["id"]] = raw, row
    store.transact(operation_id=generate_ulid(), request_id=generate_ulid(), kind="suggestion-admission", payload_hash=hash_bytes(b"workload"), mutate=lambda manifest, _: manifest["records"].update(rows), objects=objects)
    before = store.head()
    view = Gateway(vault)
    candidates = view.view.candidates(availability="suggestion", limit=200)
    assert candidates["total"] == 100
    assert all("different independent" in item["record"]["statement"] for item in candidates["items"])
    variants = library(vault, budget_chars=32000)
    assert variants["review_required"] is False
    assert variants["total_groups"] == 1
    selected = candidates["items"][0]["id"]
    unit = view.context(ids=[selected], budget_chars=32000)["items"][0]
    assert unit["notices"][0]["prior_decision"]["disposition"] == "dismissed"
    assert not unit.get("withheld")  # a different possibility remains inspectable
    assert store.head() == before
    assert not (vault / "_synapse/runs").exists()


def test_declining_adoption_preserves_useful_retrieval(corpus):
    vault, _, add = corpus
    suggestion = add(disposition="declined-adoption")
    assert Gateway(vault).context(query="project")["items"][0]["root_id"] == suggestion["id"]
    assert library(vault)["total_groups"] == 1


@pytest.mark.parametrize("representation", ["json", "mcp"])
def test_complete_specialist_output_fits_minimum_budget_without_orphaned_claim(corpus, representation):
    from synapse.specialist import Specialist, request_for
    vault, store, _ = corpus
    source = next(iter(store.manifest()["sources"]))
    request = request_for("Read the original source")
    request["budget"]["max_result_characters"] = 1000
    result = Specialist(vault, ScriptedReasoner([read("source", {"source_id": source}), finish(answer="A" * 2000)])).run(request, representation=representation)
    validate_payload("result", result)
    assert transport_size(result, representation) <= 1000
    assert result["status"] == "partial"
    assert result["coverage"]["returned_records"] == 0
    assert "budget" in result["answer"]


def test_compensation_is_a_new_reviewed_change_preserving_later_unrelated_work(corpus):
    vault, store, add = corpus
    old = store.read_object(store.manifest()["records"]["me"]["version"])
    events = {"instruction": {"id": "instruction", "actor": "user", "text": "Prepare this update."}, "approval": {"id": "approval", "actor": "user", "text": "approve"}}
    class Reviewer:
        def review(self, comparison):
            return {"passed": True, "proposal_version": comparison["proposal_version"], "reason": "Explicit synthetic semantic comparison."}
    host = NativeHost(vault, event_reader=events.__getitem__, display=lambda _: None, reasoner=Reviewer())
    def adopt(raw, brief):
        delegation = host.start_investigation("instruction", purpose=brief)
        from synapse.runs import RunManager
        RunManager(vault).finish(delegation["run"]["id"], delegation["capability"], status="completed", stop_reason="Investigation complete; prepare its review brief.")
        row = store.manifest()["records"]["me"]
        group = {"id": "change", "requires": [], "read_set": [], "source_preconditions": [], "effects": [{"id": "effect", "kind": "correction", "meaning": brief, "brief_span_start": 0, "brief_span_end": len(brief)}], "changes": [{"kind": "replace-record", "path": row["path"], "target_id": "me", "before_version": row["version"], "raw": raw}]}
        packet, objects = build_proposal(store, run_id=delegation["run"]["id"], brief=brief, groups=[group])
        staged = host.stage(delegation, packet, objects)
        shown = host.show_proposal(staged["id"], staged["version"])
        return host.reply("approval", display_id=shown["display_id"])
    first = adopt(old.replace(b"Original accepted", b"Reviewed revised"), "Revise the owner history wording.")
    independent = add(statement="A separate project remains worth considering.")
    later = adopt(old, "Restore the earlier owner history wording, retaining the later project idea.")
    assert later["receipt"]["knowledge_revision"] != first["receipt"]["knowledge_revision"]
    assert store.read_object(store.manifest()["records"]["me"]["version"]) == old
    assert store.read_record(independent["id"])["statement"] == independent["statement"]
    assert "Reviewed revised" in Gateway(vault, revision=first["receipt"]["knowledge_revision"]).record("me")["text"]
