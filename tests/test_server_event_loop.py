"""Regression test for a real bug found via live docker-compose verification
against Postgres: app/mcp_server/server.py's main() used to call
`asyncio.run(_bootstrap())` (its own throwaway event loop) and then
separately start uvicorn/mcp.run(), which spins up a SECOND event loop to
actually serve requests.

app/db/session.py's engine is a module-level singleton, and asyncpg binds
every connection it opens to whichever event loop was running when the
connection was created — reusing a pooled connection from a different
(and by then already-closed) event loop raised
"asyncpg.exceptions.InterfaceError: cannot perform operation: another
operation is in progress" on the first identity_create_user call once this
was actually run against Postgres via docker-compose (SQLite/aiosqlite
doesn't bind connections to a loop the same way, so this was silent under
the SQLite-only setup this project had only ever actually run before).

The fix (_serve()) runs bootstrap and the serving loop as two `await`s in
ONE coroutine, itself run via a single `asyncio.run()` call in main() — so
both share the same event loop, and thus the same live asyncpg connections.
These tests assert that structural invariant directly, without needing a
real Postgres connection.
"""

import asyncio

import pytest

from app.mcp_server import server as server_module


async def test_serve_bootstraps_and_serves_stdio_in_one_coroutine(monkeypatch):
    """_serve("stdio") must await _bootstrap() and run_stdio_async() from
    within the SAME coroutine (and therefore the same event loop) — not via
    a nested asyncio.run(), which would open a second loop."""
    calls = []

    async def fake_bootstrap():
        calls.append("bootstrap")

    async def fake_run_stdio_async():
        calls.append("serve")

    monkeypatch.setattr(server_module, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr(server_module.mcp, "run_stdio_async", fake_run_stdio_async)

    await server_module._serve("stdio")

    assert calls == ["bootstrap", "serve"]


async def test_serve_bootstraps_and_serves_http_in_one_coroutine(monkeypatch):
    """Same invariant for the http transport: bootstrap and the uvicorn
    server's .serve() must run in the same coroutine/event loop as each
    other, via uvicorn.Server(...).serve() (async), not the synchronous
    uvicorn.run() (which internally opens its own new event loop via
    asyncio.run(), the same class of bug this test file guards against)."""
    import uvicorn

    calls = []

    async def fake_bootstrap():
        calls.append("bootstrap")

    monkeypatch.setattr(server_module, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr(
        server_module, "_authenticated_streamable_http_app", lambda: object()
    )

    class _FakeServer:
        def __init__(self, config):
            calls.append("serve")

        async def serve(self):
            pass

    monkeypatch.setattr(uvicorn, "Server", _FakeServer)

    await server_module._serve("http")

    assert calls == ["bootstrap", "serve"]


def test_main_makes_exactly_one_asyncio_run_call(monkeypatch):
    """main() must call asyncio.run() exactly once, wrapping _serve(...)
    directly — two separate asyncio.run() calls (one for bootstrap, one for
    serving) is exactly the regression this file guards against, since each
    asyncio.run() call creates and tears down its own event loop."""
    run_calls = []

    def fake_run(coro):
        run_calls.append(coro)
        coro.close()  # avoid "coroutine was never awaited" warnings

    monkeypatch.setattr(asyncio, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["server.py"])

    server_module.main()

    assert len(run_calls) == 1


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
