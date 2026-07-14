"""Tests for the SD-card history parsers and the caching HistoryStore.

Uses the real CSV fixtures under tests/fixtures/sd_zip/ (recorded from a device with FW
Ver.2.97I-01), assembled into a small in-memory zip served by a fake client — so no network and no
real download, while still exercising the true CSV shapes (UTF-8 BOM, 無効N columns, the tail
utility meters, and the per-granularity timestamp formats).
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable

import pytest

from aiseg2_mcp import parsers
from aiseg2_mcp.history import _FILESPECS, HistoryStore, _available_hint


def _sd(name: str) -> str:
    return f"sd_zip/{name}"


# --- parsers -----------------------------------------------------------------------------------


def test_parse_history_csv_columns_and_bom(read_bytes):
    parsed = parsers.parse_history_csv(read_bytes(_sd("dayhistory_rc_202507.csv")))
    names = [c.name for c in parsed.columns]
    assert not any(n.startswith("﻿") for n in names)  # BOM stripped
    assert not any(n.startswith("無効") for n in names)  # 無効N dropped
    by_name = {c.name: c for c in parsed.columns}
    assert by_name["主幹買電"].key == "grid_buy"
    assert by_name["主幹売電"].key == "grid_sell"
    assert by_name["太陽光発電(PV1)"].key == "generation_pv1"
    assert by_name["キッチン"].key is None
    assert by_name["キッチン"].label == "キッチン"
    assert "使用電力量" in by_name  # tail utility meter survives


def test_parse_history_csv_disambiguates_duplicate_headers(read_bytes):
    # The device reuses labels (電子レンジ×3, ＬＤ×2); labels must be unique via #N suffixes.
    parsed = parsers.parse_history_csv(read_bytes(_sd("dayhistory_rc_202507.csv")))
    labels = [c.label for c in parsed.columns]
    assert {"電子レンジ", "電子レンジ#2", "電子レンジ#3"} <= set(labels)
    assert {"ＬＤ", "ＬＤ#2"} <= set(labels)
    assert len(labels) == len(set(labels))  # every series label is unique


def test_select_columns_circuit_filter_matches_all_duplicates(read_bytes):
    # circuits=["電子レンジ"] must include every same-named variant (base-name match).
    parsed = parsers.parse_history_csv(read_bytes(_sd("dayhistory_rc_202507.csv")))
    selected = parsers._select_columns(parsed.columns, None, ["電子レンジ"])
    assert [c.label for c in selected] == ["電子レンジ", "電子レンジ#2", "電子レンジ#3"]


def test_parse_history_csv_folds_fullwidth_header():
    # A full-width paren/letter spelling of a standard metric still maps to its English key.
    raw = ("﻿計測日時,太陽光発電（ＰＶ１）,主幹買電\n20250701,100,200\n").encode("utf-8")
    parsed = parsers.parse_history_csv(raw)
    keys = {c.key for c in parsed.columns}
    assert "generation_pv1" in keys  # （ＰＶ１） NFKC-folded to (PV1)
    assert "grid_buy" in keys


def test_validate_range_accepts_and_rejects():
    parsers.validate_range("day", "2025-07-01", "2025-07-02")  # no raise
    parsers.validate_range("month", "2025-06", "2025-07")
    parsers.validate_range("year", "2024", "2025")
    with pytest.raises(ValueError, match="expected format YYYY-MM for granularity=month"):
        parsers.validate_range("month", "2025-06-15", "2025-06")


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


def test_history_points_filters_and_missing(read_bytes):
    parsed = parsers.parse_history_csv(read_bytes(_sd("dayhistory_rc_202507.csv")))
    pts = parsers.history_points(
        parsed, "day", "2025-07-01", "2025-07-01", metrics=["grid_buy"], circuits=None
    )
    assert len(pts) == 1
    assert pts[0].timestamp == "2025-07-01"
    assert pts[0].metric == "grid_buy"
    assert pts[0].value == 14440.0

    pts2 = parsers.history_points(
        parsed, "day", "2025-07-01", "2025-07-01", metrics=None, circuits=["キッチン"]
    )
    assert [p.metric for p in pts2] == ["キッチン"]

    pts3 = parsers.history_points(
        parsed, "day", "2025-07-10", "2025-07-12", metrics=["grid_buy"], circuits=None
    )
    assert {p.timestamp for p in pts3} == {"2025-07-10", "2025-07-11", "2025-07-12"}


def test_history_points_cost_scale(read_bytes):
    parsed = parsers.parse_history_csv(read_bytes(_sd("daycost_rc_202507.csv")))
    pts = parsers.history_points(
        parsed, "day", "2025-07-01", "2025-07-01", metrics=["grid_buy"], circuits=None, scale=0.001
    )
    assert pts[0].value == pytest.approx(499.579)  # 499579 * 0.001 JPY


def test_available_hint_year_names_fixed_file(tmp_path):
    # For the fixed-file year granularity, an empty cache yields a hint naming the missing file.
    hint = _available_hint(_FILESPECS[("history", "year")], tmp_path)
    assert "yearhistory_total_rc.csv is not present" in hint


# --- HistoryStore (fake client, synthesized zip) -----------------------------------------------


class _FakeClient:
    """Serves a zip built from the CSV fixtures; counts downloads to assert caching."""

    def __init__(self, members: list[str], read_bytes: Callable[[str], bytes]) -> None:
        self.calls = 0
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name in members:
                # nest under a rireki_* dir like the real export; the store ignores the prefix
                zf.writestr(f"rireki_test/{name}", read_bytes(_sd(name)))
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


@pytest.fixture
def store_and_client(tmp_path, read_bytes):
    def _make(ttl: int = 3600) -> tuple[HistoryStore, _FakeClient]:
        client = _FakeClient(_MEMBERS, read_bytes)
        return HistoryStore(client, cache_dir=str(tmp_path / "cache"), ttl=ttl), client

    return _make


async def test_store_get_history_and_download_cached(store_and_client):
    store, client = store_and_client()
    page = await store.get_history(
        "day", "2025-07-01", "2025-07-14", metrics=["grid_buy"], circuits=None, limit=200, offset=0
    )
    assert client.calls == 1
    assert page.unit == "Wh"
    assert page.granularity == "day"
    assert page.total_rows == 14  # 14 days, one grid_buy point each
    assert all(p.metric == "grid_buy" for p in page.series)
    first = next(p for p in page.series if p.timestamp == "2025-07-01")
    assert first.value == 14440.0

    await store.get_history(
        "day", "2025-07-01", "2025-07-02", metrics=None, circuits=None, limit=50, offset=0
    )
    assert client.calls == 1  # cached extraction reused


async def test_store_cost_history_shares_download(store_and_client):
    store, client = store_and_client()
    await store.get_history(
        "day", "2025-07-01", "2025-07-01", metrics=None, circuits=None, limit=500, offset=0
    )
    cost = await store.get_cost_history("day", "2025-07-01", "2025-07-01", limit=500, offset=0)
    assert client.calls == 1  # one shared download backs both tools
    assert cost.unit == "JPY"
    grid_buy = next(p for p in cost.series if p.metric == "grid_buy")
    assert grid_buy.value == pytest.approx(499.579)


async def test_store_pagination_offset_reuses_memo(store_and_client):
    store, client = store_and_client()
    page = await store.get_history(
        "day", "2025-07-01", "2025-07-14", metrics=None, circuits=None, limit=5, offset=0
    )
    assert len(page.series) == 5
    assert page.has_more is True
    assert page.next_offset == 5
    assert page.total_rows > 5

    # same query, different offset -> served from the memo, no extra download
    page2 = await store.get_history(
        "day", "2025-07-01", "2025-07-14", metrics=None, circuits=None,
        limit=5, offset=page.total_rows - 2,
    )
    assert page2.has_more is False
    assert page2.next_offset is None
    assert client.calls == 1


async def test_store_non_csv_is_ignored(store_and_client, tmp_path):
    store, _ = store_and_client()
    await store.get_history(
        "day", "2025-07-01", "2025-07-01", metrics=None, circuits=None, limit=10, offset=0
    )
    cache = tmp_path / "cache"
    assert not (cache / "co2.conf").exists()
    assert (cache / "dayhistory_rc_202507.csv").exists()


async def test_store_rejects_wrong_range_format(store_and_client):
    from mcp.server.fastmcp.exceptions import ToolError

    store, client = store_and_client()
    with pytest.raises(ToolError, match="expected format YYYY-MM for granularity=month"):
        await store.get_history(
            "month", "2025-06-15", "2025-06-20", metrics=None, circuits=None, limit=10, offset=0
        )
    assert client.calls == 0  # validation happens before any download


async def test_store_out_of_range_raises_with_hint(store_and_client):
    from mcp.server.fastmcp.exceptions import ToolError

    store, _ = store_and_client()
    with pytest.raises(ToolError) as excinfo:
        await store.get_history(
            "day", "2030-01-01", "2030-01-31", metrics=None, circuits=None, limit=10, offset=0
        )
    assert "available range" in str(excinfo.value)  # hint reports the real dayhistory coverage


async def test_store_granularities(store_and_client):
    store, _ = store_and_client()
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
