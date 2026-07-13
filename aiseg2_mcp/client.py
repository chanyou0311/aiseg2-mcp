"""Async HTTP client for one AiSEG2 controller (HTTP Digest over plain http on the LAN).

STRICTLY READ-ONLY. It issues GETs and the display-only data-refresh POSTs the web UI itself uses to
redraw its screens (the ``/data/**/update`` endpoints) — it never touches the device's mutating
action or settings endpoints. The AiSEG2 is a small embedded device, so requests are serialised to
at most two at a time, time out at 10 s, and back off exponentially on transient failures / 5xx
before giving up with a ToolError.

The device's data POSTs are form-encoded: the body is ``data=<json>`` with a
``application/x-www-form-urlencoded`` content type (mirroring the UI's own XHR). An empty POST body
can make the device answer 500 ("AiSEG の状態が更新されました"); we always send ``data={}`` and treat a
5xx as a retryable transient.
"""

from __future__ import annotations

import asyncio
import json
from importlib.metadata import PackageNotFoundError, version

import httpx
from mcp.server.fastmcp.exceptions import ToolError

from . import parsers

try:
    __version__ = version("aiseg2-mcp")
except PackageNotFoundError:  # pragma: no cover - only when running from an unbuilt tree
    __version__ = "0.0.0"


class AisegClient:
    """Serialised, retrying, read-only view of one AiSEG2 controller."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        http: httpx.AsyncClient | None = None,
        concurrency: int = 2,
        timeout: float = 10.0,
        max_attempts: int = 3,
        retry_base_delay: float = 2.0,
        retry_factor: float = 1.5,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._user = user
        self._password = password
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._retry_base_delay = retry_base_delay
        self._retry_factor = retry_factor
        # Cap concurrent requests to the device (it is a small embedded controller).
        self._sem = asyncio.Semaphore(concurrency)
        self._http = http  # tests inject a client on a MockTransport

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                auth=httpx.DigestAuth(self._user, self._password),
                headers={"User-Agent": f"aiseg2-mcp/{__version__}"},
                timeout=self._timeout,
            )
        return self._http

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send one request with concurrency limit + exponential backoff on transient failures.

        4xx is a client-side mistake and fails immediately; timeouts / transport errors / 5xx are
        retried up to ``max_attempts`` with a growing delay, then surfaced as a ToolError.
        """
        delay = self._retry_base_delay
        last: object = None
        for attempt in range(self._max_attempts):
            try:
                async with self._sem:
                    response = await self._client().request(
                        method, path, params=params, data=data
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
            else:
                if response.status_code < 500:
                    if response.is_error:
                        raise ToolError(
                            f"AiSEG2 {method} {path} -> HTTP {response.status_code} "
                            "(check AISEG_URL / credentials)"
                        )
                    return response
                last = f"HTTP {response.status_code}"  # 5xx: retryable transient
            if attempt < self._max_attempts - 1:
                await asyncio.sleep(delay)
                delay *= self._retry_factor
        raise ToolError(
            f"AiSEG2 {method} {path} failed after {self._max_attempts} attempts ({last})"
        )

    # --- fetchers (read-only) ------------------------------------------------------------------

    async def fetch_power_flow(self) -> dict:
        """POST /data/electricflow/111/update with body ``data={}`` -> the power-flow JSON."""
        response = await self._send(
            "POST",
            "/data/electricflow/111/update",
            data={"data": json.dumps({})},
        )
        return response.json()

    async def fetch_circuit_pages(
        self, max_pages: int = 20
    ) -> list[list[tuple[str, float | None]]]:
        """Page GET /page/electricflow/1113?id=n until the device repeats the previous page.

        Returns the accepted pages' parsed rows (the repeated terminator page is discarded).
        """
        accepted: list[list[tuple[str, float | None]]] = []
        previous: str | None = None
        for page_id in range(1, max_pages + 1):
            response = await self._send(
                "GET", "/page/electricflow/1113", params={"id": page_id}
            )
            rows = parsers.parse_circuit_page(response.text)
            signature = parsers.page_signature(rows)
            if signature == previous:
                break  # this page repeats the last -> end of real data
            accepted.append(rows)
            previous = signature
        return accepted

    async def fetch_installation_html(self) -> str:
        """GET /page/setting/installation/734 -> HTML carrying the registered circuit names."""
        response = await self._send("GET", "/page/setting/installation/734")
        return response.text

    async def fetch_graph_html(self, page_id: int) -> str:
        """GET /page/graph/{page_id} -> HTML carrying a day's cumulative kWh (span#val_kwh)."""
        response = await self._send("GET", f"/page/graph/{page_id}")
        return response.text
