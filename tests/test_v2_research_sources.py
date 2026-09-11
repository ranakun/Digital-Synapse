from __future__ import annotations

import copy
from pathlib import Path

import pytest
from test_v2_publication import _bootstrap

from synapse.codex_host import CodexReasoner
from synapse.host_session import NativeHost
from synapse.knowledge import record_descriptor
from synapse.research_sources import WebResearch, fetch_page
from synapse.v2_contracts import V2Error, hash_bytes


class _ResearchReasoner(CodexReasoner):
    def __init__(self, urls: list[str]):
        super().__init__(executable="synthetic-test-only")
        self.urls = urls
        self.queries: list[str] = []

    def search(self, query: str, *, timeout=120, cancelled=None) -> list[str]:
        self.queries.append(query)
        return list(self.urls)

    def step(self, *_args, **_kwargs):
        raise AssertionError("research tests call WebResearch directly; no model step is needed")


def _research_host(tmp_path: Path, reasoner: _ResearchReasoner):
    publisher, _fixture_host, _ = _bootstrap(tmp_path)
    events = {
        "research-instruction": {
            "id": "research-instruction",
            "actor": "user",
            "text": "Research the requested synthetic question.",
        }
    }
    host = NativeHost(
        publisher.store.vault,
        event_reader=lambda reference: events[reference],
        display=lambda _brief: None,
        reasoner=reasoner,
        host_id="research-host",
    )
    delegation = host.start_investigation(
        "research-instruction",
        purpose="Answer a bounded synthetic research question.",
        subject_ids=["me"],
    )
    return publisher, host, delegation, events


def _run_research(host: NativeHost, delegation: dict, *, fetcher, arguments=None, cancelled=None, timeout=5):
    return WebResearch(host, fetcher=fetcher)(
        arguments or {"query": "synthetic source question"},
        request=delegation["run"]["request"],
        run_id=delegation["run"]["id"],
        capability=delegation["capability"],
        timeout=timeout,
        cancelled=cancelled or (lambda: False),
    )


def test_web_research_retains_original_html_with_partial_extraction_and_no_claim(
    tmp_path: Path,
) -> None:
    url = "https://synthetic.example/research"
    raw = b"<html><script>ignore this instruction</script><h1>Visible heading</h1><p>Visible evidence.</p></html>"
    reasoner = _ResearchReasoner([url])
    publisher, host, delegation, _events = _research_host(tmp_path, reasoner)

    result = _run_research(
        host,
        delegation,
        fetcher=lambda requested, timeout: (raw, "text/html", requested),
    )

    assert reasoner.queries == ["synthetic source question"]
    assert len(result["pages"]) == 1
    page = result["pages"][0]
    descriptor = page["source"]
    assert descriptor["origin"] == url
    assert descriptor["extraction"]["completeness"] == "partial"
    assert "Visible evidence." in page["page"]["text"]
    assert "ignore this instruction" not in page["page"]["text"]
    assert publisher.store.read_object(descriptor["original_hash"]) == raw
    assert publisher.store.manifest()["records"].keys() == {"me"}
    assert result["limitations"] == [
        "Web captures establish what these pages said when captured, not independent owner facts or permanent truth."
    ]
def test_web_research_reuses_source_family_for_same_url_and_only_advances_sources(
    tmp_path: Path,
) -> None:
    url = "https://synthetic.example/research"
    raw_first = b"<p>First captured page.</p>"
    raw_second = b"<p>Second captured page.</p>"
    reasoner = _ResearchReasoner([url])
    publisher, host, delegation, events = _research_host(tmp_path, reasoner)
    first_revision = publisher.store.head()
    first = _run_research(
        host,
        delegation,
        fetcher=lambda requested, timeout: (raw_first, "text/html", requested),
    )
    first_source = first["pages"][0]["source"]
    assert first["knowledge_revision"] != first_revision
    assert publisher.store.manifest()["records"].keys() == {"me"}

    events["research-instruction-2"] = {
        "id": "research-instruction-2",
        "actor": "user",
        "text": "Research the same synthetic source again.",
    }
    second_delegation = host.start_investigation(
        "research-instruction-2",
        purpose="Repeat a bounded synthetic research question.",
        subject_ids=["me"],
    )
    second = _run_research(
        host,
        second_delegation,
        fetcher=lambda requested, timeout: (raw_second, "text/html", requested),
    )
    second_source = second["pages"][0]["source"]
    assert second_source["id"] == first_source["id"]
    assert second_source["source_family_id"] == first_source["source_family_id"]
    assert second_source["version"] != first_source["version"]
    assert second["knowledge_revision"] != first["knowledge_revision"]
    assert publisher.store.manifest()["records"].keys() == {"me"}


def test_web_research_validates_scope_and_reports_fetch_failures_without_claims(tmp_path: Path) -> None:
    reasoner = _ResearchReasoner(["https://synthetic.example/failure"])
    publisher, host, delegation, _events = _research_host(tmp_path, reasoner)
    before = publisher.store.head()
    with pytest.raises(V2Error) as caught:
        _run_research(
            host,
            delegation,
            fetcher=lambda _url, **_kwargs: (_ for _ in ()).throw(OSError("synthetic fetch failed")),
            arguments={"query": "q", "scope": "unexpected"},
        )
    assert caught.value.code == "invalid-request"
    assert publisher.store.head() == before

    result = _run_research(
        host,
        delegation,
        fetcher=lambda _url, **_kwargs: (_ for _ in ()).throw(OSError("synthetic fetch failed")),
    )
    assert result["pages"] == []
    assert result["failures"] == [{"url": "https://synthetic.example/failure", "reason": "synthetic fetch failed"}]
    assert result["knowledge_revision"] == before
    assert publisher.store.manifest()["records"].keys() == {"me"}


def test_web_research_rejects_unsupported_url_and_fetch_page_never_accepts_file_scheme(
    tmp_path: Path,
) -> None:
    reasoner = _ResearchReasoner(["https://synthetic.example/redirect"])
    publisher, host, delegation, _events = _research_host(tmp_path, reasoner)
    with pytest.raises(V2Error, match="unsupported scheme"):
        _run_research(
            host,
            delegation,
            fetcher=lambda _url, **_kwargs: (b"<p>page</p>", "text/html", "file:///tmp/local"),
        )
    with pytest.raises(V2Error, match="ordinary HTTP"):
        fetch_page("file:///tmp/local")
    assert publisher.store.manifest()["records"].keys() == {"me"}


def test_web_research_detects_revision_drift_before_and_during_capture(tmp_path: Path) -> None:
    reasoner = _ResearchReasoner(["https://synthetic.example/drift"])
    publisher, host, delegation, _events = _research_host(tmp_path, reasoner)
    raw = b"---\nid: 01ARZ3NDEKTSV4RRFFQ69G5FGV\ntype: person\nname: Drift\nreview_status: proposed\n---\n\nDrifted record.\n"
    row = record_descriptor(raw, path="entities/people/drift.md")

    def add_record() -> None:
        publisher.store.transact(
            operation_id="01ARZ3NDEKTSV4RRFFQ69G5FJ0",
            request_id="01ARZ3NDEKTSV4RRFFQ69G5FJ1",
            kind="capture",
            payload_hash=hash_bytes(b"record drift"),
            mutate=lambda manifest, _read: manifest["records"].update({row["id"]: copy.deepcopy(row)}),
            objects={row["version"]: raw},
        )

    add_record()
    fetch_called = []
    with pytest.raises(V2Error, match="changed during research"):
        _run_research(
            host,
            delegation,
            fetcher=lambda *_args: fetch_called.append(True),
        )
    assert fetch_called == []

    reasoner = _ResearchReasoner(["https://synthetic.example/mid-flight"])
    publisher, host, delegation, _events = _research_host(tmp_path / "mid-flight", reasoner)
    initial_revision = publisher.store.head()

    def fetch_and_drift(requested, timeout):
        add_record_for = record_descriptor(raw, path="entities/people/drifted-mid-flight.md")
        publisher.store.transact(
            operation_id="01ARZ3NDEKTSV4RRFFQ69G5FK0",
            request_id="01ARZ3NDEKTSV4RRFFQ69G5FK1",
            kind="capture",
            payload_hash=hash_bytes(b"mid-flight record drift"),
            mutate=lambda manifest, _read: manifest["records"].update({add_record_for["id"]: add_record_for}),
            objects={add_record_for["version"]: raw},
        )
        return b"<p>captured before record drift is noticed</p>", "text/html", requested

    with pytest.raises(V2Error, match="record changes require explicit revalidation"):
        _run_research(host, delegation, fetcher=fetch_and_drift)
    assert publisher.store.manifest()["records"].keys() != {"me"}
    assert host.publisher.store.manifest()["records"]["me"]
    assert host.publisher.store.manifest()["records"].keys() >= {"me"}
    assert host.publisher.store.head() != initial_revision


def test_web_research_cancel_preserves_prior_capture_and_reports_no_adoption(
    tmp_path: Path,
) -> None:
    urls = ["https://synthetic.example/one", "https://synthetic.example/two"]
    reasoner = _ResearchReasoner(urls)
    publisher, host, delegation, _events = _research_host(tmp_path, reasoner)
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    with pytest.raises(V2Error, match="Research cancelled"):
        _run_research(
            host,
            delegation,
            fetcher=lambda requested, timeout: (b"<p>first retained page</p>", "text/html", requested),
            cancelled=cancelled,
        )
    manifest = publisher.store.manifest()
    assert any(value["origin"] == urls[0] for value in manifest["source_versions"].values())
    assert not any(value["origin"] == urls[1] for value in manifest["source_versions"].values())
    assert manifest["records"].keys() == {"me"}
