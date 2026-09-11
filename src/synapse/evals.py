"""Evaluation runner for golden-question fixture queries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

try:
    import pysqlite3 as sqlite3
except Exception:
    import sqlite3

from synapse.brief import build_entity_brief, build_owner_brief
from synapse.dossier import DEFAULT_DOSSIER_BUDGET_TOKENS, build_dossier
from synapse.index import connect, reindex, resolve_entity_ref
from synapse.owner_context import build_owner_context
from synapse.queries import filter_entities, find_entities, neighbors, path_between
from synapse.search import hybrid_search
from synapse.warmpath import rank_connectors


class QuestionResult:
    def __init__(
        self,
        question_id: str,
        question: str,
        passed: bool,
        error: str | None = None,
        warnings: list[str] | None = None,
        details: str | None = None,
    ) -> None:
        self.question_id = question_id
        self.question = question
        self.passed = passed
        self.error = error
        self.warnings = warnings or []
        self.details = details or ""


def resolve_merged_id(conn: sqlite3.Connection, expected_id: str) -> tuple[str, str | None]:
    """Recursively resolve merged_into pointers in entity frontmatter."""
    current_id = expected_id
    path = []
    while True:
        row = conn.execute(
            "SELECT frontmatter FROM entities WHERE id = ?", (current_id,)
        ).fetchone()
        if not row or not row["frontmatter"]:
            break
        try:
            fm = json.loads(row["frontmatter"])
            merged_into = fm.get("merged_into")
            if merged_into and merged_into != current_id:
                path.append(current_id)
                if merged_into in path:  # cycle detection
                    break
                current_id = merged_into
            else:
                break
        except Exception:
            break
    if current_id != expected_id:
        return current_id, f"Warning: expected ID {expected_id} has been merged into {current_id}"
    return expected_id, None


def load_questions_text(text: str) -> list[dict[str, Any]]:
    """Parse and validate evaluation questions from one immutable text snapshot."""
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        raise ValueError(f"Failed to parse YAML: {exc}") from exc

    if not isinstance(data, list):
        raise ValueError("Questions file must be a YAML list")

    for idx, q in enumerate(data):
        if not isinstance(q, dict):
            raise ValueError(f"Question at index {idx} is not a dictionary")
        if "id" not in q or not q["id"]:
            raise ValueError(f"Question at index {idx} is missing an 'id'")
        if "question" not in q or not q["question"]:
            raise ValueError(f"Question {q.get('id', idx)} is missing 'question'")
        via = q.get("via")
        if via not in {"search", "find", "neighbors", "path", "filter", "brief", "dossier", "warm-path", "owner-context"}:
            raise ValueError(f"Question {q['id']} has invalid or missing 'via': {via}")

    return data


def load_questions(path: Path) -> list[dict[str, Any]]:
    """Load and validate evaluation questions from a YAML file."""
    if not path.exists():
        raise FileNotFoundError(f"Questions file not found: {path}")
    return load_questions_text(path.read_text(encoding="utf-8"))


def run_question(conn: sqlite3.Connection, vault: Path, q: dict[str, Any]) -> QuestionResult:
    """Run a single evaluation question and check expectations."""
    question_id = q["id"]
    question_text = q["question"]
    via = q["via"]
    params = q.get("params") or {}
    expect = q.get("expect") or {}

    warnings: list[str] = []
    actual_entities: list[dict[str, Any]] = []
    brief_output: str | None = None

    try:
        if via == "owner-context":
            target_id = None
            if params.get("target"):
                matches = resolve_entity_ref(conn, params["target"])
                if len(matches) != 1:
                    raise ValueError("owner-context target must resolve to exactly one entity")
                target_id = matches[0]["id"]
            brief_output = build_owner_context(
                conn, facet=params.get("facet"), target_id=target_id,
                as_of=params.get("as_of"), budget_chars=params.get("budget", 8000),
            )
            # Count only explicit cited IDs, never hidden source bodies.
            import re

            ids = set(re.findall(r"`([0-9A-HJKMNP-TV-Z]{26}|me)`", brief_output))
            actual_entities = [{"id": entity_id} for entity_id in sorted(ids)]

        elif via == "search":
            query = params.get("query")
            if not query:
                raise ValueError("search operation requires 'query' param")
            limit = params.get("limit", 15)
            # Pass extra params if any
            extra = {k: v for k, v in params.items() if k not in {"query", "limit"}}
            res = hybrid_search(vault, query, limit=limit, _reindex=False, **extra)
            actual_entities = res.get("results", [])

        elif via == "find":
            text = params.get("text")
            if not text:
                raise ValueError("find operation requires 'text' param")
            limit = params.get("limit", 20)
            actual_entities = find_entities(vault, text, limit=limit, reindex=False)

        elif via == "neighbors":
            start_ref = params.get("start")
            if not start_ref:
                raise ValueError("neighbors operation requires 'start' param")
            matches = resolve_entity_ref(conn, start_ref)
            if not matches:
                raise ValueError(f"Could not resolve neighbors start ref: {start_ref}")
            start_id = matches[0]["id"]
            depth = params.get("depth", 1)
            rel_types = params.get("relation_types")
            undirected = params.get("undirected", False)
            include_weak = params.get("include_weak", False)
            query_res = neighbors(
                vault,
                start_id,
                depth=depth,
                relation_types=rel_types,
                undirected=undirected,
                include_weak=include_weak,
                reindex=False,
            )
            actual_entities = query_res.nodes

        elif via == "path":
            start_ref = params.get("start")
            end_ref = params.get("end")
            if not start_ref or not end_ref:
                raise ValueError("path operation requires 'start' and 'end' params")
            start_matches = resolve_entity_ref(conn, start_ref)
            end_matches = resolve_entity_ref(conn, end_ref)
            if not start_matches:
                raise ValueError(f"Could not resolve path start ref: {start_ref}")
            if not end_matches:
                raise ValueError(f"Could not resolve path end ref: {end_ref}")
            start_id = start_matches[0]["id"]
            end_id = end_matches[0]["id"]
            max_hops = params.get("max_hops", 4)
            include_weak = params.get("include_weak", False)
            all_paths = params.get("all", False)
            undirected = params.get("undirected", True)
            query_res = path_between(
                vault,
                start_id,
                end_id,
                max_hops=max_hops,
                include_weak=include_weak,
                all_paths=all_paths,
                undirected=undirected,
                reindex=False,
            )
            actual_entities = query_res.nodes

        elif via == "filter":
            entity_type = params.get("type")
            tag = params.get("tag")
            property_key = params.get("property")
            property_value = params.get("value")
            actual_entities = filter_entities(
                vault,
                entity_type=entity_type,
                tag=tag,
                property_key=property_key,
                property_value=property_value,
                reindex=False,
            )

        elif via == "brief":
            ref = params.get("ref")
            budget = params.get("budget", 8000)
            if ref:
                matches = resolve_entity_ref(conn, ref)
                if not matches:
                    raise ValueError(f"Could not resolve brief ref: {ref}")
                entity_id = matches[0]["id"]
                entity_budget = budget if budget != 8000 else 4000
                brief_output = build_entity_brief(conn, entity_id, budget_tokens=entity_budget)
            else:
                brief_output = build_owner_brief(conn, budget_tokens=budget)

        elif via == "dossier":
            ref = params.get("ref")
            if not ref:
                raise ValueError("dossier operation requires 'ref' param")
            matches = resolve_entity_ref(conn, ref)
            if not matches:
                raise ValueError(f"Could not resolve dossier ref: {ref}")
            entity_id = matches[0]["id"]
            budget = params.get("budget", DEFAULT_DOSSIER_BUDGET_TOKENS)
            brief_output = build_dossier(conn, entity_id, budget_tokens=budget)

        elif via == "warm-path":
            ref = params.get("ref")
            if not ref:
                raise ValueError("warm-path operation requires 'ref' param")
            matches = resolve_entity_ref(conn, ref)
            if not matches:
                raise ValueError(f"Could not resolve warm-path ref: {ref}")
            entity_id = matches[0]["id"]
            limit = int(params.get("limit", 10))
            ranked = rank_connectors(conn, entity_id, limit=limit)
            # Expose candidates for any_ids/all_ids checks and render Markdown
            # (with evidence) for text/forbid substring checks — one call, both.
            actual_entities = [{"id": c["id"], "name": c["name"]} for c in ranked]
            lines = [f"Warm paths into `{entity_id}`", ""]
            for c in ranked:
                lines.append(
                    f"- {c['name']} `{c['id']}` — score {c['score']} — {'; '.join(c['evidence'])}"
                )
            brief_output = "\n".join(lines)

    except Exception as exc:
        return QuestionResult(
            question_id=question_id,
            question=question_text,
            passed=False,
            error=str(exc),
        )

    # Compile text representation for checking substrings
    text_content = ""
    if brief_output is not None:
        text_content = brief_output
    else:
        text_parts = []
        for e in actual_entities:
            text_parts.append(e.get("name", ""))
            eid = e.get("id")
            if eid:
                text_parts.append(eid)
                row = conn.execute(
                    "SELECT body, frontmatter FROM entities WHERE id = ?", (eid,)
                ).fetchone()
                if row:
                    if row["body"]:
                        text_parts.append(row["body"])
                    if row["frontmatter"]:
                        text_parts.append(row["frontmatter"])
        text_content = "\n".join(text_parts)

    actual_ids = [e["id"] for e in actual_entities if "id" in e]
    max_results_checked = expect.get("max_results_checked")
    if max_results_checked is not None:
        try:
            max_results_checked = int(max_results_checked)
            actual_ids = actual_ids[:max_results_checked]
        except (ValueError, TypeError):
            pass

    # Expectation checks
    failures: list[str] = []

    # 1. any_ids expectation
    any_ids = expect.get("any_ids")
    if any_ids:
        resolved_any = []
        for expected_id in any_ids:
            res_id, warn = resolve_merged_id(conn, expected_id)
            if warn:
                warnings.append(warn)
            resolved_any.append(res_id)
        if not any(rid in actual_ids for rid in resolved_any):
            failures.append(
                f"None of the expected any_ids {any_ids} (resolved: {resolved_any}) "
                f"found in actual results: {actual_ids}"
            )

    # 2. all_ids expectation
    all_ids = expect.get("all_ids")
    if all_ids:
        resolved_all = []
        for expected_id in all_ids:
            res_id, warn = resolve_merged_id(conn, expected_id)
            if warn:
                warnings.append(warn)
            resolved_all.append(res_id)
        missing = [rid for rid in resolved_all if rid not in actual_ids]
        if missing:
            failures.append(
                f"Missing expected all_ids {missing} from actual results: {actual_ids}"
            )

    # 3. text expectation (substrings must appear)
    text_expectations = expect.get("text")
    if text_expectations:
        if isinstance(text_expectations, str):
            text_expectations = [text_expectations]
        for t in text_expectations:
            if t not in text_content:
                failures.append(f"Expected substring {t!r} not found in output")

    # 4. forbid expectation (substrings must NOT appear)
    forbid_expectations = expect.get("forbid")
    if forbid_expectations:
        if isinstance(forbid_expectations, str):
            forbid_expectations = [forbid_expectations]
        for t in forbid_expectations:
            if t in text_content:
                failures.append(f"Forbidden substring {t!r} was found in output")

    if failures:
        details = "; ".join(failures)
        return QuestionResult(
            question_id=question_id,
            question=question_text,
            passed=False,
            warnings=warnings,
            details=details,
        )

    return QuestionResult(
        question_id=question_id,
        question=question_text,
        passed=True,
        warnings=warnings,
    )


def run_all(
    vault: Path, questions: list[dict[str, Any]]
) -> tuple[list[QuestionResult], dict[str, Any]]:
    """Run all loaded questions against the vault, return results and summary."""
    reindex(vault)
    conn = connect(vault)
    results = []
    try:
        for q in questions:
            res = run_question(conn, vault, q)
            results.append(res)
    finally:
        conn.close()

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    ids_failed = [r.question_id for r in results if not r.passed]

    summary = {
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pass": passed,
        "fail": failed,
        "ids_failed": ids_failed,
    }
    return results, summary


def append_history(vault: Path, summary: dict[str, Any]) -> None:
    """Append a summary run record to the vault's eval history file."""
    evals_dir = vault / "evals"
    evals_dir.mkdir(parents=True, exist_ok=True)
    history_file = evals_dir / "history.jsonl"

    is_new = not history_file.exists()

    # Serialize summary line
    line = json.dumps(summary, ensure_ascii=False) + "\n"

    with open(history_file, "a", encoding="utf-8") as f:
        if is_new:
            f.write(
                "# expectations are owner-maintained; agents must not edit questions to make evals pass.\n"
            )
        f.write(line)
