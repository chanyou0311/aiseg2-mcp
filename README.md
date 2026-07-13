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

## Install & run

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
# stdio (default) — run directly:
AISEG_URL=http://192.168.0.216 AISEG_PASSWORD=... uv run aiseg2-mcp
```

Add it to Claude Code as an MCP server (stdio):

```bash
# TODO: once published to PyPI, replace `uv run` with `uvx aiseg2-mcp`.
claude mcp add aiseg2 \
  --env AISEG_URL=http://192.168.0.216 \
  --env AISEG_PASSWORD=your-digest-password \
  -- uv run --directory /path/to/aiseg2-mcp aiseg2-mcp
```

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
