# Moving from public v1

Public v2 changes the editing and review contract. Markdown remains readable, but only a retained published revision is accepted memory. Reindexing an editable file no longer publishes it. Prefer conversational capture and correction; editor synchronization is not a release goal.

Make a separate backup and trial before live activation. Keep the old tool/runtime available for the original v1 vault. Do not initialize v2 over a populated folder.

```sh
synapse v2 owner migration-preview --vault /existing/v1-vault
synapse v2 owner migration-trial /new/trial-directory --vault /existing/v1-vault
```

Inspect the reported omissions, records, original sources, labels and trial results. Arbitrary existing entity IDs and unknown metadata are preserved; no mandatory owner record is invented. Legacy accepted/verified labels are historical classifications, not newly attested facts, and missing passage provenance remains visible.

For actual activation, the trusted Codex parent must use the real owner's instruction and reviewed snapshot hash through `owner native activate`. If the snapshot changes after preview, re-review it. Test a read and backup/restore before switching the everyday connection. The operation does not silently activate another path or delete the original.

The new installation helper creates its own fresh `vault`. Restored or migrated external vaults can be used with explicit `--vault` read/host commands and a documented MCP integration; automatic switching of a managed installation to an arbitrary existing vault is not implemented in this alpha. Do not move a running vault behind the service.

## Agent/provider compatibility

The supported v2 workflow uses Codex native delegation, with Codex CLI retained for certain trusted preparation/review calls. Public v1's OpenAI-compatible API extraction configuration is not an interchangeable v2 reasoner. Existing legacy provider utilities may remain importable, but the old API-key route is not a supported v2 workflow. Keep v1 for that workflow or implement the explicit reasoner/host adapter described in the integration guide. No API keys are required by the supported Codex route.

Automated tests use injected reasoners. Existing `commit`/`verify` labels must not be used to bypass exact v2 review. Legacy writers fail visibly on activated vaults; a corrupt HEAD never causes a fallback to v1 writers.
