from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from test_v2_codex_host import _stub
from test_v2_publication import EXAMPLE, _bootstrap, _source

from synapse.codex_host import CodexReasoner
from synapse.knowledge import encode_record
from synapse.owner_host import OwnerHost
from synapse.preparation import Preparation, request_for_preparation
from synapse.publication import Publisher
from synapse.runs import RunManager
from synapse.source_store import evidence_ref, prepare_source
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes


class _Reasoner:
    def __init__(self) -> None:
        self.calls = 0
        self.states = []

    def prepare(self, state):
        self.calls += 1
        self.states.append(state)
        assert set(state) == {"sources", "existing_leads", "usage", "remaining_seconds"}
        evidence = state["sources"][0]["evidence"]
        return {
            "items": [
                {
                    "record_kind": "question",
                    "statement": "What would help develop this idea?",
                    "support": ["The retained note names an idea."],
                    "would_change_with": ["A later experiment."],
                    "evidence": [evidence],
                    "limits": ["This is a bounded question lead."],
                }
            ]
        }


def _request(tmp_path: Path, reasoner=None):
    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host, b"I tried a sketchbook to explore ideas.")
    request = request_for_preparation(
        [{"source_id": source["id"], "source_version": source["version"]}],
        "explicit-save",
        revision=publisher.store.head(),
    )
    run_id = generate_ulid()
    capability = host.record_instruction(
        "explicit-save",
        actions=["prepare"],
        scope={
            "run_id": run_id,
            "source_refs": request["source_refs"],
            "request_hash": hash_bytes(canonical_json(request)),
        },
    )
    RunManager(publisher.store.vault).start_preparation(request, capability, run_id=run_id)
    return publisher, capability, request, run_id, Preparation(publisher.store.vault, reasoner)


def _authorized_request(publisher: Publisher, host: OwnerHost, source: dict) -> tuple[dict, dict, str]:
    request = request_for_preparation(
        [{"source_id": source["id"], "source_version": source["version"]}],
        "explicit-save",
        revision=publisher.store.head(),
    )
    run_id = generate_ulid()
    capability = host.record_instruction(
        "explicit-save",
        actions=["prepare"],
        scope={
            "run_id": run_id,
            "source_refs": request["source_refs"],
            "request_hash": hash_bytes(canonical_json(request)),
        },
    )
    return request, capability, run_id


def _anchored_preparation_record(
    identity: str,
    source: dict,
    read_object,
    *,
    kind: str,
    statement: str,
    limits: str,
    would_change_with: list[str] | None = None,
    disposition: str = "none",
) -> bytes:
    payload = copy.deepcopy(EXAMPLE)
    payload.update(
        id=identity,
        subject_id=identity,
        claim_key=f"preparation.{kind}.{identity}",
        record_kind=kind,
        facets=["preparation"],
        statement=statement,
        conditions_and_limits=limits,
        support="A retained source supports this bounded preparation item.",
        would_change_with=would_change_with or ["Comparable future attempts."],
        owner_review={"status": "not-reviewed", "disposition": disposition},
        evidence=[
            evidence_ref(
                source,
                read_object,
                0,
                len(read_object(source["text_version"])),
            )
        ],
    )
    return encode_record(payload, name=f"Preparation {identity}")


def test_preparation_supplies_flat_source_pages_and_admits_question(tmp_path: Path) -> None:
    reasoner = _Reasoner()
    publisher, capability, request, run_id, preparation = _request(tmp_path, reasoner)

    result = preparation.run(request, capability=capability, run_id=run_id)

    assert result["status"] == "complete"
    assert len(result["lead_refs"]) == 1
    assert reasoner.calls == 1
    source = reasoner.states[0]["sources"][0]
    assert source["text"] == "I tried a sketchbook to explore ideas."
    assert source["evidence"]["source_version"] == request["source_refs"][0]["source_version"]
    assert publisher.store.read_record(result["lead_refs"][0]["id"])["record_kind"] == "question"


def test_absent_reasoner_preserves_capture_and_reports_partial(tmp_path: Path) -> None:
    publisher, capability, request, run_id, preparation = _request(tmp_path)

    result = preparation.run(request, capability=capability, run_id=run_id)

    assert result["status"] == "partial"
    assert result["lead_refs"] == []
    assert publisher.store.manifest()["sources"]


def test_invalid_generator_is_failed_without_publication(tmp_path: Path) -> None:
    publisher, capability, request, run_id, preparation = _request(tmp_path, object())

    result = preparation.run(request, capability=capability, run_id=run_id)

    assert result["status"] == "partial"
    assert "reasoner" in " ".join(result["limitations"])
    assert not publisher.store.manifest()["records"].keys() - {"me"}


def test_preparation_schema_rejection_preserves_capture_and_safe_diagnosis(tmp_path: Path, monkeypatch) -> None:
    script, _, _ = _stub(tmp_path)
    monkeypatch.setenv("SYNAPSE_TEST_STUB_MODE", "schema-message")
    reasoner = CodexReasoner(executable=str(script))
    publisher, capability, request, run_id, preparation = _request(tmp_path, reasoner)
    head = publisher.store.head()

    result = preparation.run(request, capability=capability, run_id=run_id)

    assert result["status"] == "failed"
    assert result["lead_refs"] == result["navigation_refs"] == []
    assert "invalid-request" in " ".join(result["limitations"])
    assert "invalid_json_schema" in " ".join(result["limitations"])
    assert "Update the Synapse adapter" in " ".join(result["limitations"])
    assert "synthetic-secret" not in json.dumps(result)
    assert publisher.store.head() == head
    assert publisher.store.manifest()["sources"]
    assert preparation.runs.get(run_id)["status"] == "failed"


@pytest.mark.parametrize("typed", [True, False])
def test_preparation_preserves_typed_message_but_not_raw_exception_or_details(tmp_path: Path, typed: bool) -> None:
    class FailingReasoner:
        def prepare(self, _state):
            if typed:
                raise V2Error("coverage-limited", "The model turn reached its deadline.", details={"raw": "synthetic-secret"})
            raise RuntimeError("synthetic-secret unexpected model output")

    publisher, capability, request, run_id, preparation = _request(tmp_path, FailingReasoner())
    head = publisher.store.head()

    result = preparation.run(request, capability=capability, run_id=run_id)

    assert result["status"] == "failed"
    assert result["limitations"] == [
        "Preparation reasoner failed (coverage-limited): The model turn reached its deadline."
        if typed else "Preparation reasoner failed (RuntimeError)."
    ]
    assert "synthetic-secret" not in json.dumps(result)
    assert publisher.store.head() == head


def test_matching_lead_after_many_unrelated_records_is_reused(tmp_path: Path) -> None:
    source, objects = prepare_source(
        b"I tried a sketchbook to explore ideas.",
        origin="late-match-source",
        source_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        captured_at="2026-09-11T08:00:00Z",
    )
    matching_id = "01ARZ3NDEKTSV4RRFFQ69GZZZZ"
    records = {}
    for index in range(300):
        identity = f"01ARZ3NDEKTSV4RRFFQ69G{index:04X}"
        records[f"entities/insights/{identity}.md"] = _anchored_preparation_record(
            identity,
            source,
            objects.__getitem__,
            kind="hypothesis",
            statement=f"Unrelated earlier record {index}.",
            limits="Unrelated to this preparation source.",
        )
    records[f"entities/insights/{matching_id}.md"] = _anchored_preparation_record(
        matching_id,
        source,
        objects.__getitem__,
        kind="question",
        statement="What would help develop this idea?",
        limits="This is a bounded question lead.",
        would_change_with=["A later experiment."],
    )
    publisher, host, _ = _bootstrap(
        tmp_path,
        extra_records=records,
        sources=[source],
        objects=objects,
    )
    request, capability, run_id = _authorized_request(publisher, host, source)
    reasoner = _Reasoner()

    result = Preparation(publisher.store.vault, reasoner).run(
        request,
        capability=capability,
        run_id=run_id,
    )

    assert result["status"] == "complete"
    assert result["lead_refs"] == [
        {
            "id": matching_id,
            "version": publisher.store.manifest()["records"][matching_id]["version"],
        }
    ]
    assert reasoner.calls == 1
    assert len(reasoner.states[0]["existing_leads"]) == 1


def test_dismissed_navigation_is_reused_after_normalized_retry(tmp_path: Path) -> None:
    class NavigationReasoner:
        def __init__(self) -> None:
            self.calls = 0

        def prepare(self, state):
            self.calls += 1
            evidence = state["sources"][0]["evidence"]
            return {
                "items": [
                    {
                        "record_kind": "navigation",
                        "statement": "Where should I look next?",
                        "support": ["The retained note leaves a useful follow-up path."],
                        "would_change_with": ["A later source."],
                        "evidence": [evidence],
                        "limits": ["Check the retained source family first."],
                    }
                ]
            }

    publisher, host, _ = _bootstrap(tmp_path)
    source = _source(publisher, host, b"A note leaves a useful follow-up path.")
    reasoner = NavigationReasoner()
    request, capability, run_id = _authorized_request(publisher, host, source)
    first = Preparation(publisher.store.vault, reasoner).run(
        request,
        capability=capability,
        run_id=run_id,
    )
    navigation = first["navigation_refs"][0]
    before = publisher.store.manifest()["records"][navigation["id"]]
    dismiss_capability = host.record_instruction(
        "dismiss-navigation",
        actions=["dispose"],
        scope={"disposition": "dismissed", "records": {navigation["id"]: before["version"]}},
    )
    publisher.disposition(
        dismiss_capability,
        [navigation["id"]],
        "dismissed",
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
    )

    retry_request, retry_capability, retry_run_id = _authorized_request(publisher, host, source)
    second = Preparation(publisher.store.vault, reasoner).run(
        retry_request,
        capability=retry_capability,
        run_id=retry_run_id,
    )

    assert second["status"] == "complete"
    assert second["navigation_refs"] == [
        {
            "id": navigation["id"],
            "version": publisher.store.manifest()["records"][navigation["id"]]["version"],
        }
    ]
    assert publisher.store.manifest()["records"][navigation["id"]]["disposition"] == "dismissed"
    assert len(publisher.store.manifest()["records"]) == 2
    assert reasoner.calls == 2
