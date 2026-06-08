from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse import __version__
from synapse.cli import app


def test_cli_version_flag_reports_package_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_package_version_matches_project_metadata() -> None:
    metadata = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert metadata["project"]["version"] == __version__
