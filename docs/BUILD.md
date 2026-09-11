# Public v2 build board

Status: implementation authorized; public release/push requires separate review.

**Latest trial result: both original blockers corrected; three native questions passed.** Live preparation succeeded on unrelated fictional sources; wrong-target refusal and matching-target retrieval passed across restarts; fresh native specialists answered planning, experimental-reasoning and missing-information questions from the intended workspace, including after a revision update. The frozen suite passes 1,086 tests and Ruff. See [trial results](TRIAL-RESULTS.md) for the distinction between engineering, transport and native reasoning checks. Publication still requires review.

## Fixed scope

Mac/Codex, one owner per workspace; phased conversational setup; explicit selected-source capture; adaptive evidence-grounded consultation; requested investigations; qualified suggestions and concise review; honest map/search/source views; startup, backup/recovery, public-v1 migration and integration documentation. Users need no terminal/configuration expertise. Technical agents can execute the same setup protocol.

Reuse the retained-revision core and existing viewer. Remove personal source assumptions, not useful features merely because they first served one person. Preserve integrity and authority contracts. No broad redesign or benchmark tuning.

## Sequence / remaining blockers

| Stage | Owner | Status / finish evidence |
| --- | --- | --- |
| 1. Curated public snapshot and generic defaults | Root + bounded worker | Implemented: public-main ancestry; synthetic fixtures and generic defaults; no private history/vault/docs copied |
| 2. Fresh generic v2 and migration | Core worker, root review | Verified: empty/source-first workspace, arbitrary legacy IDs, absent self profile, interrupted bootstrap recovery |
| 3. Setup and lifecycle | Root | Verified: isolated Mac install, project Codex connection, on-demand viewer, backup/restore, packaged read service and restart |
| 4. Public instructions and reusable roles | Root | Written: guided setup, daily use, integration boundaries, compatibility changes |
| 5. Representative integrated verification | Root | Passed: 1,086 tests, Ruff, synthetic review rehearsal, browser source inspection, installed semantic read/restart and recovery, three native questions |
| 6. Owner review / prerelease publication | Owner | Pending: exact public diff and trial handoff; no push yet |

Prefer sequential stages. Sidecar workers only own disjoint code or documentation; root handles cross-cutting issues. Test representative synthetic data before broadening. Freeze after concrete blockers clear; optional polish and marginal model performance do not delay first trial.

## Runtime boundary

Use existing Python modules and Codex native specialist delegation. The setup helper operates on an explicit installation/workspace path, emits structured progress and is safe to resume. It must not require the user to manage terminals, environments or MCP entries. A local lifecycle helper starts/stops only its own services, detects conflicts, and verifies the selected revision. Codex owns the stdio read-service process; the local viewer starts on demand. No new standalone chat application.

Conversations save only on instruction. Setup preferences are configuration, not fabricated verified personal facts. Account sign-in and real host approval remain actual user events; a script cannot manufacture them.

## Deferred work — security (future release, potentially v3)

Pending by explicit owner instruction: a dedicated security review/hardening design for local services, agent access and knowledge content, plus strong recommendations and technical safeguards against accidentally publishing a personal vault or its history to public GitHub. Do not implement or investigate this work during the present build. Existing secret/data boundaries and correctness checks remain in force. This prerelease must not claim that a dedicated security review is complete.

Other exclusions: Windows certification, shared multi-owner workspaces, a standalone chat app, automatic mailbox/continuous intake, Obsidian synchronization, broad agent/hardware support certification, schema/plugin marketplaces.

## Frozen trial build — 11 September 2026

The engineering freeze initially had no known implementation blocker. The subsequent live trial identified the two bounded blockers above. Public version is `2.0.0a1`; publication remains pending. [Trial handoff](HANDOFF.md) distinguishes verified mechanics from real-user acceptance.

Verified on macOS 26.5 arm64 / Python 3.12. The frozen suite passed 1,067 tests in 41.50 seconds; Ruff and installer shell syntax passed. Third-party settings/deprecation and fork-test warnings remain visible. Automated tests block live Codex execution and model downloads. The separate installed-package smoke used the real local embedding model with synthetic material, two independent stdio sessions, isolated Codex registration, and a matching backup/restore revision. No user Codex configuration was changed.

Independent setup review found three concrete gaps, resolved before the freeze: assistant-message capture now retains speaker attribution, explicit semantic disable persists, and changed semantic settings report the required service restart. Synthetic workflow checks verify exact review and source provenance; they do not certify philosophical reasoning quality or pretend a simulated reply was a real owner's approval.

The public checkout retains public-main ancestry and a curated source snapshot, not private history. Documentation links, bundled asset hash/notices, package contents and explicit private-marker checks passed. These boundary checks are not the deferred security audit. GitHub CI is configured but has not run remotely; no commit, push or release was performed.

Known limits to document rather than expand: English-oriented extraction/grouping, heuristic cluster labels and owner-star suppression, no automatic switch to a restored external vault, no certification of arbitrary Codex event formats or other platforms, and separate future security hardening.
