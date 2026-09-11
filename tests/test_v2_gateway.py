import copy
import json
from pathlib import Path

import pytest

from synapse.gateway import Gateway, budget_response, transport_size
from synapse.index import connect, reindex
from synapse.knowledge import encode_record, record_descriptor
from synapse.revisions import RevisionStore
from synapse.source_store import evidence_ref, prepare_source
from synapse.util import generate_ulid
from synapse.v2_contracts import hash_bytes


@pytest.fixture
def corpus(tmp_path):
    vault = tmp_path / "brain"
    store = RevisionStore(vault)
    me = b"---\nid: me\ntype: person\nname: Owner\nreview_status: proposed\n---\n\nOriginal accepted owner history.\n"
    row = record_descriptor(me, path="entities/people/me.md")
    descriptor, objects = prepare_source(b"One completed project; several different explanations remain possible.", origin="synthetic.txt")
    objects[row["version"]] = me
    def seed(manifest, _read):
        manifest["records"]["me"] = row
        manifest["sources"][descriptor["id"]] = descriptor["version"]
        manifest["source_versions"][descriptor["version"]] = descriptor
    store.transact(operation_id=generate_ulid(), request_id=generate_ulid(), kind="capture", payload_hash=hash_bytes(b"seed"), mutate=seed, objects=objects, initialize=True)
    store.refresh_checkout()
    evidence = evidence_ref(descriptor, objects.__getitem__, 0, 21)
    template = json.loads((Path(__file__).parents[1] / "docs/v2/contracts/example-knowledge_record.json").read_text())["payload"]
    def add(*, identity=None, availability="suggestion", dependencies=None, context_refs=None, statement="A project may help learning.", disposition="none"):
        value = copy.deepcopy(template)
        value.update(id=identity or generate_ulid(), statement=statement, evidence=[evidence], dependencies=dependencies or [], context_refs=context_refs or [], availability=availability)
        value["owner_review"] = {"status": "not-reviewed", "disposition": disposition}
        if disposition != "none":
            value["owner_review"]["status"] = "reviewed"
            value["owner_review"]["receipt_id"] = generate_ulid()
        raw = encode_record(value)
        path = f"entities/insights/{value['id']}.md"
        row = record_descriptor(raw, path=path)
        def mutate(manifest, _read):
            manifest["records"][row["id"]] = row
        store.transact(operation_id=generate_ulid(), request_id=generate_ulid(), kind="suggestion-admission", payload_hash=hash_bytes(raw), mutate=mutate, objects={hash_bytes(raw): raw})
        return store.read_record(value["id"])
    return vault, store, add


def test_incoming_correction_travels_with_accepted_root(corpus):
    vault, _, add = corpus
    accepted = add(availability="accepted")
    correction = add(context_refs=[{"id": accepted["id"], "version": accepted["version"], "role": "qualifies", "scope": "The cause of completion is uncertain."}])
    value = Gateway(vault).context(ids=[accepted["id"]], knowledge_policy="accepted_only", budget_chars=32000)
    unit = value["items"][0]
    assert {record["id"] for record in unit["records"]} == {accepted["id"], correction["id"]}
    assert unit["qualified"] and not unit["requires_revalidation"]
    assert unit["records"][1]["owner_review"]["status"] == "not-reviewed"
    incapable = Gateway(vault).context(ids=[accepted["id"]], supports_qualifications=False)
    assert incapable["items"][0]["withheld"]
    assert incapable["items"][0]["records"] == []


def test_dismissed_premise_qualifies_dependent_but_not_independent_rebuttal(corpus):
    vault, _, add = corpus
    premise = add()
    dependent = add(availability="accepted", dependencies=[{"id": premise["id"], "version": premise["version"], "role": "premise"}])
    rebuttal = add(context_refs=[{"id": premise["id"], "version": premise["version"], "role": "contradicts", "scope": "A different cause is supported."}], statement="The artifact supports an alternative explanation.")
    add(identity=premise["id"], disposition="dismissed")
    gateway = Gateway(vault)
    unit = gateway.context(ids=[dependent["id"]], budget_chars=32000)["items"][0]
    assert unit["requires_revalidation"]
    assert unit["notices"][0]["dependency"]["version"] == premise["version"]
    independent = gateway.context(ids=[rebuttal["id"]], budget_chars=32000)["items"][0]
    assert not independent["requires_revalidation"]
    assert independent["records"][0]["statement"] == rebuttal["statement"]


@pytest.mark.parametrize("representation", ["json", "mcp"])
@pytest.mark.parametrize("budget", [256, 650, 2400, 8000])
def test_complete_transport_budget_omits_whole_units(corpus, representation, budget):
    vault, _, add = corpus
    ids = [add()["id"] for _ in range(3)]
    result = Gateway(vault).context(ids=ids, budget_chars=budget, representation=representation)
    assert transport_size(result, representation) <= budget
    for unit in result.get("items", []):
        for record in unit["records"]:
            assert record["conditions_and_limits"]
            assert record["evidence"]
    if not result.get("items"):
        assert result.get("error") == "insufficient-budget" or result["budget"]["truncated"]


def test_closure_limit_does_not_strip_qualifications(corpus):
    vault, _, add = corpus
    root = add(availability="accepted")
    add(context_refs=[{"id": root["id"], "version": root["version"], "role": "qualifies", "scope": "Only one observation."}])
    unit = Gateway(vault).context(ids=[root["id"]], closure_limit=1)["items"][0]
    assert not unit["complete"] and not unit["records"]
    assert unit["expansion"]["ids"] == [root["id"]]


def test_gateway_and_legacy_reindex_read_committed_bytes(corpus):
    vault, store, _ = corpus
    gateway = Gateway(vault)
    original = gateway.record("me")["text"]
    (vault / "entities/people/me.md").write_text(original.replace("Original accepted", "Unpublished fabricated"))
    reindex(vault, full=True)
    with connect(vault) as conn:
        row = conn.execute("SELECT body,content_hash FROM entities WHERE id='me'").fetchone()
        assert "Original accepted" in row["body"]
        assert row["content_hash"] == store.manifest()["records"]["me"]["version"]
    assert gateway.record("me")["text"] == original


def test_unchanged_lookup_cache_does_not_cross_revisions(corpus):
    vault, _, add = corpus
    old = Gateway(vault)
    before = old.context(query="distinctivehypothesis")
    value = add(statement="A distinctivehypothesis can be investigated.")
    assert old.context(query="distinctivehypothesis") == before
    newer = Gateway(vault).context(query="distinctivehypothesis")
    assert newer["knowledge_revision"] != old.revision
    assert newer["items"][0]["root_id"] == value["id"]


def test_generic_budget_includes_adapter_escape_overhead():
    value = {"items": [{"body": '"quoted"\\\n' * 100}], "knowledge_revision": "a" * 64}
    result = budget_response(value, budget_chars=512, representation="mcp")
    assert transport_size(result, "mcp") <= 512
    assert not result.get("items")
