import copy
import json
from pathlib import Path

import pytest

from synapse.knowledge import decode_record, encode_record, record_descriptor
from synapse.v2_contracts import V2Error, hash_bytes


def example():
    return json.loads(
        (Path(__file__).parents[1] / "docs/v2/contracts/example-knowledge_record.json").read_text()
    )["payload"]


def test_markdown_roundtrip_keeps_state_and_evidence_separate():
    value = example()
    raw = encode_record(value)
    expected = copy.deepcopy(value) | {"version": hash_bytes(raw)}
    assert decode_record(raw) == expected
    assert value["version"].encode() not in raw
    row = record_descriptor(raw, path="entities/insights/learning.md")
    assert row["availability"] == "suggestion"
    assert row["review_status"] == "proposed"
    assert row["active"]


def test_mismatching_readable_body_cannot_become_a_new_fact():
    raw = encode_record(example()).replace(b"## Statement\n\n", b"## Statement\n\nNot true: ")
    with pytest.raises(V2Error, match="body disagrees"):
        decode_record(raw)


def test_legacy_bytes_and_attestation_preserved_with_tombstone():
    raw = b"---\r\nid: me\r\ntype: person\r\nname: Owner\r\nreview_status: verified\r\narchived: true\r\n---\r\n\r\nOriginal history.\r\n"
    row = record_descriptor(raw, path="entities/archive/me.md")
    assert row["version"] == hash_bytes(raw)
    assert row["availability"] == "accepted"
    assert row["review_status"] == "verified"
    assert not row["active"]
    assert decode_record(raw)["coverage_limitations"]


@pytest.mark.parametrize("path", ["../me.md", "/entities/me.md", "entities/../me.md", "other/me.md", "entities/me.txt"])
def test_record_paths_cannot_escape(path):
    with pytest.raises(V2Error):
        record_descriptor(encode_record(example()), path=path)
