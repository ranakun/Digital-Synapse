from __future__ import annotations

import copy
from pathlib import Path

from synapse.knowledge import record_descriptor
from synapse.revisions import RevisionStore
from synapse.source_store import prepare_source
from synapse.util import generate_ulid
from synapse.v2_contracts import hash_bytes
from synapse.v2_protocol import READ_OPERATIONS, dispatch


def _vault(tmp_path: Path) -> tuple[Path, RevisionStore, dict]:
    vault = tmp_path / "vault"
    store = RevisionStore(vault)
    record = (
        b"---\nid: me\ntype: person\nname: Owner\nreview_status: proposed\n---\n\nOwner profile.\n"
    )
    row = record_descriptor(record, path="entities/people/me.md")
    source_text = "Retained Unicode evidence: café 😀 supports a project decision. " * 20
    descriptor, objects = prepare_source(
        source_text.encode("utf-8"), origin="synthetic.txt", source_id=generate_ulid()
    )
    objects[row["version"]] = record

    def seed(manifest, _read):
        manifest["records"]["me"] = copy.deepcopy(row)
        manifest["sources"][descriptor["id"]] = descriptor["version"]
        manifest["source_versions"][descriptor["version"]] = descriptor

    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"seed"),
        mutate=seed,
        objects=objects,
        initialize=True,
    )
    return vault, store, descriptor


def test_allowlist_and_strict_arguments_fail_without_opening_a_gateway(
    tmp_path: Path, monkeypatch
) -> None:
    vault, store, _ = _vault(tmp_path)
    original = store.head()
    assert "search_sources" in READ_OPERATIONS
    assert (
        dispatch(vault, "delete", {}, budget_chars=512)["error"]["code"] == "unsupported-operation"
    )
    assert (
        dispatch(vault, "catalog", {"ignored": True}, budget_chars=512)["error"]["code"]
        == "invalid-request"
    )
    assert store.head() == original

    def fail_gateway(*args, **kwargs):
        raise AssertionError("unknown operations must not construct a Gateway")

    monkeypatch.setattr("synapse.v2_protocol.Gateway", fail_gateway)
    assert (
        dispatch(vault, "approve", {}, budget_chars=512)["error"]["code"] == "unsupported-operation"
    )


def test_search_sources_returns_exact_span_and_whole_unit_budget(tmp_path: Path) -> None:
    vault, _, descriptor = _vault(tmp_path)
    result = dispatch(vault, "search_sources", {"query": "café"}, budget_chars=4000)

    assert result["knowledge_revision"]
    assert result["items"]
    hit = result["items"][0]
    assert hit["excerpt"]
    assert hit["offset"] >= 0
    assert hit["evidence"]["byte_start"] >= 0
    assert hit["evidence"]["byte_end"] > hit["evidence"]["byte_start"]
    assert hit["evidence"]["source_id"] == descriptor["id"]
    assert hit["claim_extraction"] == "Not implied by text search."
    assert result["budget"]["limit"] == 4000


def test_source_and_record_pages_use_unicode_offsets_and_preserve_page_boundaries(
    tmp_path: Path,
) -> None:
    vault, _, descriptor = _vault(tmp_path)
    source = dispatch(vault, "source", {"id": descriptor["id"], "limit": 4000}, budget_chars=1200)
    assert source["next_offset"] == source["offset"] + len(source["text"])
    assert source["next_offset"] is not None
    assert source["evidence"]["byte_end"] - source["evidence"]["byte_start"] == len(
        source["text"].encode("utf-8")
    )
    following = dispatch(
        vault,
        "source",
        {"id": descriptor["id"], "offset": source["next_offset"], "limit": 4000},
        budget_chars=1200,
    )
    assert following["offset"] == source["next_offset"]
    assert following["offset"] > source["offset"]

    record = dispatch(vault, "record", {"id": "me"}, budget_chars=900)
    assert record["next_offset"] is None
    assert record["text"].endswith("\n")


def test_source_read_honors_an_older_pinned_revision(tmp_path: Path) -> None:
    vault, store, descriptor = _vault(tmp_path)
    old_revision = store.head()
    newer, objects = prepare_source(
        b"New retained source version.", origin="new.txt", source_id=descriptor["id"]
    )
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"new-source"),
        mutate=lambda manifest, _read: (
            manifest["sources"].update({newer["id"]: newer["version"]}),
            manifest["source_versions"].update({newer["version"]: newer}),
        ),
        objects=objects,
    )

    result = dispatch(
        vault, "source", {"id": descriptor["id"]}, revision=old_revision, budget_chars=3000
    )
    assert "Retained Unicode evidence" in result["text"]
    assert "New retained" not in result["text"]


def test_context_policy_alias_is_explicit_and_passage_over_budget_is_not_chopped(
    tmp_path: Path,
) -> None:
    vault, _, _ = _vault(tmp_path)
    result = dispatch(
        vault, "context", {"ids": ["me"], "knowledge_policy": "accepted-only"}, budget_chars=2000
    )
    assert "error" not in result
    passage = dispatch(vault, "passage", {"evidence": {"source_id": "missing"}}, budget_chars=512)
    assert passage["error"]["code"] in {"invalid-span", "source-unavailable", "invalid-request"}
