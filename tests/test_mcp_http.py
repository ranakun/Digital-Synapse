"""Streamable HTTP and concurrency tests for the shared MCP service."""

from __future__ import annotations

import asyncio
import shutil
import threading
import time
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import synapse.mcpserver as mcpserver
from synapse.index import reindex

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    target = tmp_path / "vault"
    shutil.copytree(
        FIXTURE_VAULT,
        target,
        ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm"),
    )
    reindex(target, full=True)
    return target


@pytest.fixture()
def http_app(vault: Path):
    mcpserver.mcp.settings.stateless_http = True
    mcpserver._prepare_server(vault, prewarm=False)
    app = mcpserver.mcp.streamable_http_app()
    try:
        yield app
    finally:
        mcpserver._close_server()


async def _call_tool(app, name: str, arguments: dict | None = None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8765",
    ) as http_client:
        async with streamable_http_client(
            "http://127.0.0.1:8765/mcp",
            http_client=http_client,
        ) as (read, write, get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert get_session_id() is None
                return await session.call_tool(name, arguments or {})


@pytest.mark.anyio
async def test_streamable_http_smoke_and_concurrency(
    http_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with http_app.router.lifespan_context(http_app):
        result = await _call_tool(http_app, "synapse_stats")

        assert result.content
        assert "Total Nodes:" in result.content[0].text

        original = mcpserver._synapse_stats_sync
        loop = asyncio.get_running_loop()
        workers_started = asyncio.Event()
        release = threading.Event()
        counter_lock = threading.Lock()
        entered = 0

        def slow_stats() -> str:
            nonlocal entered
            with counter_lock:
                entered += 1
                if entered == 2:
                    loop.call_soon_threadsafe(workers_started.set)
            assert release.wait(timeout=10), "Concurrent calls were not released"
            return original()

        monkeypatch.setattr(mcpserver, "_synapse_stats_sync", slow_stats)
        calls = [
            asyncio.create_task(_call_tool(http_app, "synapse_stats"))
            for _ in range(3)
        ]
        try:
            # The service permits two active tools; the third must wait its turn.
            await asyncio.wait_for(workers_started.wait(), timeout=5)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=http_app),
                base_url="http://127.0.0.1:8765",
            ) as client:
                health = await asyncio.wait_for(client.get("/healthz"), timeout=5)
            assert health.json()["status"] == "ok"
            assert entered == 2
            assert all(not call.done() for call in calls)
        finally:
            release.set()
            results = await asyncio.gather(*calls)

        assert all("Total Nodes:" in result.content[0].text for result in results)

        monkeypatch.setattr(mcpserver, "_synapse_stats_sync", original)
        mixed = await asyncio.gather(
            *[
                _call_tool(http_app, name, arguments)
                for name, arguments in (
                    ("synapse_stats", {}),
                    ("synapse_brief", {"ref": "me"}),
                    ("synapse_search", {"query": "owner"}),
                )
                for _ in range(3)
            ]
        )
        assert all(not item.isError for item in mixed)


def test_writer_preferred_lock_allows_readers_then_writer() -> None:
    lock = mcpserver._ReadWriteLock()
    release_readers = threading.Event()
    readers_started = threading.Barrier(3)
    writer_waiting = threading.Event()
    order: list[str] = []

    def reader(name: str) -> None:
        with lock.read():
            order.append(f"{name}-start")
            readers_started.wait()
            release_readers.wait(timeout=2)
            order.append(f"{name}-end")

    def writer() -> None:
        writer_waiting.set()
        with lock.write():
            order.append("writer")
            time.sleep(0.03)

    def late_reader() -> None:
        with lock.read():
            order.append("late-reader")

    first = threading.Thread(target=reader, args=("reader-1",))
    second = threading.Thread(target=reader, args=("reader-2",))
    first.start()
    second.start()
    readers_started.wait(timeout=2)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert writer_waiting.wait(timeout=1)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with lock._condition:
            if lock._waiting_writers:
                break
        time.sleep(0.005)
    else:
        pytest.fail("writer did not enter the lock queue")
    late = threading.Thread(target=late_reader)
    late.start()

    release_readers.set()
    for thread in (first, second, writer_thread, late):
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert order.index("writer") < order.index("late-reader")
