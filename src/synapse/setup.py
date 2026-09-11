"""Small, resumable installation protocol; no implicit capture or investigation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path

from synapse import __version__
from synapse.codex_host import locate_codex
from synapse.gateway import Gateway, workspace_binding
from synapse.revisions import RevisionStore, durable_write
from synapse.v2_contracts import V2Error, canonical_json, hash_bytes

FORMAT = "synapse-setup/1"


def default_home() -> Path:
    return Path.home() / "Library/Application Support/Digital Synapse"


@contextmanager
def setup_lock(home: Path):
    import fcntl

    home.mkdir(parents=True, exist_ok=True)
    with (home / ".setup.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def settings(home: Path) -> dict:
    home = Path(home).expanduser().resolve()
    try:
        value = json.loads((home / "setup.json").read_bytes())
        if value["format"] != FORMAT or value["home"] != str(home):
            raise ValueError("different installation")
        if Path(value["vault"]).resolve() != home / "vault":
            raise ValueError("unexpected workspace path")
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise V2Error(
            "setup-required", "Start or resume Synapse setup for this installation."
        ) from exc


def _write(home: Path, value: dict):
    durable_write(home / "setup.json", canonical_json(value))


def initialize(home: Path, *, timezone="UTC", purpose="") -> dict:
    from zoneinfo import ZoneInfo

    from synapse.public_workspace import initialize_workspace

    try:
        ZoneInfo(timezone)  # reject invalid choice before persisting a setup journal
    except (KeyError, ValueError) as exc:
        raise V2Error(
            "invalid-request", "Choose a valid IANA timezone, such as Europe/London."
        ) from exc
    home = Path(home).expanduser().resolve()
    # Refuse an unrelated installation before creating a lock or other files.
    if home.exists() and not (home / "setup.json").exists():
        allowed = {".install-owner", ".setup.lock", "runtime", "tools", "models"}
        if any(p.name not in allowed for p in home.iterdir()):
            raise V2Error(
                "invalid-path",
                "This folder already contains data. Choose a new Synapse installation folder.",
            )
    with setup_lock(home):
        if (home / "setup.json").exists():
            value = settings(home)
            if value["timezone"] != timezone and timezone != "UTC":
                raise V2Error(
                    "precondition-conflict",
                    "Existing workspace timezone differs; resume using its saved timezone.",
                )
        else:
            value = {
                "format": FORMAT,
                "home": str(home),
                "vault": str(home / "vault"),
                "timezone": timezone,
                "purpose": purpose,
                "phase": "initializing",
                "semantic": False,
                "runtime_python": sys.executable,
            }
            _write(home, value)  # resume a partial initialization, not an implicit import
        result = initialize_workspace(Path(value["vault"]), timezone=value["timezone"])
        value["phase"] = "ready-to-add-material"
        value["runtime_python"] = sys.executable
        _write(home, value)
        _write_workspace_guide(home, value)
    return status(home) | {"initialization": result}


def _write_workspace_guide(home: Path, value: dict):
    folder = home / "workspace"
    folder.mkdir(exist_ok=True)
    integration = Path(__file__).parent / "integrations"
    if not integration.exists():
        integration = Path(__file__).resolve().parents[2] / "integrations/synapse"
    target = folder / "synapse-guide"
    target.mkdir(exist_ok=True)
    files = {}
    for name in ("ROLE.md", "NATIVE-CODEX.md", "README.md"):
        source = integration / name
        if source.exists():
            files[target / name] = source.read_bytes()
    command = json.dumps([value["runtime_python"], "-m", "synapse", "setup"], ensure_ascii=False)
    guide = f"""# Use Digital Synapse

This folder is a conversation workspace, not the implementation repository.
Read synapse-guide/NATIVE-CODEX.md for native specialist delegation and trusted writes.
Read synapse-guide/ROLE.md for evidence and retrieval rules.

Installation: {home}
Vault: {value["vault"]}
Expected workspace binding ID: {workspace_binding(Path(value["vault"]))["id"]}
Setup command argv (append operation and --home with the installation above): {command}

Ask about the user's purpose and what material they explicitly want to save. Start empty if they prefer.
Do not treat setup conversation as retained personal knowledge. Save only when asked.
Use setup status; setup open opens the actual map. No background investigations.
Before saying ready, compare native MCP describe.workspace.id with the expected binding above, begin with expected_workspace_id set to it, and verify one actual read in this task. Missing identity or a mismatch means the connection is not ready. Open this workspace in a new Codex task; do not silently reuse another connection or change global settings.
For normal questions delegate a fresh native worker with the actual question/context, the role, and the visible read tools.
If tools are missing, explain the connection/restart step; never substitute raw vault scanning.
Use owner events from the actual Codex task for saving/review; never synthesize approval.
Keep user instructions in plain language; execute technical commands on their behalf when authorized.
"""
    files[folder / "AGENTS.md"] = guide.encode()
    # Project-local connection only. Global connection is an explicit separate operation.
    config = folder / ".codex/config.toml"
    config.parent.mkdir(exist_ok=True)
    body = (
        "# Managed Digital Synapse connection\n[mcp_servers.digital_synapse]\n"
        f"command = {json.dumps(value['runtime_python'])}\n"
        f"args = {json.dumps(['-m', 'synapse', 'setup', 'mcp', '--home', str(home)])}\n"
        "startup_timeout_sec = 120\ntool_timeout_sec = 60\n"
    )
    files[config] = body.encode()
    prior = value.get("managed_files", {})
    for path, content in files.items():
        if path.exists():
            current = path.read_bytes()
            if current != content and hash_bytes(current) != prior.get(str(path.relative_to(home))):
                raise V2Error(
                    "external-edit-conflict",
                    "Setup will not overwrite edited workspace instructions or MCP settings; preserve and reconcile them first.",
                )
    for path, content in files.items():
        durable_write(path, content)
    value["managed_files"] = {
        str(path.relative_to(home)): hash_bytes(content) for path, content in files.items()
    }
    _write(home, value)


def status(home: Path) -> dict:
    home = Path(home).expanduser().resolve()
    value = settings(home)
    from synapse.lifecycle import viewer_status

    try:
        described = Gateway(Path(value["vault"])).describe()
    except (V2Error, OSError, ValueError) as exc:
        return {
            "status": "initialization-incomplete",
            "home": str(home),
            "next": "Resume setup initialize with the same home.",
            "detail": str(exc),
        }
    return {
        "status": "ready",
        "version": __version__,
        "home": str(home),
        "vault": value["vault"],
        "workspace": str(home / "workspace"),
        "workspace_binding": described["workspace"],
        "knowledge_revision": described["knowledge_revision"],
        "phase": value["phase"],
        "timezone": value["timezone"],
        "purpose": value["purpose"],
        "semantic_enabled": value["semantic"],
        "semantic_capability": described.get("semantic_capability"),
        "viewer": viewer_status(home),
        "codex_available": locate_codex() is not None,
        "connection_verified_in_current_task": False,
        "next": "Open the conversation workspace in Codex; verify MCP describe/read, choose material, then try a real question.",
    }


def prepare(home: Path, *, semantic: bool | None = None) -> dict:
    home = Path(home).expanduser().resolve()
    value = settings(home)
    vault = Path(value["vault"])
    from synapse.read_view import ReadView

    with setup_lock(home):
        previously_enabled = value["semantic"]
        view = ReadView(vault)
        report = {
            "knowledge_revision": view.revision,
            "text_index": "ready",
            "semantic": "disabled",
        }
        if semantic:
            from synapse.config import load_config
            from synapse.embeddings import FastEmbedder
            from synapse.v2_semantic import build_index

            os.environ["SYNAPSE_MODEL_CACHE"] = str(home / "models")
            embedder = FastEmbedder(load_config(vault)["embeddings"]["model"], threads=2)
            report["semantic"] = build_index(vault, embedder=embedder)
            if report["semantic"].get("semantic") != "ok":
                return report | {
                    "status": "partial",
                    "next": "Text search is usable. Review semantic preparation status before enabling it.",
                }
            value["semantic"] = True
            _write(home, value)
        if semantic is False:
            value["semantic"] = False
            _write(home, value)
        changed = previously_enabled != value["semantic"]
        return report | {
            "status": "ready",
            "semantic_enabled": value["semantic"],
            "read_service_restart_required": changed,
            "next": "Reconnect the Synapse MCP service before expecting the changed semantic setting."
            if changed
            else "Use describe in the actual read service to verify capability.",
        }


def connect_codex(home: Path, *, codex_home: Path | None = None) -> dict:
    """Explicit host registration; refuse conflicts instead of replacing settings."""
    home = Path(home).expanduser().resolve()
    value = settings(home)
    codex = locate_codex()
    if not codex:
        raise V2Error(
            "setup-required", "Install/open Codex and sign in, then ask it to resume this setup."
        )
    root = (
        Path(codex_home or os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        .expanduser()
        .resolve()
    )
    path = root / "config.toml"
    expected = {
        "command": value["runtime_python"],
        "args": ["-m", "synapse", "setup", "mcp", "--home", str(home)],
    }
    existing = (
        tomllib.loads(path.read_text()).get("mcp_servers", {}).get("digital_synapse")
        if path.exists()
        else None
    )
    if existing is not None:
        if all(existing.get(key) == val for key, val in expected.items()) and existing.get(
            "enabled", True
        ):
            return {"status": "already-connected", "restart_or_new_task_required": True}
        raise V2Error(
            "external-edit-conflict",
            "A different digital_synapse connection already exists. Keep it intact and resolve the choice explicitly.",
        )
    result = subprocess.run(
        [codex, "mcp", "add", "digital_synapse", "--", expected["command"], *expected["args"]],
        env={**os.environ, "CODEX_HOME": str(root)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise V2Error(
            "setup-required",
            "Codex could not register the connection. Check the Codex setup and retry.",
        )
    return {
        "status": "connected",
        "restart_or_new_task_required": True,
        "next": "Start a new Codex task and verify actual Synapse tool access before claiming readiness.",
    }


def run_mcp(home: Path):
    """Codex owns this stdio process; only the two read-only v2 tools are exposed."""
    from contextlib import ExitStack

    from mcp.server.fastmcp import FastMCP

    from synapse.v2_mcp import register_tools
    from synapse.v2_runtime import warm_semantics

    home = Path(home).expanduser().resolve()
    value = settings(home)
    vault = Path(value["vault"])
    RevisionStore(vault).head()
    os.environ["SYNAPSE_MODEL_CACHE"] = str(home / "models")
    server = FastMCP(
        "Digital Synapse",
        instructions="User-owned knowledge across topics. Consult with the Synapse specialist role. Begin with synapse_v2_describe, pin the revision and use metered reads. Preserve sources, corrections and uncertainty. Ordinary reads save nothing. No write or approval tools are exposed.",
    )
    register_tools(server, lambda: vault)
    with ExitStack() as stack:
        if value["semantic"]:
            try:
                stack.enter_context(warm_semantics(vault))
            except (V2Error, RuntimeError, OSError) as exc:
                print(
                    f"Synapse semantic runtime unavailable ({type(exc).__name__}); exact/text reads remain available.",
                    file=sys.stderr,
                )
        server.run(transport="stdio")
