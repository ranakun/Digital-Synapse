from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_v2_preparation import _Reasoner, _request


def test_frozen_admission_retry_does_not_invoke_reasoner_again(tmp_path: Path, monkeypatch) -> None:
    reasoner = _Reasoner()
    publisher, capability, request, run_id, preparation = _request(tmp_path, reasoner)
    original = publisher.admit_preparation
    calls = 0

    def lose_response(*args, **kwargs):
        nonlocal calls
        calls += 1
        original(*args, **kwargs)
        raise RuntimeError("response lost after durable publication")

    monkeypatch.setattr(preparation.publisher, "admit_preparation", lose_response)
    first = preparation.run(request, capability=capability, run_id=run_id)
    assert first["status"] == "partial"
    assert first["recovery_pending"] is True
    assert first["lead_refs"] == []
    assert first["navigation_refs"] == []
    assert reasoner.calls == 1

    preparation.publisher.admit_preparation = original
    second = preparation.run(request, capability=capability, run_id=run_id)

    assert calls == 1
    assert second["status"] == "complete"
    assert reasoner.calls == 1
    assert len(second["lead_refs"]) == 1


def test_frozen_output_recovers_after_admission_fails_before_write(tmp_path: Path, monkeypatch) -> None:
    reasoner = _Reasoner()
    _publisher, capability, request, run_id, preparation = _request(tmp_path, reasoner)
    original = preparation.publisher.admit_preparation

    def fail_before_admit(*args, **kwargs):
        raise RuntimeError("admission failed before the write")

    monkeypatch.setattr(preparation.publisher, "admit_preparation", fail_before_admit)
    first = preparation.run(request, capability=capability, run_id=run_id)
    assert first["status"] == "partial"
    assert first["recovery_pending"] is True
    assert first["lead_refs"] == []
    assert first["navigation_refs"] == []
    assert reasoner.calls == 1

    preparation.publisher.admit_preparation = original
    second = preparation.run(request, capability=capability, run_id=run_id)
    assert second["status"] == "complete"
    assert len(second["lead_refs"]) == 1
    assert reasoner.calls == 1


def test_committed_response_loss_reconciles_after_run_deadline(tmp_path: Path, monkeypatch) -> None:
    reasoner = _Reasoner()
    publisher, capability, request, run_id, preparation = _request(tmp_path, reasoner)
    original = preparation.publisher.admit_preparation

    def lose_response_after_commit(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("response lost after durable publication")

    monkeypatch.setattr(preparation.publisher, "admit_preparation", lose_response_after_commit)
    first = preparation.run(request, capability=capability, run_id=run_id)
    assert first["status"] == "partial"
    assert first["recovery_pending"] is True
    assert first["lead_refs"] == []

    preparation.publisher.admit_preparation = original
    preparation.runs._clock = lambda: datetime.now(UTC) + timedelta(minutes=3)
    second = preparation.run(request, capability=capability, run_id=run_id)

    assert second["status"] == "complete"
    assert second["lead_refs"]
    assert reasoner.calls == 1
    run = json.loads((publisher.store.root / "runs" / f"{run_id}.json").read_bytes())
    assert run["status"] == "partial"
    assert run["stop_reason"] == "Wall-clock budget exhausted."
    assert any("run remained partial" in item for item in second["limitations"])


def test_wrong_evidence_from_generator_is_rejected(tmp_path: Path) -> None:
    class Unsound(_Reasoner):
        def prepare(self, state):
            output = super().prepare(state)
            output["items"][0]["evidence"][0] = dict(
                output["items"][0]["evidence"], byte_end=999
            )
            return output

    reasoner = Unsound()
    publisher, capability, request, run_id, preparation = _request(tmp_path, reasoner)

    result = preparation.run(request, capability=capability, run_id=run_id)

    assert result["status"] == "failed"
    assert not publisher.store.manifest()["records"].keys() - {"me"}
