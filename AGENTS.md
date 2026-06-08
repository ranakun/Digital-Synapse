# AGENTS.md

Digital Synapse is in v1 operational/maintenance mode. Agents should preserve
the SDS architecture while fixing bugs, improving tests, and helping the owner
use the tool on real data.

## First Read

Before changing code, read the relevant module and test files for the task.

## Repo And Vault Boundaries

- This repository is the tool implementation.
- The owner may create a private local vault at `.\vault`; it is ignored by this
  repo and may be its own git repository.
- Do not commit private vault content into the implementation repo.
- Do not read, print, or commit `.env`.
- Do not delete or rewrite user vault data unless the user explicitly asks.

## Architecture Rules

- Markdown entity files are canonical.
- SQLite indexes, embeddings, caches, and HTML exports are derived and
  disposable.
- Entity identity is the frontmatter ULID, not the filename or path.
- Reads are deterministic and must use indexed Markdown facts.
- Natural-language queries compile to validated query plans, not free-text
  answers.
- Ingestion and merge write proposed changes to the working tree. The human
  reviews diffs and commits.
- No graph database. Traversal stays in SQLite/Python.
- Remote generation uses the OpenAI-compatible provider abstraction.
- Deterministic importers are preferred for structured exports.
- Automated tests must not call live LLM APIs.

## Development Workflow

Use `py -3.12` plus `.venv` on this Windows machine unless `uv` is available.
Install all extras for full verification:

```powershell
& .\.venv\Scripts\python.exe -m pip install -e ".[dev,ingest,embeddings]"
```

Run before handoff:

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m ruff check src tests
```

For behavior changes, also run a temp-vault smoke that covers:

- `synapse init`
- deterministic importers when touched, especially `import-linkedin-connections`
- `reindex --full`
- `find`, `filter`, `neighbors`, `path`, `query`
- fake-provider ingestion via tests or a small script
- `status`, `commit`, and `check`
- deleting `.synapse/index.db` and rebuilding
- `export`
- `embed --all`

If you create a background `synapse serve` process, stop it before handoff.

## Owner Involvement

The owner is only needed for:

- Choosing/approving the remote provider route and ZDR acknowledgement.
- Supplying real private documents for ingestion.
- Reviewing generated knowledge-base diffs.
- Deciding whether proposed duplicate merges are correct.
- Final acceptance of behavior on the real vault.

For mildly corrupt structured exports, prefer local repair/normalization into a
clean CSV/JSON shape, then run a deterministic importer. Do not default to
remote LLM extraction for structured data.
