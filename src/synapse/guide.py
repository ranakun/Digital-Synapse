"""Generated vault AGENTS.md — orientation document for models with vault access."""

from __future__ import annotations

from pathlib import Path

try:
    import pysqlite3 as sqlite3
except Exception:  # pragma: no cover
    import sqlite3  # type: ignore[no-redef]

from synapse.index import connect, reindex
from synapse.models import ENTITY_TYPES, RELATION_TYPES
from synapse.util import utc_now


def _live_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT type, COUNT(*) AS c FROM entities GROUP BY type").fetchall()
    return {row["type"]: row["c"] for row in rows}


def _relation_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT type, COUNT(*) AS c FROM relations WHERE weak = 0 GROUP BY type"
    ).fetchall()
    return {row["type"]: row["c"] for row in rows}


def _section_what(counts: dict[str, int]) -> str:
    total = sum(counts.values())
    return f"""\
## 1. What This Vault Is

This is a Digital Synapse personal knowledge graph — a private, owner-controlled
collection of Markdown entity files. It currently holds **{total} entities**.
The canonical source of truth is the Markdown files under `entities/`; the SQLite
index at `.synapse/index.db` is a derived, disposable cache rebuilt from those
files. Never modify the index directly — edit the Markdown files instead.

Regenerate the index: `synapse reindex --full`"""


def _section_entity_types(counts: dict[str, int], rel_counts: dict[str, int]) -> str:
    lines = ["## 2. Entity Types and Relation Vocabulary\n"]
    lines.append("### Entity Types (live counts)\n")
    for etype in sorted(ENTITY_TYPES):
        c = counts.get(etype, 0)
        lines.append(f"- `{etype}`: {c}")
    lines.append("\n### Relation Vocabulary (live counts, strong edges only)\n")
    lines.append(
        "Direction is owner-relative by default (outgoing from the declaring entity). "
        "Use `direction: incoming` in frontmatter to flip the edge. "
        "Directed traversal from non-owner nodes sees little — prefer `--undirected`."
    )
    lines.append("")
    for rtype in sorted(RELATION_TYPES):
        c = rel_counts.get(rtype, 0)
        lines.append(f"- `{rtype}`: {c}")
    return "\n".join(lines)


def _section_conventions() -> str:
    return """\
## 3. Conventions

- **IDs**: 26-character ULIDs (e.g., `01J00000000000000000000001`). The owner entity
  has the stable id `me`. Never create human-readable IDs; always use ULIDs.
- **File paths**: `entities/<type-folder>/<slug>.md`. The ULID in frontmatter is
  canonical; the filename is not.
- **Wikilinks**: `[[slug]]` or `[[slug|Display]]` resolve to entities by filename stem.
  These are weak links (type `mentioned_in`); prefer typed `relations:` frontmatter
  for structured edges.
- **Frontmatter keys**: `id`, `type`, `name`, `aliases`, `review_status`, `tags`,
  `relations`, `properties`, `created_at`, `updated_at`. The `provenance` block
  records machine-extraction origin (`source_file`, `extracted_by`, `extracted_at`).
- **Managed sections**: Sections between `<!-- synapse:managed -->` markers are
  rewritten by importers. Edit outside those markers only.
- **Owner entity**: `entities/people/me.md` with `id: me` and tag `owner`.
  Relations from `me` are the backbone of every query."""


def _section_trust() -> str:
    return """\
## 4. Trust Semantics

Every entity has `review_status: proposed | verified`.

- **`proposed`** — written by an importer, extractor, or applied proposal; not yet
  attested by the owner. Treat as *probably-true-but-cite-provenance*. The vast
  majority of entities will be `proposed` forever — that is expected and fine.
- **`verified`** — the owner has looked at this entity and attests it is correct.
  A human attestation set **only** by `synapse verify`. Agents and `apply` never
  set `verified`. State `verified` facts without hedging.

Relations inherit the declaring entity's status at index time. Per-relation
verification is not supported.

The owner brief header marks lines sourced exclusively from `proposed` entities
with `(unverified)`. Do not exclude `proposed` entities from query results — recall
matters more than purity for a personal knowledge graph; labeling is the mechanism.

**Recommended initial verification pass (document in conversations):** verify the
owner entity, positions/education/skills (owner-authored career core), and the
handful of key people/companies central to the job hunt. Leave long-tail connections
`proposed` — that is the expected steady state.

Re-imports by importers demote touched files back to `proposed` by design."""


def _section_query_cookbook() -> str:
    return """\
## 5. Query Cookbook

All commands assume `--vault .` from the vault root.

**Find entities by text:**
```
synapse find "machine learning"
synapse find "Alice" --vault /path/to/vault
```

**Filter by type, tag, or property:**
```
synapse filter --type opportunity --vault .
synapse filter --type person --tag recruiter
synapse filter --type opportunity --property status --value applied
```

**Explore neighbors (recommended: use --undirected):**
```
synapse neighbors me --depth 2 --undirected
synapse neighbors 01J000... --rel works_at --undirected
```
Note: directed traversal from non-owner nodes sees little due to the hub-and-spoke
graph shape. Prefer `--undirected` unless direction is specifically meaningful.

**Find path between two entities:**
```
synapse path me 01J000...
synapse path me 01J000... --max-hops 3
```

**Semantic + FTS hybrid search (recommended for human queries):**
```
synapse search "digital asset custody"
synapse search "senior security engineer" --limit 10
synapse search "Alice" --text-only
```
`search` fuses FTS and semantic results with reciprocal rank fusion. For semantic results, run `synapse embed --all` first. Use `find` for exact-text/scripting use cases where determinism matters.

**One-call target context and warm entry:**
```
synapse dossier "Alice"           # full person/company context + provenance
synapse warm-path "Example Corp"  # ranked connectors with explained evidence
```

**Owner orientation or a compact fallback brief:**
```
synapse brief                     # owner brief (default 8 000 tokens)
synapse brief "Alice"             # entity brief for any person/company/opportunity
synapse brief me --budget 4000    # owner brief with tighter budget
synapse brief 01J000...           # by ULID
```

**Natural-language query (compiles to a deterministic plan):**
```
synapse query "who are my recruiter contacts at Google?"
```

**Targeted career-data enrichment:**
```
synapse enrichment-queue --limit 50
synapse enrichment-attach profile.pdf --person 01J... --captured-at 2026-06-15
synapse import-linkedin-profile profile.pdf --person 01J... --captured-at 2026-06-15
synapse import-whatsapp-chat chat.txt --chat-key recruiter-jane --min-messages 2
```
Profile PDFs are manually downloaded with LinkedIn's supported Save-to-PDF action.
WhatsApp chats use supported per-chat text exports. Both importers are local and
deterministic; ambiguous identities stay unresolved for review. Configure every
owner phone alias under `identity.owner_phones` in `.synapse/config.yaml` before
WhatsApp imports."""


def _section_graph_shape() -> str:
    return """\
## 6. Graph Shape Notes

- **Owner-centric**: `me` is the hub; most paths route through it. Dense edges from
  `me` via `works_at`, `demonstrates_skill`, `has_goal`, `participated_in`.
- **Where job-hunt signal lives**: `opportunity` entities (with `status` properties)
  and `conversation` entities (with recruiters, dates, next-action properties).
- **Weak links** (`mentioned_in`) are off by default in traversals. Enable with
  `--include-weak` if you want wiki-link connections.
- **High-volume relation types** (`knows`, `mentioned_in`) exist from LinkedIn imports.
  Filter with `--rel` to avoid noise.
- Long-tail connections (1 000+ people) are expected; most will be `proposed` with
  minimal metadata. Use `filter --type person --tag recruiter` to focus."""


def _section_citation() -> str:
    return """\
## 7. Citation Convention

When reporting vault facts to the owner, always cite:

```
file_path: entities/people/alice-smith.md
id: `01J000000000000000000000XX`
```

Include both so the owner can open the file directly or use the ULID in tool calls.
Do not fabricate facts not present in the index. If uncertain, query first."""


def _section_feedback_loop() -> str:
    return """\
## 8. Feedback Loop

If a query returned nothing useful or produced incorrect results, that is signal.
Queries are logged to `.synapse/query-log.jsonl`; zero-hit patterns surface in
`synapse query-report`. Flag persistent gaps to the maintainer — they will either
add data, fix importers, or improve query plans.

Preferred feedback format:
- What you queried (`synapse search "..."`, `synapse find "..."`, `synapse neighbors me --rel works_at`)
- What you expected vs. what you got
- The entity IDs or names involved"""


def _section_mcp() -> str:
    return """\
## 9. Model Context Protocol (MCP) Tools

If you are running as an MCP-enabled agent with the `synapse` tools registered, you should prefer using the following tools directly:

- **Orient on the owner**: Call `synapse_brief` with no arguments for the owner brief.
- **Deep target context**: Use `synapse_dossier` for a known person or company; it is the one-call default for "tell me about X" and "who do I know at X".
- **Warm introductions**: Use `synapse_warm_path` for "who should I go through to reach X?"; every result explains its evidence.
- **Search details**: Use `synapse_search` to query concepts, job titles, or keywords. It uses hybrid search and returns ranked results.
- **Traverse context**: Use `synapse_neighbors` to inspect connections grouped by type, and `synapse_path` to find chains between entities.
- **Inspect raw detail**: Use `synapse_brief` or `synapse_entity` when a dossier does not cover the question or the full record is required.
- **Metadata**: Call `synapse_stats` for general database stats.

Always check tool outputs for `…truncated` tails and refine your queries to fit within budget limits."""


def _section_question_playbook() -> str:
    return """\
## 10. Question Playbook

Every archetype below is a chain of the primitives above (CLI commands or the
equivalent MCP tools), each within a **≤3-call budget**. Full detail, setup
recipes, and the mode-v2 write policy: `docs/USING-THE-BRAIN.md` (repo root).

| Archetype | Sequence | Calls | Cites |
|---|---|---|---|
| Who do I know at / for company X | `dossier <company>` | 1 | contact names + ids, roles, evidence |
| Who can introduce me to X | `warm-path <X>` | 1 | ranked connector ids + explained evidence |
| Tell me about person / company X | `dossier <X>` | 1 | identity, role, relations + provenance |
| What happened with X (convos/meetings) | `search "X"` → `neighbors <X> --rel has_interaction --rel participated_in --rel met_at` | ≤2 | conversation/event ids + dates |
| Find people by domain / skill | `search "<domain>"` or `filter --type person --property ...` | 1 | ranked people + ids |
| Capture what I just told you | (in-repo, mode v2) compile ops → `apply` dry-run → `--execute` | n/a | the proposal + audit line |

Front door: `search` (or `synapse_search`) first for any who/what question;
`brief`/`synapse_brief` for depth on one entity once you have its id."""


def _section_consuming_vs_developing() -> str:
    return """\
## 11. Consuming vs Developing

Using this brain to answer questions (not changing the tool itself)? Read
`docs/USING-THE-BRAIN.md` at the repo root — it is the single entry point for
a consuming agent (surfaces, trust rules, the question playbook, setup
recipes, mode-v2 write policy). This file (`AGENTS.md`) is the vault-local
mirror of that guidance plus the live schema. Developing the tool itself
(editing `src/synapse/`) instead starts at the repo's `AGENTS.md` /
`docs/CONTINUE-HERE.md`."""


def _section_owner_knowledge() -> str:
    return """## 12. Owner Knowledge and Personal Context

Use synapse owner-context (MCP: synapse_owner_context) for personalized learning,
reasoning, engagement, collaboration, refinement and planning questions.
It returns related authored findings, essential conditions, source basis,
owner position, dates, evidence and scoped corrections together. Omit the facet
to see available topics; narrow with --facet learning, --facet collaboration,
or the work/planning umbrella topics. --target accepts an existing entity ref.
--as-of takes YYYY-MM-DD and defaults to today in UTC. --budget is characters,
1–32000, default 8000, on both CLI and MCP. Ordinary CLI brief budgets remain
approximate tokens (characters divided by four).

For a named project's actual scope or status, read that project as well; personal
context does not replace project evidence. For broad personalized questions,
begin with owner-context and use cited record IDs for more detail. A small
budget may omit whole cards. Do not treat omitted material as absent knowledge.
Search hits are discovery candidates: read the complete statement and limits,
especially when a qualification or revision notice appears.

Profile records use existing insight/conversation/event types and flat metadata.
Record identity remains the entity ID. Source basis, owner acceptance and the
formal proposed/verified status are different things: an attested record may
still explicitly describe a hypothesis. Several interpretations of one source
family do not become independent evidence. Dates distinguish historical context,
current preferences and plans; a scheduled action does not prove adherence.

Corrections qualify a specific account or explicitly revise the same finding.
A later timestamp alone does not supersede everything in an older mixed record.
Keep reported capabilities and examples useful without assigning an IQ,
diagnosis or causal mechanism that the evidence does not establish. Stored
preferences and goals inform reasoning; they do not authorize new actions.

Capture updates through proposal dry-run then apply --execute under the authorized
mode, refresh the index and embeddings, and log the judgment. Agents never set
verified, edit the owner evaluation bank, or run git in the vault. Source bodies
remain available as provenance. See docs/OWNER-KNOWLEDGE.md for the record contract."""


def generate_agent_guide(conn: sqlite3.Connection, vault: Path) -> str:
    """Render the vault AGENTS.md from live index data and static templates."""
    counts = _live_counts(conn)
    rel_counts = _relation_counts(conn)

    sections = [
        f"# Digital Synapse Vault — Agent Guide\n\n"
        f"_Generated {utc_now()} — regenerate with `synapse agent-guide`_\n",
        _section_what(counts),
        _section_entity_types(counts, rel_counts),
        _section_conventions(),
        _section_trust(),
        _section_query_cookbook(),
        _section_graph_shape(),
        _section_citation(),
        _section_feedback_loop(),
        _section_mcp(),
        _section_question_playbook(),
        _section_consuming_vs_developing(),
        _section_owner_knowledge(),
    ]

    return "\n\n---\n\n".join(sections) + "\n"


def write_agent_guide(vault: str | Path | None = None, *, reindex_first: bool = True) -> Path:
    """Generate and write <vault>/AGENTS.md. Returns the path written."""
    from synapse.config import resolve_vault

    root = resolve_vault(vault)
    if reindex_first:
        reindex(root)
    conn = connect(root)
    try:
        content = generate_agent_guide(conn, root)
    finally:
        conn.close()
    out = root / "AGENTS.md"
    out.write_text(content, encoding="utf-8")
    return out
