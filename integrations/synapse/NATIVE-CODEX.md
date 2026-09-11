# Native Codex workflow

## Consultation

When the person invokes Synapse, use the host's native subagent mechanism. Give a fresh specialist `ROLE.md`, the actual question, relevant context/constraints, the selected vault and visible read tools. Do not fork unrelated history or supply hidden benchmark hints. Use one of the documented effort presets and return sources, contrary evidence, limitations and the usage receipt.

Read the intended installation's `setup status` and pass its `workspace_binding.id` to the specialist. Compare it with native `synapse_v2_describe` → `workspace.id`, then begin a metered consultation with `expected_workspace_id` in its arguments. Missing identity or mismatch requires correcting the connection before any knowledge read; never replace the expected value with whatever another service reports. Pin the returned revision, adapt read methods as needed and end the consultation. A task and its workers can inherit an older connection; open the generated conversation workspace in a new Codex task and verify again. Do not silently replace a global connection. Missing tools are a connection problem, not permission to inspect raw vault files or silently launch `codex exec` to answer the question.

The specialist is a temporary reasoning worker. Starting the MCP server or viewer does not start a thinking agent. Ordinary consultation neither saves the conversation nor creates a run or reminder.

## Explicit saving, investigation and review

The parent task owns these operations. Resolve actual messages from this task:

```sh
synapse v2 owner events --thread ACTUAL_THREAD_ID
```

Use returned event IDs, never invented IDs, synthetic transcripts or a worker's `approved=true`. Run a trusted operation using a JSON arguments file:

```sh
synapse v2 owner native capture-file --input /path/to/arguments.json --owner-event ACTUAL_USER_EVENT --thread ACTUAL_THREAD_ID --vault /path/to/vault
```

For `capture-file`, arguments are `{"path":"/explicitly/selected/file"}` with optional stable `operation_id`/`request_id` for retries. For `capture-message`, supply `material_ref` identifying the actual user or assistant message the user asked to save. The source origin retains the speaker; saved assistant text does not become a user statement or permission. Preserve returned receipts and operation IDs. Preparation may be partial while original capture succeeded. After a changed revision, ask the setup helper to prepare its derived indexes.

`start` creates a scoped investigation; `continue` requires a later explicit instruction; `cancel` stops that run. Use actual runtime contracts for arguments (`synapse v2 owner native --help`, packaged schemas and HostControl). A native worker may produce findings for `admit`/`stage`, but it cannot publish them. `stage` returns the exact brief and groups after semantic fidelity review. Show that brief to the user, bind its actual assistant message with `bind-display`, and resolve a later real user reply through `reply`. Never approve unseen groups or reuse an earlier unrelated “yes.”

Current limitation: saved-source preparation, `execute-run`, semantic review and some reply interpretation still use the subscription-authenticated Codex CLI reasoner. Native consultation itself does not. Keep these execution routes explicit and preserve required validation. Do not imply this is a fully native write runtime. If the installed Codex event format cannot establish authentic messages, report the limitation; do not fabricate an event-reader workaround. A direct owner-operated terminal host exists for developers, not an agent-authored approval substitute.

## Scope and defaults

Only explicitly selected source material is captured. Keep personal vocabulary optional; a new workspace has no mandatory `me` record. Do not ask a source-only item for record neighbors. The map and clusters are navigation, not evidence. Saved leads suggest possible future work but grant no permission to start it. No background investigations, automatic mailbox ingestion or proactive reminders are configured by setup.

Tool registration uses the documented Codex [MCP interface](https://developers.openai.com/codex/mcp). The generated project configuration starts a stdio read process; a user can separately request registration outside the workspace. This guide does not depend on undocumented agent-definition TOML or promise every task inherits the connection automatically.
