# ruff: noqa: E402
"""Smoke test for MCP stdio server."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

# Skip this test if mcp package is not installed
mcp = pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from synapse.index import reindex

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, v, ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm"))
    reindex(v, full=True)
    return v


@pytest.mark.anyio
async def test_mcp_server_smoke(vault: Path) -> None:
    # Set up stdio server parameters to spawn the subprocess
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from synapse.cli import app; app()", "mcp", "--vault", str(vault)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.instructions is not None
            assert "synapse_dossier" in initialized.instructions
            assert "synapse_warm_path" in initialized.instructions

            # 1. List tools
            tools_response = await session.list_tools()
            tools = [t.name for t in tools_response.tools]
            
            expected_tools = {
                "synapse_brief",
                "synapse_owner_context",
                "synapse_dossier",
                "synapse_warm_path",
                "synapse_search",
                "synapse_entity",
                "synapse_neighbors",
                "synapse_path",
                "synapse_stats",
                "synapse_reindex",
                "synapse_v2_describe",
                "synapse_v2_read",
            }
            assert set(tools) == expected_tools
            for et in expected_tools:
                assert et in tools, f"Expected tool {et} not found in registered tools: {tools}"

            # 2. Call synapse_stats
            result = await session.call_tool("synapse_stats")
            assert result.content is not None
            assert len(result.content) > 0
            text_result = result.content[0].text
            assert "Total Nodes:" in text_result
