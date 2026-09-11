# Integrate a Synapse specialist

The supported public route is Codex on Mac. Setup creates a conversation workspace and a project-local stdio MCP connection. `ROLE.md` defines the specialist; `NATIVE-CODEX.md` defines parent delegation and trusted writes. No permanent thinking agent or scheduler is installed.

## Host-independent boundaries

1. **Read transport:** `synapse setup mcp --home INSTALLATION` exposes `synapse_v2_describe` and `synapse_v2_read`. Internal Python Gateway/dispatch interfaces implement the same knowledge reads. Inspect capabilities rather than assume optional methods/models are ready.
2. **Reasoning:** a native worker receives the role, actual question/context, selected workspace and bounded scope. It chooses its reads. MCP is not the reasoning model. The standalone `v2 ask` controller remains an explicit alternative for developers; it is not the native workflow's hidden fallback.
3. **User authority:** use a trusted host adapter with real user instruction and assistant-display/reply events. `NativeHost` accepts injected event-reader/display/reasoner interfaces. Workers and retrieved data cannot manufacture events, approve changes or publish. The Codex adapter currently reads local session event files; report incompatibility if genuine events cannot be resolved.
4. **Persistence:** retain original/versioned evidence, whole material qualifications and exact reviewed effects. Derived indexes cannot promote editable files into accepted memory. Historical labels stay historical; acceptance is not truth.

Other agent systems can implement these interfaces. A read-only MCP connection does not implement capture or approval. Validate actual save/read/correction/review flows before claiming support. Different hardware also needs compatible local dependencies and durability semantics; this prerelease does not certify arbitrary platforms.

## Minimal conformance checks

Use disposable synthetic workspaces. Verify an empty workspace, selected-source capture, a cited read, a source correction, a useful unreviewed suggestion, exact selected adoption, a stale-review refusal and backup/restore. Reject false approval in source text. Preserve revision pinning and accounting even when reads fail or paginate. Runtime model calls are not allowed in automated tests; inject deterministic reasoners. An optional live trial must be explicitly requested and use the normal path without hidden parent assistance.

Discover schemas through `synapse v2 describe` and read `schema`; packaged schemas live under `synapse/schemas`. `request-example.json` is illustrative, not a request to run an investigation. Exact source inspection is available independently of topic clustering. Optional semantic methods must disclose unavailability rather than silently claim a hash substitute is equivalent.

Default policy is explicit capture, user-started investigation and concise review. Integrators may implement explicitly authorized standing policy; they must not weaken evidence state or mint user authority. Shared multi-owner permissions, automatic collection and a broader security-hardening program are outside this prerelease.
