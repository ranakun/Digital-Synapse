"""Exercise tools/list and tools/call through real MCP client/server sessions."""
from __future__ import annotations

import asyncio
import json

import pytest
import test_v2_gateway
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from test_v2_mcp import _vault

from synapse.revisions import RevisionStore
from synapse.source_purpose import write_source_purposes
from synapse.v2_mcp import make_server, register_tools

corpus = test_v2_gateway.corpus


async def call(client, operation, **fields):
    result = await client.call_tool("synapse_v2_read", {"operation": operation, **fields})
    assert len(result.content) == 1
    assert result.structuredContent is None
    assert len(result.model_dump_json(exclude_none=True)) <= fields.get("budget", 8000)
    return result, json.loads(result.content[0].text)


@pytest.mark.parametrize("field", ["budget_chars", "max_result_characters", "response_size", "approved_by"])
def test_unknown_top_level_fields_fail_before_read_and_charge_once(tmp_path, monkeypatch, field):
    vault, source_id = _vault(tmp_path)
    server = make_server(vault)

    async def scenario():
        async with create_connected_server_and_client_session(server) as client:
            _, started = await call(client, "begin_consultation")
            token = started["session_token"]
            with monkeypatch.context() as patch:
                def forbidden(*args, **kwargs):
                    pytest.fail("Rejected MCP arguments must not reach the gateway")
                patch.setattr("synapse.v2_mcp.dispatch", forbidden)
                for index, operation in enumerate(("record", "source"), 1):
                    result, value = await call(client, operation, arguments={"id": source_id},
                                               session_token=token, **{field: 32000})
                    assert result.isError
                    assert value["error"]["code"] == "invalid-request"
                    assert field in value["error"]["message"]
                    assert "top-level budget" in value["error"]["message"]
                    assert value["usage"]["calls"] == index
                    assert value["usage"]["expansions"] == index - 1
            result, value = await call(client, "record", arguments={"id": "me"}, session_token=token)
            assert not result.isError
            assert value["usage"]["calls"] == 3
            assert value["knowledge_revision"] == started["knowledge_revision"]
            _, ended = await call(client, "end_consultation", session_token=token)
            assert ended["usage"]["calls"] == 3
    asyncio.run(scenario())


def test_discovery_exact_example_and_both_strict_tool_schemas(tmp_path):
    vault, _ = _vault(tmp_path)
    server = make_server(vault)

    async def scenario():
        async with create_connected_server_and_client_session(server) as client:
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == {"synapse_v2_describe", "synapse_v2_read"}
            assert all(tool.inputSchema["additionalProperties"] is False for tool in listed.tools)
            result = await client.call_tool("synapse_v2_describe", {})
            assert not result.isError
            assert len(result.model_dump_json(exclude_none=True)) <= 16000
            discovery = json.loads(result.content[0].text)
            contract = discovery["consultation"]["response_budget"]
            assert (contract["field"], contract["default"], contract["maximum"]) == ("budget", 8000, 32000)
            bad = await client.call_tool("synapse_v2_describe", {"budget": 32000})
            assert bad.isError
            assert "accepts no arguments" in json.loads(bad.content[0].text)["error"]["message"]
            _, started = await call(client, "begin_consultation", arguments={"preset": "broad"})
            example = contract["example"]["arguments"]
            example.update(session_token=started["session_token"], revision=started["knowledge_revision"])
            result = await client.call_tool(contract["example"]["name"], example)
            assert not result.isError
            assert len(result.model_dump_json(exclude_none=True)) <= example["budget"]
            value = json.loads(result.content[0].text)
            assert value["items"][0]["complete"]
            assert value["usage"]["calls"] == 1
            await call(client, "end_consultation", session_token=started["session_token"])
    asyncio.run(scenario())


@pytest.mark.parametrize("budget", [256, 512, 1024, 8000, 32000])
def test_unknown_fields_bound_diagnostics_and_keep_receipts(tmp_path, budget):
    vault, _ = _vault(tmp_path)

    async def scenario():
        async with create_connected_server_and_client_session(make_server(vault)) as client:
            _, started = await call(client, "begin_consultation")
            result, value = await call(client, "passage", session_token=started["session_token"],
                                       budget=budget, **{'"\\😀' * 10000: "ignored"})
            assert result.isError
            assert value["error"]["code"] == "invalid-request"
            assert value["usage"]["calls"] == value["usage"]["expansions"] == 1
            await call(client, "end_consultation", session_token=started["session_token"])
    asyncio.run(scenario())


def test_rejected_begin_end_and_exhaustion_cannot_reset_or_release_session(tmp_path):
    vault, _ = _vault(tmp_path)

    async def scenario():
        async with create_connected_server_and_client_session(make_server(vault)) as client:
            result, value = await call(client, "begin_consultation", budget_chars=32000)
            assert result.isError and "session_token" not in value
            _, started = await call(client, "begin_consultation")
            token = started["session_token"]
            _, value = await call(client, "begin_consultation", session_token=token, budget_chars=32000)
            assert value["usage"]["calls"] == 1
            _, value = await call(client, "end_consultation", session_token=token, budget_chars=32000)
            assert value["usage"]["calls"] == 1 and "status" not in value
            for index in range(2, 9):
                _, value = await call(client, "source", session_token=token, budget_chars=32000)
                assert value["usage"]["calls"] == index
            _, value = await call(client, "record", session_token=token, budget_chars=32000)
            assert value["error"]["code"] == "coverage-limited"
            assert "exhausted" in value["error"]["message"]
            _, value = await call(client, "schema", session_token=token, budget_chars=32000)
            assert value["error"]["code"] == "invalid-request" and value["usage"]["calls"] == 8
            _, ended = await call(client, "end_consultation", session_token=token)
            assert ended["status"] == "ended" and ended["usage"]["calls"] == 8
    asyncio.run(scenario())


def test_shared_server_leaves_legacy_argument_handling_unchanged(tmp_path):
    vault, _ = _vault(tmp_path)
    server = FastMCP("synthetic-shared")

    @server.tool()
    def legacy_echo(text: str) -> str:
        return text

    register_tools(server, lambda: vault)

    async def scenario():
        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool("legacy_echo", {"text": "legacy", "extra": 42})
            assert not result.isError and result.content[0].text == "legacy"
            result, _ = await call(client, "record", arguments={"id": "me"}, extra=42)
            assert result.isError
    asyncio.run(scenario())


def test_complete_qualification_recovery_does_not_strip_evidence(corpus):
    vault, store, add = corpus
    root = add(availability="accepted", statement="Synthetic root " + "detail " * 450)
    correction = add(statement="Synthetic qualification " + "condition " * 450,
                     context_refs=[{"id": root["id"], "version": root["version"],
                                    "role": "qualifies", "scope": "One synthetic observation."}])
    revision = store.head()

    async def scenario():
        async with create_connected_server_and_client_session(make_server(vault)) as client:
            _, started = await call(client, "begin_consultation", arguments={"preset": "broad"})
            fields = {"session_token": started["session_token"], "revision": started["knowledge_revision"],
                      "arguments": {"ids": [root["id"]], "knowledge_policy": "mixed"}}
            _, small = await call(client, "context", **fields)
            assert not small.get("items")
            assert small.get("error") or small["budget"]["omitted_units"]
            result, large = await call(client, "context", budget=24000, **fields)
            assert not result.isError
            unit = large["items"][0]
            assert unit["complete"]
            records = {record["id"]: record for record in unit["records"]}
            for record in (root, correction):
                assert records[record["id"]]["statement"] == record["statement"]
                assert records[record["id"]]["evidence"] == record["evidence"]
                assert records[record["id"]]["conditions_and_limits"] == record["conditions_and_limits"]
            assert large["usage"]["calls"] == 2
            await call(client, "end_consultation", session_token=started["session_token"])
    asyncio.run(scenario())
    assert store.head() == revision


def test_policy_drift_remains_rejected_after_argument_failure(tmp_path):
    vault, source = _vault(tmp_path)
    store = RevisionStore(vault)

    async def scenario():
        async with create_connected_server_and_client_session(make_server(vault)) as client:
            _, started = await call(client, "begin_consultation")
            token = started["session_token"]
            await call(client, "source", session_token=token, budget_chars=32000)
            write_source_purposes(vault, revision=started["knowledge_revision"], classifications=[{
                "source_id": source, "source_version": store.manifest()["sources"][source],
                "purpose": "internal", "reason": "Synthetic drift."}])
            result, value = await call(client, "source", arguments={"id": source}, session_token=token)
            assert result.isError and value["error"]["code"] == "stale-selection"
            assert value["usage"]["calls"] == value["usage"]["expansions"] == 2
            await call(client, "end_consultation", session_token=token)
    asyncio.run(scenario())


def test_passage_at_transport_ceiling_gives_bounded_truthful_recovery(tmp_path):
    from synapse.source_store import evidence_ref, prepare_source
    from synapse.util import generate_ulid
    from synapse.v2_contracts import hash_bytes

    vault, _ = _vault(tmp_path)
    store = RevisionStore(vault)
    descriptor, objects = prepare_source(('Exact "evidence" café 😀\\\n' * 1800).encode(),
                                         origin="synthetic-large.txt")
    store.transact(operation_id=generate_ulid(), request_id=generate_ulid(), kind="capture",
                   payload_hash=hash_bytes(b"large synthetic evidence"),
                   mutate=lambda manifest, _read: (
                       manifest["sources"].update({descriptor["id"]: descriptor["version"]}),
                       manifest["source_versions"].update({descriptor["version"]: descriptor}),
                   ), objects=objects)
    evidence = evidence_ref(descriptor, store.read_object, 0, 40000)
    revision = store.head()

    async def scenario():
        async with create_connected_server_and_client_session(make_server(vault)) as client:
            _, started = await call(client, "begin_consultation", arguments={"preset": "broad"})
            result, value = await call(client, "passage", arguments={"evidence": evidence, "context_characters": 0},
                                       budget=32000, session_token=started["session_token"])
            assert result.isError and "text" not in value
            message = value["error"]["message"]
            assert "maximum 32000" in message and "At the ceiling" in message
            assert "withhold conclusions" in message and "same session_token" in message
            assert value["usage"]["calls"] == value["usage"]["expansions"] == 1
            await call(client, "end_consultation", session_token=started["session_token"])
    asyncio.run(scenario())
    assert store.head() == revision


def test_nested_budget_is_not_an_alias_and_failures_keep_pinned_revision(tmp_path):
    from synapse.util import generate_ulid
    from synapse.v2_contracts import hash_bytes

    vault, _ = _vault(tmp_path)
    store = RevisionStore(vault)

    async def scenario():
        async with create_connected_server_and_client_session(make_server(vault)) as client:
            _, started = await call(client, "begin_consultation")
            token = started["session_token"]
            result, value = await call(client, "context", arguments={"ids": ["me"], "budget": 24000}, session_token=token)
            assert result.isError and value["error"]["code"] == "invalid-request"
            assert value["usage"]["calls"] == 1
            store.transact(operation_id=generate_ulid(), request_id=generate_ulid(), kind="capture",
                           payload_hash=hash_bytes(b"synthetic revision advance"),
                           mutate=lambda _manifest, _read: None, objects={})
            assert store.head() != started["knowledge_revision"]
            result, value = await call(client, "context", arguments={"ids": ["me"]}, budget=12000, session_token=token)
            assert not result.isError and value["knowledge_revision"] == started["knowledge_revision"]
            assert value["usage"]["calls"] == 2
            await call(client, "end_consultation", session_token=token)
    asyncio.run(scenario())
