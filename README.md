# Digital Synapse

A knowledge workspace you own. Save useful context, ask an agent to connect ideas, and explore the evidence behind its answers.

**Public v2 alpha — Mac and Codex first.** An early release with tested setup and native-agent workflows; see [the trial results](docs/TRIAL-RESULTS.md) for coverage and limits. Windows and other agent integrations are not certified. A dedicated security-hardening review is pending; see [the roadmap](docs/BUILD.md#deferred-work--security-future-release-potentially-v3).

## Start with your agent

Open this project in Codex and ask:

> Help me set up Digital Synapse using SETUP.md. Walk me through the choices in plain language, start with only the material I select, and show me how to use it with a real example.

[The setup walkthrough](SETUP.md) gives the agent the technical steps. You should not have to configure Python, run servers in terminals or edit tool settings. You need Codex installed and signed in; account sign-in and operating-system permissions remain your actions.

You can start empty. A few notes about projects, learning, ideas or decisions are enough. A large contact import is optional, and does not become the center of everything.

## What you can do

- **Ask:** “What connects these ideas?” or “What am I overlooking in this plan?” The specialist chooses relevant search and relationship paths and shows supporting evidence and gaps.
- **Save:** explicitly retain a thought, document or supported export. Saving the material and completing its interpretation are different steps.
- **Investigate:** request deeper synthesis, contradictions, missing context or possible next steps. Nothing starts merely because an agent saved a lead.
- **Review:** useful unreviewed suggestions stay available with attribution. Brief reviews cover concrete consequential changes; you do not need to approve every note.
- **Explore:** open a map, search a topic and inspect sources. Groups overlap; sampled points and similarity links do not pretend to represent certainty or importance.
- **Correct:** revise understanding while retaining sources and history.

Ordinary conversations are not automatically saved. The default is user-started work with explicit capture. Your workspace is separate from this implementation repository.

## How it works

Retained sources and Markdown revisions hold the durable knowledge. Search indexes and graph layouts can be rebuilt. The Synapse specialist reasons using deterministic evidence tools; the tools preserve versions, qualifications and review states. MCP connects those read tools to Codex. The map is a local human-facing read surface.

The system distinguishes source material, supported assertions, hypotheses, accepted changes and user permission. Agreement is not evidence; a suggestion is not a commitment. No system guarantees that every useful connection will be discovered.

## Documentation

- [Guided setup and daily return](SETUP.md)
- [Specialist and host integration](integrations/synapse/README.md)
- [Native Codex workflow](integrations/synapse/NATIVE-CODEX.md)
- [Architecture and contracts](docs/ARCHITECTURE.md)
- [Migration from public v1](docs/MIGRATION.md)
- [Build scope, limitations and deferred work](docs/BUILD.md)

For contributors: Python 3.12, `uv sync --extra dev --extra ingest --extra embeddings --extra mcp`, then `uv run pytest -q` and `uv run ruff check src tests`. Tests use synthetic material; they do not launch live models. See [AGENTS.md](AGENTS.md).

Licensed under Apache-2.0. Bundled assets retain their own notices.
