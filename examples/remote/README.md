# Remote deployment (authenticated claude.ai Custom Connector)

日本語版は [README.ja.md](./README.ja.md) をご覧ください。

This example exposes `aiseg2-mcp` to [claude.ai](https://claude.ai) as an authenticated
[Custom Connector](https://support.anthropic.com/en/articles/11175166-about-custom-connectors-remote-mcp),
reachable from anywhere while your AiSEG2 stays on your LAN.

```
claude.ai ──OAuth 2.1──▶ Cloudflare Tunnel ──▶ mcp-auth-proxy (GitHub OAuth) ──▶ aiseg2-mcp ──▶ AiSEG2 (LAN)
```

> **Security — read first.** The MCP server has **no authentication of its own**. The GitHub-OAuth
> proxy ([`mcp-auth-proxy`](https://github.com/sigbit/mcp-auth-proxy)) is the trust boundary and the
> only service the tunnel exposes; `aiseg2-mcp` is reachable only on the internal compose network.
> `AISEG_DISABLE_DNS_REBINDING_PROTECTION=true` is set **because** the server sits behind that
> authenticating proxy. **Never expose `aiseg2-mcp` (or the proxy without OAuth) directly to the
> internet.**

## Prerequisites

1. **A host on the same LAN as your AiSEG2** with Docker + Docker Compose.
2. **A Cloudflare Tunnel.** In the Cloudflare Zero Trust dashboard, create a remotely-managed tunnel
   and add a **public hostname** (e.g. `mcp-aiseg2.example.com`) whose service is
   `http://mcp-auth-proxy:8080`. Copy the tunnel **token** into `TUNNEL_TOKEN`.
3. **A GitHub OAuth App** (GitHub → Settings → Developer settings → OAuth Apps). Set the
   **Authorization callback URL** to `https://<your-host>/.auth/github/callback` (i.e.
   `${MCP_EXTERNAL_URL}/.auth/github/callback`). Copy the Client ID / Client Secret into
   `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET`, and set `GITHUB_ALLOWED_USERS` to the GitHub logins
   allowed to connect.

## Run

```bash
cp .env.example .env
# edit .env
docker compose up -d
```

Confirm the public hostname serves the OAuth metadata:

```bash
curl https://<your-host>/.well-known/oauth-authorization-server
```

## Register in claude.ai

1. claude.ai → **Settings → Connectors → Add custom connector**.
2. URL: `https://<your-host>/mcp`.
3. Complete the GitHub sign-in when prompted; only `GITHUB_ALLOWED_USERS` may authorize.
4. The six read-only AiSEG2 tools then appear in claude.ai.

## Files

- [`docker-compose.yml`](./docker-compose.yml) — the three services (aiseg2-mcp, mcp-auth-proxy, cloudflared).
- [`.env.example`](./.env.example) — configuration template.
