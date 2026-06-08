# Digital Synapse

Digital Synapse is a local-first digital brain, second brain, and personal
knowledge graph. It turns your private structured exports and documents into a
durable Markdown knowledge base with deterministic graph queries.

Markdown entity files are the source of truth; SQLite indexes, embeddings, and
HTML exports are derived and rebuildable.

This repository contains
the tool implementation. Your private knowledge base should live in a separate
vault directory, typically `.\vault`, which is ignored by this repository.

## What Works In v1

- Canonical Markdown entities with stable frontmatter IDs.
- Disposable SQLite index with FTS5, typed relations, weak wikilinks, and full
  rebuild support.
- Deterministic commands: `find`, `neighbors`, `path`, `filter`.
- Deterministic LinkedIn connections importer with no API key or LLM required.
- Structured ingestion from TXT, Markdown, CSV, JSON, PDF, DOCX, WhatsApp-style
  chat logs, and vCard files, with optional remote LLM extraction for messy
  unstructured documents.
- Human review gate through the vault working tree and `git diff`.
- Natural-language queries as validated plans, not generated answers.
- Offline local graph UI and self-contained HTML export.
- Maintenance commands: `check`, `merge`, `embed`, and `watch`.
- Local embeddings by default through `fastembed`, with deterministic hash
  fallback if the local model is unavailable.
- Secret-blocking pre-commit hook installed in new vaults.

## Setup

Preferred setup with `uv`:

```powershell
py -3.12 -m pip install --user uv
py -3.12 -m uv sync --all-extras --dev
py -3.12 -m uv run synapse --help
py -3.12 -m uv run pytest
```

Plain `venv` fallback:

```powershell
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -U pip setuptools wheel
& .\.venv\Scripts\python.exe -m pip install -e ".[dev,ingest,embeddings]"
& .\.venv\Scripts\synapse.exe --help
```

Run verification:

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m ruff check src tests
```

## Create Your Vault

Create a local private vault inside this repo:

```powershell
& .\.venv\Scripts\synapse.exe init --path .\vault
```

This creates:

```text
vault/
  .synapse/config.yaml
  entities/
  inbox/
  inbox/processed/
  ledgers/
```

`.\vault` is ignored by this implementation repo. It is also initialized as its
own git repo, so review and commits happen inside the vault:

```powershell
git -C .\vault status
```

## Configure Remote Ingestion

Read/query/visualization commands work offline. Deterministic importers such as
LinkedIn connections also work offline and do not need an API key.

Remote LLM configuration is only needed for:

- `synapse ingest <file>` on messy or unstructured documents.
- non-shorthand `synapse query "<question>"` when compiling natural language to
  a query plan.

Put the key in local `.env` at the implementation repo root or in the vault:

```powershell
SYNAPSE_LLM_API_KEY=your_key_here
```

Do not commit `.env`. It is ignored.

Then edit `.\vault\.synapse\config.yaml`:

```yaml
llm:
  base_url: https://your-openai-compatible-gateway.example/v1
  model: your-extraction-model
  zdr_required: true
  zdr_acknowledged: true
```

Set `zdr_acknowledged: true` only after you accept the provider route and data
retention behavior.

## Import Structured Data

Prefer deterministic importers for structured exports. They are faster, cheaper,
more private, and easier to review than LLM extraction.

LinkedIn connections:

```powershell
Copy-Item "C:\path\to\Connections.csv" .\vault\inbox\Connections.csv
& .\.venv\Scripts\synapse.exe import-linkedin-connections .\vault\inbox\Connections.csv --vault .\vault
& .\.venv\Scripts\synapse.exe check --vault .\vault
git -C .\vault diff
```

If the proposed Markdown looks correct:

```powershell
& .\.venv\Scripts\synapse.exe commit --vault .\vault -m "Import LinkedIn connections"
```

## Ingest Documents

Use `ingest` when the source is not structured enough for a deterministic
importer:

Copy source files into `.\vault\inbox`, then ingest one file at a time:

```powershell
& .\.venv\Scripts\synapse.exe ingest .\vault\inbox\source.pdf --vault .\vault
& .\.venv\Scripts\synapse.exe status --vault .\vault
git -C .\vault diff
```

Review and edit the generated Markdown. If it is correct:

```powershell
& .\.venv\Scripts\synapse.exe commit --vault .\vault -m "Ingest source.pdf"
```

If it is wrong:

```powershell
git -C .\vault restore .
```

The raw source stays in `inbox` until `synapse commit` succeeds, then moves to
`inbox/processed`.

## Query And Explore

```powershell
& .\.venv\Scripts\synapse.exe reindex --full --vault .\vault
& .\.venv\Scripts\synapse.exe find "person or company" --vault .\vault
& .\.venv\Scripts\synapse.exe filter --type person --vault .\vault
& .\.venv\Scripts\synapse.exe neighbors <entity-id> --depth 2 --undirected --vault .\vault
& .\.venv\Scripts\synapse.exe path <entity-a-id> <entity-b-id> --vault .\vault
```

Graph UI:

```powershell
& .\.venv\Scripts\synapse.exe serve --vault .\vault --port 7777
```

Open `http://127.0.0.1:7777/`.

Export a subgraph:

```powershell
& .\.venv\Scripts\synapse.exe export "neighbors Example Person" --html .\out.html --vault .\vault
```

## Maintain The Vault

```powershell
& .\.venv\Scripts\synapse.exe check --vault .\vault
& .\.venv\Scripts\synapse.exe merge <keep-id> <merge-id> --vault .\vault
& .\.venv\Scripts\synapse.exe embed --all --vault .\vault
```

`merge` writes reviewable working-tree changes. Inspect `git -C .\vault diff`
before committing.

## Agent Workflow

When using Cursor, Claude Code, Codex, or another agent, give it
[AGENTS.md](AGENTS.md). Agents should not read or print
`.env`, and should not commit private vault contents into this implementation repo.

For corrupt or odd structured exports, a coding agent can usually clean the file
locally into a normal CSV/JSON shape, then run a deterministic importer. That
does not require a Digital Synapse API key unless you explicitly choose to use a
remote model.

## Public Repo / Private Vault

Recommended split:

- Public repo: this implementation, tests, and fake fixtures.
- Private repo: your actual `vault/` with personal Markdown entities and raw
  processed sources.

The public repo should never contain personal data. The private vault can live
inside this checkout as `.\vault` because it is ignored by `.gitignore`, while
still being its own git repository.

## Troubleshooting

- `python` opens the Microsoft Store: use `py -3.12` or
  `.\.venv\Scripts\python.exe`.
- `SQLite FTS5 is unavailable`: reinstall with the project dependencies, or use
  the bundled `.venv` path on this machine.
- `Remote calls require llm.zdr_acknowledged`: set it to `true` only after
  accepting the provider route.
- `SYNAPSE_LLM_API_KEY is not set`: add it to `.env` or the process
  environment.
- Bad ingestion output: inspect `git -C .\vault diff`; use
  `git -C .\vault restore .` to discard generated changes.
