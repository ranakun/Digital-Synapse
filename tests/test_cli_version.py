from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse import __version__
from synapse.cli import _configure_utf8_console, app


def test_cli_version_flag_reports_package_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_package_version_matches_project_metadata() -> None:
    metadata = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert metadata["project"]["version"] == __version__


def test_windows_cli_reconfigures_console_streams_to_utf8(monkeypatch) -> None:
    class FakeStream:
        encoding = "cp1252"

        def reconfigure(self, *, encoding: str) -> None:
            self.encoding = encoding

    stdout = FakeStream()
    stderr = FakeStream()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    _configure_utf8_console()

    assert stdout.encoding == "utf-8"
    assert stderr.encoding == "utf-8"
