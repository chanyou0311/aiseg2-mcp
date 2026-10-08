"""Pins each tool's name, annotations and input schema so an SDK upgrade cannot change them silently.

Regenerate after an intended change: ``AISEG_UPDATE_SNAPSHOT=1 uv run pytest tests/test_tool_snapshot.py``.
"""

from __future__ import annotations

import json
import os
import pathlib

from aiseg2_mcp.server import mcp

SNAPSHOT = pathlib.Path(__file__).parent / "fixtures" / "tool_surface.json"


async def _surface() -> dict[str, dict[str, object]]:
    return {
        tool.name: {
            "annotations": tool.annotations.model_dump(by_alias=True, exclude_none=True) if tool.annotations else None,
            "inputSchema": tool.input_schema,
        }
        for tool in sorted(await mcp.list_tools(), key=lambda t: t.name)
    }


async def test_tool_surface_matches_snapshot():
    surface = await _surface()
    if os.environ.get("AISEG_UPDATE_SNAPSHOT"):
        SNAPSHOT.write_text(json.dumps(surface, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert surface == json.loads(SNAPSHOT.read_text(encoding="utf-8"))
