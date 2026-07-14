"""Tests for the SD-card history parsers and the caching HistoryStore.

Uses the real CSV fixtures under tests/fixtures/sd_zip/ (recorded from a device with FW
Ver.2.97I-01), assembled into a small in-memory zip served by a fake client — so no network and no
real download, while still exercising the true CSV shapes (UTF-8 BOM, 無効N columns, the tail
utility meters, and the per-granularity timestamp formats).
"""

from __future__ import annotations

import io
import pathlib
import zipfile

import pytest

from aiseg2_mcp import parsers
from aiseg2_mcp.history import HistoryStore

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "sd_zip"


def _read_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# --- parsers -----------------------------------------------------------------------------------


def test_parse_history_csv_columns_and_bom():
    parsed = parsers.parse_history_csv(_read_bytes("dayhistory_rc_202507.csv"))
    names = [c.name for c in parsed.columns]
    # BOM stripped: the first real header is not prefixed with ﻿.
    assert not any(n.startswith("﻿") for n in names)
    # 無効N columns are dropped entirely.
    assert not any(n.startswith("無効") for n in names)
    # standard leading columns carry an English key.
    by_name = {c.name: c for c in parsed.columns}
    assert by_name["主幹買電"].key == "grid_buy"
    assert by_name["主幹売電"].key == "grid_sell"
    assert by_name["太陽光発電(PV1)"].key == "generation_pv1"
    # a per-circuit column has no key -> label falls back to its Japanese name.
    assert by_name["キッチン"].key is None
    assert by_name["キッチン"].label == "キッチン"
    # tail utility meters survive as name-keyed columns.
    assert "使用電力量" in by_name


@pytest.mark.parametrize(
    ("raw", "granularity", "display", "key"),
    [
        ("20250701", "day", "2025-07-01", "2025-07-01"),
        ("202501", "month", "2025-01", "2025-01"),
        ("2014", "year", "2014", "2014"),
        ("202604120030+0900", "30min", "2026-04-12T00:30:00+09:00", "2026-04-12"),
        ("2026041200+0900", "hour", "2026-04-12T00:00:00+09:00", "2026-04-12"),
    ],
)
def test_history_timestamp(raw, granularity, display, key):
    assert parsers.history_timestamp(raw, granularity) == (display, key)


def test_history_points_filters_and_missing():
    parsed = parsers.parse_history_csv(_read_bytes("dayhistory_rc_202507.csv"))
    pts = parsers.history_points(
        parsed, "day", "2025-07-01", "2025-07-01", metrics=["grid_buy"], circuits=None
    )
    assert len(pts) == 1
    assert pts[0].timestamp == "2025-07-01"
    assert pts[0].metric == "grid_buy"
    assert pts[0].value == 14440.0  # Wh, unscaled

    # circuit filter by Japanese name
    pts2 = parsers.history_points(
        parsed, "day", "2025-07-01", "2025-07-01", metrics=None, circuits=["キッチン"]
    )
    assert [p.metric for p in pts2] == ["キッチン"]

    # range excludes rows outside [start, end]
    pts3 = parsers.history_points(
        parsed, "day", "2025-07-10", "2025-07-12", metrics=["grid_buy"], circuits=None
    )
    assert {p.timestamp for p in pts3} == {"2025-07-10", "2025-07-11", "2025-07-12"}


def test_history_points_cost_scale():
    parsed = parsers.parse_history_csv(_read_bytes("daycost_rc_202507.csv"))
    pts = parsers.history_points(
        parsed, "day", "2025-07-01", "2025-07-01", metrics=["grid_buy"], circuits=None, scale=0.001
    )
    assert pts[0].value == pytest.approx(499.579)  # 499579 * 0.001 JPY


# --- HistoryStore (fake client, synthesized zip) -----------------------------------------------


class _FakeClient:
    """Serves a zip built from the CSV fixtures; counts downloads to assert caching."""

    def __init__(self, members: list[str]) -> None:
        self.calls = 0
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name in members:
                # nest under a rireki_* dir like the real export; store ignores the prefix
                zf.writestr(f"rireki_test/{name}", _read_bytes(name))
        self._zip = buf.getvalue()

    async def download_history_zip(self, *, timeout: float = 60.0) -> bytes:
        self.calls += 1
        return self._zip


_MEMBERS = [
    "dayhistory_rc_202507.csv",
    "daycost_rc_202507.csv",
    "monthhistory_rc_2025.csv",
    "monthcost_rc_2025.csv",
    "yearhistory_total_rc.csv",
    "yearcost_total_rc.csv",
    "30minhistory_rc_20260412.csv",
    "hourhistory_rc_20260412.csv",
    "co2.conf",  # non-CSV: must be ignored on extract
]


def _store(tmp_path, ttl=3600) -> tuple[HistoryStore, _FakeClient]:
    client = _FakeClient(_MEMBERS)
    return HistoryStore(client, cache_dir=str(tmp_path / "cache"), ttl=ttl), client


async def test_store_get_history_and_download_cached(tmp_path):
    store, client = _store(tmp_path)
    page = await store.get_history(
        "day", "2025-07-01", "2025-07-14", metrics=["grid_buy"], circuits=None, limit=200, offset=0
    )
    assert client.calls == 1
    assert page.unit == "Wh"
    assert page.granularity == "day"
    # 14 days, one grid_buy point each
    assert page.total_rows == 14
    assert all(p.metric == "grid_buy" for p in page.series)
    first = next(p for p in page.series if p.timestamp == "2025-07-01")
    assert first.value == 14440.0

    # second query reuses the cached extraction (no second download)
    await store.get_history(
        "day", "2025-07-01", "2025-07-02", metrics=None, circuits=None, limit=50, offset=0
    )
    assert client.calls == 1


async def test_store_cost_history_shares_download(tmp_path):
    store, client = _store(tmp_path)
    await store.get_history(
        "day", "2025-07-01", "2025-07-01", metrics=None, circuits=None, limit=500, offset=0
    )
    cost = await store.get_cost_history("day", "2025-07-01", "2025-07-01", limit=500, offset=0)
    assert client.calls == 1  # one shared download backs both tools
    assert cost.unit == "JPY"
    grid_buy = next(p for p in cost.series if p.metric == "grid_buy")
    assert grid_buy.value == pytest.approx(499.579)


async def test_store_pagination(tmp_path):
    store, _ = _store(tmp_path)
    page = await store.get_history(
        "day", "2025-07-01", "2025-07-14", metrics=None, circuits=None, limit=5, offset=0
    )
    assert len(page.series) == 5
    assert page.has_more is True
    assert page.next_offset == 5
    assert page.total_rows > 5

    last = await store.get_history(
        "day", "2025-07-01", "2025-07-14", metrics=None, circuits=None,
        limit=5, offset=page.total_rows - 2,
    )
    assert last.has_more is False
    assert last.next_offset is None


async def test_store_non_csv_is_ignored(tmp_path):
    store, _ = _store(tmp_path)
    await store.get_history(
        "day", "2025-07-01", "2025-07-01", metrics=None, circuits=None, limit=10, offset=0
    )
    cache = tmp_path / "cache"
    assert not (cache / "co2.conf").exists()  # .conf dropped
    assert (cache / "dayhistory_rc_202507.csv").exists()


async def test_store_out_of_range_raises_with_hint(tmp_path):
    from mcp.server.fastmcp.exceptions import ToolError

    store, _ = _store(tmp_path)
    with pytest.raises(ToolError) as excinfo:
        await store.get_history(
            "day", "2030-01-01", "2030-01-31", metrics=None, circuits=None, limit=10, offset=0
        )
    # the hint reports the actually-available range (dayhistory covers 202507)
    assert "available day range" in str(excinfo.value)


async def test_store_granularities(tmp_path):
    store, _ = _store(tmp_path)
    m = await store.get_history(
        "month", "2025-01", "2025-12", metrics=["grid_buy"], circuits=None, limit=100, offset=0
    )
    assert m.total_rows >= 1 and all(p.metric == "grid_buy" for p in m.series)
    y = await store.get_history(
        "year", "2020", "2026", metrics=["grid_buy"], circuits=None, limit=100, offset=0
    )
    assert y.total_rows >= 1
    h = await store.get_history(
        "30min", "2026-04-12", "2026-04-12", metrics=["grid_buy"], circuits=None, limit=100, offset=0
    )
    assert h.total_rows == 48  # 48 half-hour buckets in a day
    assert h.series[0].timestamp.startswith("2026-04-12T")
