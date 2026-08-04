# 📰 news-crawler-mcp — Financial News & Market MCP Server

**Languages:** [简体中文](./README.md) | **English**

Expose Yahoo Finance, CNBC, configurable RSS feeds, cnYES news, and Yahoo/Investing OHLCV history as **MCP tools** for Cursor, Claude Desktop, and Java Spring AI.

MCP tools only — no standalone HTTP API, no DuckDuckGo. Related HTTP project: [analyst-python](https://gitee.com/shenzhen-minglue/analyst-python).

| | |
|---|---|
| Repository | [gitee.com/gdouage/news-crawler-mcp](https://gitee.com/gdouage/news-crawler-mcp) |
| License | [MIT](./LICENSE) |
| Author | abbuibuibui |
| Contact | [3244940576@qq.com](mailto:3244940576@qq.com) |

## Quick start

```bash
git clone https://gitee.com/gdouage/news-crawler-mcp.git
cd news-crawler-mcp
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt

# Optional proxy (copy sample, then edit locally)
cp config/app.example.yaml config/app.yaml
# edit config/app.yaml → proxy.enabled / host / port

python scripts/verify_mcp.py   # smoke test
# Usually started by your MCP client; for manual debug:
python -m app
```

> `config/app.yaml` is local-only. The repo ships `config/app.example.yaml` as the public sample.

## Cursor

```json
{
  "mcpServers": {
    "news-crawler": {
      "command": "D:\\path\\to\\news-crawler-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "app"],
      "cwd": "D:\\path\\to\\news-crawler-mcp"
    }
  }
}
```

## Tools

| Tool | Purpose |
|------|---------|
| `search_news` | Yahoo unified search |
| `get_cnbc_news` | CNBC |
| `get_rss_news` / `list_rss_categories` / `list_rss_sources` | RSS |
| `get_cnyes_news` / `list_cnyes_categories` | cnYES |
| `fetch_article` | Article body by URL |
| `get_market_history` | Yahoo OHLCV (recommended) |
| `get_investing_history` / `get_investing_history_native` / `list_investing_mappings` | Investing.com OHLCV |

RSS `category` must be a **category name** from the config (e.g. `中国经济`), not a source name. cnYES `category` uses English slugs (e.g. `headline`). Prefer Yahoo for market history if Investing is blocked.

See the Chinese README for fuller details.
