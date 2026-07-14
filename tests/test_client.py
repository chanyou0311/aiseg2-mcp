"""Client tests using httpx.MockTransport (no real device / network).

Covers the wire contract the AiSEG2 requires: the display POST is form-encoded as ``data=<json>``;
a transient 5xx (the empty-body-500 quirk) is retried with exponential backoff; and requests to the
device are serialised by the concurrency semaphore.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from aiseg2_mcp.client import AisegClient


def _make_client(handler, **kwargs) -> AisegClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="http://device.test")
    # retry_base_delay=0 keeps tests fast unless a test overrides it.
    kwargs.setdefault("retry_base_delay", 0.0)
    return AisegClient("http://device.test", "aiseg", "pw", http=http, **kwargs)


async def test_power_flow_post_is_form_encoded():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"g_capacity": "1.0", "u_capacity": "0.5"})

    client = _make_client(handler)
    await client.fetch_power_flow()

    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/data/electricflow/111/update"
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    # body is data=<json>, i.e. {} url-encoded as %7B%7D
    assert req.content == b"data=%7B%7D"
    assert json.dumps({}) == "{}"  # the encoded payload is json.dumps({})


async def test_empty_body_500_is_retried_once_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # the device's empty-body quirk: 500 with an HTML "state updated" page
            return httpx.Response(500, text="AiSEGの状態が更新されました")
        return httpx.Response(200, json={"g_capacity": "1.0", "u_capacity": "0.5"})

    client = _make_client(handler)
    data = await client.fetch_power_flow()
    assert calls["n"] == 2  # one retry
    assert data["g_capacity"] == "1.0"


async def test_persistent_5xx_raises_tool_error_after_max_attempts():
    from mcp.server.fastmcp.exceptions import ToolError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    client = _make_client(handler, max_attempts=3)
    with pytest.raises(ToolError):
        await client.fetch_power_flow()


async def test_4xx_raises_immediately_without_retry():
    from mcp.server.fastmcp.exceptions import ToolError

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="unauthorized")

    client = _make_client(handler)
    with pytest.raises(ToolError):
        await client.fetch_power_flow()
    assert calls["n"] == 1  # 4xx is not retried


async def test_backoff_grows_exponentially(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, text="transient")
        return httpx.Response(200, json={"g_capacity": "1.0", "u_capacity": "0.5"})

    client = _make_client(
        handler, retry_base_delay=2.0, retry_factor=1.5, max_attempts=3
    )
    await client.fetch_power_flow()
    assert slept == [2.0, 3.0]  # base, then base * factor


async def test_circuit_paging_stops_at_repeated_page(fixtures_dir):
    # Serve the recorded 1113 fixtures; id>=4 (and 5..) repeat, so paging stops after 4.
    def handler(request: httpx.Request) -> httpx.Response:
        page_id = int(request.url.params["id"])
        idx = min(page_id, 5)  # fixtures exist for id 1..5; 5 repeats 4
        html = (fixtures_dir / f"electricflow_1113_id{idx}.html").read_text(encoding="utf-8")
        return httpx.Response(200, text=html)

    client = _make_client(handler)
    pages = await client.fetch_circuit_pages()
    # id1,id2,id3 distinct + id4 distinct; id5 repeats id4 -> discarded. 4 accepted pages.
    assert len(pages) == 4


async def test_unexpected_redirect_raises_tool_error():
    from mcp.server.fastmcp.exceptions import ToolError

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(302, headers={"location": "/login.html"})

    client = _make_client(handler)
    with pytest.raises(ToolError, match="redirect"):
        await client.fetch_power_flow()
    assert calls["n"] == 1  # a 3xx is not followed and not retried


async def test_download_history_uses_csrftoken_from_page(fixtures_dir):
    html = (fixtures_dir / "exectop2.html").read_text(encoding="utf-8")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "downType" in request.url.params:
            return httpx.Response(200, content=b"PK\x03\x04-fake-zip")
        return httpx.Response(200, text=html)

    client = _make_client(handler)
    data = await client.download_history_zip(timeout=1.0)
    assert data == b"PK\x03\x04-fake-zip"
    download = next(r for r in seen if "downType" in r.url.params)
    assert download.url.params["csrftoken"] == "99999"  # extracted from the fixture via lxml


async def test_download_history_accepts_non_numeric_token():
    # The token is not always numeric; lxml extraction + a [^"]+ fallback must accept it.
    html = '<html><body><input type="hidden" NAME="csrftoken" VALUE="ab12-XY" /></body></html>'
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "downType" in request.url.params:
            return httpx.Response(200, content=b"zip")
        return httpx.Response(200, text=html)

    client = _make_client(handler)
    await client.download_history_zip(timeout=1.0)
    download = next(r for r in seen if "downType" in r.url.params)
    assert download.url.params["csrftoken"] == "ab12-XY"


async def test_download_history_missing_token_raises():
    from mcp.server.fastmcp.exceptions import ToolError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>no token here</body></html>")

    client = _make_client(handler)
    with pytest.raises(ToolError, match="csrftoken"):
        await client.download_history_zip(timeout=1.0)


async def test_concurrency_semaphore_limits_in_flight():
    active = {"now": 0, "max": 0}

    class _FakeHttp:
        async def request(self, method, path, *, params=None, data=None):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            await asyncio.sleep(0.02)  # hold so concurrent calls overlap
            active["now"] -= 1
            return httpx.Response(200, text="<html></html>")

    client = AisegClient(
        "http://device.test", "aiseg", "pw", http=_FakeHttp(), concurrency=2
    )
    await asyncio.gather(*(client.fetch_graph_html(51111) for _ in range(5)))
    assert active["max"] <= 2
