# Contributing

Read AGENTS.md and docs/BUILD.md. Keep changes within the public release scope and demonstrate them on synthetic workspaces. Do not copy private vault contents, personal audit documents or private repository history into a contribution.

Use Python 3.12 and `uv sync --all-extras`. Run `uv run pytest -q`, `uv run ruff check src tests`, and a wheel build. Tests must inject model/reasoner behavior; no live Codex, API calls or model downloads in automated tests. Browser/MCP checks may use disposable loopback servers and must stop them afterward.

Preserve source versions, qualifications, independent knowledge/review states and exact host-authorized publication. Native agents may reason adaptively; deterministic tools enforce the read/write contracts. Reviewable proposals are not approval. Test failed and interrupted operations, not just a successful demo.

Importers normalize supported source formats and create candidates. On v2, only the explicit publication workflow changes retained knowledge. Legacy editable-vault behavior remains behind its compatibility boundary. Do not use reindex to import arbitrary edits into an activated vault.

Keep defaults generic. Optional domain terminology or explicit reviewed mappings must not embed one contributor's personal assumptions. The human viewer must distinguish samples, derived similarity and recorded evidence. Large imports and source-only workspaces are required representative cases.

Before a release, verify package installation, durable backup/restore, synthetic migration, source/asset attribution and the public-history boundary. Dedicated security hardening and prevention of accidental public-vault publication are tracked separately; do not claim those reviews were performed merely because tests pass.
