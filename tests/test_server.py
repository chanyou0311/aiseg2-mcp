"""Server-level tests: the DNS-rebinding toggle (via .env) and the _audited tool wrapper."""

from __future__ import annotations

import logging

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from aiseg2_mcp.config import Settings
from aiseg2_mcp.server import _audited, _streamable_http_options, mcp

# --- A-1: DNS-rebinding toggle is read from Settings (so .env works) ----------------------------


def _write_env(tmp_path, extra: str) -> None:
    (tmp_path / ".env").write_text(
        "AISEG_URL=http://192.168.0.216\nAISEG_PASSWORD=secret\n" + extra,
        encoding="utf-8",
    )


def test_dns_rebinding_toggle_enabled_via_env(monkeypatch, tmp_path):
    _write_env(tmp_path, "AISEG_DISABLE_DNS_REBINDING_PROTECTION=true\n")
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.aiseg_disable_dns_rebinding_protection is True
    security = _streamable_http_options(settings)["transport_security"]
    assert security.enable_dns_rebinding_protection is False


def test_dns_rebinding_default_keeps_protection(monkeypatch, tmp_path):
    _write_env(tmp_path, "")  # flag absent -> default
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.aiseg_disable_dns_rebinding_protection is False
    security = _streamable_http_options(settings)["transport_security"]
    # same as SDK v1: protection on with the localhost-only allowlist, even when bound to 0.0.0.0
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["127.0.0.1:*", "localhost:*", "[::1]:*"]


def test_streamable_http_options_keep_v1_modes_and_bind(monkeypatch, tmp_path):
    _write_env(tmp_path, "AISEG_HOST=127.0.0.1\nAISEG_PORT=9000\n")
    monkeypatch.chdir(tmp_path)
    options = _streamable_http_options(Settings())
    assert options["host"] == "127.0.0.1"
    assert options["port"] == 9000
    assert options["stateless_http"] is True
    assert options["json_response"] is True


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
    # The wrapper must not hide parameters from MCPServer's schema generation.
    tools = {t.name: t for t in await mcp.list_tools()}
    props = tools["get_history"].input_schema["properties"]
    assert {"granularity", "start", "end", "limit", "offset"} <= set(props)
