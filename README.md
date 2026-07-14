<!-- mcp-name: io.github.chanyou0311/aiseg2-mcp -->

# aiseg2-mcp

日本語版は [README.ja.md](./README.ja.md) をご覧ください。

An **unofficial, read-only** [Model Context Protocol](https://modelcontextprotocol.io) server for
the **Panasonic AiSEG2** home energy management (HEMS) controller. It lets an MCP client (e.g.
Claude) read your home's live power flow, per-circuit consumption, circuit names, and daily energy
totals from the AiSEG2's local web interface.

This project is not affiliated with or endorsed by Panasonic. "AiSEG" is a Panasonic trademark.

## Verified environment

Developed and tested against:

- AiSEG2 model **MKN713** series
- Firmware **Ver.2.97I-01**

The AiSEG2 web interface is undocumented and changes between firmware revisions. **On a different
model or firmware the pages this server scrapes may differ and some tools may not work.** If you hit
a parse error, please open an issue with your model / firmware version.

## Tools

All tools are **read-only** (annotated `readOnlyHint`, non-destructive). The server only issues
GETs and the display-only refresh POSTs the web UI itself uses; it never touches settings or any
`/action/` endpoint.

| Tool | Returns |
|---|---|
| `get_power_flow` | Instantaneous generation/consumption (kW), buy/sell state, battery status, generation sources, top consuming circuits |
| `get_circuit_breakdown` | Every measured circuit's instantaneous draw (W), ranked highest first, with the total |
| `list_circuits` | Registered circuit ids and names (the authoritative naming source) |
| `get_daily_totals` | Today's cumulative generation / consumption / grid-buy / grid-sell (kWh) |
| `get_history` | Long-term energy history from the SD-card export (Wh), long-form points. Args: `granularity` (`30min`/`hour`/`day`/`month`/`year`), `start`/`end` (per granularity: `YYYY-MM-DD`, `YYYY-MM`, or `YYYY`), optional `metrics`/`circuits` filters, `limit`/`offset` paging |
| `get_cost_history` | Long-term energy-cost history from the SD-card export (JPY). Args: `granularity` (`day`/`month`/`year`), `start`/`end`, `limit`/`offset` |

> **The two history tools require an SD card inserted in the AiSEG2** — they read the device's SD-card CSV export. The export is downloaded once and cached (see `AISEG_CACHE_DIR` / `AISEG_CACHE_TTL`), so the first call is slow and later calls are fast.

## Install & run

Three ways to run it, depending on your setup.

### 1. uvx (PyPI — once published)

The simplest option for a local (stdio) MCP client. Requires [uv](https://docs.astral.sh/uv/).

```bash
AISEG_URL=http://192.168.0.216 AISEG_PASSWORD=... uvx aiseg2-mcp
```

Add it to Claude Code:

```bash
claude mcp add aiseg2 \
  --env AISEG_URL=http://192.168.0.216 \
  --env AISEG_PASSWORD=your-digest-password \
  -- uvx aiseg2-mcp
```

### 2. docker run (GHCR)

The container defaults to the `streamable-http` transport (long-lived network service). Only expose
it behind an authenticating proxy — see [Security](#security).

```bash
docker run --rm -p 8000:8000 \
  -e AISEG_URL=http://192.168.0.216 \
  -e AISEG_PASSWORD=your-digest-password \
  ghcr.io/chanyou0311/aiseg2-mcp:latest
```

### 3. From source

Requires Python 3.12+ and uv.

```bash
uv sync
AISEG_URL=http://192.168.0.216 AISEG_PASSWORD=... uv run aiseg2-mcp
```

### Remote (authenticated claude.ai Custom Connector)

To reach the server from claude.ai while your AiSEG2 stays on your LAN, see
[`examples/remote/`](./examples/remote/) — a Docker Compose stack (MCP + GitHub-OAuth proxy +
Cloudflare Tunnel).

## Configuration (environment variables)

| Variable | Required | Default | Description |
|---|---|---|---|
| `AISEG_URL` | yes | — | AiSEG2 base URL, e.g. `http://192.168.0.216` (http only) |
| `AISEG_PASSWORD` | yes | — | HTTP Digest password for the AiSEG2 web UI |
| `AISEG_USER` | no | `aiseg` | HTTP Digest user |
| `AISEG_TRANSPORT` | no | `stdio` | `stdio` or `streamable-http` |
| `AISEG_HOST` | no | `0.0.0.0` | Bind host (streamable-http only) |
| `AISEG_PORT` | no | `8000` | Bind port (streamable-http only) |
| `AISEG_DISABLE_DNS_REBINDING_PROTECTION` | no | `false` | Disable the SDK Host allowlist — **only** behind a trusted auth proxy |
| `AISEG_CACHE_DIR` | no | `<tempdir>/aiseg2-mcp-cache` | Where the SD-card history export is cached |
| `AISEG_CACHE_TTL` | no | `3600` | Seconds to reuse a cached history export before re-downloading |
| `LOG_LEVEL` | no | `info` | Log level |

## Security

- **LAN-only by design.** The AiSEG2 speaks plain HTTP with Digest auth; keep it and this server on
  a trusted local network. The password is read from the environment and is never logged.
- **Read-only.** There is no tool that changes a device setting. The tool surface is enforced by
  tests (registered-tool allowlist, tool-name guard, a source scan for `/action/`, and read-only
  annotation checks).
- **Do not expose the `streamable-http` transport to untrusted networks without authentication.**
  This server carries no auth of its own; if you run it as a network service, put an authenticating
  reverse proxy in front of it. `AISEG_DISABLE_DNS_REBINDING_PROTECTION=true` is only appropriate in
  that proxied setup.

## Acknowledgements

The AiSEG2 web interface is undocumented; this project builds on the reverse-engineering knowledge
shared by prior work:

- [shimosyan/aiseg2-influxdb-forwarder](https://github.com/shimosyan/aiseg2-influxdb-forwarder) — the circuit-paging "repeat the last page" terminator and the electric-flow fields.
- [hiroaki0923/aiseg2-bridge](https://github.com/hiroaki0923/aiseg2-bridge) — endpoint and page structure.
- [Bugfire/aiseg_download](https://github.com/Bugfire/aiseg_download) — Digest auth and data-endpoint conventions.

## License

[MIT](./LICENSE)
