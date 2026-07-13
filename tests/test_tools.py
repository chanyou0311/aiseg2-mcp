"""Adversarial tests for the tool surface: it must stay exactly the four read-only observations.

The safety property is that this server never mutates the AiSEG2: no write/settings tool may be
registered, no tool name may hint at mutation, no code path may call the device's /action/
endpoints, and every tool must be annotated read-only / non-destructive. These guards fail closed
if a future change adds a mutating surface.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from aiseg2_mcp.server import mcp

EXPECTED_TOOLS = {
    "get_power_flow",
    "get_circuit_breakdown",
    "list_circuits",
    "get_daily_totals",
}

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "aiseg2_mcp"


async def test_registered_tools_are_exactly_the_four_read_only_tools():
    names = {t.name for t in await mcp.list_tools()}
    assert names == EXPECTED_TOOLS


async def test_no_tool_name_hints_at_mutation():
    names = {t.name for t in await mcp.list_tools()}
    forbidden = re.compile(r"set|update_|control|write|delete", re.IGNORECASE)
    offenders = [n for n in names if forbidden.search(n)]
    assert not offenders, f"tool name suggests mutation: {offenders}"


async def test_all_tools_are_annotated_read_only_and_non_destructive():
    for tool in await mcp.list_tools():
        ann = tool.annotations
        assert ann is not None, f"{tool.name} has no annotations"
        assert ann.readOnlyHint is True, f"{tool.name} is not readOnlyHint=True"
        assert ann.destructiveHint is False, f"{tool.name} is not destructiveHint=False"


def test_source_never_calls_action_endpoints():
    # No code path may hit the AiSEG2's mutating /action/ endpoints.
    pattern = re.compile(r"/action/")
    hits = [
        f"{py.name}:{i}: {line.strip()}"
        for py in PACKAGE.rglob("*.py")
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert not hits, "found a reference to a mutating /action/ endpoint: " + "; ".join(hits)


def test_client_only_requests_get_or_display_post_paths():
    # Belt-and-suspenders: the only POST paths in the client are the display-only /data/**/update
    # refresh endpoints the web UI itself uses — never a settings/action write.
    src = (PACKAGE / "client.py").read_text(encoding="utf-8")
    post_paths = re.findall(r'_send\(\s*"POST",\s*"([^"]+)"', src)
    assert post_paths, "expected at least one POST path in the client"
    assert all(p.startswith("/data/") and p.endswith("/update") for p in post_paths), post_paths


def test_mcp_app_constructs():
    assert mcp is not None
