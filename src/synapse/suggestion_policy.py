"""Exact scoped duplicate recognition; similarity is never disposition authority."""

from synapse.v2_contracts import canonical_json, hash_bytes


def meaning_key(record):
    if "record_kind" not in record:
        return None
    fields = {key: record.get(key) for key in ("subject_id", "claim_key", "record_kind", "statement", "conditions_and_limits", "support", "counterevidence", "alternatives", "would_change_with", "dependencies", "context_refs", "relationship", "applies_from", "applies_until")}
    # A changed extraction version or unrelated edit to the original is not
    # new evidence for an unchanged passage from the same source family.
    fields["evidence"] = sorted({(ref["source_family_id"], ref["excerpt_hash"]) for ref in record.get("evidence", [])})
    fields["evidence"] = [list(item) for item in fields["evidence"]]
    return hash_bytes(canonical_json(fields))


def logical_preparation_key(record):
    """Key preparation leads by meaning, scope and source family.

    Preparation may use the source record itself as the subject identity, so
    subject and record IDs are intentionally absent.  Source versions and
    passage hashes are also absent: a revised extraction of the same retained
    original remains the same source family and must not re-admit a dismissed
    lead.
    """

    if record.get("record_kind") not in {"question", "navigation"}:
        return None
    fields = {
        key: record.get(key)
        for key in (
            "record_kind",
            "statement",
            "conditions_and_limits",
            "would_change_with",
            "applies_from",
            "applies_until",
        )
    }
    fields["source_families"] = sorted(
        {ref.get("source_family_id") for ref in record.get("evidence", [])}
    )
    return hash_bytes(canonical_json(fields))
