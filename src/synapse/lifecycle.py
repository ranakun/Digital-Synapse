"""Owned local viewer lifecycle and recoverable workspace copies.

The public functions deliberately operate on explicit paths.  The viewer has
no fixed port and its state file is only trusted after the child proves the
per-start ownership token over loopback HTTP.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from synapse.revisions import RevisionStore, durable_write, is_v2
from synapse.v2_contracts import canonical_json, hash_bytes
from synapse.web import SynapseHandler

_STATE_NAME = "viewer.json"
_LOCK_NAME = "viewer.lock"
_HEALTH_PATH = "/_synapse/lifecycle"
_STARTUP_SECONDS = 8.0


class LifecycleError(RuntimeError):
    """An explicit lifecycle request could not safely be completed."""


class _LoopbackHTTPServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # HTTPServer resolves a display hostname here; a local viewer needs no DNS.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def _run_dir(home: Path) -> Path:
    return Path(home).resolve() / "run"


def _state_path(home: Path) -> Path:
    return _run_dir(home) / _STATE_NAME


@contextmanager
def _lifecycle_lock(home: Path):
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - v2 itself requires POSIX
        raise LifecycleError("Viewer lifecycle requires a tested POSIX lock") from exc
    directory = _run_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    with (directory / _LOCK_NAME).open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write_state(home: Path, state: dict[str, Any]) -> None:
    path = _state_path(home)
    durable_write(path, canonical_json(state))
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _read_state(home: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(_state_path(home).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _valid_state(value: dict[str, Any]) -> bool:
    if value.get("status") != "running":
        return False
    if not isinstance(value.get("pid"), int) or value["pid"] <= 0:
        return False
    if not all(
        isinstance(value.get(key), str) and value[key]
        for key in ("url", "token", "vault", "started_revision")
    ):
        return False
    parsed = urlsplit(value["url"])
    try:
        port = parsed.port
    except ValueError:
        return False
    return parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and port is not None


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _health(state: dict[str, Any]) -> dict[str, Any] | None:
    if not _valid_state(state):
        return None
    request = Request(
        state["url"] + _HEALTH_PATH,
        headers={"X-Synapse-Ownership": state["token"]},
    )
    try:
        with urlopen(request, timeout=0.4) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeError, ValueError, URLError):
        return None
    if not (
        isinstance(value, dict)
        and value.get("status") == "running"
        and value.get("pid") == state["pid"]
        and value.get("vault") == state["vault"]
        and value.get("started_revision") == state["started_revision"]
        and isinstance(value.get("current_revision"), str)
        and secrets.compare_digest(str(value.get("token", "")), state["token"])
    ):
        return None
    return value


def _public_state(state: dict[str, Any], health: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {
        key: state[key]
        for key in ("status", "url", "pid", "vault", "started_revision")
        if key in state
    }
    if health is not None:
        result["current_revision"] = health["current_revision"]
    return result


def viewer_status(home: Path) -> dict[str, Any]:
    """Return the truthful state of the service recorded under ``home/run``."""
    state = _read_state(home)
    if state is None:
        return {"status": "missing"}
    if state.get("status") == "failed":
        return {"status": "crashed", "error": state.get("error", "viewer startup failed")}
    if not _valid_state(state):
        return {"status": "stale", "error": "viewer state is malformed"}
    health = _health(state)
    if health is not None:
        return _public_state(state, health)
    result = _public_state(state) | {"status": "stale"}
    if not _pid_is_alive(state["pid"]):
        result["status"] = "crashed"
    return result


def _require_current_head(vault: Path) -> str:
    vault = Path(vault).resolve()
    if not is_v2(vault):
        raise LifecycleError("Viewer and backup require an activated v2 workspace")
    store = RevisionStore(vault)
    head = store.head()
    store.manifest(head)
    return head


def start_viewer(home: Path, vault: Path) -> dict[str, Any]:
    """Start one owned detached viewer for a selected activated workspace."""
    home, vault = Path(home).resolve(), Path(vault).resolve()
    _require_current_head(vault)
    with _lifecycle_lock(home):
        state = _read_state(home)
        current = viewer_status(home)
        if current["status"] == "running":
            if state is not None and state.get("vault") == str(vault):
                return current
            raise LifecycleError("An owned viewer is already running for a different workspace or revision")
        if current["status"] == "stale":
            raise LifecycleError("Viewer state belongs to a live but unverified process; it was not touched")
        if state is not None:
            _state_path(home).unlink(missing_ok=True)

        token = secrets.token_urlsafe(32)
        command = [
            sys.executable,
            "-m",
            "synapse.lifecycle",
            "--child",
            "--home",
            str(home),
            "--vault",
            str(vault),
            "--token",
            token,
        ]
        with open(os.devnull, "wb") as sink:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=sink,
                start_new_session=True,
                close_fds=True,
            )
        deadline = time.monotonic() + _STARTUP_SECONDS
        while time.monotonic() < deadline:
            state = _read_state(home)
            if state is not None and state.get("status") == "failed":
                raise LifecycleError(str(state.get("error", "viewer startup failed")))
            health = _health(state) if state is not None and state.get("token") == token else None
            if state is not None and health is not None:
                return _public_state(state, health)
            time.sleep(0.05)
        raise LifecycleError("Viewer did not become ready before the startup deadline")


def stop_viewer(home: Path) -> dict[str, Any]:
    """Ask an authenticated owned viewer to stop; never signal an unproven PID."""
    home = Path(home).resolve()
    with _lifecycle_lock(home):
        state = _read_state(home)
        current = viewer_status(home)
        if state is None or current["status"] != "running" or _health(state) is None:
            return current | {"stopped": False}
        request = Request(
            state["url"] + _HEALTH_PATH,
            method="POST",
            headers={"X-Synapse-Ownership": state["token"]},
        )
        try:
            with urlopen(request, timeout=1.0) as response:
                if response.status != 200:
                    raise LifecycleError("Owned viewer refused its stop request")
        except (OSError, URLError) as exc:
            raise LifecycleError("Owned viewer could not be stopped") from exc
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if _health(state) is None:
                _state_path(home).unlink(missing_ok=True)
                return _public_state(state) | {"status": "stopped", "stopped": True}
            time.sleep(0.05)
        raise LifecycleError("Owned viewer did not stop before the shutdown deadline")


def _safe_workspace_files(root: Path):
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink() or not path.is_file():
            continue
        if any(part == ".env" or part.startswith(".env.") for part in relative.parts):
            continue
        if relative.parts[0] == ".synapse" and (
            len(relative.parts) > 1
            and (
                relative.parts[1] == "v2-indexes"
                or relative.parts[1] == "index.db"
                or relative.parts[1].startswith("index.db-")
                or relative.parts[1] == "query-log.jsonl"
            )
        ):
            continue
        yield path, relative


def _workspace_hashes(root: Path) -> dict[str, str]:
    return {relative.as_posix(): hash_bytes(path.read_bytes()) for path, relative in _safe_workspace_files(root)}


def _verify_retained_objects(vault: Path) -> str:
    """Validate every retained object reachable from the current revision history."""
    store = RevisionStore(vault)
    head = _require_current_head(vault)
    revision, seen = head, set()
    while revision:
        if revision in seen:
            raise LifecycleError("Retained revision history contains a cycle")
        seen.add(revision)
        manifest = store.manifest(revision)
        for record in manifest["records"].values():
            store.read_object(record["version"])
        for source in manifest["source_versions"].values():
            store.read_object(source["original_hash"])
            if source.get("text_version"):
                store.read_object(source["text_version"])
        revision = manifest.get("parent")
    return head


def _copy_workspace(source: Path, destination: Path) -> dict[str, Any]:
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_dir() or destination.exists() or destination == source or source in destination.parents:
        raise LifecycleError("Destination must be a new directory outside the source workspace")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".synapse-backup-", dir=destination.parent))
    try:
        store = RevisionStore(source)
        with store.writer_lock():
            before_head = _verify_retained_objects(source)
            before = _workspace_hashes(source)
            for path, relative in _safe_workspace_files(source):
                target = temporary / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target, follow_symlinks=False)
            after = _workspace_hashes(source)
            after_head = _verify_retained_objects(source)
            copied = _workspace_hashes(temporary)
        if before != after or before != copied or before_head != after_head:
            raise LifecycleError("Workspace changed during backup; no backup was published")
        if _verify_retained_objects(temporary) != before_head:
            raise LifecycleError("Copied workspace does not retain the selected revision")
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "backup": str(destination),
        "files": len(copied),
        "content_hash": hash_bytes(canonical_json(copied)),
        "knowledge_revision": before_head,
    }


def backup_workspace(vault: Path, destination: Path) -> dict[str, Any]:
    """Create a locked directory backup including durable host and revision state."""
    report = _copy_workspace(vault, destination)
    return report | {"status": "backed_up", "vault": str(Path(vault).resolve())}


def restore_workspace(backup: Path, destination: Path) -> dict[str, Any]:
    """Restore an explicit directory backup without treating host events as approvals."""
    report = _copy_workspace(backup, destination)
    return report | {
        "status": "restored",
        "backup_source": str(Path(backup).resolve()),
        "warnings": [
            "Pending host events remain evidence only and cannot become approval on another host.",
            "Retained receipts were copied unchanged and do not authorize new host actions.",
        ],
    }


def _child(home: Path, vault: Path, token: str) -> int:
    home, vault = Path(home).resolve(), Path(vault).resolve()
    try:
        head = _require_current_head(vault)
        if not token:
            raise LifecycleError("Missing viewer ownership token")
        handler_base = type("LifecycleSynapseHandler", (SynapseHandler,), {"vault": vault})

        class Handler(handler_base):
            def _owned(self) -> bool:
                return secrets.compare_digest(self.headers.get("X-Synapse-Ownership", ""), token)

            def _lifecycle_response(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                if urlsplit(self.path).path != _HEALTH_PATH:
                    super().do_GET()
                    return
                if not self._owned():
                    self._lifecycle_response(403, {"error": "ownership required"})
                    return
                try:
                    current_revision = _require_current_head(vault)
                except Exception:
                    self._lifecycle_response(503, {"error": "current revision is unavailable"})
                    return
                self._lifecycle_response(
                    200,
                    {
                        "status": "running",
                        "pid": os.getpid(),
                        "vault": str(vault),
                        "started_revision": head,
                        "current_revision": current_revision,
                        "token": token,
                    },
                )

            def do_POST(self) -> None:  # noqa: N802
                if urlsplit(self.path).path != _HEALTH_PATH:
                    super().do_POST()
                    return
                if not self._owned():
                    self._lifecycle_response(403, {"error": "ownership required"})
                    return
                self._lifecycle_response(200, {"status": "stopping"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()

        server = _LoopbackHTTPServer(("127.0.0.1", 0), Handler)
        state = {
            "status": "running",
            "url": f"http://127.0.0.1:{server.server_port}",
            "pid": os.getpid(),
            "token": token,
            "vault": str(vault),
            "started_revision": head,
        }
        _write_state(home, state)

        def terminate(_signum: int, _frame: Any) -> None:
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            server.server_close()
            current = _read_state(home)
            if current is not None and current.get("token") == token:
                _state_path(home).unlink(missing_ok=True)
        return 0
    except Exception as exc:
        _write_state(home, {"status": "failed", "error": str(exc)})
        return 1


def _main() -> int:
    parser = argparse.ArgumentParser(description="Digital Synapse owned viewer child")
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    if not args.child:
        parser.error("This module is launched by start_viewer")
    return _child(args.home, args.vault, args.token)


if __name__ == "__main__":  # pragma: no cover - executed in the child process
    raise SystemExit(_main())
