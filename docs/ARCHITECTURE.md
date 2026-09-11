# Architecture

Digital Synapse separates retained understanding, derived navigation, model reasoning and user authority.

- **Durable knowledge:** original sources, exact extracted versions, retained Markdown records, manifests and operation receipts under `_synapse`. A published HEAD identifies the readable revision. Host operation journals and explicit source-purpose policy are also durable decisions and belong in backups.
- **Derived reads:** SQLite/FTS, semantic indexes and overlapping graph areas. Rebuild them from retained revisions; never import external checkout edits implicitly. Source format, record count, topic membership and evidential support mean different things.
- **Knowledge states:** source material, assertion, hypothesis, decision and correction have distinct meaning. Availability, evidence strength, human review, position and attestation are separate. Incoming corrections and material premises accompany claims; incomplete qualifications cannot be replaced by a raw snippet.
- **Specialist:** a temporary native agent uses composable deterministic tools. It can reformulate a query, inspect indirect relations, consider alternatives and stop with a useful partial result. Budgets and revisions remain explicit; discovery is not proof of having read evidence.
- **Trusted host:** actual user instructions authorize capture/investigation; exact displayed changes and a subsequent reply authorize publication. Models cannot grant themselves permission. Host event support is separate from model transport and from MCP read access.
- **Human surfaces:** chat for capture, questions and brief review; local map/search/source inspection for exploration. Bounded samples show counts and omissions. Geometry and similarity are not asserted relationships.

The current implementation reuses Markdown and SQLite/Python; no graph database or generic plugin engine. One owner per workspace, with optional profile/domain data. Sources can be useful before any entities or claims exist. Existing v1 vaults require explicit migration; legacy labels gain no invented evidence or attestation.

Contract schemas and synthetic examples are under `docs/v2/contracts`; the runtime copies are under `src/synapse/schemas`. Source IDs and new record/operation identities have typed contracts; compatible legacy entity IDs are preserved. See the specialist role for retrieval and evidence-handling details.

## Runtime and configuration

The guided setup helper creates an isolated installation, a retained vault and a Codex conversation workspace. Codex manages the read-only stdio service. The viewer starts on demand and binds a free loopback port; no login daemon or permanent agent runs. Local semantic inference is explicitly prepared and its models live under the installation. Missing semantic capability leaves exact/text reads with an explicit limitation.

Workspace timezone is resolved consistently for reads; setup purpose is configuration, not an inferred personal fact. English-oriented extraction and grouping are initial capability limits. Other hosts can use the read protocol and implement trusted host/reasoner adapters, but are not certified by this Mac/Codex release.

Connection discovery includes a local workspace binding derived from its resolved vault path. Setup exposes the expected binding; native parents and specialists compare it before reading. `begin_consultation` accepts `expected_workspace_id` and rejects a mismatch before opening retained knowledge. Revision identity alone cannot distinguish a copied vault. Binding is routing metadata, not a credential, permission or portable knowledge identity. The parameter remains optional for legacy callers; the public native workflow requires it.
