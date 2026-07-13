"""Pure parsers: AiSEG2 JSON / HTML -> return models. No network, no MCP — unit-testable in isolation.

Every parser raises ``ValueError`` with a message naming the missing key / selector when the
AiSEG2 response does not have the expected shape; the server layer turns that into a ToolError.
Keeping these functions side-effect-free (a dict / an HTML string in, a model out) is what lets the
fixture-driven regression tests pin the device's quirks (full-width digits, ``<br/>`` inside a
circuit label, the "repeat the last page" paging terminator).

Field/selector semantics were transcribed from the device's own scripts (electricflow/111.js
dispBuySell + dispBattery, the 1113 stage layout, the installation/734 ``init({...})`` payload).
"""

from __future__ import annotations

import json
import re

import lxml.html

from .models import (
    BatteryStatus,
    CircuitBreakdown,
    CircuitInfo,
    CircuitWatt,
    DailyTotals,
    NamedWatt,
    PowerFlow,
)

# Full-width digits and the full-width comma/period the AiSEG2 can emit -> ASCII.
_ZENKAKU = str.maketrans("０１２３４５６７８９．，", "0123456789.,")
_NUMBER_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")

# Values above this are treated as a device/parse error rather than a real reading.
_MAX_REASONABLE = 999_999.0


def _document(html: str) -> lxml.html.HtmlElement:
    """Parse an AiSEG2 page. The pages are XHTML with an ``<?xml ... encoding?>`` declaration, which
    lxml rejects on a ``str`` input — so we hand it UTF-8 bytes and let it honour the declaration."""
    return lxml.html.fromstring(html.encode("utf-8"))


def normalize_number(raw: object) -> float | None:
    """Normalize an AiSEG2 numeric value to a float, or None when the reading is absent.

    Accepts either a JSON number (``2040``) or one of the device's stringy values (``" 224W"``,
    ``"2.0"``, full-width digits). ``"-"`` / empty / no-digits -> None (a genuinely missing value).
    A negative value or one above ~1e6 is not a plausible reading and raises ValueError.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):  # bool is an int subclass; never a measurement
        raise ValueError(f"expected a number, got bool {raw!r}")
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        s = str(raw).translate(_ZENKAKU).replace(",", "").strip()
        if s in ("", "-"):
            return None
        m = _NUMBER_RE.search(s)
        if not m:
            return None
        value = float(m.group())
    if value < 0 or value > _MAX_REASONABLE:
        raise ValueError(f"number out of range: {raw!r}")
    return value


# --- electricflow/111 (instantaneous power flow) -----------------------------------------------

# 0 -> buy, 1 -> sell (from 111.js dispBuySell); 2 -> none (device shows neither). Anything else is
# a state the device did not describe -> "unknown" (surfaced honestly, not coerced).
_BUY_SELL = {0: "buy", 1: "sell", 2: "none"}


def parse_power_flow(data: object) -> PowerFlow:
    """Parse the /data/electricflow/111/update JSON payload into a PowerFlow."""
    if not isinstance(data, dict):
        raise ValueError("electricflow/111 response was not a JSON object")
    if data.get("measReg") == 0:
        raise ValueError("AiSEG2 measurement unit is not registered (measReg=0)")

    generation = normalize_number(data.get("g_capacity"))
    consumption = normalize_number(data.get("u_capacity"))
    if generation is None or consumption is None:
        raise ValueError("g_capacity / u_capacity missing in electricflow/111 response")

    buy_sell = _BUY_SELL.get(data.get("lo_buy_sell"), "unknown")

    battery: BatteryStatus | None = None
    if data.get("connSb"):  # 0 / None -> no storage battery adapter -> omit
        soc = data.get("soc")
        battery = BatteryStatus(
            percent=normalize_number(data.get("percent")),
            level=soc if isinstance(soc, int) and 1 <= soc <= 5 else None,
            charging={0: True, 1: False}.get(data.get("charge")),
        )

    # Generation sources (g_d_1..3): keep only labelled entries with a real reading.
    generation_detail = [
        NamedWatt(name=title, watt=watt)
        for i in (1, 2, 3)
        if (title := str(data.get(f"g_d_{i}_title") or "").strip())
        and (watt := normalize_number(data.get(f"g_d_{i}_capacity"))) is not None
    ]

    # Top consumers (u_d_1..3), each gated by its best{i} visibility flag (matches the UI).
    top_consumers = [
        NamedWatt(name=title, watt=watt)
        for i in (1, 2, 3)
        if data.get(f"best{i}") == 1
        and (title := str(data.get(f"u_d_{i}_title") or "").strip())
        and (watt := normalize_number(data.get(f"u_d_{i}_capacity"))) is not None
    ]

    return PowerFlow(
        generation_kw=generation,
        consumption_kw=consumption,
        buy_sell=buy_sell,
        battery=battery,
        generation_detail=generation_detail,
        top_consumers=top_consumers,
    )


# --- electricflow/1113 (per-circuit instantaneous draw, paged) ---------------------------------


def _text_join(el: lxml.html.HtmlElement) -> str:
    """Join an element's text nodes with single spaces (collapses the ``<br/>`` inside c_device)."""
    return " ".join(t.strip() for t in el.xpath(".//text()") if t.strip())


def parse_circuit_page(html: str) -> list[tuple[str, float | None]]:
    """Parse one 1113 page into up to 10 (circuit-name, watts) rows in display order (W desc).

    Names are the display-derived labels (a ``<br/>`` wrap becomes a single space; the
    authoritative names come from list_circuits / page 734). A slot's watt value is None when the
    device leaves it blank (a 0 W or unassigned position).
    """
    doc = _document(html)
    rows: list[tuple[str, float | None]] = []
    for n in range(1, 11):
        stage = doc.xpath(f'//div[@id="stage_{n}"]')
        if not stage:
            continue
        device = stage[0].xpath('./div[@class="c_device"]')
        if not device:
            continue
        value = stage[0].xpath('./div[@class="c_value"]')
        name = _text_join(device[0])
        watt = normalize_number(_text_join(value[0])) if value else None
        rows.append((name, watt))
    return rows


def page_signature(rows: list[tuple[str, float | None]]) -> str:
    """Comma-join a page's circuit names — the terminator key (shimosyan's paging convention).

    The 1113 endpoint keeps serving the last page for ids past the real data, so a page whose name
    list equals the previous page's marks the end.
    """
    return ",".join(name for name, _ in rows)


def assemble_breakdown(pages: list[list[tuple[str, float | None]]]) -> CircuitBreakdown:
    """Stitch the accepted pages into a ranked breakdown.

    Placeholder rows (an unassigned ``-`` / blank slot) are dropped; a real circuit with a blank
    reading counts as 0 W. Ranks are 1-based across all pages (the device already sorts W desc).
    """
    circuits: list[CircuitWatt] = []
    total = 0.0
    for page in pages:
        for name, watt in page:
            if name.strip() in ("", "-"):
                continue
            w = watt if watt is not None else 0.0
            circuits.append(CircuitWatt(rank=len(circuits) + 1, name=name, watt=w))
            total += w
    return CircuitBreakdown(circuits=circuits, total_watt=total, page_count=len(pages))


# --- setting/installation/734 (registered circuit names, authoritative) ------------------------


def parse_installation_circuits(html: str) -> list[CircuitInfo]:
    """Parse the ``window.onload = init({...})`` payload for registered measurement circuits.

    Only entries with ``strBtnType == "1"`` are real measurement circuits; ``strId`` is the stable
    id and ``strCircuit`` the configured name (blank -> ``Circuit {id}``).
    """
    doc = _document(html)
    script = next(
        (
            s
            for s in doc.xpath("//script/text()")
            if "window.onload" in s and "arrayCircuitNameList" in s
        ),
        None,
    )
    if script is None:
        raise ValueError("installation/734: window.onload init(...) script not found")
    lp, rp = script.find("("), script.rfind(")")
    if lp == -1 or rp == -1 or rp <= lp:
        raise ValueError("installation/734: could not locate init(...) argument")
    try:
        payload = json.loads(script[lp + 1 : rp])
    except json.JSONDecodeError as e:  # pragma: no cover - defensive
        raise ValueError(f"installation/734: init(...) argument is not valid JSON: {e}") from e

    circuits = [
        CircuitInfo(
            id=str(entry.get("strId")),
            name=str(entry.get("strCircuit") or "").strip() or f"Circuit {entry.get('strId')}",
        )
        for entry in payload.get("arrayCircuitNameList", [])
        if entry.get("strBtnType") == "1"
    ]
    if not circuits:
        raise ValueError("installation/734: no circuits with strBtnType==1 found")
    return circuits


# --- graph/5x111 (daily cumulative totals) -----------------------------------------------------


def parse_graph_kwh(html: str) -> float | None:
    """Extract the cumulative kWh from a graph page (span#val_kwh); "-" -> None."""
    doc = _document(html)
    vals = doc.xpath('//span[@id="val_kwh"]/text()')
    if not vals:
        raise ValueError("graph: span#val_kwh not found")
    return normalize_number(vals[0])


def parse_graph_date(html: str) -> str | None:
    """Extract the AiSEG2's current day from a graph page (#val_current, "YYYY/MM/DD" -> ISO)."""
    doc = _document(html)
    vals = doc.xpath('//*[@id="val_current"]/text()')
    if not vals:
        return None
    return vals[0].strip().replace("/", "-") or None


def build_daily_totals(
    date: str | None,
    generation_html: str,
    consumption_html: str,
    buy_html: str,
    sell_html: str,
) -> DailyTotals:
    """Assemble the four graph pages into a DailyTotals (date falls back to '' if absent)."""
    return DailyTotals(
        date=date or "",
        generation_kwh=parse_graph_kwh(generation_html),
        consumption_kwh=parse_graph_kwh(consumption_html),
        buy_kwh=parse_graph_kwh(buy_html),
        sell_kwh=parse_graph_kwh(sell_html),
    )
