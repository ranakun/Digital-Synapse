# Guided setup

This walkthrough is for the agent helping a person set up Digital Synapse. Execute technical steps on their behalf when authorized. Explain choices in plain language; do not ask them to copy commands or diagnose runtime errors.

## 1. Understand and choose

Ask what they hope Synapse will help with and which small selection of material they want to start with. Offer examples if useful. Starting empty is valid. Do not require a life taxonomy, owner profile, contact import or full questionnaire.

Confirm the installation folder, timezone and selected material. Explain that setup installs a local Python runtime and dependencies; optional local semantic search downloads a model. Codex must be installed/signed in. Do not save setup conversation as personal knowledge unless asked. Do not search the computer for material to import.

## 2. Prepare the installation

Run `scripts/install-macos.sh` from this reviewed source checkout with the agreed absolute installation directory as its first argument. Default: `~/Library/Application Support/Digital Synapse`. Existing unrelated directories are refused. The script confines its runtime/tools to that directory and leaves shell profiles and Codex settings unchanged.

Use the resulting `<installation>/runtime/bin/synapse` for subsequent commands. All setup commands return JSON for an agent to inspect. Paths below are placeholders; use argument arrays or correct shell quoting, especially for spaces.

```sh
synapse setup initialize --home /absolute/installation --timezone Europe/London --purpose "Learning and project planning"
synapse setup status --home /absolute/installation
```

Initialization creates an empty v2 vault and a `workspace` folder containing conversation instructions and project-local Codex MCP configuration. Open that workspace in Codex, accept any actual project trust interaction, and start a new task if needed. Do not auto-approve project trust. Use the installed runtime, not a developer virtual environment that may disappear.

If the user wants Synapse tools available outside that workspace, explicitly explain the single additional Codex connection and run:

```sh
synapse setup connect-codex --home /absolute/installation
```

This uses Codex's registration command and refuses to replace an existing different connection. A new task/restart may be required. Configuration success is not proof that the current agent sees the tools.

## 3. Add only selected material

Use the actual current Codex user event and trusted host operations in `integrations/synapse/NATIVE-CODEX.md`. Do not create synthetic messages or directly write accepted vault files. A capture receipt distinguishes retained originals from preparation success. Source-only workspaces are useful before inferred records exist. Unsupported extraction is a visible limitation, not a successful import.

For supported structured exports, the `synapse v2 owner prepare-import --help` path builds a disposable candidate; it does not bypass retained revisions or adopt the result. Follow the documented review/publication boundary. Never run legacy import/reindex writers on an activated vault.

After capture, rebuild derived text/search state with `synapse setup prepare --home /absolute/installation`. For the chosen local semantic capability, run it with `--semantic`; this may download the model on first use and stores it in the installation's `models` directory. Repeat after new material to prepare the new revision. When enabling or disabling semantics, reconnect/restart the Synapse MCP connection as reported; an already-running read process retains its previous setting. `--no-semantic` explicitly disables it, while omitting the flag preserves the choice. Report incomplete extraction or semantic preparation honestly; exact/text reads remain usable. Do not claim an index means its contents are true.

## 4. Demonstrate real use

Verify the actual native connection before any knowledge read. Obtain the intended `workspace_binding.id` from `setup status --home` and compare it with `synapse_v2_describe` → `workspace.id`. The generated conversation instructions also contain the expected ID. A missing ID, another path/ID, or absent tools means this task is not connected to the intended workspace. Opening a configuration file does not rebind an existing task or its children: open the generated conversation workspace in a new Codex task and check again. Preserve other Synapse connections and global settings.

With a matching connection, call `begin_consultation` with `arguments={"preset":"consult","expected_workspace_id":"<the intended binding ID>"}`, perform one actual read, and call `end_consultation`. The service rejects a mismatched expected ID before opening a consultation. Pass the expected binding to delegated specialists; never substitute an ID from another connection just to make the check pass. A copied/restored vault at another location has a different local binding even when its revision matches. No fabricated test answer or raw-file fallback.

Ask one useful question based on the material the person chose. Delegate a fresh native specialist using the role, actual question/context and suitable budget. Explain the answer's sources, any inference and any missing information. Demonstrate “save this” and “correct that” only when the user actually requests them. Explain that unreviewed suggestions remain available and that final choices stay with the person.

Open the actual map:

```sh
synapse setup open --home /absolute/installation
```

A new or sparse collection can show loose material/search rather than invented clusters. Show a source, a connection and its basis if those exist. A tutorial is not permission to create example knowledge in the person's vault.

## 5. Return, recover and back up

To return, reopen the conversation workspace in Codex or ask the connected agent to use Synapse. Codex starts/stops its stdio read service; no MCP terminal is required. Ask the agent to open the map when wanted. The viewer runs only after explicit opening and can be stopped with `synapse setup stop --home /absolute/installation`; no login daemon is installed.

Setup can resume with the same home. A failed operation is not a reason to recreate a vault. Use status and receipts, preserve operation IDs for retries and inspect the reported next step. A workspace timezone is configuration, not a personal fact inferred from notes.

```sh
synapse setup backup /new/backup-directory --home /absolute/installation
synapse setup restore /existing/backup-directory /new/restored-vault
```

Backups preserve durable knowledge and local decisions. Restore requires a new directory; it does not overwrite the original or automatically switch the connected vault. Have the agent verify the restored revision before planning a deliberate switch. Do not copy host approval from one task into another.

Finish with a short handoff: where to return, how to ask/save/open the map, what material is ready, and any remaining limits. Never mark the current task's connection verified just because `setup status` reports the storage ready.
