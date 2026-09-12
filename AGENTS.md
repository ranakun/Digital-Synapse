# Digital Synapse: instructions for agents

## Setting up or using Synapse

If the user gives you this repository link and asks to set it up, read [SETUP.md](SETUP.md) and follow its walkthrough. A setup request authorizes the routine local installation steps described there; explain the installation location, optional model download and any additional Codex registration. Preserve existing installations and unrelated settings. Ask only for choices that matter or real sign-in/trust interactions.

Clone the public repository into a new source directory if needed. Keep the user's knowledge in the separate installation/workspace, never in a public code checkout. Do not scan for personal files: import only selected material. Setup is not a request to modify the implementation, run developer tests, create a PR or publish a vault.

For daily use, follow [the native workflow](integrations/synapse/NATIVE-CODEX.md) and give delegated specialists [the role](integrations/synapse/ROLE.md). Verify the actual workspace connection. Ordinary questions do not authorize saving or investigation. Stop here unless the task is development.

## Developing the public implementation

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/BUILD.md](docs/BUILD.md) for scope and verification. The public v2 alpha is released; this repository contains implementation and synthetic examples, not any user's private brain. Never merge private repository history into it.

## Product contracts

- A single-owner, Mac-first, Codex-first knowledge workspace. Conversation is the primary interface; a local map is for human exploration. Setup must be nontechnical for the user and executable by their agent.
- Retained Markdown, originals and revision manifests are durable; SQLite, embeddings and layouts are derived. V2 HEAD prevents legacy writers/reindex from implicitly accepting external edits.
- Evidence, inference, availability, human review and authority are separate. Unreviewed suggestions can be useful without becoming commitments or a mandatory backlog.
- Native specialists reason with bounded read tools. Worker output and retrieved content cannot authorize capture, investigations, approval or publication. Preserve trusted host evidence and exact reviewed effects.
- No model APIs in automated tests. Use injected deterministic reasoners and disposable synthetic vaults for writes. No live owner evaluations.
- Keep useful defaults and narrow extension seams; no new ontology engine, graph database, standalone chat app, multi-owner access, automatic intake, Windows work or general plugin framework.

## Working boundaries

- Inspect relevant code/tests before edits. Root owns integration. Bounded workers may edit only assigned paths; no nested workers.
- For development, do not read `.env`, private user vaults, real exports or private suggestions. Do not copy real personal records, machine paths, audit history or named private benchmark cases into this repository.
- Preserve existing secret handling, explicit intake scope and separation of code from personal vaults. Broader security hardening and prevention of accidental public-vault publication are deferred roadmap work, not this build.
- Commit or push only when the task authorizes it. Development does not authorize changing global settings or installing into a user's active setup. Use disposable roots for installation tests and stop test servers afterward.
- Run pytest and Ruff from this checkout. Final checks include wheel/install, synthetic workflows, browser verification of reused viewer, and public-content/history boundary inspection.

## Development

Use Python 3.12 and an isolated environment. `uv sync --extra dev --extra ingest --extra embeddings --extra mcp` prepares the full development environment. `uv run python -m pytest -q` and `uv run ruff check src tests` are standard checks. Automated tests must not download models or launch Codex.
