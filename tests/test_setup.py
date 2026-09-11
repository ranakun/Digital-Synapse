from __future__ import annotations

import asyncio
import json
import sys
import tomllib

import pytest
from typer.testing import CliRunner

from synapse import setup
from synapse.cli import app
from synapse.config import resolve_timezone
from synapse.host_session import NativeHost
from synapse.read_view import ReadView
from synapse.v2_contracts import V2Error


def test_setup_is_empty_resumable_and_workspace_scoped(tmp_path):
    home = tmp_path / "My Synapse"
    first = setup.initialize(home, timezone="Europe/London", purpose="Reading")
    second = setup.initialize(home)
    assert first["knowledge_revision"] == second["knowledge_revision"]
    assert second["timezone"] == "Europe/London"
    assert not second["connection_verified_in_current_task"]
    assert not ReadView(home / "vault").manifest["records"]
    assert ReadView(home / "vault").timezone == "Europe/London"
    config = tomllib.loads((home / "workspace/.codex/config.toml").read_text())
    assert config["mcp_servers"]["digital_synapse"]["args"][-1] == str(home)
    assert (home / "workspace/synapse-guide/ROLE.md").exists()


def test_unrelated_install_and_modified_config_are_not_overwritten(tmp_path):
    home = tmp_path / "existing"
    home.mkdir()
    (home / "note.md").write_text("keep")
    with pytest.raises(V2Error):
        setup.initialize(home)
    assert list(home.iterdir()) == [home / "note.md"]
    own = tmp_path / "new"
    setup.initialize(own)
    config = own / "workspace/.codex/config.toml"
    config.write_text("keep = true\n")
    with pytest.raises(V2Error, match="overwrite"):
        setup.initialize(own)
    assert config.read_text() == "keep = true\n"


def test_setup_cli_and_text_preparation(tmp_path):
    home = tmp_path / "setup"
    runner = CliRunner()
    result = runner.invoke(app, ["setup", "initialize", "--home", str(home)])
    assert result.exit_code == 0, result.output
    revision = json.loads(result.output)["knowledge_revision"]
    result = runner.invoke(app, ["setup", "prepare", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["knowledge_revision"] == revision
    assert setup.settings(home)["semantic"] is False
    assert resolve_timezone(home / "vault") == "UTC"


def test_codex_registration_refuses_different_existing_server(tmp_path, monkeypatch):
    home = tmp_path / "synapse"
    setup.initialize(home)
    host = tmp_path / "codex"
    host.mkdir()
    config = host / "config.toml"
    config.write_text('[mcp_servers.digital_synapse]\ncommand="another"\n')
    monkeypatch.setattr(setup, "locate_codex", lambda: "/synthetic/codex")

    def forbidden(*a, **kw):
        pytest.fail("must not replace a different connection")

    monkeypatch.setattr(setup.subprocess, "run", forbidden)
    with pytest.raises(V2Error):
        setup.connect_codex(home, codex_home=host)
    assert "another" in config.read_text()


def test_selected_source_then_real_stdio_mcp_read(tmp_path):
    """Real subprocess/transport with synthetic host events; no Codex/model call."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    home = tmp_path / "spaces in install"
    setup.initialize(home, timezone="America/New_York")
    material = tmp_path / "note.md"
    material.write_text("Project Cedar connects community gardening and soil research.")
    event = {"id": "synthetic-save", "actor": "user", "text": "Save this selected gardening note."}

    class NoModel:
        def prepare(self, state):
            return {
                "status": "partial",
                "reason": "No automatic interpretation in this synthetic fixture.",
            }

    host = NativeHost(
        home / "vault", event_reader=lambda _: event, display=lambda _: None, reasoner=NoModel()
    )
    receipt = host.capture_file("synthetic-save", material)
    assert receipt["knowledge_revision"]
    setup.prepare(home)

    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "synapse", "setup", "mcp", "--home", str(home)]
        )
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as client,
        ):
            await client.initialize()
            names = {t.name for t in (await client.list_tools()).tools}
            assert names == {"synapse_v2_describe", "synapse_v2_read"}
            described = json.loads(
                (await client.call_tool("synapse_v2_describe", {})).content[0].text
            )
            revision = described["knowledge_revision"]
            opened = json.loads(
                (
                    await client.call_tool(
                        "synapse_v2_read",
                        {"operation": "begin_consultation", "arguments": {"preset": "consult"}},
                    )
                )
                .content[0]
                .text
            )
            token = opened["session_token"]
            result = await client.call_tool(
                "synapse_v2_read",
                {
                    "operation": "search_sources",
                    "arguments": {"query": "gardening"},
                    "session_token": token,
                    "revision": revision,
                },
            )
            assert not result.isError, result
            text = result.content[0].text
            assert "gardening" in text.lower()
            ended = await client.call_tool(
                "synapse_v2_read", {"operation": "end_consultation", "session_token": token}
            )
            assert not ended.isError

    asyncio.run(run())


def test_semantic_preparation_uses_actual_index_report_contract(tmp_path, monkeypatch):
    home = tmp_path / "semantic-install"
    setup.initialize(home)
    monkeypatch.setattr("synapse.embeddings.FastEmbedder", lambda *a, **kw: object())
    monkeypatch.setattr(
        "synapse.v2_semantic.build_index",
        lambda *a, **kw: {"semantic": "ok", "index_state": "current"},
    )
    result = setup.prepare(home, semantic=True)
    assert result["status"] == "ready"
    assert setup.settings(home)["semantic"] is True


def test_modified_generated_guide_is_preserved_before_any_update(tmp_path):
    home = tmp_path / "guides"
    setup.initialize(home)
    guide = home / "workspace/AGENTS.md"
    custom = guide.read_text() + "\nMy custom preference.\n"
    guide.write_text(custom)
    config = home / "workspace/.codex/config.toml"
    previous = config.read_bytes()
    with pytest.raises(V2Error, match="overwrite"):
        setup.initialize(home)
    assert guide.read_text() == custom
    assert config.read_bytes() == previous


def test_invalid_timezone_does_not_poison_resumable_setup(tmp_path):
    home = tmp_path / "timezone"
    result = CliRunner().invoke(
        app, ["setup", "initialize", "--home", str(home), "--timezone", "Unknown/Place"]
    )
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "invalid-request"
    assert not home.exists()
    assert setup.initialize(home)["status"] == "ready"


def test_semantic_off_is_explicit_and_requires_service_restart(tmp_path):
    home = tmp_path / "switch"
    setup.initialize(home)
    value = setup.settings(home)
    value["semantic"] = True
    setup._write(home, value)
    assert setup.prepare(home)["semantic_enabled"] is True
    result = CliRunner().invoke(app, ["setup", "prepare", "--home", str(home), "--no-semantic"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["read_service_restart_required"] is True
    assert setup.settings(home)["semantic"] is False


def test_save_assistant_message_retains_attribution_not_user_authority(tmp_path):
    home = tmp_path / "assistant-capture"
    setup.initialize(home)
    events = {
        "instruction": {"id": "instruction", "actor": "user", "text": "Save that answer."},
        "answer": {
            "id": "answer",
            "actor": "assistant",
            "text": "An unreviewed suggestion about gardening.",
        },
    }

    class NoModel:
        def prepare(self, state):
            return {"status": "partial"}

    host = NativeHost(
        home / "vault", event_reader=events.__getitem__, display=lambda _: None, reasoner=NoModel()
    )
    host.capture_message("instruction", "answer")
    view = ReadView(home / "vault")
    descriptor = next(iter(view.manifest["source_versions"].values()))
    assert ":assistant:answer" in descriptor["origin"]
    with pytest.raises(V2Error, match="actual owner"):
        host.capture_message("answer", "answer")
