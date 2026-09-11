from __future__ import annotations

import json
from pathlib import Path

from scripts.v2_acceptance_cases import build_fixture
from synapse.knowledge import decode_record
from synapse.read_view import ReadView
from synapse.v2_contracts import hash_bytes


def _fixture(tmp_path: Path) -> tuple[Path, dict, ReadView]:
    output = tmp_path / "holdout"
    config = build_fixture(output)
    return output, config, ReadView(output / "vault", revision=config["revision"])


def test_factory_has_six_cases_and_real_published_revision(tmp_path: Path):
    output, config, view = _fixture(tmp_path)

    assert (output / "cases.json").is_file()
    assert config["schema"] == "digital-synapse-v2/acceptance-holdout-1"
    assert len(config["cases"]) == 6
    assert config["revision"] == view.revision
    assert len(view.manifest["sources"]) == 12
    assert len(view.manifest["records"]) == 11
    assert config["native_worker_brief"].startswith("Read docs/v2/SPECIALIST-PLAYBOOK.md")
    assert all("native_worker_brief" in case for case in config["cases"])


def test_sources_are_exactly_retained_with_provenance_and_dates(tmp_path: Path):
    output, config, view = _fixture(tmp_path)
    serialized = json.loads((output / "cases.json").read_text(encoding="utf-8"))
    assert serialized == config

    for slug, expected in config["sources"].items():
        descriptor = view.manifest["source_versions"][expected["version"]]
        raw = (output / "source-files" / f"{slug}.txt").read_bytes()
        assert descriptor["id"] == expected["id"]
        assert descriptor["origin"] == f"fixture://sources/{slug}.txt"
        assert descriptor["captured_at"] == expected["captured_at"]
        assert hash_bytes(raw) == descriptor["original_hash"]
        page = view.source(expected["id"], version=expected["version"], limit=4000)
        assert page["source_version"] == expected["version"]
        assert page["text_version"] == expected["text_version"]
        assert page["total_characters"] == len(raw.decode("utf-8"))


def test_long_terminology_case_keeps_late_decisive_caveat_readable(tmp_path: Path):
    _output, config, view = _fixture(tmp_path)
    case = next(case for case in config["cases"] if case["id"] == "case-2-terminology-mismatch")
    source_id = case["source_ids"][0]
    text = ""
    offset = 0
    while True:
        page = view.source(source_id, offset=offset, limit=4000)
        text += page["text"]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert len(text) > 70_000
    assert text.index("DECISIVE CAVEAT") > 70_000
    assert "human reviewer" in text[text.index("DECISIVE CAVEAT") :]


def test_record_evidence_and_scoped_refs_bind_to_retained_versions(tmp_path: Path):
    _output, config, view = _fixture(tmp_path)
    for descriptor in view.manifest["records"].values():
        if descriptor["profile"] != "knowledge-v2":
            continue
        record = view.store.read_record(descriptor["id"], view.revision)
        for reference in record["evidence"]:
            source = view.manifest["source_versions"][reference["source_version"]]
            assert source["id"] == reference["source_id"]
            assert hash_bytes(view.store.read_object(reference["text_version"])) == reference["text_version"]
        for reference in record["dependencies"] + record.get("context_refs", []):
            target = view.manifest["records"][reference["id"]]
            assert target["version"] == reference["version"]
    assert config["manifest"]["records"] == view.manifest["records"]


def test_case_assertions_are_evidence_qualified_and_nonprescriptive(tmp_path: Path):
    _output, config, view = _fixture(tmp_path)
    cases = {case["id"]: case for case in config["cases"]}
    assert set(cases) == {
        "case-1-direct-factual",
        "case-2-terminology-mismatch",
        "case-3-cross-domain-connection",
        "case-4-negative-shared-word",
        "case-5-changed-schedule",
        "case-6-unfamiliar-choice",
    }
    assert cases["case-4-negative-shared-word"]["expected"]["outcome_criteria"]
    assert "leaves the final schedule decision to the owner" in cases["case-5-changed-schedule"]["expected"]["outcome_criteria"][0]
    assert "does not choose for the owner" in cases["case-6-unfamiliar-choice"]["expected"]["outcome_criteria"][0]

    preference_rows = [row for row in view.manifest["records"].values() if row["id"] in config["records"] and row["id"] != "me" and row["name"].lower().find("preference") >= 0]
    assert any(row["availability"] == "suggestion" and row["disposition"] == "declined-adoption" for row in preference_rows)
    owner_preference = next(row for row in preference_rows if row["availability"] == "accepted")
    assert decode_record(view.store.read_object(owner_preference["version"]), path=owner_preference["path"])["owner_position"] == "stated"
