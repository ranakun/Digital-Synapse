"""Hybrid FTS + semantic search with RRF fusion."""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from synapse.embeddings import cosine, default_embedder, stored_models
from synapse.index import connect, ensure_schema, reindex
from synapse.queries import _fts_query
from synapse.querylog import append as log_append
from synapse.util import blob_to_vector

# Module-level flag: None = not yet determined; True/False = determined.
_FTS_HAS_RANK: bool | None = None
_TEXT_RRF_WEIGHT = 1.0
_SEMANTIC_RRF_WEIGHT = 0.25

_ENTITY_TYPE_ALIASES = {
    "people": "person",
    "persons": "person",
    "companies": "company",
    "opportunities": "opportunity",
    "conversations": "conversation",
    "events": "event",
    "skills": "skill",
    "projects": "project",
    "goals": "goal",
    "finances": "finance",
    "insights": "insight",
}

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
_TERM_ALIASES = {"go": ("go", "golang")}
_STOPWORDS = {
    "a",
    "about",
    "after",
    "all",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "can",
    "did",
    "do",
    "for",
    "from",
    "have",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "out",
    "the",
    "they",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
    "with",
}


def _embed_query(text: str, emb: Any) -> tuple[list[float] | None, str | None]:
    """Embed a query, relying on the caller's embedder for hard time bounds."""
    try:
        return emb.embed([text])[0], None
    except TimeoutError:
        print(
            "semantic search unavailable: embedding timeout; running text-only",
            file=sys.stderr,
        )
        return None, "timeout"
    except Exception as exc:
        print(
            f"semantic search unavailable: embedding error ({type(exc).__name__});"
            " running text-only",
            file=sys.stderr,
        )
        return None, "error"


def _snippet(body: str | None, query: str, max_len: int = 120) -> str:
    """Return a short snippet from body containing a query token."""
    if not body:
        return ""
    tokens = [t.lower() for t in query.split() if t]
    # Try to find a position containing any token
    body_lower = body.lower()
    best_pos = len(body)
    for token in tokens:
        pos = body_lower.find(token)
        if pos != -1 and pos < best_pos:
            best_pos = pos
    if best_pos < len(body):
        start = max(0, best_pos - 20)
        return body[start : start + max_len]
    return body[:max_len]


def _query_terms(query: str) -> list[str]:
    terms = []
    for token in _TOKEN_RE.findall(query.casefold()):
        if token in _STOPWORDS:
            continue
        if len(token) <= 1 and not token.isdigit():
            continue
        terms.append(token)
    return terms


def _query_phrases(query: str) -> list[str]:
    raw = [token.casefold() for token in _TOKEN_RE.findall(query)]
    phrases: list[str] = []
    for size in (4, 3, 2):
        for index in range(0, max(0, len(raw) - size + 1)):
            phrase_terms = raw[index : index + size]
            if all(term in _STOPWORDS for term in phrase_terms):
                continue
            phrase = " ".join(phrase_terms)
            if len(phrase) > 4 and phrase not in phrases:
                phrases.append(phrase)
    return phrases


def _entity_search_parts(row: Any) -> tuple[str, str, str]:
    name = str(row["name"] or "").casefold()
    body = str(row["body"] or "").casefold()
    frontmatter = ""
    try:
        fm = json.loads(row["frontmatter"] or "{}")
    except (TypeError, json.JSONDecodeError):
        fm = {}
    if isinstance(fm, dict):
        tags = " ".join(str(tag) for tag in (fm.get("tags") or []))
        props = json.dumps(fm.get("properties") or {}, ensure_ascii=False)
        aliases = " ".join(str(alias) for alias in (fm.get("aliases") or []))
        frontmatter = f"{tags} {props} {aliases}".casefold()
    return name, frontmatter, body


def _term_count(text: str, term: str) -> int:
    """Count short terms as words and include common source-language aliases."""
    tokens: list[str] | None = None
    count = 0
    for candidate in _TERM_ALIASES.get(term, (term,)):
        if len(candidate) <= 2:
            tokens = tokens or _TOKEN_RE.findall(text)
            count += tokens.count(candidate)
        else:
            count += text.count(candidate)
    return count


def _lexical_score(row: Any, query: str, terms: list[str], phrases: list[str]) -> int:
    name, frontmatter, body = _entity_search_parts(row)
    haystack = f"{name} {frontmatter} {body}"
    score = 0

    normalized_query = " ".join(_TOKEN_RE.findall(query.casefold()))
    exact_query_match = normalized_query in haystack
    if len(normalized_query) <= 2:
        exact_query_match = bool(_term_count(haystack, normalized_query))
    if normalized_query and exact_query_match:
        score += 40

    for phrase in phrases[:80]:
        if phrase in name:
            score += 28
        if phrase in frontmatter:
            score += 14
        if phrase in body:
            score += 10

    for term in terms:
        if _term_count(name, term):
            score += 12
        if _term_count(frontmatter, term):
            score += 5
        score += min(4, _term_count(body, term))

    query_lower = query.casefold()
    try:
        fm = json.loads(row["frontmatter"] or "{}")
    except (TypeError, json.JSONDecodeError):
        fm = {}
    tags = set(fm.get("tags") or []) if isinstance(fm, dict) else set()

    if ("company" in query_lower or "companies" in query_lower) and row["type"] == "company":
        score += 10
        if any(_term_count(name, term) for term in terms):
            score += 18
    if "recruiter" in query_lower and "recruiter" in tags:
        score += 8
    if ("opportunity" in query_lower or "role" in query_lower) and row["type"] == "opportunity":
        score += 4
    if ("conversation" in query_lower or "reached out" in query_lower) and row["type"] == "conversation":
        score += 4

    return score


def _lexical_hits(
    conn: Any,
    query: str,
    limit: int = 80,
    entity_type: str | None = None,
) -> list[dict[str, Any]]:
    terms = _query_terms(query)
    phrases = _query_phrases(query)
    if not terms and not phrases:
        return []

    hits = []
    if entity_type:
        rows = conn.execute("SELECT * FROM entities WHERE type = ?", (entity_type,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM entities").fetchall()
    for row in rows:
        score = _lexical_score(row, query, terms, phrases)
        if score <= 0:
            continue
        hits.append({"row": row, "snippet": _snippet(row["body"] or "", query), "score": score})
    hits.sort(key=lambda item: (-item["score"], item["row"]["type"], item["row"]["name"]))
    return hits[:limit]


def hybrid_search(
    vault: str | Path | None,
    query: str,
    *,
    limit: int = 15,
    text_only: bool = False,
    semantic_only: bool = False,
    embedder: Any = None,
    semantic_index: Any = None,
    entity_type: str | None = None,
    _reindex: bool = True,
) -> dict[str, Any]:
    """Run hybrid FTS + semantic search and fuse results with RRF.

    Returns a dict with keys:
        results: list of result dicts (id, name, type, snippet, legs, score)
        semantic: "ok" | "refused:<reason>"
        query: the query string
    """
    global _FTS_HAS_RANK  # noqa: PLW0603

    t_start = time.perf_counter()
    if entity_type:
        entity_type = _ENTITY_TYPE_ALIASES.get(entity_type.strip().casefold(), entity_type)

    if _reindex:
        reindex(vault)

    conn = connect(vault)
    try:
        if _reindex:
            ensure_schema(conn)

        # ------------------------------------------------------------------ #
        # Text leg                                                             #
        # ------------------------------------------------------------------ #
        text_hits: list[dict[str, Any]] = []  # ordered list, index = rank-1
        if not semantic_only:
            fts_expr = _fts_query(query)
            if fts_expr:
                # Determine whether ORDER BY rank works (detect once per process)
                if _FTS_HAS_RANK is None:
                    try:
                        conn.execute(
                            "SELECT rank FROM entities_fts WHERE entities_fts MATCH ? LIMIT 1",
                            (fts_expr,),
                        ).fetchone()
                        _FTS_HAS_RANK = True
                    except Exception:
                        _FTS_HAS_RANK = False

                try:
                    if _FTS_HAS_RANK:
                        sql = (
                            "SELECT e.*, f.rank FROM entities_fts f"
                            " JOIN entities e ON e.rowid = f.rowid"
                            " WHERE entities_fts MATCH ?"
                            " ORDER BY rank LIMIT 50"
                        )
                    else:
                        sql = (
                            "SELECT e.*, bm25(entities_fts) AS rank FROM entities_fts f"
                            " JOIN entities e ON e.rowid = f.rowid"
                            " WHERE entities_fts MATCH ?"
                            " ORDER BY rank LIMIT 50"
                        )
                    params: tuple[str, ...] = (fts_expr,)
                    if entity_type:
                        sql = sql.replace(" ORDER BY rank", " AND e.type = ? ORDER BY rank")
                        params = (fts_expr, entity_type)
                    rows = conn.execute(sql, params).fetchall()
                    for row in rows:
                        body = row["body"] or ""
                        text_hits.append(
                            {
                                "row": row,
                                "snippet": _snippet(body, query),
                            }
                        )
                except Exception:
                    # FTS error — leave text_hits empty
                    pass

            combined_text_hits: dict[str, dict[str, Any]] = {}
            for rank, hit in enumerate(text_hits):
                combined_text_hits[hit["row"]["id"]] = {
                    **hit,
                    "fts_rank": rank,
                    "lexical_rank": None,
                    "lexical_score": 0,
                }

            for rank, hit in enumerate(_lexical_hits(conn, query, entity_type=entity_type)):
                eid = hit["row"]["id"]
                if eid in combined_text_hits:
                    combined_text_hits[eid]["lexical_rank"] = rank
                    combined_text_hits[eid]["lexical_score"] = hit["score"]
                    if not combined_text_hits[eid].get("snippet"):
                        combined_text_hits[eid]["snippet"] = hit["snippet"]
                    continue
                combined_text_hits[eid] = {
                    **hit,
                    "fts_rank": None,
                    "lexical_rank": rank,
                    "lexical_score": hit["score"],
                }

            def _text_hit_sort(hit: dict[str, Any]) -> tuple[int, int]:
                lexical_rank = hit.get("lexical_rank")
                if lexical_rank is not None:
                    return (0, int(lexical_rank))
                fts_rank = hit.get("fts_rank")
                return (1, int(fts_rank) if fts_rank is not None else 1_000_000)

            text_hits = sorted(combined_text_hits.values(), key=_text_hit_sort)[:80]

        # ------------------------------------------------------------------ #
        # Semantic leg                                                         #
        # ------------------------------------------------------------------ #
        # semantic status ∈ {ok, refused:hash-fallback, refused:model-mismatch, refused:timeout}
        semantic_status = "ok"
        semantic_hits: list[dict[str, Any]] = []  # ordered list, index = rank-1

        if not text_only:
            emb = embedder or default_embedder(vault)

            if emb.model == "hash-local-test":
                semantic_status = "refused:hash-fallback"
                print(
                    "semantic search unavailable: hash-fallback embeddings;"
                    " install the embeddings extra and run `synapse embed --all`",
                    file=sys.stderr,
                )
            else:
                # Check stored model set
                db_models = stored_models(conn)
                if db_models != {emb.model}:
                    semantic_status = "refused:model-mismatch"
                    print(
                        "semantic search unavailable: stored model mismatch;"
                        " run `synapse embed --all` to re-embed with the current model",
                        file=sys.stderr,
                    )
                else:
                    # E5 prefix heuristic
                    query_text = f"query: {query}" if "e5" in emb.model else query
                    query_vec, refusal = _embed_query(query_text, emb)
                    if query_vec is not None:
                        if semantic_index is not None:
                            try:
                                semantic_hits = semantic_index.search(
                                    query_vec,
                                    model=emb.model,
                                    entity_type=entity_type,
                                    limit=50,
                                )
                            except Exception as exc:
                                print(
                                    "semantic search unavailable: vector index error "
                                    f"({type(exc).__name__}); running text-only",
                                    file=sys.stderr,
                                )
                                semantic_status = "refused:index-error"
                        else:
                            sql = (
                                "SELECT e.id, e.name, e.type, e.body, emb.vector"
                                " FROM embeddings emb"
                                " JOIN entities e ON e.id = emb.entity_id"
                            )
                            params = ()
                            if entity_type:
                                sql += " WHERE e.type = ?"
                                params = (entity_type,)
                            emb_rows = conn.execute(sql, params).fetchall()
                            scored = []
                            for emb_row in emb_rows:
                                vec = blob_to_vector(emb_row["vector"])
                                score = cosine(query_vec, vec)
                                scored.append((emb_row, score))
                            scored.sort(key=lambda x: x[1], reverse=True)
                            semantic_hits = [
                                {"row": row, "score": score}
                                for row, score in scored[:50]
                            ]
                    else:
                        semantic_status = f"refused:{refusal}"

        # ------------------------------------------------------------------ #
        # RRF fusion                                                           #
        # ------------------------------------------------------------------ #
        # score dict: entity_id -> cumulative RRF score
        # legs dict: entity_id -> set of legs
        # snippet dict: entity_id -> snippet string (from text leg)
        # entity_row dict: entity_id -> sqlite row (for row_to_entity)
        scores: dict[str, float] = {}
        legs: dict[str, list[str]] = {}
        snippets: dict[str, str] = {}
        entity_rows: dict[str, Any] = {}

        for rank0, hit in enumerate(text_hits):
            eid = hit["row"]["id"]
            rrf = _TEXT_RRF_WEIGHT / (60 + rank0 + 1)
            scores[eid] = scores.get(eid, 0.0) + rrf
            if eid not in legs:
                legs[eid] = []
            if "text" not in legs[eid]:
                legs[eid].append("text")
            if eid not in snippets:
                snippets[eid] = hit["snippet"]
            if eid not in entity_rows:
                entity_rows[eid] = hit["row"]

        for rank0, hit in enumerate(semantic_hits):
            eid = hit["row"]["id"]
            rrf = _SEMANTIC_RRF_WEIGHT / (60 + rank0 + 1)
            scores[eid] = scores.get(eid, 0.0) + rrf
            if eid not in legs:
                legs[eid] = []
            if "semantic" not in legs[eid]:
                legs[eid].append("semantic")
            if eid not in entity_rows:
                entity_rows[eid] = hit["row"]
            if eid not in snippets:
                snippets[eid] = _snippet(hit["row"]["body"] or "", query)

        # Sort by score descending, take limit
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]

        results: list[dict[str, Any]] = []
        for eid, score in ranked:
            row = entity_rows[eid]
            participants: list[dict[str, str]] = []
            if row["type"] == "conversation":
                participant_rows = conn.execute(
                    """
                    SELECT e.id, e.name
                    FROM relations r
                    JOIN entities e ON e.id = CASE
                        WHEN r.from_id = ? THEN r.to_id ELSE r.from_id END
                    WHERE r.type = 'participated_in'
                      AND (r.from_id = ? OR r.to_id = ?)
                      AND e.type = 'person' AND e.id != 'me'
                    ORDER BY e.name, e.id
                    """,
                    (eid, eid, eid),
                ).fetchall()
                participants = [
                    {"id": item["id"], "name": item["name"]} for item in participant_rows
                ]
            results.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "type": row["type"],
                    "snippet": snippets.get(eid, ""),
                    "legs": legs.get(eid, []),
                    "score": score,
                    "participants": participants,
                }
            )

    finally:
        conn.close()

    duration_ms = int((time.perf_counter() - t_start) * 1000)

    log_append(
        vault,
        {
            "iface": "cli",
            "op": "search",
            "params": {
                "q": query,
                "limit": limit,
                "text_only": text_only,
                "semantic_only": semantic_only,
                "entity_type": entity_type,
            },
            "result_count": len(results),
            "duration_ms": duration_ms,
            "zero_hit": len(results) == 0,
            "fallback": None,
            "semantic": semantic_status,
        },
    )

    return {
        "results": results,
        "semantic": semantic_status,
        "query": query,
    }
