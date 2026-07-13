"""FastMCP server exposing a Panasonic AiSEG2 HEMS controller, READ-ONLY.

Four tools, all read-only: the instantaneous whole-home flow, the per-circuit breakdown, the
registered circuit names, and today's cumulative kWh totals. There is deliberately NO tool that
changes any device setting — the client only issues GETs and the display-only refresh POSTs the
web UI itself uses. Every tool is annotated readOnlyHint=True / destructiveHint=False so a caller
can see the surface is non-mutating, and a failure raises ToolError naming the missing key/selector.

Transport:
  * stdio (default) for a local MCP client (e.g. ``claude mcp add``).
  * streamable-http to run as a network service. It then serves the MCP endpoint at ``/mcp`` and a
    ``/health`` route, binding AISEG_HOST:AISEG_PORT.

transport_security (streamable-http only): the SDK's DNS-rebinding/Host allowlist is a defense for
directly-exposed localhost servers. Behind a trusted authenticating reverse proxy the proxied Host
header trips that allowlist (HTTP 421) before the tool runs; set
AISEG_DISABLE_DNS_REBINDING_PROTECTION=true ONLY in that deployment. The default keeps protection on.
"""

from __future__ import annotations

import asyncio
import logging
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import parsers
from .client import AisegClient
from .config import Settings
from .models import CircuitBreakdown, CircuitList, DailyTotals, PowerFlow

logger = logging.getLogger("aiseg2_mcp")
# Dedicated audit stream (records each tool call: outcome + counts/timing only). Credentials and
# response bodies are NEVER part of a message.
audit = logging.getLogger("aiseg2_mcp.audit")

# Graph page ids for the four daily meters (generation / consumption / buy / sell).
_GRAPH_GENERATION = 51111
_GRAPH_CONSUMPTION = 52111
_GRAPH_BUY = 53111
_GRAPH_SELL = 54111


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


# transport_security is decided at construction time (before main() reads Settings). Default =
# None keeps the SDK default (protection ON); only disable it when explicitly opted in.
_transport_security = (
    TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if _env_flag("AISEG_DISABLE_DNS_REBINDING_PROTECTION")
    else None
)

mcp = FastMCP(
    "aiseg2-mcp",
    stateless_http=True,
    json_response=True,
    transport_security=_transport_security,
)

# Built in main(); the tools read this module global.
_aiseg: AisegClient | None = None


def _client() -> AisegClient:
    if _aiseg is None:  # pragma: no cover - guarded by main() init ordering
        raise RuntimeError("AiSEG2 client is not initialized")
    return _aiseg


# Shared annotations: every tool is a read-only, non-destructive, idempotent observation of a
# device on the local network (no open-world/random effects).
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


@mcp.tool(annotations=_READ_ONLY)
async def get_power_flow() -> PowerFlow:
    """Read-only. Get the AiSEG2's instantaneous whole-home power flow right now.

    Returns current generation and consumption (kW), whether the home is buying or selling grid
    power, storage-battery status if a battery is connected, the per-source generation breakdown,
    and the top consuming circuits at this moment.
    """
    data = await _client().fetch_power_flow()
    try:
        flow = parsers.parse_power_flow(data)
    except ValueError as exc:
        audit.info("get_power_flow outcome=error")
        raise _tool_error(exc)
    audit.info(
        "get_power_flow outcome=ok gen_kw=%s con_kw=%s buy_sell=%s",
        flow.generation_kw,
        flow.consumption_kw,
        flow.buy_sell,
    )
    return flow


@mcp.tool(annotations=_READ_ONLY)
async def get_circuit_breakdown() -> CircuitBreakdown:
    """Read-only. Get the instantaneous power draw of every measured circuit, ranked highest first.

    Pages through the AiSEG2's circuit list and returns each circuit's rank, name and watts, plus
    the total measured watts and how many device pages were read. Circuit names here are
    display-derived (they may wrap); list_circuits() is the authoritative name source.
    """
    pages = await _client().fetch_circuit_pages()
    try:
        breakdown = parsers.assemble_breakdown(pages)
    except ValueError as exc:
        audit.info("get_circuit_breakdown outcome=error")
        raise _tool_error(exc)
    audit.info(
        "get_circuit_breakdown outcome=ok circuits=%d pages=%d",
        len(breakdown.circuits),
        breakdown.page_count,
    )
    return breakdown


@mcp.tool(annotations=_READ_ONLY)
async def list_circuits() -> CircuitList:
    """Read-only. List the registered measurement circuits with their stable ids and names.

    This is the AUTHORITATIVE source of circuit naming (from the device's installation settings).
    The names in get_circuit_breakdown() are display-derived and may differ (line wraps); prefer
    these when you need a canonical circuit name.
    """
    html = await _client().fetch_installation_html()
    try:
        circuits = parsers.parse_installation_circuits(html)
    except ValueError as exc:
        audit.info("list_circuits outcome=error")
        raise _tool_error(exc)
    audit.info("list_circuits outcome=ok count=%d", len(circuits))
    return CircuitList(circuits=circuits)


@mcp.tool(annotations=_READ_ONLY)
async def get_daily_totals() -> DailyTotals:
    """Read-only. Get today's cumulative energy totals (kWh) as of the AiSEG2's current day.

    Returns generation, consumption, grid-buy and grid-sell totals for the day. Any meter the
    device reports as unavailable ("-") comes back as null.
    """
    generation, consumption, buy, sell = await asyncio.gather(
        _client().fetch_graph_html(_GRAPH_GENERATION),
        _client().fetch_graph_html(_GRAPH_CONSUMPTION),
        _client().fetch_graph_html(_GRAPH_BUY),
        _client().fetch_graph_html(_GRAPH_SELL),
    )
    try:
        # The current day is identical across the four pages; read it from the consumption page.
        date = parsers.parse_graph_date(consumption)
        totals = parsers.build_daily_totals(date, generation, consumption, buy, sell)
    except ValueError as exc:
        audit.info("get_daily_totals outcome=error")
        raise _tool_error(exc)
    audit.info("get_daily_totals outcome=ok date=%s", totals.date)
    return totals


def _tool_error(exc: ValueError) -> ToolError:
    """Wrap a parser ValueError (which names the missing key/selector) as a ToolError."""
    return ToolError(str(exc))


def main() -> None:
    global _aiseg
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

    if settings.aiseg_transport == "streamable-http":
        mcp.settings.host = settings.aiseg_host
        mcp.settings.port = settings.aiseg_port

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
