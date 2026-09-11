"""Bounded, capture-scoped preparation of retained source material.

Preparation is deliberately a small coordinator.  Retention and extraction
are source services, while run authority and publication remain in their
root-owned services.  A supplied reasoner can suggest only source-anchored
questions and navigation hints; it cannot provide identities or authority.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from synapse.knowledge import decode_record, encode_record
from synapse.publication import Publisher
from synapse.read_view import ReadView
from synapse.revisions import RevisionStore
from synapse.runs import RunManager
from synapse.source_store import evidence_ref, read_passage, read_source_page
from synapse.suggestion_policy import logical_preparation_key
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes, validate_payload

_PAGE_CHARACTERS = 4_000
_MAX_GENERATOR_LEADS = 64
_ITEM_KEYS = {
    "record_kind",
    "statement",
    "support",
    "would_change_with",
    "evidence",
    "limits",
}
_PREPARATION_STATES = {"pending", "running", "complete", "partial", "failed"}


class _BudgetExhausted(Exception):
    pass


class _GeneratorError(Exception):
    pass


def request_for_preparation(
    source_refs: list[Mapping[str, str]],
    owner_instruction_ref: str,
    *,
    revision: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build the exact bounded request accepted by ``RunManager``."""

    if not isinstance(source_refs, list):
        raise V2Error("invalid-request", "source_refs must be a list")
    refs = [dict(ref) for ref in source_refs]
    refs.sort(key=lambda ref: ref.get("source_id", ""))
    value = {
        "id": request_id or generate_ulid(),
        "mode": "prepare",
        "owner_instruction_ref": owner_instruction_ref,
        "source_refs": refs,
        "pinned_revision": revision,
        "budget": {
            "max_minutes": 2,
            "max_operations": 8,
            "max_source_expansions": 8,
            "max_leads": 3,
            "max_hints": 3,
            "max_result_characters": 8_000,
        },
    }
    validate_payload("preparation_request", value)
    if len({ref["source_id"] for ref in refs}) != len(refs):
        raise V2Error("invalid-request", "Preparation source references must be unique")
    if refs != sorted(refs, key=lambda ref: ref["source_id"]):
        raise V2Error("invalid-request", "Preparation source references must be sorted")
    return value


class Preparation:
    """Run one explicitly authorized preparation request."""

    def __init__(self, vault: Path, reasoner: Any = None):
        self.vault = Path(vault)
        self.store = RevisionStore(self.vault)
        self.reasoner = reasoner
        self.runs = RunManager(self.vault)
        self.publisher = Publisher(self.vault)

    def run(
        self,
        request: dict[str, Any],
        *,
        capability: dict[str, str],
        run_id: str,
    ) -> dict[str, Any]:
        """Prepare selected sources and reconcile any frozen prior result."""

        validate_payload("preparation_request", request)
        self._validate_request_shape(request)
        started = self.runs.start_preparation(request, capability, run_id=run_id)

        frozen = started.get("prepared_output")
        if frozen is not None:
            return self._reconcile_frozen(request, capability, run_id, started)

        if started.get("status") != "running":
            return self._result(
                run_id,
                self._wire_status(started.get("status")),
                coverage=self._coverage(started, 0, 0, 0, 0),
                limitations=["Preparation was already stopped; a new owner request is required."],
            )
        if self.reasoner is None or not callable(getattr(self.reasoner, "prepare", None)):
            finished = self.runs.finish(
                run_id,
                capability,
                status="partial",
                stop_reason="No preparation reasoner is available.",
            )
            return self._result(
                run_id,
                "partial",
                coverage=self._coverage(finished, 0, 0, 0, 0),
                limitations=["Source capture succeeded, but no preparation reasoner is available."],
            )

        limitations: list[str] = []
        passages: list[dict[str, Any]] = []
        source_count = len(request["source_refs"])
        try:
            current = self.store.manifest()
            pinned = self.store.manifest(request["pinned_revision"])
            descriptors = []
            for ref in request["source_refs"]:
                version = ref["source_version"]
                if pinned["sources"].get(ref["source_id"]) != version or current["sources"].get(ref["source_id"]) != version:
                    raise V2Error("stale-selection", "Preparation source changed before reading")
                descriptor = pinned["source_versions"].get(version)
                if not descriptor:
                    raise V2Error("source-unavailable", "Preparation source descriptor is unavailable")
                descriptors.append(descriptor)

            self._reserve(
                operations=1,
                source_expansions=source_count,
                state=self._state(passages, [], started),
                run_id=run_id,
                capability=capability,
            )
            for descriptor in descriptors:
                try:
                    page = read_source_page(
                        descriptor,
                        self.store.read_object,
                        offset=0,
                        limit=_PAGE_CHARACTERS,
                    )
                except V2Error as exc:
                    limitations.append(
                        f"Source {descriptor['id']} is unavailable for preparation ({exc.code})."
                    )
                    continue
                text = page["text"]
                if not text:
                    limitations.append(f"Source {descriptor['id']} has no readable text.")
                    continue
                text_bytes = text.encode("utf-8")
                reference = evidence_ref(
                    descriptor,
                    self.store.read_object,
                    0,
                    len(text_bytes),
                )
                source_page = copy.deepcopy(page)
                source_page["evidence"] = reference
                passages.append(source_page)
                if page["truncated"]:
                    limitations.append(
                        f"Source {descriptor['id']} coverage is limited to the first {_PAGE_CHARACTERS} characters."
                    )

            matching = self._matching_leads(current, passages, limitations)
            context_matching = matching[:_MAX_GENERATOR_LEADS]
            if len(matching) > len(context_matching):
                limitations.append(
                    f"Generator context includes the first {_MAX_GENERATOR_LEADS} matching prior leads; deduplication considered all {len(matching)}."
                )
            self._reserve(
                operations=1,
                state=self._state(passages, context_matching, self.runs.get(run_id)),
                run_id=run_id,
                capability=capability,
            )
            state = self._state(passages, context_matching, self.runs.get(run_id))
            self._reserve(
                operations=1,
                state=state,
                run_id=run_id,
                capability=capability,
            )
            try:
                generated = self.reasoner.prepare(copy.deepcopy(state))
            except Exception as exc:
                # Typed boundary failures carry caller-safe messages; raw
                # unexpected exceptions and diagnostic details stay private.
                failure = (
                    f"Preparation reasoner failed ({exc.code}): {exc}"
                    if isinstance(exc, V2Error)
                    else f"Preparation reasoner failed ({exc.__class__.__name__})."
                )
                return self._failed(
                    run_id,
                    capability,
                    limitations + [failure],
                    source_count,
                    len(passages),
                    len(matching),
                )
            self.runs.checkpoint(
                run_id,
                capability,
                state=state,
            )
            records, lead_refs, navigation_refs, output_limits = self._records(
                generated,
                passages,
                matching,
                run_id,
                request,
            )
            limitations.extend(output_limits)
            if not records:
                finished = self.runs.finish(
                    run_id,
                    capability,
                    status="completed",
                    stop_reason="Preparation completed without new leads.",
                    state=state,
                )
                return self._result(
                    run_id,
                    "complete",
                    lead_refs=lead_refs,
                    navigation_refs=navigation_refs,
                    coverage=self._coverage(finished, source_count, len(passages), len(lead_refs), len(navigation_refs)),
                    limitations=limitations,
                )

            operation_id = run_id
            request_id = request["id"]
            self.runs.freeze_preparation(
                run_id,
                capability,
                records,
                operation_id=operation_id,
                request_id=request_id,
            )
            try:
                receipt = self.publisher.admit_preparation(
                    capability,
                    records,
                    run_id=run_id,
                    operation_id=operation_id,
                    request_id=request_id,
                )
            except Exception as exc:
                # The freeze is durable even when the caller loses the
                # admission response.  Leave the run recoverable so a retry
                # reconciles its receipt without invoking the reasoner again.
                return self._result(
                    run_id,
                    "partial",
                    recovery_pending=True,
                    coverage=self._coverage(
                        self.runs.get(run_id),
                        source_count,
                        len(passages),
                        len(lead_refs),
                        len(navigation_refs),
                    ),
                    limitations=limitations
                    + [
                        f"Frozen preparation admission response was unavailable ({exc.__class__.__name__}); retry is recoverable."
                    ],
                )
            finished, publication_limits = self._finish_publication(
                run_id,
                capability,
                state=state,
            )
            return self._result(
                run_id,
                "complete",
                lead_refs=lead_refs,
                navigation_refs=navigation_refs,
                coverage=self._coverage(finished, source_count, len(passages), len(lead_refs), len(navigation_refs)),
                limitations=limitations + publication_limits,
                receipt=receipt,
            )
        except _BudgetExhausted:
            run = self.runs.get(run_id)
            return self._result(
                run_id,
                "partial",
                coverage=self._coverage(run, source_count, len(passages), 0, 0),
                limitations=limitations + ["Preparation budget was exhausted before all bounded calls could run."],
            )
        except _GeneratorError as exc:
            return self._failed(
                run_id,
                capability,
                limitations + [str(exc)],
                source_count,
                len(passages),
                0,
            )
        except V2Error as exc:
            return self._failed(
                run_id,
                capability,
                limitations + [f"Preparation stopped ({exc.code})."],
                source_count,
                len(passages),
                0,
            )

    def _reconcile_frozen(
        self,
        request: Mapping[str, Any],
        capability: dict[str, str],
        run_id: str,
        run: Mapping[str, Any],
    ) -> dict[str, Any]:
        frozen = run["prepared_output"]
        records = {
            path: self.store.read_object(version)
            for path, version in frozen["records"].items()
        }
        receipt = self.store.receipt(frozen["operation_id"])
        if receipt is None:
            run = self.runs.status(run_id)
            if run["status"] != "running":
                return self._result(
                    run_id,
                    "partial",
                    coverage=self._coverage(
                        run, len(request["source_refs"]), 0, 0, 0
                    ),
                    limitations=[
                        "Frozen preparation has no admission receipt and its run budget has expired; request preparation again to publish it."
                    ],
                    recovery_pending=True,
                )
            try:
                self.publisher.admit_preparation(
                    capability,
                    records,
                    run_id=run_id,
                    operation_id=frozen["operation_id"],
                    request_id=frozen["request_id"],
                )
            except Exception as exc:
                return self._result(
                    run_id,
                    "partial",
                    coverage=self._coverage(
                        run, len(request["source_refs"]), 0, 0, 0
                    ),
                    limitations=[
                        f"Frozen preparation admission is still unconfirmed ({exc.__class__.__name__}); retry is recoverable."
                    ],
                    recovery_pending=True,
                )
            receipt = self.store.receipt(frozen["operation_id"])
            if receipt is None:
                return self._result(
                    run_id,
                    "partial",
                    coverage=self._coverage(
                        run, len(request["source_refs"]), 0, 0, 0
                    ),
                    limitations=[
                        "Frozen preparation admission returned without a durable receipt; retry is recoverable."
                    ],
                    recovery_pending=True,
                )
        lead_refs, navigation_refs = self._record_refs(records)
        run, publication_limits = self._finish_publication(run_id, capability)
        limitations = [
            "Preparation output was already frozen and was reconciled without rerunning the reasoner."
        ] + publication_limits
        return self._result(
            run_id,
            "complete",
            lead_refs=lead_refs,
            navigation_refs=navigation_refs,
            coverage=self._coverage(run, len(request["source_refs"]), 0, len(lead_refs), len(navigation_refs)),
            limitations=limitations,
            receipt=receipt,
        )

    def _finish_publication(self, run_id, capability, *, state=None):
        """Finish a committed publication without hiding a deadline transition."""

        run = self.runs.status(run_id)
        if run["status"] == "running":
            try:
                run = self.runs.finish(
                    run_id,
                    capability,
                    status="completed",
                    state=state,
                )
            except V2Error:
                # The deadline may have elapsed between status refresh and
                # finish.  The committed receipt still proves availability;
                # retain the run's truthful partial usage state.
                run = self.runs.status(run_id)
                if run["status"] == "running":
                    raise
        limitations = []
        if run["status"] != "completed":
            if run["status"] == "partial":
                limitations.append(
                    "Publication is committed, but the preparation run remained partial after its bounded budget elapsed."
                )
            else:
                limitations.append(
                    f"Publication is committed, but the preparation run remained {run['status']}."
                )
        return run, limitations

    def _reserve(self, *, operations, source_expansions=0, state, run_id, capability):
        run = self.runs.status(run_id)
        if run["status"] != "running":
            raise _BudgetExhausted
        budget = run["usage"]["budget"]
        used = run["usage"]
        if (
            used["operations"] + operations > budget["max_operations"]
            or used["source_expansions"] + source_expansions > budget["max_source_expansions"]
        ):
            self.runs.finish(
                run_id,
                capability,
                status="partial",
                stop_reason="Operation or source-expansion budget exhausted.",
                state=state,
            )
            raise _BudgetExhausted
        return self.runs.checkpoint(
            run_id,
            capability,
            operations=operations,
            source_expansions=source_expansions,
            state=state,
        )

    def _matching_leads(self, manifest, passages, limitations):
        families = {passage["source_family_id"] for passage in passages}
        if not families:
            return []
        matches = []
        view = ReadView(self.vault, revision=self.store.head())
        connection = view._connect()  # The index is a disposable read projection.
        try:
            clauses = " OR ".join("record_json LIKE ?" for _ in families)
            rows = connection.execute(
                f"SELECT id, availability, active, merged_into, record_json FROM records WHERE {clauses} ORDER BY id",
                [f"%{family}%" for family in sorted(families)],
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            try:
                record = json.loads(row["record_json"])
            except (TypeError, ValueError) as exc:
                raise V2Error(
                    "recovery-required",
                    "The retained read index contains an invalid record payload",
                ) from exc
            if record.get("record_kind") not in {"question", "navigation"}:
                continue
            if (
                row["availability"] == "draft"
                or not bool(row["active"])
                or row["merged_into"] is not None
                or record.get("lifecycle") == "withdrawn"
            ):
                continue
            record_families = {
                ref.get("source_family_id")
                for ref in record.get("evidence", [])
                if isinstance(ref, dict)
            }
            if not families.intersection(record_families):
                continue
            identity = record.get("id")
            if not isinstance(identity, str) or identity not in manifest["records"]:
                raise V2Error("recovery-required", "The retained read index disagrees with the manifest")
            manifest_row = manifest["records"][identity]
            matches.append(
                {
                    "id": identity,
                    "version": manifest_row["version"],
                    "record_kind": record["record_kind"],
                    "statement": record["statement"],
                    "conditions_and_limits": record["conditions_and_limits"],
                    "support": record["support"],
                    "would_change_with": list(record["would_change_with"]),
                    "evidence": copy.deepcopy(record["evidence"]),
                    "owner_review": copy.deepcopy(record["owner_review"]),
                    "logical_key": logical_preparation_key(record),
                }
            )
        return matches

    def _records(self, output, passages, matching, run_id, request):
        if not isinstance(output, dict) or set(output) != {"items"} or not isinstance(output["items"], list):
            raise _GeneratorError("Preparation reasoner returned an invalid output envelope.")
        limits = request["budget"]
        if len(output["items"]) > limits["max_leads"] + limits["max_hints"]:
            raise _GeneratorError("Preparation reasoner returned more leads than the bounded output budget.")
        allowed_evidence = {
            canonical_json(passage["evidence"]): passage["evidence"] for passage in passages
        }
        existing_by_key = {item["logical_key"]: item for item in matching}
        records = {}
        lead_refs = []
        navigation_refs = []
        seen = set()
        counts = {"question": 0, "navigation": 0}
        output_limits = []
        for item in output["items"]:
            if not isinstance(item, dict) or set(item) != _ITEM_KEYS:
                raise _GeneratorError("Preparation reasoner returned an invalid item shape.")
            kind = item["record_kind"]
            if kind not in counts:
                raise _GeneratorError("Preparation output contains an unsupported record kind.")
            if counts[kind] >= limits["max_leads" if kind == "question" else "max_hints"]:
                raise _GeneratorError("Preparation output exceeds its lead or hint budget.")
            statement = self._text(item["statement"], "statement")
            support = self._texts(item["support"], "support", required=True)
            would_change = self._texts(item["would_change_with"], "would_change_with", required=False)
            item_limits = self._texts(item["limits"], "limits", required=True)
            if not isinstance(item["evidence"], list) or not item["evidence"]:
                raise _GeneratorError("Every preparation lead needs exact source evidence.")
            evidence = []
            for candidate in item["evidence"]:
                if not isinstance(candidate, dict):
                    raise _GeneratorError("Preparation evidence references must be objects.")
                key = canonical_json(candidate)
                reference = allowed_evidence.get(key)
                if reference is None:
                    raise _GeneratorError("Preparation evidence must cite an exact selected source passage.")
                descriptor = self.store.manifest()["source_versions"].get(
                    reference["source_version"]
                )
                if descriptor is None:
                    raise _GeneratorError(
                        "Preparation evidence no longer resolves to a retained source."
                    )
                read_passage(descriptor, reference, self.store.read_object, context_characters=0)
                evidence.append(copy.deepcopy(reference))
            conditions = self._conditions(kind, item_limits)
            candidate = {
                "claim_key": self._claim_key(kind, statement, [conditions], evidence),
                "record_kind": kind,
                "statement": statement,
                "conditions_and_limits": conditions,
                "support": " ".join(support),
                "would_change_with": would_change,
                "evidence": evidence,
                "as_of": date.today().isoformat(),
            }
            logical = logical_preparation_key(candidate)
            if logical in seen:
                continue
            seen.add(logical)
            existing = existing_by_key.get(logical)
            if existing is not None:
                ref = {"id": existing["id"], "version": existing["version"]}
                (lead_refs if kind == "question" else navigation_refs).append(ref)
                if existing["owner_review"]["disposition"] in {"dismissed", "disputed", "deferred"}:
                    output_limits.append(f"Existing {kind} {existing['id']} was retained with its prior disposition.")
                counts[kind] += 1
                continue
            identity = generate_ulid()
            payload = {
                "id": identity,
                "version": "0" * 64,
                "subject_id": identity,
                "claim_key": candidate["claim_key"],
                "record_kind": kind,
                "availability": "suggestion",
                "review_status": "proposed",
                "owner_review": {"status": "not-reviewed", "disposition": "none"},
                "owner_position": "unreviewed",
                "lifecycle": "current",
                "epistemic_basis": ["assistant-hypothesis"],
                "facets": ["preparation"],
                "statement": statement,
                "conditions_and_limits": candidate["conditions_and_limits"],
                "support": " ".join(support),
                "counterevidence": [],
                "alternatives": [],
                "would_change_with": would_change,
                "evidence": evidence,
                "dependencies": [],
                "as_of": candidate["as_of"],
                "origin_run_id": run_id,
            }
            raw = encode_record(payload)
            if sum(len(value) for value in records.values()) + len(raw) > limits["max_result_characters"]:
                output_limits.append("Some valid preparation leads were omitted at the result-character limit.")
                break
            records[f"entities/insights/{identity}.md"] = raw
            ref = {"id": identity, "version": hash_bytes(raw)}
            (lead_refs if kind == "question" else navigation_refs).append(ref)
            counts[kind] += 1
        return records, lead_refs, navigation_refs, output_limits

    def _state(self, passages, matching, run):
        usage = run.get("usage", {})
        remaining_seconds = 0
        segments = run.get("segments", [])
        if segments:
            deadline = segments[-1].get("deadline_at")
            if isinstance(deadline, str):
                try:
                    remaining_seconds = max(
                        0,
                        int(
                            (
                                datetime.fromisoformat(deadline.replace("Z", "+00:00"))
                                - datetime.now(UTC)
                            ).total_seconds()
                        ),
                    )
                except ValueError:
                    remaining_seconds = 0
        return {
            "sources": copy.deepcopy(passages),
            "existing_leads": copy.deepcopy(matching),
            "usage": {
                "operations": usage.get("operations", 0),
                "source_expansions": usage.get("source_expansions", 0),
                "elapsed_seconds": usage.get("elapsed_seconds", 0),
            },
            "remaining_seconds": remaining_seconds,
        }

    @staticmethod
    def _text(value, field):
        if not isinstance(value, str) or not value.strip():
            raise _GeneratorError(f"Preparation {field} must be a non-empty string.")
        return value.strip()

    def _texts(self, value, field, *, required):
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise _GeneratorError(f"Preparation {field} must be a list of non-empty strings.")
        if required and not value:
            raise _GeneratorError(f"Preparation {field} must contain at least one item.")
        return [item.strip() for item in value]

    @staticmethod
    def _claim_key(kind, statement, limits, evidence):
        return f"preparation.{kind}.{hash_bytes(canonical_json({'statement': statement, 'limits': limits, 'families': sorted({ref['source_family_id'] for ref in evidence})}))[:20]}"

    @staticmethod
    def _conditions(kind, limits):
        value = " ".join(limits)
        if kind == "navigation":
            return "Grounded retrieval hint; it is not independent factual corroboration. " + value
        return value

    @staticmethod
    def _record_refs(records):
        leads, navigation = [], []
        for raw in records.values():
            record = decode_record(raw)
            ref = {"id": record["id"], "version": record["version"]}
            (leads if record["record_kind"] == "question" else navigation).append(ref)
        return leads, navigation

    @staticmethod
    def _validate_request_shape(request):
        if request["source_refs"] != sorted(request["source_refs"], key=lambda ref: ref["source_id"]):
            raise V2Error("invalid-request", "Preparation source references must be sorted")
        if len({ref["source_id"] for ref in request["source_refs"]}) != len(request["source_refs"]):
            raise V2Error("invalid-request", "Preparation source references must be unique")

    def _failed(self, run_id, capability, limitations, source_count, passage_count, lead_count):
        run = self.runs.status(run_id)
        if run.get("status") == "running":
            try:
                run = self.runs.finish(run_id, capability, status="failed", stop_reason="Preparation failed.")
            except V2Error:
                # ``finish`` may observe the wall-clock transition after the
                # status refresh.  Preserve the resulting partial run state.
                run = self.runs.status(run_id)
                if run.get("status") == "running":
                    raise
        return self._result(
            run_id,
            "failed",
            coverage=self._coverage(run, source_count, passage_count, lead_count, 0),
            limitations=limitations,
        )

    @staticmethod
    def _wire_status(status):
        return {"completed": "complete", "partial": "partial", "failed": "failed", "cancelled": "partial", "running": "running"}.get(status, "failed")

    @staticmethod
    def _coverage(run, selected, passages, leads, hints):
        usage = run.get("usage", {})
        return {
            "sources_selected": selected,
            "source_passages_read": passages,
            "returned_leads": leads,
            "returned_hints": hints,
            "operations": usage.get("operations", 0),
            "source_expansions": usage.get("source_expansions", 0),
        }

    @staticmethod
    def _result(run_id, status, *, lead_refs=None, navigation_refs=None, coverage=None, limitations=None, receipt=None, recovery_pending=False):
        result = {
            "run_id": run_id,
            "status": status if status in _PREPARATION_STATES else "failed",
            "lead_refs": lead_refs or [],
            "navigation_refs": navigation_refs or [],
            "coverage": coverage or {},
            "limitations": limitations or [],
        }
        if receipt is not None:
            result["receipt"] = receipt
        if recovery_pending:
            result["recovery_pending"] = True
        return result
