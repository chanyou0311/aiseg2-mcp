"""FastMCP server exposing a Panasonic AiSEG2 HEMS controller, READ-ONLY.

Six tools, all read-only: the instantaneous whole-home flow, the per-circuit breakdown, the
registered circuit names, today's cumulative kWh totals, and the long-term energy / cost history
from the SD-card export. There is deliberately NO tool that changes any device setting — the client
only issues GETs and the display-only refresh POSTs the web UI itself uses. Every tool is annotated
readOnlyHint=True / destructiveHint=False so a caller can see the surface is non-mutating.

Each tool is wrapped by ``_audited``, which records a structured audit line on success and on
failure and normalizes a parser ValueError into a ToolError — so every outcome is audited and every
error reaches the caller as a ToolError, with no per-tool try/except boilerplate.

Transport:
  * stdio (default) for a local MCP client (e.g. ``claude mcp add``).
  * streamable-http to run as a network service. It then serves the MCP endpoint at ``/mcp`` and a
    ``/health`` route, binding AISEG_HOST:AISEG_PORT.

transport_security (streamable-http only): the SDK's DNS-rebinding/Host allowlist is a defense for
directly-exposed localhost servers. Behind a trusted authenticating reverse proxy the proxied Host
header trips that allowlist (HTTP 421) before the tool runs; set
AISEG_DISABLE_DNS_REBINDING_PROTECTION=true ONLY in that deployment. The default keeps protection
on. The flag is read from Settings in main() (so .env works), not at import time.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Literal, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import parsers
from .client import AisegClient
from .config import Settings
from .history import HistoryStore
from .models import (
    CircuitBreakdown,
    CircuitList,
    DailyTotals,
    PowerFlow,
    SeriesPage,
)

logger = logging.getLogger("aiseg2_mcp")
# Dedicated audit stream (records each tool call: outcome + counts/timing only). Credentials and
# response bodies are NEVER part of a message.
audit = logging.getLogger("aiseg2_mcp.audit")

mcp = FastMCP(
    "aiseg2-mcp",
    stateless_http=True,
    json_response=True,
)

# Built in main(); the tools read these module globals.
_aiseg: AisegClient | None = None
_history: HistoryStore | None = None


def _client() -> AisegClient:
    if _aiseg is None:  # pragma: no cover - guarded by main() init ordering
        raise RuntimeError("AiSEG2 client is not initialized")
    return _aiseg


def _store() -> HistoryStore:
    if _history is None:  # pragma: no cover - guarded by main() init ordering
        raise RuntimeError("AiSEG2 history store is not initialized")
    return _history


# Shared annotations: every tool is a read-only, non-destructive, idempotent observation of a
# device on the local network (no open-world/random effects).
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

_R = TypeVar("_R")


def _audited(
    name: str, summary: Callable[[_R], str] | None = None
) -> Callable[[Callable[..., Awaitable[_R]]], Callable[..., Awaitable[_R]]]:
    """Wrap a tool coroutine with uniform audit logging + error normalization.

    On success, logs ``<name> outcome=ok <summary>``. On failure, logs ``<name> outcome=error`` and
    either re-raises a ToolError (already caller-facing) or converts a parser ValueError into one.
    This closes the audit gaps (errors that skipped logging) and removes the per-tool try/except.
    """

    def decorator(fn: Callable[..., Awaitable[_R]]) -> Callable[..., Awaitable[_R]]:
        @functools.wraps(fn)
        async def wrapper(*args: object, **kwargs: object) -> _R:
            try:
                result = await fn(*args, **kwargs)
            except ToolError:
                audit.info("%s outcome=error", name)
                raise
            except ValueError as exc:  # a parser shape error -> caller-facing ToolError
                audit.info("%s outcome=error", name)
                raise ToolError(str(exc)) from exc
            audit.info("%s outcome=ok %s", name, summary(result) if summary else "")
            return result

        return wrapper

    return decorator


@mcp.tool(annotations=_READ_ONLY)
@_audited(
    "get_power_flow",
    lambda f: f"gen_kw={f.generation_kw} con_kw={f.consumption_kw} buy_sell={f.buy_sell}",
)
async def get_power_flow() -> PowerFlow:
    """Read-only. Get the AiSEG2's instantaneous whole-home power flow right now.

    Returns current generation and consumption (kW), whether the home is buying or selling grid
    power, storage-battery status if a battery is connected, the per-source generation breakdown,
    and the top consuming circuits at this moment.
    """
    return parsers.parse_power_flow(await _client().fetch_power_flow())


@mcp.tool(annotations=_READ_ONLY)
@_audited(
    "get_circuit_breakdown", lambda b: f"circuits={len(b.circuits)} pages={b.page_count}"
)
async def get_circuit_breakdown() -> CircuitBreakdown:
    """Read-only. Get the instantaneous power draw of every measured circuit, ranked highest first.

    Pages through the AiSEG2's circuit list and returns each circuit's rank, name and watts, plus
    the total measured watts and how many device pages were read. Circuit names here are
    display-derived (they may wrap); list_circuits() is the authoritative name source.
    """
    return parsers.assemble_breakdown(await _client().fetch_circuit_pages())


@mcp.tool(annotations=_READ_ONLY)
@_audited("list_circuits", lambda c: f"count={len(c.circuits)}")
async def list_circuits() -> CircuitList:
    """Read-only. List the registered measurement circuits with their stable ids and names.

    This is the AUTHORITATIVE source of circuit naming (from the device's installation settings).
    The names in get_circuit_breakdown() are display-derived and may differ (line wraps); prefer
    these when you need a canonical circuit name.
    """
    return CircuitList(circuits=parsers.parse_installation_circuits(await _client().fetch_installation_html()))


@mcp.tool(annotations=_READ_ONLY)
@_audited("get_daily_totals", lambda t: f"date={t.date}")
async def get_daily_totals() -> DailyTotals:
    """Read-only. Get today's cumulative energy totals (kWh) as of the AiSEG2's current day.

    Returns generation, consumption, grid-buy and grid-sell totals for the day. Any meter the
    device reports as unavailable ("-") comes back as null.
    """
    return await _client().fetch_daily_totals()


@mcp.tool(annotations=_READ_ONLY)
@_audited(
    "get_history",
    lambda p: f"granularity={p.granularity} points={len(p.series)} total={p.total_rows}",
)
async def get_history(
    granularity: Literal["30min", "hour", "day", "month", "year"],
    start: str,
    end: str,
    metrics: list[str] | None = None,
    circuits: list[str] | None = None,
    limit: int = 200,
    offset: int = 0,
) -> SeriesPage:
    """Read-only. Query the AiSEG2's long-term energy history from its SD-card export (values in Wh).

    Requires an SD card inserted in the AiSEG2. The export is downloaded once and cached, so the
    first call is slow and later calls are fast. Returns long-form points ({timestamp, metric,
    value}); use limit/offset to page (limit caps the number of returned points).

    Args:
        granularity: Time resolution. "30min"/"hour"/"day" take start/end as YYYY-MM-DD; "month"
            takes YYYY-MM; "year" takes YYYY.
        start: Range start (inclusive), formatted per the granularity.
        end: Range end (inclusive), formatted per the granularity.
        metrics: Optional filter by standard series keys (e.g. "generation_pv1", "grid_buy",
            "grid_sell", "battery_charge", "battery_discharge", "ev_charge") or their Japanese
            header names. Omit for all series.
        circuits: Optional filter by circuit name (see list_circuits). Omit for all circuits.
        limit: Maximum number of series points to return (default 200).
        offset: Number of points to skip (for pagination).
    """
    return await _store().get_history(granularity, start, end, metrics, circuits, limit, offset)


@mcp.tool(annotations=_READ_ONLY)
@_audited(
    "get_cost_history",
    lambda p: f"granularity={p.granularity} points={len(p.series)} total={p.total_rows}",
)
async def get_cost_history(
    granularity: Literal["day", "month", "year"],
    start: str,
    end: str,
    limit: int = 200,
    offset: int = 0,
) -> SeriesPage:
    """Read-only. Query the AiSEG2's long-term energy-cost history from the SD-card export (JPY).

    Requires an SD card inserted in the AiSEG2. Shares the same cached download as get_history.
    Returns long-form cost points ({timestamp, metric, value}) with values in Japanese yen.

    Args:
        granularity: "day" takes start/end as YYYY-MM-DD; "month" as YYYY-MM; "year" as YYYY.
        start: Range start (inclusive), formatted per the granularity.
        end: Range end (inclusive), formatted per the granularity.
        limit: Maximum number of series points to return (default 200).
        offset: Number of points to skip (for pagination).
    """
    return await _store().get_cost_history(granularity, start, end, limit, offset)


def _configure_streamable_http(settings: Settings) -> None:
    """Apply streamable-http settings to ``mcp`` before run(): bind address + DNS-rebinding toggle.

    Reading the toggle from Settings here (not at import time) is what makes it honour .env and the
    process environment. When enabled, protection is turned off (for a trusted proxy deployment);
    otherwise the SDK's construction-time default (protection on) is left in place.
    """
    mcp.settings.host = settings.aiseg_host
    mcp.settings.port = settings.aiseg_port
    if settings.aiseg_disable_dns_rebinding_protection:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )


def main() -> None:
    global _aiseg, _history
    settings = Settings()  # env (+ local .env); missing AISEG_URL / AISEG_PASSWORD -> ValidationError
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    _aiseg = AisegClient(
        base_url=settings.aiseg_url,
        user=settings.aiseg_user,
        password=settings.aiseg_password,
    )
    _history = HistoryStore(
        _aiseg,
        cache_dir=settings.aiseg_cache_dir or None,
        ttl=settings.aiseg_cache_ttl,
    )

    if settings.aiseg_transport == "streamable-http":
        _configure_streamable_http(settings)

        @mcp.custom_route("/health", methods=["GET"])
        async def health(_request):  # type: ignore[no-untyped-def]
            from starlette.responses import JSONResponse

            return JSONResponse({"status": "ok"})

        logger.info(
            "starting AiSEG2 MCP server (streamable-http) on %s:%s/mcp",
            settings.aiseg_host,
            settings.aiseg_port,
        )
        mcp.run(transport="streamable-http")
    else:
        logger.info("starting AiSEG2 MCP server (stdio)")
        mcp.run(transport="stdio")
