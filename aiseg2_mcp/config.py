"""Typed configuration from environment variables (repo convention: config is a Pydantic model).

The AiSEG2 speaks HTTP Digest over plain http on the LAN, so the two required values are the
device URL and the Digest password. Everything else has a default. ``AISEG_URL`` /
``AISEG_PASSWORD`` are required: a missing one raises ValidationError at startup rather than
failing on the first tool call.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Required values missing from the env raise ValidationError at startup."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- AiSEG2 connection (required) ---
    # Base URL of the AiSEG2, e.g. http://192.168.0.216 (http only — the device has no HTTPS).
    aiseg_url: str = Field(alias="AISEG_URL")
    # HTTP Digest password. Injected from the environment / a secret; never logged.
    aiseg_password: str = Field(alias="AISEG_PASSWORD")
    # HTTP Digest user. The AiSEG2 default web user is "aiseg".
    aiseg_user: str = Field(default="aiseg", alias="AISEG_USER")

    # --- transport ---
    # stdio (default) for a local MCP client; streamable-http to run as a network service.
    aiseg_transport: Literal["stdio", "streamable-http"] = Field(
        default="stdio", alias="AISEG_TRANSPORT"
    )
    # Bind for streamable-http only (FastMCP defaults to 127.0.0.1 = unreachable from a container).
    aiseg_host: str = Field(default="0.0.0.0", alias="AISEG_HOST")
    aiseg_port: int = Field(default=8000, alias="AISEG_PORT")

    # DNS-rebinding protection for streamable-http. Default False = keep the SDK default (protection
    # ON). Set true ONLY when this server sits behind a trusted authenticating reverse proxy whose
    # Host header would otherwise trip the SDK's allowlist (HTTP 421). See server.py.
    aiseg_disable_dns_rebinding_protection: bool = Field(
        default=False, alias="AISEG_DISABLE_DNS_REBINDING_PROTECTION"
    )

    # --- SD-card history cache ---
    # Where the downloaded history zip is extracted. Empty -> <tempdir>/aiseg2-mcp-cache.
    aiseg_cache_dir: str = Field(default="", alias="AISEG_CACHE_DIR")
    # How long a cached export is reused before re-downloading (seconds).
    aiseg_cache_ttl: int = Field(default=3600, alias="AISEG_CACHE_TTL")

    log_level: str = Field(default="info", alias="LOG_LEVEL")
