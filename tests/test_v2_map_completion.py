from contextlib import contextmanager

from test_v2_gateway import corpus as corpus
from typer.testing import CliRunner

from synapse import cli, v2_runtime
from synapse.gateway import Gateway
from synapse.organization import Organization
from synapse.web_v2 import _map_selection, build_v2_area, build_v2_map, build_v2_session


def test_map_connections_keep_acceptance_separate_from_unreviewed_premises(corpus, monkeypatch):
    vault, _, add = corpus
    premise = add(statement="One tentative explanation.")
    accepted = add(availability="accepted", dependencies=[{"id": premise["id"], "version": premise["version"], "role": "premise"}])
    gateway = Gateway(vault)
    members = [
        {"id": "record:" + row["id"], "kind": "record", "label": row["statement"], "ref": {"record_id": row["id"], "record_version": row["version"]}, "area_ids": ["area:a", "area:b"], "reasons": [], "qualification": gateway._unit(row["id"], closure_limit=128, supports_qualifications=True)}
        for row in (premise, accepted)
    ]
    areas = [{"id": "area:" + name, "label": name, "member_ids": [m["id"] for m in members], "coverage": {"unique_records": 2, "unique_sources": 0, "memberships": 2}, "summary": {"text": "Derived area", "limitations": []}} for name in ("a", "b")]
    links = [{"id": row["id"], "from": "area:a", "to": "area:b", "channel": "recorded", "assertion_ids": [row["id"]], "member_ids": [], "explanation": "Recorded relation"} for row in (premise, accepted)]
    snapshot = {"knowledge_revision": gateway.revision, "organization_revision": "a" * 64, "projection_state": "current", "method": "lexical-tfidf", "areas": areas, "members": members, "links": links, "coverage": {}, "loose": [], "limitations": [], "member_links": [{"id": "edge", "from": members[0]["id"], "to": members[1]["id"], "channel": "recorded", "assertion_id": accepted["id"]}]}
    monkeypatch.setattr(Organization, "_snapshot_view", lambda self, **kwargs: snapshot)
    monkeypatch.setattr(Organization, "areas", lambda self, **kwargs: {"items": areas, "total": 2, "next_offset": None})

    overview = build_v2_map(vault)
    assert {edge["availability"] for edge in overview["edges"]} == {"accepted", "suggestion"}
    assert next(edge for edge in overview["edges"] if edge["id"] == premise["id"])["qualification_summary"]["suggestion"] == 1
    focus = build_v2_area(vault, "area:a", organization_revision="a" * 64)
    edge = focus["edges"][0]
    assert edge["availability"] == "accepted"
    assert edge["owner_review"]["status"] == "not-reviewed"
    assert edge["qualification"]["provisional_dependencies"]
    assert {record["id"] for record in edge["qualification"]["records"]} == {accepted["id"], premise["id"]}


def test_regrouping_and_source_aliases_do_not_report_removed_material():
    ref = {"source_id": "source-id", "source_version": "v", "text_version": "t", "evidence": []}
    first = {"id": "source:passage1", "kind": "source", "label": "Original", "ref": ref, "area_ids": [], "reasons": [], "qualification": {}}
    second = dict(first, id="source:passage2")
    snapshot = {"areas": [], "members": [first, second], "organization_revision": "r"}
    assert _map_selection(snapshot, "area:old", []) ["state"] == "outside-view"
    selected = _map_selection(snapshot, "source-id", [second])
    assert selected["state"] == "available"
    assert selected["id"] == second["id"]


def test_session_detects_v2_without_deriving_knowledge(corpus, monkeypatch, tmp_path):
    vault, store, _ = corpus
    def refuse(*args, **kwargs):
        raise AssertionError("Mode detection must not build an index or projection")
    monkeypatch.setattr(Gateway, "__init__", refuse)
    assert build_v2_session(vault) == {"mode": "v2", "knowledge_revision": store.head(), "session": {"revision": store.head(), "head_revision": store.head(), "changed": False, "notice": None}}
    assert build_v2_session(tmp_path / "legacy") == {"mode": "legacy"}


def test_viewer_semantic_runtime_is_explicit_and_closes_on_server_failure(corpus, monkeypatch):
    vault, _, _ = corpus
    calls = []
    @contextmanager
    def warm(path):
        calls.append(("open", path))
        try:
            yield
        finally:
            calls.append(("close", path))
    def serve(path, **kwargs):
        calls.append(("serve", path))
    monkeypatch.setattr(v2_runtime, "warm_semantics", warm)
    monkeypatch.setattr(cli, "serve_web", serve)
    runner = CliRunner()
    assert runner.invoke(cli.app, ["serve", "--vault", str(vault)]).exit_code == 0
    assert [call[0] for call in calls] == ["serve"]
    calls.clear()
    assert runner.invoke(cli.app, ["serve", "--vault", str(vault), "--semantic"]).exit_code == 0
    assert [call[0] for call in calls] == ["open", "serve", "close"]
    calls.clear()
    def failing(path, **kwargs):
        raise RuntimeError("synthetic stop")
    monkeypatch.setattr(cli, "serve_web", failing)
    assert runner.invoke(cli.app, ["serve", "--vault", str(vault), "--semantic"]).exit_code != 0
    assert [call[0] for call in calls] == ["open", "close"]
