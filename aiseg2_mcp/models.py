"""Pydantic return models — the typed shapes the read-only tools hand back to the model.

Kept deliberately small: only the fields a caller reasons about ("how much am I generating /
consuming right now", "which circuits draw the most", "what are the day's totals"). Units are
encoded in the field names (``_kw`` / ``_kwh`` / ``watt``) so the model never has to guess.
Every result carries ``as_of`` so the caller can tell when the values were read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def now() -> datetime:
    """The current time in UTC, to the second (the precision of an ``as_of``)."""
    return datetime.now(UTC).replace(microsecond=0)


def _require_as_of(schema: dict) -> None:
    schema.setdefault("required", []).append("as_of")


class Live(BaseModel):
    """A reading of the device's current screens. Built right after the read, so ``as_of`` is then."""

    # The default always fills as_of, so the output schema lists it as required like SeriesPage's.
    model_config = ConfigDict(json_schema_extra=_require_as_of)

    as_of: datetime = Field(
        default_factory=now,
        description="When this server read the values from the AiSEG2 (ISO 8601, UTC).",
    )


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


class PowerFlow(Live):
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


class CircuitBreakdown(Live):
    """Per-circuit instantaneous consumption, paged out of the AiSEG2 and stitched together."""

    circuits: list[CircuitWatt]
    total_watt: float
    page_count: int


class CircuitInfo(BaseModel):
    """A registered measurement circuit: its stable id and configured name."""

    id: str
    name: str


class CircuitList(Live):
    """The registered circuit names — the authoritative source of circuit naming."""

    circuits: list[CircuitInfo]


class DailyTotals(Live):
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
    as_of: datetime = Field(
        description=(
            "When the SD-card export behind these values was downloaded (ISO 8601, UTC). "
            "An upper bound: nothing later is included, and the device's export may lag behind it."
        )
    )
    series: list[HistorySeriesPoint]
    has_more: bool
    total_rows: int
    next_offset: int | None = None
