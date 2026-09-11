"""Rehearse the trusted v2 workflow using only generated synthetic material.

Run with PYTHONPATH=src .venv/bin/python scripts/v2_workflow_rehearsal.py.
Default: automatically cleaned temporary directory. --output NEW_DIRECTORY
retains the disposable vault, simulated events and report for inspection.
No real session discovery, live model, owner attestation or activation occurs.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from synapse.codex_events import CodexEvents, CodexSessionHost
from synapse.gateway import Gateway
from synapse.host_control import HostControl
from synapse.knowledge import decode_record
from synapse.owner_host import OwnerHost
from synapse.publication import Publisher, snapshot_fingerprint
from synapse.revisions import RevisionStore
from synapse.util import generate_ulid
from synapse.web_v2 import build_v2_area, build_v2_map

THREAD = "11111111-1111-4111-8111-111111111111"
NOTE = (
    "SYNTHETIC owner note: I spent 60 minutes on a paper-folding workshop. "
    "The assistant suggested daily workshops might suit me; that is its "
    "suggestion, not my stated preference."
)
CORRECTION = (
    "SYNTHETIC owner correction to the paper-folding note: I spent 20 minutes, "
    "not 60. One attempt does not establish a learning preference. "
    "The daily-workshop suggestion was the assistant's, not mine."
)
STATEMENTS = [
    "A second 20-minute paper-folding session may be a useful small experiment.",
    "Comparing a written guide with a workshop may help distinguish format from topic interest.",
]
BRIEF = (
    "SYNTHETIC review — two unreviewed assistant suggestions, based on the saved "
    "correction (20 minutes, not 60).\n"
    "1. Adopt as a possibility: " + STATEMENTS[0] + "\n"
    "2. Adopt as a possibility: " + STATEMENTS[1] + "\n"
    "One attempt cannot establish a preference. Adoption retains a hypothesis; "
    "it does not verify it or schedule work. Unselected suggestions stay unreviewed.\n"
    "Reply ‘approve first’ to adopt only 1."
)
LIMITATIONS = [
    "Synthetic mechanics only; no real owner comprehension or approval was assessed.",
    "Deterministic generators and comparison do not assess generative judgment quality.",
    "Correction is an explicitly saved follow-up source; the original stays retained.",
    "Map/source data adapters are checked; browser appearance and semantic readiness are not.",
    "No live-vault activation, durable host setup, or owner attestation is performed.",
]


def _check(condition, message):
    if not condition:
        raise RuntimeError(f"Synthetic rehearsal failed: {message}")


class SyntheticEvents:
    """Append simulated events when each workflow step actually happens."""

    def __init__(self, path):
        self.path = path
        self.entries = []

    def append(self, identity, role, text):
        stamp = datetime(2026, 9, 11, 10, tzinfo=UTC) + timedelta(seconds=len(self.entries))
        metadata = {"turn_id": f"synthetic-turn-{identity}"}
        if role == "user":
            metadata["content_item_kinds"] = ["user.text"]
        entry = {
            "timestamp": stamp.isoformat(), "type": "response_item",
            "payload": {
                "id": identity, "type": "message", "role": role,
                "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
                "internal_chat_message_metadata_passthrough": metadata,
            },
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        self.entries.append(entry)


def _step(method=None, arguments=None, *, answer="", findings=None, ids=None):
    return {
        "action": "read" if method else "finish",
        "calls": [{"method": method, "arguments_json": json.dumps(arguments)}] if method else [],
        "answer": answer, "used_record_ids": ids or [], "alternatives": [],
        "uncertainties": ["Synthetic single-attempt evidence cannot establish a preference."],
        "findings_json": json.dumps(findings or []),
        "stop_reason": "Synthetic retained evidence inspected." if not method else "",
    }


class SyntheticReasoner:
    """Explicit test generator; retrieves through the real Specialist gateway."""

    def prepare(self, state):
        return {"items": [{
            "record_kind": "question",
            "statement": "Synthetic lead: what would make the paper-folding experiment informative?",
            "support": ["The synthetic retained note describes one attempt."],
            "would_change_with": ["A comparable second attempt."],
            "evidence": [state["sources"][0]["evidence"]],
            "limits": ["Synthetic question only; no permission to investigate or adopted preference."],
        }]}

    def step(self, state, **_kwargs):
        observations = state["observations"]
        if not observations:
            return _step("search_sources", {"query": "paper-folding"})
        hits = observations[0]["response"]["items"]
        pages = [item["response"] for item in observations if item["method"] == "source"]
        if len(pages) < len(hits):
            hit = hits[len(pages)]
            return _step("source", {"source_id": hit["source_id"], "version": hit["source_version"]})
        corrected = any(page["text"] == CORRECTION for page in pages)
        answer = (
            "Synthetic saved owner account: 20 minutes, correcting the earlier 60."
            if corrected else "Synthetic saved owner account: 60 minutes (as originally recorded)."
        ) + " The daily-workshop idea is attributed to the assistant, not an owner preference."
        findings = []
        if state["request"]["mode"] != "consult":
            _check(corrected, "investigation must retrieve the saved correction")
            evidence = [page["evidence"] for page in pages]
            for index, statement in enumerate(STATEMENTS):
                findings.append({
                    "id": state["finding_ids"][index], "version": "0" * 64,
                    "subject_id": "me", "claim_key": f"synthetic-folding-{index}",
                    "record_kind": "hypothesis", "availability": "suggestion",
                    "review_status": "proposed",
                    "owner_review": {"status": "not-reviewed", "disposition": "none"},
                    "owner_position": "unreviewed", "lifecycle": "current",
                    "epistemic_basis": ["assistant-hypothesis"], "facets": ["learning"],
                    "statement": statement,
                    "conditions_and_limits": "Synthetic: corrected duration is 20 minutes, not 60; one attempt is not a preference.",
                    "support": "Synthetic owner note and explicit saved correction.",
                    "counterevidence": [], "alternatives": ["Topic interest may explain engagement."],
                    "would_change_with": ["Comparable future attempts."],
                    "evidence": evidence, "dependencies": [], "as_of": "2026-09-11",
                    "origin_run_id": state["run_id"],
                })
            answer += " Unreviewed assistant suggestions: " + " ".join(STATEMENTS)
        return _step(answer=answer, findings=findings)

    def review(self, value):
        # Deterministic comparison of this fixture's exact adoption-only delta.
        _check(value["brief"] == BRIEF, "exact synthetic review brief")
        _check(len(value["groups"]) == 2, "two independent review choices")
        for index, group in enumerate(value["groups"]):
            _check(len(group["changes"]) == 1, "one change per choice")
            change = group["changes"][0]
            before = decode_record(change["before"].encode())
            after = decode_record(change["after"].encode())
            _check(before["statement"] == STATEMENTS[index], "brief statement matches exact record")
            _check(after["availability"] == "accepted", "adoption availability")
            _check(after["owner_review"]["disposition"] == "adopted", "adoption disposition")
            for key in before.keys() - {"version", "availability", "owner_review"}:
                _check(before[key] == after[key], f"adoption preserves {key}")
        return {"passed": True, "proposal_version": value["proposal_version"], "reason": "Synthetic exact adoption-only comparison passed."}


class SuggestionReader:
    """Fresh consultation that uses suggestions without changing their state."""

    def __init__(self, identities):
        self.identities = identities

    def step(self, state, **_kwargs):
        if not state["observations"]:
            return _step("context", {"ids": self.identities, "knowledge_policy": "mixed"})
        records = {
            record["id"]: record
            for unit in state["observations"][0]["response"]["items"] for record in unit["records"]
        }
        answer = "SYNTHETIC possibilities, not verified preferences: " + " ".join(
            f"{records[identity]['statement']} [{records[identity]['availability']}; "
            f"{records[identity]['owner_review']['status']}]" for identity in self.identities
        )
        return _step(answer=answer, ids=self.identities)


def _new_output(output):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Refusing existing output; choose a new disposable directory")
    # Check ancestor markers without opening any vault content, evals or secrets.
    for parent in output.resolve().parents:
        if any((parent / marker).exists() for marker in ("_synapse", ".synapse", "entities")):
            raise ValueError("Refusing output inside an existing vault")
    output.mkdir()  # Exclusive creation; no recursive creation in arbitrary targets.
    return output


def run_rehearsal(output: Path) -> dict:
    output = _new_output(output)
    vault = output / "vault"
    store = RevisionStore(vault)
    records = {"entities/people/me.md": b"---\nid: me\ntype: person\nname: Synthetic Owner\nreview_status: proposed\n---\nSynthetic rehearsal only.\n"}
    capability = OwnerHost(store, host_id="synthetic-bootstrap").record_instruction(
        "synthetic-bootstrap", actions=["capture"],
        scope={"bootstrap": True, "snapshot_hash": snapshot_fingerprint(records, [])},
    )
    Publisher(vault).bootstrap(capability, operation_id=generate_ulid(), request_id=generate_ulid(), records=records)
    log = SyntheticEvents(output / "synthetic-events.jsonl")

    def host(reasoner=None):
        return CodexSessionHost(vault, CodexEvents([log.path]), reasoner=reasoner or SyntheticReasoner(), thread_id=THREAD)

    checkpoints = []

    def checkpoint(name):
        checkpoints.append({"step": name, "knowledge_revision": store.head(), "simulated_events": len(log.entries)})

    def consult(reasoner=None):
        before = store.head()
        runs = set((store.root / "runs").glob("*.json"))
        result = host(reasoner).consult("Synthetic fresh consultation of saved learning material.")
        _check(result["status"] == "completed", f"fresh consultation: {result}")
        _check(store.head() == before, "consultation must not publish")
        _check(set((store.root / "runs").glob("*.json")) == runs, "consultation must not start investigation")
        return result

    log.append("synthetic-note", "user", NOTE)
    log.append("synthetic-unsaved", "user", "SYNTHETIC ordinary conversation; do not retain this message.")
    log.append("synthetic-save", "user", "Save only the synthetic paper-folding note.")
    control = HostControl(host())
    saved = control.execute("capture-message", {"material_ref": "synthetic-note"}, owner_event_ref="synthetic-save")
    _check(saved["preparation_state"] == "complete" and saved["lead_refs"], "save prepares an unreviewed question")
    checkpoint("explicit-save")
    first_read = consult()
    _check("60 minutes" in first_read["answer"], "fresh consultation retrieves original note")
    checkpoint("fresh-attributed-read")
    old_gateway = Gateway(vault)

    log.append("synthetic-correction", "user", CORRECTION)
    log.append("synthetic-save-correction", "user", "Save this explicit correction alongside my synthetic original note.")
    corrected = control.execute("capture-message", {"material_ref": "synthetic-correction"}, owner_event_ref="synthetic-save-correction")
    _check(corrected["preparation_state"] == "complete", "correction preparation")
    correction_read = consult()
    _check("20 minutes" in correction_read["answer"], "fresh consultation uses correction")
    _check(len(old_gateway.search_sources("paper-folding")["items"]) == 1, "old revision remains pinned")
    checkpoint("saved-correction-and-fresh-read")

    log.append("synthetic-investigate", "user", "Investigate two small learning experiments using the synthetic note and correction; leave them as suggestions.")
    started = control.execute("start", {"purpose": "Synthetic learning experiments from the corrected note.", "subject_ids": ["me"]}, owner_event_ref="synthetic-investigate")
    checkpoint("requested-investigation-started")
    investigated = control.execute("execute-run", {"run_id": started["id"], "research": False})
    _check(investigated["status"] == "completed", f"investigation: {investigated}")
    identities = investigated["suggestion_ids"]
    _check(len(identities) == 2, "two admitted suggestions")
    suggestions_before = {identity: store.read_record(identity) for identity in identities}
    for record in suggestions_before.values():
        _check(record["availability"] == "suggestion" and record["owner_review"] == {"status": "not-reviewed", "disposition": "none"}, "suggestions start unreviewed")
    usable = consult(SuggestionReader(identities))
    _check("not-reviewed" in usable["answer"], "usable suggestions explicitly remain unreviewed")
    checkpoint("unreviewed-suggestions-used")

    manifest = store.manifest()
    groups = []
    for index, identity in enumerate(identities):
        row = manifest["records"][identity]
        start = BRIEF.index(f"{index + 1}. Adopt")
        end = BRIEF.index("\n", start)
        groups.append({
            "id": f"g{index + 1}", "requires": [],
            "effects": [{"id": f"adopt-{index + 1}", "kind": "mechanical", "meaning": BRIEF[start:end], "brief_span_start": start, "brief_span_end": end}],
            "changes": [{"kind": "replace-record", "target_id": identity, "path": row["path"], "before_version": row["version"], "raw": store.read_object(row["version"]).decode()}],
            "read_set": [], "source_preconditions": [],
        })
    staged = control.execute("stage", {"run_id": started["id"], "brief": BRIEF, "groups": groups})
    _check(store.head() == usable["knowledge_revision"], "staging does not adopt")
    log.append("synthetic-brief", "assistant", BRIEF)
    shown = control.execute("bind-display", {"proposal_id": staged["proposal_id"], "version": staged["version"], "assistant_event_ref": "synthetic-brief"})
    checkpoint("exact-brief-displayed")
    log.append("synthetic-reply", "user", "approve first")
    adopted = control.execute("reply", {"display_id": shown["display_id"]}, owner_event_ref="synthetic-reply")
    _check(adopted["receipt"]["selected_group_ids"] == ["g1"], "only first choice adopted")
    final_records = {identity: store.read_record(identity) for identity in identities}
    _check(final_records[identities[0]]["availability"] == "accepted", "selected adoption")
    _check(final_records[identities[1]] == suggestions_before[identities[1]], "unselected suggestion preserved exactly")
    for record in final_records.values():
        _check(record["review_status"] == "proposed" and record["owner_position"] == "unreviewed", "adoption is not attestation or a stated owner preference")
        _check(record["epistemic_basis"] == ["assistant-hypothesis"], "assistant attribution survives adoption")
    checkpoint("selected-adoption")
    final_read = consult(SuggestionReader(identities))

    source_pages = []
    for capture, text, event in ((saved, NOTE, "synthetic-note"), (corrected, CORRECTION, "synthetic-correction")):
        ref = capture["source_refs"][0]
        page = Gateway(vault).source(ref["source_id"], version=ref["source_version"])
        descriptor = store.manifest()["source_versions"][ref["source_version"]]
        _check(page["text"] == text, "exact retained source visible")
        _check(descriptor["origin"] == f"conversation:codex:{THREAD}:user:{event}", "source event attribution")
        source_pages.append(page)
    _check(len(store.manifest()["sources"]) == 2, "only explicitly selected messages saved")

    scene = build_v2_map(vault)
    nodes = []
    area_ids = [node["id"] for node in scene["nodes"]] + ["loose"]
    for area_id in area_ids:
        area = build_v2_area(vault, area_id, organization_revision=scene["organization_revision"], revision=scene["knowledge_revision"], limit=151)
        _check(area["page"]["next_offset"] is None, "small synthetic map fits one area page")
        nodes.extend(area["nodes"])
    visible_sources = {node["ref"].get("source_id") for node in nodes if node["kind"] == "source"}
    visible_records = {node["ref"].get("record_id") for node in nodes if node["kind"] == "record"}
    _check({page["source_id"] for page in source_pages} <= visible_sources, "both saved sources visible on map")
    _check(set(identities) <= visible_records, "accepted and unselected suggestions visible on map")
    checkpoint("fresh-read-map-and-sources")
    report = {
        "synthetic": True, "status": "passed", "vault": str(vault),
        "checkpoints": checkpoints, "brief": BRIEF, "simulated_reply": "approve first",
        "captures": [saved, corrected], "first_read": first_read, "correction_read": correction_read,
        "investigation": investigated, "suggestion_use": usable, "final_read": final_read,
        "suggestions_before": suggestions_before, "final_records": final_records,
        "proposal": staged, "display": shown, "adoption_receipt": adopted["receipt"],
        "source_pages": source_pages, "map": {"scene": scene, "material_nodes": nodes},
        "limitations": LIMITATIONS,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="new disposable output directory; must not exist")
    args = parser.parse_args(argv)
    try:
        if args.output:
            result = run_rehearsal(args.output)
            print(f"Synthetic rehearsal passed. Report: {args.output.absolute() / 'report.json'}")
        else:
            with tempfile.TemporaryDirectory(prefix="synapse-synthetic-rehearsal-") as temporary:
                result = run_rehearsal(Path(temporary) / "rehearsal")
            print("Synthetic rehearsal passed; temporary vault removed.")
    except (ValueError, FileExistsError) as exc:
        parser.error(str(exc))
    print(result["brief"])
    print("Simulated later reply: " + result["simulated_reply"])
    print("\n".join(result["limitations"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
