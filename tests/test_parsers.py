"""Fixture-driven regression tests for the pure parsers (device HTML/JSON -> models).

The fixtures under tests/fixtures/ were recorded from a real AiSEG2 (FW Ver.2.97I-01). These tests
pin the device's quirks: full-width normalization, "-" -> None, the <br/>-in-label join, the
"repeat the previous page" paging terminator, and the strBtnType filter on page 734.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from aiseg2_mcp import parsers

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- normalize_number --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (2040, 2040.0),
        ("2.0", 2.0),
        (" 224W", 224.0),
        ("-", None),
        ("", None),
        (None, None),
        ("１２３．５", 123.5),  # full-width digits + full-width period
        ("1,234", 1234.0),  # thousands separator stripped
    ],
)
def test_normalize_number(raw, expected):
    assert parsers.normalize_number(raw) == expected


@pytest.mark.parametrize("raw", [-5, 1_000_000, "9999999W"])
def test_normalize_number_out_of_range_raises(raw):
    with pytest.raises(ValueError):
        parsers.normalize_number(raw)


# --- power flow (electricflow/111) -------------------------------------------------------------


def test_parse_power_flow_fields():
    data = json.loads(_read("electricflow_111.json"))
    flow = parsers.parse_power_flow(data)

    assert flow.generation_kw == 2.1
    assert flow.consumption_kw == 1.2
    assert flow.buy_sell == "sell"  # lo_buy_sell == 1
    assert flow.battery is None  # connSb == 0
    # generation detail: only the labelled source with a real reading (太陽光発電/2130W);
    # g_d_2 has a blank title + "-" capacity and is dropped.
    assert [(d.name, d.watt) for d in flow.generation_detail] == [("太陽光発電", 2130.0)]
    # top consumers gated by best1..3; capacities are watts
    assert [(c.name, c.watt) for c in flow.top_consumers] == [
        ("ＬＤＫ・エアコン", 825.0),
        ("洋室３", 70.0),
        ("ＬＤ", 62.0),
    ]


def test_parse_power_flow_buy_sell_mapping():
    base = json.loads(_read("electricflow_111.json"))
    assert parsers.parse_power_flow({**base, "lo_buy_sell": 0}).buy_sell == "buy"
    assert parsers.parse_power_flow({**base, "lo_buy_sell": 1}).buy_sell == "sell"
    assert parsers.parse_power_flow({**base, "lo_buy_sell": 2}).buy_sell == "none"
    assert parsers.parse_power_flow({**base, "lo_buy_sell": 9}).buy_sell == "unknown"


def test_parse_power_flow_battery_when_connected():
    base = json.loads(_read("electricflow_111.json"))
    flow = parsers.parse_power_flow(
        {**base, "connSb": 1, "soc": 3, "percent": "60", "charge": 0}
    )
    assert flow.battery is not None
    assert flow.battery.percent == 60.0
    assert flow.battery.level == 3
    assert flow.battery.charging is True  # charge == 0


def test_parse_power_flow_meas_reg_zero_raises():
    base = json.loads(_read("electricflow_111.json"))
    with pytest.raises(ValueError):
        parsers.parse_power_flow({**base, "measReg": 0})


# --- circuit breakdown (electricflow/1113) -----------------------------------------------------


def test_parse_circuit_page_names_and_watts():
    rows = parsers.parse_circuit_page(_read("electricflow_1113_id1.html"))
    assert len(rows) == 10
    assert rows[0] == ("ＬＤ", 224.0)
    assert rows[1] == ("洋室３", 77.0)


def test_parse_circuit_page_br_join():
    # id=3 contains a label wrapped by <br/> -> joined with a single space.
    rows = parsers.parse_circuit_page(_read("electricflow_1113_id3.html"))
    names = [name for name, _ in rows]
    assert "寝室　エアコ ン" in names  # <br/> between エアコ and ン becomes a space


def test_circuit_page_terminal_detection():
    # id=5 repeats id=4 -> identical signatures -> terminal.
    rows4 = parsers.parse_circuit_page(_read("electricflow_1113_id4.html"))
    rows5 = parsers.parse_circuit_page(_read("electricflow_1113_id5.html"))
    assert parsers.page_signature(rows4) == parsers.page_signature(rows5)
    # id=3 differs from id=4 -> not terminal.
    rows3 = parsers.parse_circuit_page(_read("electricflow_1113_id3.html"))
    assert parsers.page_signature(rows3) != parsers.page_signature(rows4)


def test_assemble_breakdown_filters_placeholders_and_ranks():
    pages = [
        parsers.parse_circuit_page(_read(f"electricflow_1113_id{i}.html")) for i in (1, 2, 3, 4)
    ]
    breakdown = parsers.assemble_breakdown(pages)
    assert breakdown.page_count == 4
    # ranks are contiguous 1..N
    assert [c.rank for c in breakdown.circuits] == list(range(1, len(breakdown.circuits) + 1))
    # the "-" placeholder slot on id=4 is dropped; the real 0 W circuit (ＩＨ) survives at 0.0
    names = [c.name for c in breakdown.circuits]
    assert "-" not in names
    assert "ＩＨ" in names
    ih = next(c for c in breakdown.circuits if c.name == "ＩＨ")
    assert ih.watt == 0.0
    assert breakdown.total_watt == pytest.approx(sum(c.watt for c in breakdown.circuits))


# --- installation circuit names (setting/installation/734) -------------------------------------


def test_parse_installation_circuits_filters_btn_type():
    circuits = parsers.parse_installation_circuits(_read("installation_734.html"))
    # 32 measurement circuits (strBtnType == "1") on this unit.
    assert len(circuits) == 32
    # every id is present and non-empty; names are non-empty
    assert all(c.id and c.name for c in circuits)
    ids = {c.id for c in circuits}
    assert "8" in ids  # first measurement circuit


# --- daily graph totals (graph/5x111) ----------------------------------------------------------


def test_parse_graph_kwh_and_date():
    assert parsers.parse_graph_kwh(_read("graph_52111.html")) == 6.118
    assert parsers.parse_graph_date(_read("graph_52111.html")) == "2026-07-14"


def test_parse_graph_kwh_missing_returns_none():
    # a val_kwh of "-" -> None (meter unavailable)
    html = _read("graph_52111.html").replace(">6.118<", ">-<")
    assert parsers.parse_graph_kwh(html) is None


def test_build_daily_totals():
    totals = parsers.build_daily_totals(
        parsers.parse_graph_date(_read("graph_52111.html")),
        _read("graph_51111.html"),
        _read("graph_52111.html"),
        _read("graph_53111.html"),
        _read("graph_54111.html"),
    )
    assert totals.date == "2026-07-14"
    assert totals.generation_kwh == 2.169
    assert totals.consumption_kwh == 6.118
    assert totals.buy_kwh == 4.615
    assert totals.sell_kwh == 0.666
