# aiseg2-mcp

English version: [README.md](./README.md)

Panasonic **AiSEG2**（HEMS コントローラー）を対象とした、**非公式・読み取り専用**の
[Model Context Protocol](https://modelcontextprotocol.io) サーバーです。MCP クライアント
（Claude など）から、AiSEG2 のローカル Web インターフェース経由で、自宅のリアルタイムな電力フロー・
回路別消費電力・回路名・日次エネルギー総量を読み取れます。

本プロジェクトは Panasonic とは無関係であり、Panasonic による承認・提携はありません。
「AiSEG」は Panasonic の商標です。

## 検証済み環境

以下の環境で開発・動作確認しています。

- AiSEG2 機種: **MKN713** 系
- ファームウェア: **Ver.2.97I-01**

AiSEG2 の Web インターフェースは非公開で、ファームウェアの版により変化します。
**機種やファームウェアが異なると、本サーバーがスクレイプするページの構造が変わり、一部のツールが
動作しない可能性があります。** パースエラーが出た場合は、機種・ファームウェア版を添えて issue を
立ててください。

## ツール一覧

すべて**読み取り専用**（`readOnlyHint`・非破壊のアノテーション付き）です。本サーバーは GET と、
Web UI 自身が画面更新に使う表示用 POST のみを発行し、設定変更や `/action/` 系エンドポイントには
一切触れません。

| ツール | 返す内容 |
|---|---|
| `get_power_flow` | 瞬時の発電/消費（kW）、売買電の状態、蓄電池状態、発電内訳、消費上位回路 |
| `get_circuit_breakdown` | 計測回路ごとの瞬時消費電力（W）を降順で全件・合計付き |
| `list_circuits` | 登録された回路の id と名称（名称の正本） |
| `get_daily_totals` | 当日の発電/消費/買電/売電の積算（kWh） |
| `get_history` | SD カードエクスポートからの長期履歴（Wh）をロング形式で。引数: `granularity`（`30min`/`hour`/`day`/`month`/`year`）、`start`/`end`（粒度に応じ `YYYY-MM-DD` / `YYYY-MM` / `YYYY`）、任意の `metrics`/`circuits` フィルタ、`limit`/`offset` ページング |
| `get_cost_history` | SD カードエクスポートからの長期コスト履歴（円）。引数: `granularity`（`day`/`month`/`year`）、`start`/`end`、`limit`/`offset` |

> **履歴系 2 ツールは AiSEG2 に SD カードが挿入されている場合のみ動作する** — デバイスの SD カード CSV エクスポートを読む。エクスポートは 1 回だけダウンロードしてキャッシュする（`AISEG_CACHE_DIR` / `AISEG_CACHE_TTL` 参照）。初回呼び出しは遅く、以降は高速。

## インストール・起動

環境に応じて 3 通りの起動方法があります。

### 1. uvx（PyPI 公開後）

ローカル（stdio）MCP クライアント向けの最も簡単な方法。[uv](https://docs.astral.sh/uv/) が必要です。

```bash
AISEG_URL=http://192.168.0.216 AISEG_PASSWORD=... uvx aiseg2-mcp
```

Claude Code に追加:

```bash
claude mcp add aiseg2 \
  --env AISEG_URL=http://192.168.0.216 \
  --env AISEG_PASSWORD=your-digest-password \
  -- uvx aiseg2-mcp
```

### 2. docker run（GHCR）

コンテナは既定で `streamable-http` トランスポート（常駐ネットワークサービス）になります。
必ず認証付きプロキシの背後で公開してください（[セキュリティ](#セキュリティ)参照）。

```bash
docker run --rm -p 8000:8000 \
  -e AISEG_URL=http://192.168.0.216 \
  -e AISEG_PASSWORD=your-digest-password \
  ghcr.io/chanyou0311/aiseg2-mcp:latest
```

### 3. ソースから

Python 3.12 以上と uv が必要です。

```bash
uv sync
AISEG_URL=http://192.168.0.216 AISEG_PASSWORD=... uv run aiseg2-mcp
```

### リモート（認証付き claude.ai Custom Connector）

AiSEG2 を LAN 内に置いたまま claude.ai から接続するには、[`examples/remote/`](./examples/remote/)
を参照してください（MCP + GitHub OAuth プロキシ + Cloudflare Tunnel の Docker Compose 構成）。

## 設定（環境変数）

| 変数 | 必須 | 既定値 | 説明 |
|---|---|---|---|
| `AISEG_URL` | はい | — | AiSEG2 のベース URL 例: `http://192.168.0.216`（http のみ） |
| `AISEG_PASSWORD` | はい | — | AiSEG2 Web UI の HTTP Digest パスワード |
| `AISEG_USER` | いいえ | `aiseg` | HTTP Digest ユーザー |
| `AISEG_TRANSPORT` | いいえ | `stdio` | `stdio` または `streamable-http` |
| `AISEG_HOST` | いいえ | `0.0.0.0` | バインドホスト（streamable-http のみ） |
| `AISEG_PORT` | いいえ | `8000` | バインドポート（streamable-http のみ） |
| `AISEG_DISABLE_DNS_REBINDING_PROTECTION` | いいえ | `false` | SDK の Host 許可リストを無効化。**信頼できる認証プロキシ配下でのみ** |
| `AISEG_CACHE_DIR` | いいえ | `<tempdir>/aiseg2-mcp-cache` | SD カード履歴エクスポートのキャッシュ先 |
| `AISEG_CACHE_TTL` | いいえ | `3600` | キャッシュした履歴エクスポートを再ダウンロードするまでの秒数 |
| `LOG_LEVEL` | いいえ | `info` | ログレベル |

## セキュリティ

- **LAN 内利用が前提。** AiSEG2 は平文 HTTP + Digest 認証です。AiSEG2 と本サーバーは信頼できる
  ローカルネットワーク内に置いてください。パスワードは環境変数から読み込み、ログには出力しません。
- **読み取り専用。** 設定を変更するツールは存在しません。ツール表面はテストで強制しています
  （登録ツールの許可リスト、ツール名ガード、`/action/` のソース走査、読み取り専用アノテーション検査）。
- **`streamable-http` トランスポートを認証なしで信頼できないネットワークに公開しないでください。**
  本サーバー自身は認証を持ちません。ネットワークサービスとして動かす場合は、前段に認証付きリバース
  プロキシを置いてください。`AISEG_DISABLE_DNS_REBINDING_PROTECTION=true` はそのプロキシ構成でのみ
  適切です。

## 謝辞

AiSEG2 の Web インターフェースは非公開であり、本プロジェクトは先行するリバースエンジニアリングの
知見に基づいています。

- [shimosyan/aiseg2-influxdb-forwarder](https://github.com/shimosyan/aiseg2-influxdb-forwarder) — 回路ページングの「前頁と同一で終端」判定、電力フローのフィールド。
- [hiroaki0923/aiseg2-bridge](https://github.com/hiroaki0923/aiseg2-bridge) — エンドポイントとページ構造。
- [Bugfire/aiseg_download](https://github.com/Bugfire/aiseg_download) — Digest 認証と data エンドポイントの流儀。

## ライセンス

[MIT](./LICENSE)
