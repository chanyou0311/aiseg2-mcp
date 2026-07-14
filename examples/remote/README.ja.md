# リモートデプロイ（認証付き claude.ai Custom Connector）

English version: [README.md](./README.md)

この例は `aiseg2-mcp` を [claude.ai](https://claude.ai) の認証付き
[Custom Connector](https://support.anthropic.com/en/articles/11175166-about-custom-connectors-remote-mcp)
として公開します。AiSEG2 は LAN 内に置いたまま、どこからでも接続できます。

```
claude.ai ──OAuth 2.1──▶ Cloudflare Tunnel ──▶ mcp-auth-proxy (GitHub OAuth) ──▶ aiseg2-mcp ──▶ AiSEG2 (LAN)
```

> **セキュリティ（最初に読むこと）。** MCP サーバー自身は**認証を持ちません**。GitHub OAuth プロキシ
> （[`mcp-auth-proxy`](https://github.com/sigbit/mcp-auth-proxy)）が信頼境界であり、トンネルが公開する
> 唯一のサービスです。`aiseg2-mcp` は compose の内部ネットワークからのみ到達可能です。
> `AISEG_DISABLE_DNS_REBINDING_PROTECTION=true` は、この認証プロキシの背後にいる**ことを前提に**
> 設定しています。**`aiseg2-mcp`（および OAuth なしのプロキシ）を直接インターネットへ公開しないでください。**

## 前提条件

1. **AiSEG2 と同一 LAN 上のホスト**（Docker + Docker Compose）。
2. **Cloudflare Tunnel。** Cloudflare Zero Trust ダッシュボードで remotely-managed トンネルを作成し、
   **public hostname**（例: `mcp-aiseg2.example.com`）のサービスを `http://mcp-auth-proxy:8080` に
   設定します。トンネルの **token** を `TUNNEL_TOKEN` にコピーします。
3. **GitHub OAuth App**（GitHub → Settings → Developer settings → OAuth Apps）。
   **Authorization callback URL** を `https://<your-host>/.auth/github/callback`
   （= `${MCP_EXTERNAL_URL}/.auth/github/callback`）に設定します。Client ID / Client Secret を
   `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` にコピーし、接続を許可する GitHub ログインを
   `GITHUB_ALLOWED_USERS` に設定します。

## 起動

```bash
cp .env.example .env
# .env を編集
docker compose up -d
```

public hostname が OAuth メタデータを返すことを確認:

```bash
curl https://<your-host>/.well-known/oauth-authorization-server
```

## claude.ai への登録

1. claude.ai → **設定 → コネクタ → カスタムコネクタを追加**。
2. URL: `https://<your-host>/mcp`。
3. 案内に従い GitHub サインインを完了（`GITHUB_ALLOWED_USERS` のユーザーのみ認可可能）。
4. 6 つの読み取り専用 AiSEG2 ツールが claude.ai に表示されます。

## ファイル

- [`docker-compose.yml`](./docker-compose.yml) — 3 サービス（aiseg2-mcp, mcp-auth-proxy, cloudflared）。
- [`.env.example`](./.env.example) — 設定テンプレート。
