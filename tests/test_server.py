"""Server-level tests: the DNS-rebinding toggle (via .env) and the _audited tool wrapper."""

from __future__ import annotations

import logging

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from aiseg2_mcp.config import Settings
from aiseg2_mcp.server import _audited, _configure_streamable_http, mcp

# --- A-1: DNS-rebinding toggle is read from Settings (so .env works) ----------------------------


def _write_env(tmp_path, extra: str) -> None:
    (tmp_path / ".env").write_text(
        "AISEG_URL=http://192.168.0.216\nAISEG_PASSWORD=secret\n" + extra,
        encoding="utf-8",
    )


def test_dns_rebinding_toggle_enabled_via_env(monkeypatch, tmp_path):
    _write_env(tmp_path, "AISEG_DISABLE_DNS_REBINDING_PROTECTION=true\n")
    monkeypatch.chdir(tmp_path)
    original = mcp.settings.transport_security
    try:
        settings = Settings()
        assert settings.aiseg_disable_dns_rebinding_protection is True
        _configure_streamable_http(settings)
        assert mcp.settings.transport_security is not None
        assert mcp.settings.transport_security.enable_dns_rebinding_protection is False
    finally:
        mcp.settings.transport_security = original


def test_dns_rebinding_default_keeps_protection(monkeypatch, tmp_path):
    _write_env(tmp_path, "")  # flag absent -> default
    monkeypatch.chdir(tmp_path)
    original = mcp.settings.transport_security
    try:
        settings = Settings()
        assert settings.aiseg_disable_dns_rebinding_protection is False
        _configure_streamable_http(settings)
        # the SDK's construction-time default (protection on) is left in place
        assert mcp.settings.transport_security is not None
        assert mcp.settings.transport_security.enable_dns_rebinding_protection is True
    finally:
        mcp.settings.transport_security = original


# --- A-2: the _audited wrapper (uniform audit + error normalization) ----------------------------


async def test_audited_success_logs_ok(caplog):
    @_audited("demo", lambda r: f"value={r}")
    async def fn() -> int:
        return 42

    with caplog.at_level(logging.INFO, logger="aiseg2_mcp.audit"):
        assert await fn() == 42
    assert "demo outcome=ok value=42" in caplog.text


async def test_audited_valueerror_becomes_toolerror(caplog):
    @_audited("demo")
    async def fn() -> int:
        raise ValueError("bad shape")

    with caplog.at_level(logging.INFO, logger="aiseg2_mcp.audit"):
        with pytest.raises(ToolError, match="bad shape"):
            await fn()
    assert "demo outcome=error" in caplog.text


async def test_audited_toolerror_is_logged_and_reraised(caplog):
    @_audited("demo")
    async def fn() -> int:
        raise ToolError("device down")

    with caplog.at_level(logging.INFO, logger="aiseg2_mcp.audit"):
        with pytest.raises(ToolError, match="device down"):
            await fn()
    assert "demo outcome=error" in caplog.text


async def test_audited_preserves_tool_signature():
    # The wrapper must not hide parameters from FastMCP's schema generation.
    tools = {t.name: t for t in await mcp.list_tools()}
    props = tools["get_history"].inputSchema["properties"]
    assert {"granularity", "start", "end", "limit", "offset"} <= set(props)
