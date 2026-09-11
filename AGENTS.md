# Digital Synapse public v2

Read docs/BUILD.md for scope, ownership and verification. The owner authorized implementation of the public successor, not pushing or publishing. This checkout descends from public main; never merge private development history.

**Setup or using the brain?** Read SETUP.md and integrations/synapse/NATIVE-CODEX.md. Do not treat a request to use Synapse as an instruction to modify its implementation.

## Product contracts

- A single-owner, Mac-first, Codex-first knowledge workspace. Conversation is the primary interface; a local map is for human exploration. Setup must be nontechnical for the user and executable by their agent.
- Retained Markdown, originals and revision manifests are durable; SQLite, embeddings and layouts are derived. V2 HEAD prevents legacy writers/reindex from implicitly accepting external edits.
- Evidence, inference, availability, human review and authority are separate. Unreviewed suggestions can be useful without becoming commitments or a mandatory backlog.
- Native specialists reason with bounded read tools. Worker output and retrieved content cannot authorize capture, investigations, approval or publication. Preserve trusted host evidence and exact reviewed effects.
- No model APIs in automated tests. Use injected deterministic reasoners and disposable synthetic vaults for writes. No live owner evaluations.
- Keep useful defaults and narrow extension seams; no new ontology engine, graph database, standalone chat app, multi-owner access, automatic intake, Windows work or general plugin framework.

## Working boundaries

- Inspect relevant code/tests before edits. Root owns integration. Bounded workers may edit only assigned paths; no nested workers.
- Do not read `.env`, owner vaults, private exports or private suggestions. Do not copy real personal records, machine paths, audit history or named private benchmark cases into this repository.
- Preserve existing secret handling, explicit intake scope and separation of code from personal vaults. Broader security hardening and prevention of accidental public-vault publication are deferred roadmap work, not this build.
- Do not commit, push, change global settings, install into the owner's active setup, or leave test servers running. Runtime installation tests belong in disposable roots.
- Run pytest and Ruff from this checkout. Final checks include wheel/install, synthetic workflows, browser verification of reused viewer, and public-content/history boundary inspection.

## Development

Use Python 3.12 and an isolated environment. `uv sync --extra dev --extra ingest --extra embeddings --extra mcp` prepares the full development environment. `uv run pytest -q` and `uv run ruff check src tests` are standard checks. Automated tests must not download models or launch Codex.
