"""Explicit initialization for a source-first public v2 workspace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from synapse.config import config_path, resolve_timezone, resolve_vault
from synapse.owner_host import OwnerHost
from synapse.publication import Publisher, snapshot_fingerprint
from synapse.revisions import RevisionStore, durable_write, is_v2
from synapse.util import generate_ulid
from synapse.v2_contracts import V2Error, canonical_json

_MARKER_NAME = "public-workspace-initialization.json"
_MARKER_FORMAT = "public-workspace-initialization/1"


def _workspace_config(root: Path, timezone: str) -> str:
    """Write only local setup preferences, never an owner identity or fact."""
    path = config_path(root)
    value: dict[str, Any] = {"vault_path": ".", "workspace": {"timezone": timezone}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return timezone


def _marker_path(root: Path) -> Path:
    return root / ".synapse" / _MARKER_NAME


def _marker_timezone(root: Path) -> str | None:
    path = _marker_path(root)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_bytes())
        if set(value) != {"format", "timezone"} or value["format"] != _MARKER_FORMAT:
            return None
        return resolve_timezone(root, value["timezone"])
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _write_marker(root: Path, timezone: str) -> None:
    durable_write(
        _marker_path(root),
        canonical_json({"format": _MARKER_FORMAT, "timezone": timezone}),
    )


def _partial_workspace_timezone(root: Path) -> str | None:
    """Recognize only this initializer's own incomplete bootstrap footprint."""
    timezone = _marker_timezone(root)
    setup = root / ".synapse"
    state = root / "_synapse"
    if (
        timezone is None
        or not setup.is_dir()
        or setup.is_symlink()
        or {path.name for path in setup.iterdir()} != {_MARKER_NAME}
    ):
        return None
    root_entries = {path.name for path in root.iterdir()}
    if root_entries == {".synapse"}:
        return timezone
    if root_entries != {".synapse", "_synapse"} or not state.is_dir() or state.is_symlink():
        return None
    allowed = {_MARKER_NAME, "writer.lock", "approval-journal", "revisions"}
    if not {path.name for path in state.iterdir()} <= allowed:
        return None
    for directory in (state / "approval-journal", state / "revisions"):
        if directory.exists() and (
            not directory.is_dir()
            or directory.is_symlink()
            or any(not item.is_file() or item.is_symlink() for item in directory.iterdir())
        ):
            return None
    return timezone


def _clear_marker(root: Path) -> None:
    if _marker_timezone(root) is not None:
        _marker_path(root).unlink(missing_ok=True)


def initialize_workspace(path: str | Path, *, timezone: str = "UTC") -> dict:
    """Create or reopen an explicit, empty v2 workspace.

    The initial publication holds no records and grants no continuing capture
    authority. Its scoped capability only binds this exact empty baseline.
    """
    root = resolve_vault(path)
    try:
        selected_timezone = resolve_timezone(root, timezone)
    except ValueError as exc:
        raise V2Error("invalid-request", str(exc)) from exc

    if is_v2(root):
        store = RevisionStore(root)
        if not config_path(root).exists():
            configured_timezone = _marker_timezone(root) or selected_timezone
            _workspace_config(root, configured_timezone)
        else:
            try:
                configured_timezone = resolve_timezone(root)
            except ValueError as exc:
                raise V2Error("invalid-request", str(exc)) from exc
        _clear_marker(root)
        return {
            "workspace_path": str(root),
            "timezone": configured_timezone,
            "knowledge_revision": store.head(),
        }

    resumed_timezone = _partial_workspace_timezone(root) if root.exists() else None
    if root.exists() and any(root.iterdir()) and resumed_timezone is None:
        raise V2Error(
            "invalid-request",
            "Workspace initialization requires an empty destination or an initialized v2 workspace",
        )
    root.mkdir(parents=True, exist_ok=True)
    if resumed_timezone is None:
        _write_marker(root, selected_timezone)
    else:
        selected_timezone = resumed_timezone

    records: dict[str, bytes] = {}
    publisher = Publisher(root)
    capability = OwnerHost(publisher.store, host_id="explicit-workspace-initialization").record_instruction(
        "explicit-workspace-initialization",
        actions=["capture"],
        scope={"bootstrap": True, "snapshot_hash": snapshot_fingerprint(records)},
    )
    receipt = publisher.bootstrap(
        capability,
        operation_id=generate_ulid(),
        request_id=generate_ulid(),
        records=records,
        sources=[],
        objects={},
        legacy_edges=[],
    )
    result = {
        "workspace_path": str(root),
        "timezone": _workspace_config(root, selected_timezone),
        "knowledge_revision": receipt["knowledge_revision"],
    }
    _clear_marker(root)
    return result
