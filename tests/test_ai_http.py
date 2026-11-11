# Copyright 2026 3LC Inc.
import asyncio

import httpx
import pytest

from tlc_plugin_sdk.ai.http import ClientPool, client


def test_reuses_connections_but_never_session_cookies_or_default_credentials(monkeypatch):
    transports, seen = [], []

    class Transport(httpx.AsyncBaseTransport):
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False and kwargs["retries"] == 0
            self.closed = False
            transports.append(self)

        async def handle_async_request(self, request):
            assert not self.closed
            seen.append(dict(request.headers))
            return httpx.Response(200, headers={"Set-Cookie": "private=must-not-persist; Path=/"}, json={})

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", Transport)

    async def run():
        pool = ClientPool()
        with pool.bind():
            async with client(timeout=30) as first:
                await first.get("https://api.example/v1", headers={"Authorization": "Bearer first-call"})
            assert len(transports) == 1 and not transports[0].closed
            async with client(timeout=30) as second:
                await second.get("https://api.example/v1", headers={"Authorization": "Bearer second-call"})
            assert len(transports) == 1
        await pool.aclose()
        assert transports[0].closed
        with pytest.raises(RuntimeError), pool.bind():
            pass

    asyncio.run(run())
    assert [h["authorization"] for h in seen] == ["Bearer first-call", "Bearer second-call"]
    assert all("cookie" not in h for h in seen)


def test_pool_does_not_cross_job_event_loops():
    pool = ClientPool()

    async def bind():
        with pool.bind():
            pass

    asyncio.run(bind())
    with pytest.raises(RuntimeError, match="one active job"):
        asyncio.run(bind())


@pytest.mark.parametrize("connections,keepalive", [(0, 0), (4097, 0), (True, 0), (64, 65), (64, -1)])
def test_rejects_invalid_capacity(connections, keepalive):
    with pytest.raises(ValueError, match="capacity"):
        ClientPool(max_connections=connections, max_keepalive_connections=keepalive)


def test_thousand_async_calls_keep_credentials_isolated(monkeypatch):
    async def run():
        entered, release = 0, asyncio.Event()
        seen = set()

        class Transport(httpx.AsyncBaseTransport):
            def __init__(self, **kwargs):
                assert kwargs["limits"].max_connections == 1024
                assert kwargs["limits"].max_keepalive_connections == 128

            async def handle_async_request(self, request):
                nonlocal entered
                entered += 1
                seen.add(request.headers["authorization"])
                if entered == 1000:
                    release.set()
                await release.wait()
                return httpx.Response(200, json={})

        monkeypatch.setattr(httpx, "AsyncHTTPTransport", Transport)
        pool = ClientPool(max_connections=1024, max_keepalive_connections=128)

        async def call(i):
            with pool.bind():
                async with client() as session:
                    await session.get("https://provider.example/model", headers={"Authorization": f"case-{i}"})

        await asyncio.wait_for(asyncio.gather(*(call(i) for i in range(1000))), timeout=10)
        await pool.aclose()
        assert entered == len(seen) == 1000

    asyncio.run(run())
