from __future__ import annotations

import hashlib
import importlib.resources
import json
from copy import deepcopy
from pathlib import Path

import pytest

from synapse.v2_contracts import (
    V2Error,
    canonical_json,
    hash_bytes,
    validate_envelope,
    validate_payload,
    version_for,
)

ROOT = Path(__file__).parents[1]
EXAMPLES = ROOT / "docs" / "v2" / "contracts"


def _load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def test_all_synthetic_envelopes_validate() -> None:
    names = (
        "example-error.json",
        "example-knowledge_record.json",
        "example-receipt.json",
        "example-legacy_record.json",
        "example-identity-merge.json",
        "example-proposal.json",
        "example-source_version.json",
        "example-result.json",
        "example-request.json",
    )

    for name in names:
        validate_envelope(_load(name))


def test_packaged_schema_is_an_exact_copy() -> None:
    packaged = importlib.resources.files("synapse").joinpath(
        "schemas", "synapse-v2.schema.json"
    )
    assert packaged.read_bytes() == (EXAMPLES / "synapse-v2.schema.json").read_bytes()


@pytest.mark.parametrize(
    ("value", "encoded", "digest"),
    [
        (
            {},
            b"{}",
            "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        ),
        (
            {
                "z": 2,
                "a": {
                    "label": "Zoë — café 😀",
                    "escaped": "a\\nb",
                    "empty": None,
                    "flag": True,
                },
            },
            '{"a":{"empty":null,"escaped":"a\\\\nb","flag":true,"label":"Zoë — café 😀"},"z":2}'.encode(),
            "399534d523e77be48f78609e0b95aee12481268bd02e7cd4c050365db3c9ffb3",
        ),
        (
            {
                "proposal_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "proposal_version": "a" * 64,
                "brief_hash": "b" * 64,
                "selected_group_ids": ["g1", "g2"],
            },
            b'{"brief_hash":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","proposal_id":"01ARZ3NDEKTSV4RRFFQ69G5FAV","proposal_version":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","selected_group_ids":["g1","g2"]}',
            "33cef84b93e5405f2e2caba7ef3b81e01c753e6aaaced0c5c42168b7c3a0e64d",
        ),
        (
            {
                "parent": None,
                "sequence": 0,
                "local_receipts": {
                    "operation-1": {
                        "kind": "suggestion-admission",
                        "payload_hash": "c" * 64,
                    }
                },
                "receipt_origins": {},
            },
            b'{"local_receipts":{"operation-1":{"kind":"suggestion-admission","payload_hash":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}},"parent":null,"receipt_origins":{},"sequence":0}',
            "c977b7b3fd2828883d8e7d435b2353b707dae60ded21bcc21da5a8d997432548",
        ),
    ],
)
def test_hash_vectors(value: object, encoded: bytes, digest: str) -> None:
    assert canonical_json(value) == encoded
    assert hash_bytes(encoded) == digest
    assert hashlib.sha256(canonical_json(value)).hexdigest() == digest


def test_hash_bytes_hashes_exact_bytes_only() -> None:
    assert hash_bytes(b"a\x00\xff") == hashlib.sha256(b"a\x00\xff").hexdigest()
    with pytest.raises(V2Error, match="exact bytes"):
        hash_bytes(bytearray(b"a"))


@pytest.mark.parametrize(
    "value",
    [
        1.0,
        float("inf"),
        float("nan"),
        _MAX := 9_007_199_254_740_992,
        {_MAX: "non-string key"},
        ("tuple",),
        "\ud800",
        {"\udfff": "bad key"},
    ],
)
def test_canonical_json_rejects_unsafe_values(value: object) -> None:
    with pytest.raises(V2Error):
        canonical_json(value)


def test_canonical_json_preserves_unicode_without_normalization_or_newline() -> None:
    value = {"text": "e\u0301\n"}
    encoded = canonical_json(value)
    assert encoded == b'{"text":"e\xcc\x81\\n"}'
    assert not encoded.endswith(b"\n")


@pytest.mark.parametrize(
    "envelope",
    [
        {"schema_version": "synapse-v2/1", "kind": "mystery", "payload": {}},
        {"schema_version": "synapse-v2/2", "kind": "error", "payload": {}},
        {"schema_version": "synapse-v2/1", "kind": "error"},
        {
            "schema_version": "synapse-v2/1",
            "kind": "error",
            "payload": {
                "code": "unsupported-history",
                "message": "bad",
                "retryable": False,
                "writes": "none",
                "unexpected": True,
            },
        },
    ],
)
def test_invalid_envelopes_raise_structured_errors(envelope: dict) -> None:
    with pytest.raises(V2Error) as raised:
        validate_envelope(envelope)
    assert raised.value.code in {"invalid-request", "unsupported-operation"}
    assert raised.value.to_dict()["writes"] == "none"


def test_invalid_date_time_and_state_values_fail() -> None:
    source = _load("example-source_version.json")
    source["payload"]["captured_at"] = "2026-09-10 08:00:00"
    with pytest.raises(V2Error, match="captured_at"):
        validate_envelope(source)

    record = _load("example-knowledge_record.json")
    record["payload"]["availability"] = "verified"
    with pytest.raises(V2Error, match="availability"):
        validate_envelope(record)

    request = _load("example-request.json")
    request["payload"]["budget"]["max_minutes"] = 1.5
    with pytest.raises(V2Error, match="max_minutes"):
        validate_envelope(request)


def test_validate_payload_uses_the_selected_kind_definition() -> None:
    payload = _load("example-error.json")["payload"]
    validate_payload("error", payload)
    with pytest.raises(V2Error, match="Unknown Synapse v2 contract kind"):
        validate_payload("unknown", payload)
    with pytest.raises(V2Error, match="message"):
        validate_payload("error", {"code": "cancelled"})


def test_version_projections_change_only_for_hashed_fields() -> None:
    source = _load("example-source_version.json")["payload"]
    source_version = version_for("source_version", source)
    changed_source_version = deepcopy(source)
    changed_source_version["original_hash"] = "f" * 64
    assert version_for("source_version", changed_source_version) != source_version
    unchanged_source_version = deepcopy(source)
    unchanged_source_version["version"] = "f" * 64
    assert version_for("source_version", unchanged_source_version) == source_version

    proposal = _load("example-proposal.json")["payload"]
    proposal_version = version_for("proposal", proposal)
    changed_proposal = deepcopy(proposal)
    changed_proposal["brief"] += " Extra scope."
    assert version_for("proposal", changed_proposal) != proposal_version
    excluded_proposal_fields = deepcopy(proposal)
    excluded_proposal_fields["version"] = "a" * 64
    excluded_proposal_fields["semantic_review"] = {"status": "passed", "model": "different"}
    assert version_for("proposal", excluded_proposal_fields) == proposal_version


def test_version_for_rejects_kinds_without_a_recipe() -> None:
    with pytest.raises(V2Error, match="No version recipe"):
        version_for("error", {})
    with pytest.raises(V2Error, match="Unknown Synapse v2 contract kind"):
        version_for("not-a-kind", {})


def test_v2_error_serializes_schema_fields_without_inventing_correlation_ids() -> None:
    error = V2Error(
        "revision-unavailable",
        "Revision is not retained.",
        details={"path": "revision", "current_revision": "a" * 64, "request_id": "invented"},
        retryable=True,
        writes="none",
    )
    assert error.to_dict() == {
        "code": "revision-unavailable",
        "message": "Revision is not retained.",
        "retryable": True,
        "writes": "none",
        "current_revision": "a" * 64,
    }
    validate_payload("error", error.to_dict())


@pytest.mark.parametrize(
    ("internal_code", "public_code"),
    [
        ("invalid-record", "invalid-request"),
        ("invalid-path", "invalid-request"),
        ("invalid-version", "invalid-request"),
        ("legacy-write-blocked", "unsupported-operation"),
        ("record-unavailable", "revision-unavailable"),
        ("object-integrity", "recovery-required"),
        ("manifest-integrity", "recovery-required"),
        ("invalid-source", "source-unavailable"),
        ("path-conflict", "invalid-request"),
        ("future-internal-code", "invalid-request"),
    ],
)
def test_v2_error_maps_internal_codes_to_public_schema_codes(
    internal_code: str, public_code: str
) -> None:
    error = V2Error(internal_code, "Internal diagnostic.")
    assert error.code == internal_code
    assert error.to_dict()["code"] == public_code
    validate_payload("error", error.to_dict())
