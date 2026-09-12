# Agent-operated first-use trial — 11 September 2026

This is the historical pre-publication trial record. For the published alpha, final test count, CI results and current limitations, see [release status](HANDOFF.md).

**Initial trial: partial pass; the complete conversational journey was blocked.** Corrections and the second-round evidence follow below. The prior 1,067-test engineering result did not cover the live preparation schema or native connection inheritance demonstrated here. Do not treat it as proof of first-user readiness.

The owner requested that the orchestrator perform the trial. It used a new disposable Mac installation, a fictional photography-planning note, and the actual current user instruction for capture authority. No private knowledge was imported, global Codex configuration changed, or approval reply fabricated. A fresh native specialist received the ordinary question and role, with no answer hints or build history. No developer retrieval substitute was used to answer the question.

| Step | Observed result |
| --- | --- |
| Fresh install without `uv` on PATH | Passed: official bootstrap, isolated Python runtime and dependencies installed |
| Empty initialization and generated workspace | Passed |
| Save selected fictional note through native owner command | Passed: original retained, source reference and committed receipt returned |
| Automatic source preparation | Failed: live model rejected the output schema |
| Text preparation and map | Passed: one actual source, no invented clusters; full original including fictional-material label visible |
| Fresh native specialist consultation | Blocked: inherited connection described a different revision/collection; specialist stopped before reading records |
| Resume and recovery | Passed: resume preserved revision; backup/restored HEAD matched; trial viewer stopped |

## Two concrete blockers

1. **Preparation schema and error reporting.** `CodexReasoner.prepare` emits `uniqueItems` in `evidence_indices`. The live transport returned `invalid_json_schema`: that keyword is not permitted. The original capture receipt instead said only `Preparation reasoner failed (V2Error)`. A separate minimal structured transport request succeeded, so basic sign-in/transport availability is not the diagnosis. A read-only diagnostic using the actual preparation schema reproduced the rejection; no diagnostic output was published as knowledge. The bounded correction is to use a supported transport schema, retain evidence validation locally, and surface an actionable reason while preserving successful capture.

2. **Target connection verification.** Creating the trial workspace configuration did not rebind a worker spawned from an already-connected parent task. The native describe response did not identify its vault, and its revision differed from the trial. The worker correctly stopped. This does not prove that opening a new task in the generated workspace fails; that exact host transition remains unverified. Setup needs an explicit expected-target check and a verified new-workspace connection before claiming the specialist can answer from it. Never silently reuse another connection or replace existing host configuration.

No evidence-backed first-week answer, new review/adoption, or complete save-to-question success is claimed. Those dependent steps remain untested in this trial. The earlier synthetic review rehearsal is separate evidence, not a replacement for them.

Recommendation: resolve these two bounded integration issues and rerun this journey before inviting a nontechnical first user. No redesign, new features, or deferred security work is needed for this correction. This trial changed status documentation only; implementation files remain unchanged.

## Correction round

Implemented the two bounded code corrections, with no new product features:

- The transport schema omits `uniqueItems`; local checks still reject empty, duplicate, noninteger or out-of-range evidence selections. Known schema rejection is classified with a fixed actionable message; capture retains the useful typed failure instead of reporting only `V2Error`.
- Describe and setup status expose a local workspace binding. Generated instructions require a match, and a guarded begin refuses an unexpected target before constructing its read gateway. Copied vaults have separate bindings; symlinks to the same location agree. The default metered describe response remains within its existing character budget.

Verification on the frozen code: **1,086 tests passed; Ruff passed.** Tests include transport-schema compatibility stubs, invalid evidence selections, safe error reporting, capture preservation and wrong-target refusal before opening knowledge.

Two manual live-model captures through the installed native owner command completed: the original fictional photography-planning note, and an unrelated fictional bread experiment with confounded observations. Each retained the original and produced one grounded question and one navigation hint. The inspected photography question preserved that availability does not establish interest and that the scenario is fictional. No user approval or attestation was invented.

Installed read-service checks passed for both workspaces on initial start and restart: actual describe binding, rejection of a wrong expected binding, matching-target begin, source search and end receipt. Each consultation recorded one read. These are real MCP transport checks, not a claim that a native reasoning worker answered the question. Browser inspection showed the actual source and two prepared items, with unreviewed attribution and source access.

The owner authorized a temporary native desktop task. Adding the generated configuration to an already-started task did not expose Synapse tools on its next turn, and it correctly stopped. **A fresh task created after configuration existed succeeded:** both parent and a fresh specialist matched the intended binding and used actual native MCP tools. The parent verification used one read; the specialist used four reads and one source expansion, then ended its session. It inspected the full original plus two qualified preparation records and returned a first-week plan, identifying missing walk logistics and preserving the unadopted course suggestion. It did not save or adopt its recommendation. Semantic search was not enabled in this native trial.

The local Codex CLI resolves the correct enabled stdio server in that directory. Project-scoped MCP is supported for trusted projects by the [official MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli); configuration discovery alone does not establish tools in a running task. The verified recovery is a new task after configuration, not repeatedly sending messages to the already-started task. Tests used a projectless test task and then a fresh same-directory fork; the latter loaded the configuration.

## Final native results

After another ordinary capture added the experiment note, two independent fresh specialists both observed the newer revision and verified the same intended binding.

| Native question | Observed answer | Measured specialist receipt |
| --- | --- | --- |
| First-week photography plan | Used available Tuesday/Saturday time and existing phone; kept the walk optional, identified missing logistics, and did not adopt the paid-course suggestion | 4 calls, 1 expansion, 30 seconds; ended |
| What does the bread experiment establish, and what next? | Identified the hydration/proof-duration confound; proposed two controlled small batches without buying equipment; did not claim causation from the original observation | 5 calls, 2 expansions, 38 seconds; ended |
| Exact address and start time of the photo walk? | Declined to invent either; cited the original statement that they were unconfirmed and recommended asking the organizer | 3 calls, 1 expansion, 29 seconds; ended |

All answers used actual native MCP tools, exact retained source references and bounded sessions. No parent supplied answer hints, raw source replacements or a custom retrieval bridge. The bread worker encountered the photography source in a catalog but did not use it as evidence for the experiment. No retrieval failure, truncation or unwanted topic interference was reported. Derived records remained qualified rather than serving as independent corroboration. The final backup/restored revision also matched.

**Assessment:** both original blockers are resolved, and these scoped native journeys pass. This is sufficient to move to release review, not evidence that arbitrary imports, all reasoning questions or every host version work. Semantic search was not enabled in these native questions; separate installed semantic tests passed earlier. A live human adoption reply was not fabricated: exact approval/adoption mechanics remain covered by the synthetic rehearsal and automated suite. No commit or public publication occurred. Dedicated security hardening remains deferred.
