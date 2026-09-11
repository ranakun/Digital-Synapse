from contextlib import contextmanager

import pytest
from test_v2_read_view import _commit, _legacy
from test_v2_source_purpose import _classify, corpus  # noqa: F401
from typer.testing import CliRunner

from synapse import cli, mcpserver, v2_runtime
from synapse.read_view import ReadView
from synapse.source_purpose import load_source_purposes
from synapse.v2_migration import copy_stable


@pytest.mark.parametrize("command,runner_name", [
    ("mcp", "run_mcp_server"), ("mcp-http", "run_mcp_http_server"),
])
def test_mcp_semantics_opt_in_and_cleanup(tmp_path, monkeypatch, command, runner_name):
    vault = tmp_path / "vault"
    _commit(vault, [_legacy("me")])
    calls = []

    @contextmanager
    def warm(path):
        calls.append("open")
        try:
            yield
        finally:
            calls.append("close")

    def serve(path, **kwargs):
        calls.append("serve")

    monkeypatch.setattr(v2_runtime, "warm_semantics", warm)
    monkeypatch.setattr(mcpserver, runner_name, serve)
    runner = CliRunner()
    args = [command, "--vault", str(vault)]
    assert runner.invoke(cli.app, args).exit_code == 0
    assert calls == ["serve"]
    calls.clear()
    assert runner.invoke(cli.app, args + ["--semantic"]).exit_code == 0
    assert calls == ["open", "serve", "close"]
    calls.clear()

    def fail(path, **kwargs):
        raise RuntimeError("synthetic server failure")

    monkeypatch.setattr(mcpserver, runner_name, fail)
    assert runner.invoke(cli.app, args + ["--semantic"]).exit_code != 0
    assert calls == ["open", "close"]
    calls.clear()
    result = runner.invoke(cli.app, [command, "--vault", str(tmp_path / "v1"), "--semantic"])
    assert result.exit_code != 0
    assert "requires an activated v2 vault" in result.output
    assert calls == []


def test_backup_restore_retains_source_policy_but_rebuilds_indexes(tmp_path, corpus):  # noqa: F811
    store, sources, _ = corpus
    policy = _classify(store, sources)
    ReadView(store.vault)
    backup = tmp_path / "backup"
    restored = tmp_path / "restored"
    report = copy_stable(store.vault, backup)
    assert report == copy_stable(backup, restored)
    assert not (restored / ".synapse" / "v2-indexes").exists()
    view = ReadView(restored)
    assert view.revision == store.head()
    assert load_source_purposes(restored, revision=view.revision).metadata() == policy.metadata()
    assert view.records(ids=["me"]) == ReadView(store.vault).records(ids=["me"])
