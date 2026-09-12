# Public v2: release status and first use

**Released 11 September 2026:** [Digital Synapse v2 Alpha 1](https://github.com/ranakun/Digital-Synapse/releases/tag/v2.0.0-alpha.1), tag `v2.0.0-alpha.1`, Python package `2.0.0a1`. The public implementation is on `main`; reviewed wheel and source packages are attached to the GitHub prerelease. It is not published to PyPI.

The final release passed **1,087 automated tests and Ruff**, package checks, and native-agent trials. PR, release-tag and final main CI passed. An intermittent hosted-CI viewer-startup timeout occurred; the unchanged main retry passed. That timing sensitivity remains an alpha limitation, not a claim of flawless operation. [Release verification](https://github.com/ranakun/Digital-Synapse/actions/runs/34593948508) and the [historical trial results](TRIAL-RESULTS.md) describe different checks.

## Start

Give your Codex agent the repository link and say:

> Set up https://github.com/ranakun/Digital-Synapse for me using SETUP.md. Use a separate new installation folder, keep my existing Synapse untouched, and walk me through adding one note and asking one useful question. Do the technical setup for me.

The agent should agree on the folder and timezone, install the runtime, initialize an empty workspace and guide you into its conversation workspace. A new Codex task or connection restart may be necessary. Start with one explicitly selected note; a network export and an owner profile are optional.

For the first trial, check four things: the agent can explain what was actually saved; an answer points to inspectable evidence; the map opens the actual material without invented clusters; and an explicit correction or review preserves the source and uncertainty. A brief “what worked / what was confusing” report is enough. Do not start with a large import merely to exercise setup.

## What is ready

- Guided, resumable Mac setup; project-local read-tool connection and optional explicit wider Codex registration.
- Source-first knowledge across domains, adaptive native specialist consultation, qualified suggestions and concise exact review.
- Existing map, search and source inspector, including empty/small collections and bounded large neighborhoods.
- On-demand viewer, retained revisions, integrity-checked backup/restore and documented public-v1 migration.

The pre-publication trial passed 1,086 automated tests; final publication added one regression check, bringing the release total to 1,087. Installed-package checks covered fresh setup, retained synthetic material, real local semantic retrieval across restart, isolated Codex registration and backup/restore. Browser checks covered empty and sparse collections, unreviewed suggestions and exact source passages. The corrected live trial also exercised native specialist delegation and evidence-backed reasoning on fictional material; real-user acceptance remains separate.

## Accepted alpha limitations

- Mac/Codex is the supported path. Other platforms/hosts are documented integration work, not certified support. The official Codex CLI found during verification was `0.153.4`.
- Consultation uses native delegation. Some preparation and review operations still use the subscription-authenticated Codex CLI reasoner; they are not a fully native write runtime. Authentic event lookup depends on compatible Codex session data and must fail visibly if unavailable.
- Start a new Codex task after the generated workspace configuration exists. Adding it to an already-started task did not load the tools in the observed trial. Verify the intended binding in the new task before reading.
- Semantic search is optional and downloads a local model. Prepare it after knowledge changes; reconnect the read service after enabling/disabling it. Exact/text reads remain available when semantics are unavailable.
- Extraction/group labels are English-oriented and heuristic. Very small collections remain ungrouped. Legacy `me` hub suppression is retained. No guarantee of finding every useful connection.
- Restore creates a verified separate vault; automatically switching a managed installation to it is not implemented. An agent must plan that switch explicitly.
- The installer and official bootstrap fallback were exercised. Remote GitHub CI passed; intermittent viewer-startup timing sensitivity on a hosted runner is documented above.
- Dedicated security hardening and stronger protection against publishing personal vaults publicly are explicitly pending for a later release, potentially v3.

The alpha publication is complete. The next step is ordinary use and focused feedback: share confusing behavior or a reproducible problem using synthetic examples. Keep personal notes, source excerpts and credentials out of public issues. Broader security hardening remains separate roadmap work; it has not been completed by these release checks.
