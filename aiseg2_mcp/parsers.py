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

import csv
import io
import json
import re
from dataclasses import dataclass

import lxml.html

from .models import (
    BatteryStatus,
    CircuitBreakdown,
    CircuitInfo,
    CircuitWatt,
    DailyTotals,
    HistorySeriesPoint,
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


# --- SD-card history CSVs (30min / hour / day / month / year, + cost) --------------------------

# Japanese CSV header -> stable English metric key for the fixed leading columns. Columns not in
# this map (per-circuit columns, plus tail utility meters like 使用電力量 / ガス使用量) keep their
# Japanese header text as their series label. 計測日時 is the timestamp column (index 0).
STANDARD_METRICS: dict[str, str] = {
    "太陽光発電(創蓄パワコン)": "generation_pcs",
    "蓄電池充電": "battery_charge",
    "蓄電池放電": "battery_discharge",
    "主幹買電": "grid_buy",
    "主幹売電": "grid_sell",
    "太陽光発電(PV1)": "generation_pv1",
    "太陽光発電(PV2)": "generation_pv2",
    "HP消費電力量": "heatpump_consumption",
    "燃料電池発電電力量": "fuelcell_generation",
    "EV充電電力量": "ev_charge",
    "EV放電電力量": "ev_discharge",
}

# "無効N" columns are reserved/disabled measurement slots and are always dropped.
_INVALID_COLUMN_RE = re.compile(r"^無効\d+$")


@dataclass
class HistoryColumn:
    """One selectable CSV column: its position, display name, and English key (None if not fixed)."""

    index: int
    name: str
    key: str | None

    @property
    def label(self) -> str:
        """The series label to surface: the English key when the column is a fixed metric."""
        return self.key or self.name


@dataclass
class HistoryCsv:
    """A parsed history CSV: its selectable columns and its data rows (timestamp in row[0])."""

    columns: list[HistoryColumn]
    rows: list[list[str]]


def _csv_number(raw: str) -> float | None:
    """Parse a history CSV cell to a float. ``"-"`` / blank -> None. No upper cap: cumulative Wh /
    0.001-JPY totals over a year legitimately exceed the instantaneous-reading range guard."""
    s = str(raw).translate(_ZENKAKU).replace(",", "").strip()
    if s in ("", "-"):
        return None
    m = _NUMBER_RE.search(s)
    return float(m.group()) if m else None


def parse_history_csv(raw: bytes) -> HistoryCsv:
    """Parse a UTF-8-BOM history/cost CSV into its columns (minus 無効N) and data rows."""
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    if not rows:
        raise ValueError("history CSV is empty")
    header = rows[0]
    columns = [
        HistoryColumn(index=idx, name=name, key=STANDARD_METRICS.get(name))
        for idx, name in enumerate(header)
        if idx != 0 and not _INVALID_COLUMN_RE.match(name)
    ]
    data = [r for r in rows[1:] if r and r[0].strip()]
    return HistoryCsv(columns=columns, rows=data)


def history_timestamp(raw: str, granularity: str) -> tuple[str, str]:
    """Return (ISO display, range key) for a raw history timestamp at ``granularity``.

    The device encodes the timestamp differently per granularity; the range key is the coarser
    ``YYYY-MM-DD`` / ``YYYY-MM`` / ``YYYY`` used to filter against start/end (ISO string compare is
    chronological). 30min = ``202604120030+0900``, hour = ``2026041200+0900``, day = ``20250701``,
    month = ``202501``, year = ``2014``.
    """
    ts = raw.strip()
    if granularity in ("30min", "hour"):
        digits = ts.split("+", 1)[0]
        tz = ts[len(digits) :] or "+0900"
        y, mo, d, h = digits[0:4], digits[4:6], digits[6:8], digits[8:10]
        mi = digits[10:12] if granularity == "30min" else "00"
        offset = f"{tz[:3]}:{tz[3:]}" if len(tz) == 5 else tz
        return f"{y}-{mo}-{d}T{h}:{mi}:00{offset}", f"{y}-{mo}-{d}"
    if granularity == "day":
        y, mo, d = ts[0:4], ts[4:6], ts[6:8]
        iso = f"{y}-{mo}-{d}"
        return iso, iso
    if granularity == "month":
        y, mo = ts[0:4], ts[4:6]
        iso = f"{y}-{mo}"
        return iso, iso
    if granularity == "year":
        return ts, ts
    raise ValueError(f"unknown granularity: {granularity!r}")


def _select_columns(
    columns: list[HistoryColumn],
    metrics: list[str] | None,
    circuits: list[str] | None,
) -> list[HistoryColumn]:
    """Choose columns per the metrics/circuits filters (both None -> every non-無効 column)."""
    if metrics is None and circuits is None:
        return columns
    wanted_metrics = set(metrics or [])
    wanted_circuits = set(circuits or [])
    selected: list[HistoryColumn] = []
    for col in columns:
        by_metric = bool(wanted_metrics) and (col.key in wanted_metrics or col.name in wanted_metrics)
        by_circuit = bool(wanted_circuits) and col.name in wanted_circuits
        if by_metric or by_circuit:
            selected.append(col)
    return selected


def history_points(
    parsed: HistoryCsv,
    granularity: str,
    start: str,
    end: str,
    metrics: list[str] | None,
    circuits: list[str] | None,
    scale: float = 1.0,
) -> list[HistorySeriesPoint]:
    """Long-form the selected columns of one CSV into points within [start, end], missing dropped.

    ``scale`` converts the raw cell (1.0 for Wh history; 0.001 for 0.001-JPY cost -> JPY).
    """
    selected = _select_columns(parsed.columns, metrics, circuits)
    points: list[HistorySeriesPoint] = []
    for row in parsed.rows:
        display, key = history_timestamp(row[0], granularity)
        if not (start <= key <= end):
            continue
        for col in selected:
            if col.index >= len(row):
                continue
            value = _csv_number(row[col.index])
            if value is None:
                continue
            points.append(
                HistorySeriesPoint(timestamp=display, metric=col.label, value=value * scale)
            )
    return points
