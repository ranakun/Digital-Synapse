# Contributing

Digital Synapse is a local-first digital brain / personal knowledge graph built
around canonical Markdown files and a disposable SQLite index.

## Ground Rules

- Do not add graph databases, cloud sync, multi-user features, or generative
  answer output.
- Keep read-path behavior deterministic.
- Keep remote LLM calls optional and isolated behind provider interfaces.
- Automated tests must not call live LLM APIs.
- Never commit personal vault data, `.env`, API keys, caches, or derived indexes.

## Development Setup

With `uv`:

```powershell
py -3.12 -m pip install --user uv
py -3.12 -m uv sync --all-extras --dev
```

With plain `venv`:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -e ".[dev,ingest,embeddings]"
```

Run checks:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m pip check
```

## Importer Policy

Prefer deterministic importers for structured exports such as CSV, JSON, vCard,
and platform data archives. Remote LLM extraction should be a fallback for messy
or unstructured files, not the default path for well-structured data.

New importers should:

- Write proposed Markdown changes to the vault working tree.
- Preserve provenance in frontmatter.
- Avoid committing or moving source files directly.
- Use `synapse commit` for the review gate.
- Include fixture tests with fake data only.

## Pull Request Checklist

- Tests and Ruff pass.
- Documentation/README updated for user-visible behavior.
- No personal/private data included.
