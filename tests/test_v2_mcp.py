from __future__ import annotations

import asyncio
import copy
from pathlib import Path

from mcp.types import CallToolResult

from synapse.knowledge import record_descriptor
from synapse.revisions import RevisionStore
from synapse.source_store import prepare_source
from synapse.util import generate_ulid
from synapse.v2_contracts import hash_bytes
from synapse.v2_mcp import make_server


def _vault(tmp_path: Path) -> tuple[Path, str]:
    vault = tmp_path / "vault"
    store = RevisionStore(vault)
    raw = (
        b"---\nid: me\ntype: person\nname: Owner\nreview_status: proposed\n---\n\nOwner profile.\n"
    )
    row = record_descriptor(raw, path="entities/people/me.md")
    descriptor, objects = prepare_source(
        ("Searchable retained material café 😀. " * 30).encode("utf-8"),
        origin="synthetic.txt",
        source_id=generate_ulid(),
    )
    objects[row["version"]] = raw
    store.transact(
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        kind="capture",
        payload_hash=hash_bytes(b"seed"),
        mutate=lambda manifest, _read: (
            manifest["records"].update({"me": copy.deepcopy(row)}),
            manifest["sources"].update({descriptor["id"]: descriptor["version"]}),
            manifest["source_versions"].update({descriptor["version"]: descriptor}),
        ),
        objects=objects,
        initialize=True,
    )
    return vault, descriptor["id"]


def _call(server, name: str, arguments: dict) -> CallToolResult:
    return asyncio.run(server._tool_manager.call_tool(name, arguments, convert_result=False))


def test_server_lists_only_read_tools_and_returns_one_serialized_content(tmp_path: Path) -> None:
    vault, _ = _vault(tmp_path)
    server = make_server(vault)
    tools = server._tool_manager.list_tools()
    assert {tool.name for tool in tools} == {"synapse_v2_describe", "synapse_v2_read"}

    result = _call(
        server,
        "synapse_v2_read",
        {"operation": "search_sources", "arguments": {"query": "café"}, "budget": 4000},
    )
    assert isinstance(result, CallToolResult)
    assert len(result.content) == 1
    assert result.structuredContent is None
    assert len(result.model_dump_json(exclude_none=True)) <= 4000
    assert '"items"' in result.content[0].text


def test_mcp_budget_is_measured_on_complete_call_result_and_errors_are_errors(
    tmp_path: Path,
) -> None:
    vault, source_id = _vault(tmp_path)
    server = make_server(vault)
    tiny = _call(
        server,
        "synapse_v2_read",
        {"operation": "source", "arguments": {"id": source_id, "limit": 4000}, "budget": 256},
    )
    assert tiny.isError
    assert len(tiny.content) == 1
    assert len(tiny.model_dump_json(exclude_none=True)) <= 256

    unknown = _call(
        server, "synapse_v2_read", {"operation": "capture", "arguments": {}, "budget": 512}
    )
    assert unknown.isError
    assert '"unsupported-operation"' in unknown.content[0].text


def test_workspace_binding_distinguishes_copies_and_rejects_wrong_target_before_read(tmp_path, monkeypatch):
    import json
    import shutil

    from synapse.gateway import workspace_binding

    vault, _ = _vault(tmp_path)
    clone = tmp_path / 'copied-vault'
    shutil.copytree(vault, clone)
    expected = workspace_binding(vault)
    assert workspace_binding(clone)['id'] != expected['id']
    alias = tmp_path / 'alias'
    alias.symlink_to(vault, target_is_directory=True)
    assert workspace_binding(alias) == expected
    server = make_server(vault)
    description = json.loads(_call(server, 'synapse_v2_describe', {}).content[0].text)
    assert description['workspace'] == expected
    opened = _call(server, 'synapse_v2_read', {'operation': 'begin_consultation', 'arguments': {'preset': 'consult', 'expected_workspace_id': expected['id']}})
    assert not opened.isError
    receipt = json.loads(opened.content[0].text)
    assert receipt['workspace'] == expected
    _call(server, 'synapse_v2_read', {'operation': 'end_consultation', 'session_token': receipt['session_token']})

    def forbidden(*args, **kwargs):
        raise AssertionError('Wrong target must reject before opening retained knowledge')

    monkeypatch.setattr('synapse.v2_mcp.Gateway', forbidden)
    refused = _call(server, 'synapse_v2_read', {'operation': 'begin_consultation', 'arguments': {'expected_workspace_id': workspace_binding(clone)['id']}})
    assert refused.isError
    assert 'another workspace' in refused.content[0].text
    assert 'session_token' not in refused.content[0].text
