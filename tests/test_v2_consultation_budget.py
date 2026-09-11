from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_v2_mcp import _call, _vault
from test_v2_specialist import ScriptedReasoner, finish, read

from synapse.consultation_budget import (
    PRESETS,
    ConsultationBudget,
    ConsultationSessions,
    preset_budget,
)
from synapse.revisions import RevisionStore
from synapse.source_purpose import write_source_purposes
from synapse.specialist import Specialist, request_for
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, hash_bytes, validate_payload
from synapse.v2_mcp import _result, make_server


def invoke(server, operation, **kwargs):
    result = _call(server, "synapse_v2_read", {"operation": operation, **kwargs})
    assert len(result.content) == 1
    assert result.structuredContent is None
    return json.loads(result.content[0].text)


def begin(server, preset="consult", **kwargs):
    return invoke(server, "begin_consultation", arguments={"preset": preset}, **kwargs)


@pytest.mark.parametrize("preset,limit", [("consult", 8), ("focused", 8), ("broad", 20)])
def test_failed_mixed_mcp_calls_share_preset_budget(tmp_path, preset, limit):
    vault, _ = _vault(tmp_path)
    server = make_server(vault)
    started = begin(server, preset)
    token = started["session_token"]
    assert len(token) == 43
    for index in range(limit):
        operation, args = ("context", {"query": "missing"}) if index % 2 else ("source", {})
        value = invoke(server, operation, arguments=args, session_token=token)
        assert value["usage"]["calls"] == index + 1
        assert value["usage"]["expansions"] == (index + 2) // 2
    denied = invoke(server, "record", arguments={"id": "me"}, session_token=token)
    assert denied["error"]["code"] == "coverage-limited"
    assert denied["usage"]["calls"] == limit
    ended = invoke(server, "end_consultation", session_token=token)
    assert ended["status"] == "ended"
    assert ended["usage"]["remaining"]["calls"] == 0
    assert invoke(server, "context", session_token=token)["error"]["code"] == "precondition-expired"


def test_discovery_advertises_sessions_and_stateless_reads_are_unmetered(tmp_path):
    vault, _ = _vault(tmp_path)
    server = make_server(vault)
    payload = json.loads(_call(server, "synapse_v2_describe", {}).content[0].text)
    contract = payload["consultation"]
    assert contract["presets"]["broad"]["max_operations"] == 20
    assert contract["presets"]["focused"]["max_operations"] == 8
    assert contract["begin"]["operation"] == "begin_consultation"
    assert payload["metering"] == "unmetered"
    assert invoke(server, "record", arguments={"id": "me"})["metering"] == "unmetered"
    assert set(tool.name for tool in server._tool_manager.list_tools()) == {
        "synapse_v2_describe", "synapse_v2_read",
    }


def test_schema_and_describe_are_free_even_after_exhaustion(tmp_path):
    vault, _ = _vault(tmp_path)
    server = make_server(vault)
    token = begin(server)["session_token"]
    for _ in range(8):
        invoke(server, "catalog", session_token=token)
    for operation, arguments in [("describe", {}), ("schema", {"kind": "error"}),
                                 ("schema", {"kind": "not-a-schema"})]:
        value = invoke(server, operation, arguments=arguments, session_token=token)
        assert value["usage"]["calls"] == 8
        if operation == "describe":
            assert "consultation" in value


def test_session_cannot_reset_or_gain_write_authority(tmp_path):
    vault, _ = _vault(tmp_path)
    server = make_server(vault)
    token = begin(server)["session_token"]
    attempted = invoke(server, "begin_consultation", arguments={"preset": "broad"}, session_token=token)
    assert attempted["error"]["code"] == "invalid-request"
    assert attempted["usage"]["calls"] == 1
    for operation in ("capture", "approve", "run-start", "research"):
        value = invoke(server, operation, session_token=token)
        assert value["error"]["code"] == "unsupported-operation"
    assert value["usage"]["calls"] == 5
    assert value["usage"]["expansions"] == 1
    assert not (vault / "_synapse/runs").exists()


@pytest.mark.parametrize("overrides", [
    {"revision": "f" * 64}, {"known_at": "2020-01-01T00:00:00Z"}, {"timezone": "Europe/Paris"},
    {"arguments": {"id": "me", "pinned_revision": "f" * 64}},
])
def test_wrong_revision_or_temporal_view_fails_before_any_dispatch(tmp_path, monkeypatch, overrides):
    vault, _ = _vault(tmp_path)
    server = make_server(vault)
    token = begin(server)["session_token"]
    def forbidden(*_args, **_kwargs):
        pytest.fail("A mismatched session must not read")
    monkeypatch.setattr("synapse.v2_mcp.dispatch", forbidden)
    value = invoke(server, "record", session_token=token, **overrides)
    assert value["error"]["code"] == "invalid-request"
    assert value["usage"]["calls"] == 1


def test_head_changes_do_not_change_the_session_revision(tmp_path):
    vault, _ = _vault(tmp_path)
    server = make_server(vault)
    started = begin(server)
    store = RevisionStore(vault)
    store.transact(operation_id=generate_ulid(), request_id=generate_ulid(), kind="capture",
                   payload_hash=hash_bytes(b"new synthetic revision"), mutate=lambda _manifest, _read: None,
                   objects={})
    assert store.head() != started["knowledge_revision"]
    value = invoke(server, "record", arguments={"id": "me"}, session_token=started["session_token"])
    assert value["knowledge_revision"] == started["knowledge_revision"]


def test_source_purpose_hash_is_pinned_and_drift_counts_without_reading(tmp_path, monkeypatch):
    vault, source = _vault(tmp_path)
    server = make_server(vault)
    started = begin(server)
    assert started["source_purpose_hash"] is None
    store = RevisionStore(vault)
    write_source_purposes(vault, revision=started["knowledge_revision"], classifications=[{
        "source_id": source, "source_version": store.manifest()["sources"][source],
        "purpose": "internal", "reason": "Synthetic classification for a session drift check.",
    }])
    assert store.head() == started["knowledge_revision"]
    def forbidden(*_args, **_kwargs):
        pytest.fail("Policy drift must reject before reading")
    monkeypatch.setattr("synapse.v2_mcp.dispatch", forbidden)
    value = invoke(server, "search_sources", arguments={"query": "retained"}, session_token=started["session_token"])
    assert value["error"]["code"] == "stale-selection"
    assert value["usage"]["calls"] == 1
    assert invoke(server, "end_consultation", session_token=started["session_token"])["status"] == "ended"


def test_concurrent_native_reads_cannot_overdraw_or_reset(tmp_path, monkeypatch):
    vault, _ = _vault(tmp_path)
    server = make_server(vault)
    token = begin(server)["session_token"]
    calls = []
    def fake_dispatch(*_args, **_kwargs):
        calls.append(1)
        return {"items": []}
    monkeypatch.setattr("synapse.v2_mcp.dispatch", fake_dispatch)
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: invoke(server, "catalog", session_token=token), range(32)))
    assert len(calls) == 8
    assert sum("error" not in item for item in results) == 8
    assert all(item["usage"]["calls"] <= 8 for item in results)
    assert invoke(server, "end_consultation", session_token=token)["usage"]["calls"] == 8


def test_budget_reservations_are_atomic_and_failed_calls_are_not_refunded():
    budget = ConsultationBudget("broad")
    def attempt(_index):
        try:
            budget.charge("passage")
            return True
        except V2Error:
            return False
    with ThreadPoolExecutor(max_workers=16) as pool:
        assert sum(pool.map(attempt, range(64))) == 20
    assert budget.receipt()["calls"] == budget.receipt()["expansions"] == 20


def test_fixed_expiry_capacity_and_end_do_not_retain_durable_state(tmp_path):
    now = [0]
    sessions = ConsultationSessions(capacity=1, clock=lambda: now[0])
    token, session = sessions.begin(tmp_path, "a" * 64)
    session.budget.charge("source")
    with pytest.raises(V2Error, match="capacity"):
        sessions.begin(tmp_path, "a" * 64)
    now[0] = 120
    with pytest.raises(V2Error, match="exhausted"):
        session.budget.charge("record")
    assert sessions.get(token) is session
    assert session.budget.receipt()["remaining"]["seconds"] == 0
    receipt = sessions.end(token, session)
    now[0] = 130
    assert session.budget.receipt() == receipt
    with pytest.raises(V2Error, match="ended"):
        session.budget.charge("describe")
    token, _ = sessions.begin(tmp_path, "a" * 64)
    now[0] += 420
    with pytest.raises(V2Error, match="expired"):
        sessions.get(token)
    sessions.begin(tmp_path, "a" * 64)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad", ["unknown", "", None, True, {}, []])
def test_preset_validation_rejects_unknown_or_malformed_values(bad):
    with pytest.raises(V2Error, match="preset"):
        ConsultationBudget(bad)


def test_selected_preset_limits_cannot_be_increased():
    limits = preset_budget("consult")
    limits["max_minutes"] = 20
    with pytest.raises(V2Error, match="selected bounded preset"):
        ConsultationBudget("consult", limits=limits)
    limits = preset_budget("consult")
    limits["max_source_expansions"] = True
    with pytest.raises(V2Error):
        ConsultationBudget("consult", limits=limits)


def test_research_counts_successful_and_failed_pages_and_rejects_overrun():
    budget = ConsultationBudget()
    budget.charge("research")
    budget.charge_research_pages({"pages": [{}, {}], "failures": [{}]})
    assert budget.receipt()["calls"] == 1
    assert budget.receipt()["expansions"] == 3
    budget.charge("research")
    with pytest.raises(V2Error, match="source limit"):
        budget.charge_research_pages({"pages": [{}] * 6})
    assert budget.receipt()["expansions"] == 8
    with pytest.raises(V2Error, match="expansion budget"):
        budget.charge("passage")
    budget.charge("record")
    assert budget.receipt()["calls"] == 4


@pytest.mark.parametrize("size", [256, 512, 1024, 4000, 8000, 32000])
def test_mcp_full_transport_bounds_include_receipt(tmp_path, size):
    vault, source = _vault(tmp_path)
    server = make_server(vault)
    token = begin(server)["session_token"]
    result = _call(server, "synapse_v2_read", {"operation": "source", "arguments": {"id": source},
                                              "session_token": token, "budget": size})
    assert len(result.model_dump_json(exclude_none=True)) <= size
    value = json.loads(result.content[0].text)
    assert value["usage"]["calls"] == 1
    assert value["usage"]["expansions"] == 1
    assert "elapsed_seconds" in value["usage"]
    assert "remaining" in value["usage"]


def test_tiny_receipt_fallback_does_not_silently_strip_usage():
    value = {"items": [{"text": '"😀\\' * 8000}],
             "usage": {"calls": 20, "expansions": 20, "elapsed_seconds": 1500,
                       "remaining": {"calls": 0, "expansions": 0, "seconds": 0}}}
    result = _result(value, budget=256)
    assert len(result.model_dump_json(exclude_none=True)) <= 256
    assert json.loads(result.content[0].text)["usage"]["calls"] == 20


@pytest.mark.parametrize("preset", list(PRESETS))
def test_hosted_and_native_share_limits_and_allow_final_synthesis(tmp_path, preset):
    vault, _ = _vault(tmp_path)
    steps = [read("source", {}) for _ in range(PRESETS[preset].max_operations)] + [finish()]
    reasoner = ScriptedReasoner(steps)
    progress = []
    specialist = Specialist(vault, reasoner, progress=progress.append)
    before = sorted(str(path.relative_to(vault)) for path in (vault / "_synapse").rglob("*"))
    result = specialist.run(request_for("Inspect synthetic evidence.", preset=preset))
    validate_payload("result", result)
    assert result["status"] == "completed"
    assert reasoner.seen[-1]["synthesis_only"]
    assert specialist.last_usage["calls"] == PRESETS[preset].max_operations
    assert specialist.last_usage["expansions"] == PRESETS[preset].max_source_expansions
    assert progress[-1]["usage"] == specialist.last_usage
    assert before == sorted(str(path.relative_to(vault)) for path in (vault / "_synapse").rglob("*"))


def test_hosted_invalid_json_costs_a_call_and_can_be_synthesized(tmp_path):
    vault, _ = _vault(tmp_path)
    step = read("passage", {})
    step["calls"][0]["arguments_json"] = "{broken"
    specialist = Specialist(vault, ScriptedReasoner([step, finish()]))
    assert specialist.run(request_for("Inspect synthetic evidence."))["status"] == "completed"
    assert specialist.last_usage["calls"] == specialist.last_usage["expansions"] == 1


def test_hosted_time_exhaustion_preserves_one_synthesis_turn(tmp_path, monkeypatch):
    vault, _ = _vault(tmp_path)
    now = [0]
    monkeypatch.setattr("synapse.consultation_budget.time.monotonic", lambda: now[0])
    class SlowReasoner(ScriptedReasoner):
        def step(self, context, **kwargs):
            now[0] = 121
            return super().step(context, **kwargs)
    reasoner = SlowReasoner([read("record", {"id": "me"}), finish()])
    specialist = Specialist(vault, reasoner)
    result = specialist.run(request_for("Inspect synthetic evidence."))
    assert result["status"] == "completed"
    assert reasoner.seen[-1]["synthesis_only"]
    assert specialist.last_usage["calls"] == 0
    assert specialist.last_usage["remaining"]["seconds"] == 0


@pytest.mark.parametrize("phase", ["before-read", "during-read", "before-finish", "during-assembly"])
def test_hosted_policy_drift_withholds_claims_and_requires_revalidation(tmp_path, monkeypatch, phase):
    import synapse.specialist as module

    vault, source = _vault(tmp_path)
    store = RevisionStore(vault)
    revision = store.head()

    def drift():
        write_source_purposes(vault, revision=revision, classifications=[{
            "source_id": source, "source_version": store.manifest()["sources"][source],
            "purpose": "internal", "reason": "Synthetic policy drift during hosted consultation.",
        }])

    original_dispatch = module.dispatch
    actual_reads = []

    def dispatch_and_drift(*args, **kwargs):
        actual_reads.append(1)
        result = original_dispatch(*args, **kwargs)
        if phase == "during-read":
            drift()
        return result

    monkeypatch.setattr(module, "dispatch", dispatch_and_drift)
    original_result = Specialist._result

    def assemble_and_drift(*args):
        result = original_result(*args)
        drift()
        return result

    if phase == "during-assembly":
        monkeypatch.setattr(Specialist, "_result", staticmethod(assemble_and_drift))

    class DriftingReasoner(ScriptedReasoner):
        def step(self, context, **kwargs):
            step = super().step(context, **kwargs)
            if phase == "before-read" and step["action"] == "read":
                drift()
            if phase == "before-finish" and step["action"] == "finish":
                drift()
            return step

    reasoner = DriftingReasoner([read("record", {"id": "me"}), finish(["me"], "Synthetic conclusion.")])
    specialist = Specialist(vault, reasoner)
    result = specialist.run(request_for("Inspect synthetic evidence."))
    validate_payload("result", result)
    assert result["status"] == "partial"
    assert "revalidate" in result["answer"]
    assert "Source purpose policy" in result["stop_reason"]
    assert result["records"] == result["evidence"] == []
    assert specialist.last_usage["calls"] == 1
    assert len(actual_reads) == (0 if phase == "before-read" else 1)
    assert reasoner.seen[0]["source_purpose_hash"] is None
    assert store.head() == revision
    assert not (vault / "_synapse/runs").exists()


@pytest.mark.parametrize("change_old_policy", [False, True])
def test_hosted_research_rebinds_inherited_policy_without_reclassifying_prior_sources(tmp_path, change_old_policy):
    from synapse.owner_host import OwnerHost
    from synapse.runs import RunManager
    from synapse.source_purpose import load_source_purposes
    from synapse.source_store import prepare_source

    vault, source = _vault(tmp_path)
    store = RevisionStore(vault)
    revision = store.head()
    classification = {"source_id": source, "source_version": store.manifest()["sources"][source],
                      "purpose": "internal", "reason": "Synthetic operational source."}
    pinned = write_source_purposes(vault, revision=revision, classifications=[classification])
    run_id = generate_ulid()
    request = request_for("Investigate a synthetic project.", mode="investigate", preset="focused",
                          owner_instruction_ref="synthetic-policy-research")
    capability = OwnerHost(store).record_instruction("synthetic-policy-research",
                                                    actions=["investigate", "admit", "stage"],
                                                    scope={"run_id": run_id, "subject_ids": []})
    RunManager(vault).start(request, capability, run_id=run_id)
    new_source = []

    def research(_arguments, **kwargs):
        assert kwargs["source_limit"] == 8
        descriptor, objects = prepare_source(b"New synthetic project evidence.", origin="synthetic-new.txt")
        new_source.append(descriptor["id"])
        store.transact(operation_id=generate_ulid(), request_id=generate_ulid(), kind="capture",
                       payload_hash=hash_bytes(b"synthetic research advance"),
                       mutate=lambda manifest, _read: (
                           manifest["sources"].update({descriptor["id"]: descriptor["version"]}),
                           manifest["source_versions"].update({descriptor["version"]: descriptor}),
                       ), objects=objects)
        if change_old_policy:
            write_source_purposes(vault, revision=store.head(), classifications=[
                {**classification, "purpose": "knowledge", "reason": "Synthetic unrequested reclassification."},
            ])
        return {"knowledge_revision": store.head(), "pages": [{"source": descriptor}], "failures": []}

    reasoner = ScriptedReasoner([read("research", {"query": "synthetic project"}),
                                read("catalog", {"kind": "sources"}), finish()])
    specialist = Specialist(vault, reasoner, research=research)
    result = specialist.run(request, capability=capability, run_id=run_id)
    validate_payload("result", result)
    if change_old_policy:
        assert result["status"] == "partial"
        assert "revalidate" in result["answer"]
        assert len(reasoner.seen) == 1
    else:
        assert result["status"] == "completed"
        rebound = load_source_purposes(vault, revision=store.head())
        assert rebound.inherited_from_revision == revision
        assert rebound.snapshot_hash != pinned.snapshot_hash
        assert reasoner.seen[1]["source_purpose_hash"] == rebound.snapshot_hash
        assert reasoner.seen[1]["capabilities"]["source_policy"]["inherited_from_revision"] == revision
        assert rebound.purpose(source, classification["source_version"]) == "internal"
        assert rebound.purpose(new_source[0], store.manifest()["sources"][new_source[0]]) == "unknown"
        assert specialist.last_usage["calls"] == 2
    assert specialist.last_usage["expansions"] == 1


@pytest.mark.parametrize("drift_stage", ["before-admission", "after-admission"])
@pytest.mark.parametrize("representation", ["json", "mcp"])
@pytest.mark.parametrize("output_budget", [1000, 8000])
def test_admission_policy_drift_preserves_actual_write_outcome(tmp_path, monkeypatch, drift_stage, representation, output_budget):
    from pathlib import Path

    import synapse.specialist as module
    from synapse.gateway import transport_size
    from synapse.owner_host import OwnerHost
    from synapse.publication import Publisher
    from synapse.runs import RunManager
    from synapse.source_store import evidence_ref

    vault, source = _vault(tmp_path)
    store = RevisionStore(vault)
    original_revision = store.head()
    descriptor = store.manifest()["source_versions"][store.manifest()["sources"][source]]
    run_id = generate_ulid()
    request = request_for("Inspect a synthetic finding.", mode="investigate", preset="focused",
                          owner_instruction_ref="synthetic-admission-policy")
    request["budget"]["max_result_characters"] = output_budget
    capability = OwnerHost(store).record_instruction("synthetic-admission-policy",
                                                    actions=["investigate", "admit", "stage"],
                                                    scope={"run_id": run_id, "subject_ids": []})
    manager = RunManager(vault)
    manager.start(request, capability, run_id=run_id)
    example = Path(__file__).parents[1] / "docs/v2/contracts/example-knowledge_record.json"
    finding = json.loads(example.read_text())["payload"]
    finding.update(id=generate_ulid(), origin_run_id=run_id,
                   evidence=[evidence_ref(descriptor, store.read_object, 0, 20)])
    final = finish(answer="Synthetic finding prepared.")
    final["findings_json"] = json.dumps([finding])
    receipts = []

    def drift():
        write_source_purposes(vault, revision=store.head(), classifications=[{
            "source_id": source, "source_version": descriptor["version"],
            "purpose": "internal", "reason": "Synthetic policy change at admission boundary.",
        }])

    original_admit = Publisher.admit

    def admit_then_drift(self, *args, **kwargs):
        receipt = original_admit(self, *args, **kwargs)
        receipts.append(receipt)
        if drift_stage == "after-admission":
            drift()
        return receipt

    monkeypatch.setattr(Publisher, "admit", admit_then_drift)
    original_encode = module.encode_record

    def encode_then_drift(*args, **kwargs):
        raw = original_encode(*args, **kwargs)
        if drift_stage == "before-admission":
            drift()
        return raw

    monkeypatch.setattr(module, "encode_record", encode_then_drift)
    progress = []
    result = Specialist(vault, ScriptedReasoner([final]), progress=progress.append).run(
        request, capability=capability, run_id=run_id, representation=representation,
    )
    validate_payload("result", result)
    assert transport_size(result, representation) <= output_budget
    assert result["status"] == "partial"
    assert result["records"] == result["evidence"] == []
    assert manager.get(run_id)["status"] == "partial"
    if drift_stage == "after-admission":
        receipt = receipts[0]
        assert store.head() != original_revision
        assert result["knowledge_revision"] == receipt["knowledge_revision"] == store.head()
        assert result["receipt_ids"] == [receipt["id"]]
        assert store.read_record(finding["id"])["origin_run_id"] == run_id
        assert "Findings were admitted" in result["answer"]
        assert "revalidation" in result["answer"]
        assert "not admitted" not in json.dumps(result)
        assert progress[-1]["knowledge_revision"] == receipt["knowledge_revision"]
    else:
        assert receipts == []
        assert store.head() == original_revision == result["knowledge_revision"]
        assert result["receipt_ids"] == []
        assert finding["id"] not in store.manifest()["records"]
