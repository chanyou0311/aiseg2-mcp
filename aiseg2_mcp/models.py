"""Pydantic return models — the typed shapes the four read-only tools hand back to the model.

Kept deliberately small: only the fields a caller reasons about ("how much am I generating /
consuming right now", "which circuits draw the most", "what are the day's totals"). Units are
encoded in the field names (``_kw`` / ``_kwh`` / ``watt``) so the model never has to guess.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class NamedWatt(BaseModel):
    """A labelled instantaneous power reading in watts (generation source or consumer circuit)."""

    name: str
    watt: float


class BatteryStatus(BaseModel):
    """Storage-battery state, present only when a battery net adapter is connected (connSb != 0).

    Semantics transcribed from the device's 111.js (dispBattery):
      * ``percent`` — state of charge in %, from the ``percent`` field ("-" -> None).
      * ``level`` — the 1..5 bar level the device draws (the ``soc`` field), or None.
      * ``charging`` — True while charging (charge==0), False while discharging (charge==1),
        None for any other/idle state.
    """

    percent: float | None = None
    level: int | None = None
    charging: bool | None = None


class PowerFlow(BaseModel):
    """The instantaneous whole-home power flow (the AiSEG2 "electric flow" screen)."""

    generation_kw: float
    consumption_kw: float
    # 0 -> buy, 1 -> sell (confirmed from 111.js dispBuySell); 2 -> none (neither); any other
    # value -> unknown rather than silently mapping to a state the device did not report.
    buy_sell: Literal["buy", "sell", "none", "unknown"]
    battery: BatteryStatus | None = None
    generation_detail: list[NamedWatt] = []
    top_consumers: list[NamedWatt] = []


class CircuitWatt(BaseModel):
    """One circuit's instantaneous draw, ranked by watts (highest first)."""

    rank: int
    name: str
    watt: float


class CircuitBreakdown(BaseModel):
    """Per-circuit instantaneous consumption, paged out of the AiSEG2 and stitched together."""

    circuits: list[CircuitWatt]
    total_watt: float
    page_count: int


class CircuitInfo(BaseModel):
    """A registered measurement circuit: its stable id and configured name."""

    id: str
    name: str


class CircuitList(BaseModel):
    """The registered circuit names — the authoritative source of circuit naming."""

    circuits: list[CircuitInfo]


class DailyTotals(BaseModel):
    """Today's cumulative energy totals in kWh (as of the AiSEG2's current day)."""

    date: str
    generation_kwh: float | None = None
    consumption_kwh: float | None = None
    buy_kwh: float | None = None
    sell_kwh: float | None = None


# --- SD-card long-term history -----------------------------------------------------------------


class HistorySeriesPoint(BaseModel):
    """One long-form data point: a timestamp, the series (metric key or circuit name), a value."""

    timestamp: str
    metric: str
    value: float


class SeriesPage(BaseModel):
    """A paginated slice of long-term series data. ``unit`` distinguishes energy (Wh) from cost (JPY)."""

    granularity: str
    unit: str
    series: list[HistorySeriesPoint]
    has_more: bool
    total_rows: int
    next_offset: int | None = None
