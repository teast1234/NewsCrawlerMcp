# 📰 news-crawler-mcp — 财经新闻与行情 MCP 服务

**语言 / Languages:** **简体中文** | [English](./README.en.md)

把 Yahoo Finance、CNBC、可配置多路 RSS、鉅亨網（cnYES）的新闻查询，以及 Yahoo / Investing 历史行情（OHLCV），封装为标准 **MCP（Model Context Protocol）** 工具，供 Cursor、Claude Desktop、Java Spring AI 等客户端直接调用。

**本项目是 MCP 工具服务**：不含独立 HTTP API、不含 DuckDuckGo 全网搜索。同源 HTTP 版见 [analyst-python](https://gitee.com/shenzhen-minglue/analyst-python)。

> ⭐ **如果这个项目帮到你，欢迎去仓库点个 Star 支持一下～**  
> 👉 [https://gitee.com/gdouage/news-crawler-mcp](https://gitee.com/gdouage/news-crawler-mcp)  
> 你的一颗星，是个人开发者继续更新的最大动力 ❤️

| | |
|---|---|
| 仓库 | [gitee.com/gdouage/news-crawler-mcp](https://gitee.com/gdouage/news-crawler-mcp) |
| 协议 | [MIT](./LICENSE) |
| 作者 | abbuibuibui |
| 联系 | [3244940576@qq.com](mailto:3244940576@qq.com) |

---

## 📌 能做什么

| 能力 | 说明 |
|------|------|
| Yahoo 统一搜索 | 个股 / 关键词 / 大盘头条 / 时间区间 / 美股·港股 |
| CNBC | 多栏目聚合 + 关键词 / 时间过滤 |
| RSS | 多源并发抓取，按分类筛选 |
| 鉅亨網 | 按分类与关键词查询（默认最近 2 天） |
| 正文抓取 | 按链接抽取文章正文 |
| 历史行情 | Yahoo OHLCV（推荐）；Investing.com 第二数据源（映射表 / 原生参数） |
| 接入方式 | **stdio MCP**（Cursor / Claude / Spring AI） |

**使用注意：**

- 数据来自公开接口，不保证历史完整性；Yahoo 侧以近期为主。
- 请求过快可能触发限流（`rate_limited` / 429），建议适当间隔。
- 开启 `with_content` 或调用 `fetch_article` 会明显变慢。
- RSS 的 `category` 填**分类名**（如 `中国经济`），不要填源名（如 `东方财富`）。
- 鉅亨網的 `category` 填英文 slug（如 `headline`），可用 `list_cnyes_categories` 查看。
- Investing 线上接口可能受风控影响；日常行情优先用 `get_market_history`（Yahoo）。

---

## 🚀 快速开始

### 1. 环境要求

- Python **3.10+**
- 能访问外网（Yahoo / CNBC / RSS / 鉅亨網）
- 访问受限时可配置本地代理（见下方）

### 2. 安装

```bash
git clone https://gitee.com/gdouage/news-crawler-mcp.git
cd news-crawler-mcp

python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
```

### 3. 配置代理（可选）

若本机需要代理才能访问外网，请先复制配置样本再修改：

```bash
# Windows
copy config\app.example.yaml config\app.yaml

# macOS / Linux
# cp config/app.example.yaml config/app.yaml
```

编辑 `config/app.yaml`：

```yaml
proxy:
  enabled: true
  host: 127.0.0.1
  port: 8800    # QuickQ 常见 8800；Clash 常见 7890
```

不需要代理时保持 `enabled: false` 即可。首次启动若还没有 `app.yaml`，程序会自动从 `app.example.yaml` 生成一份。

> `app.yaml` 属于本机配置（可能含代理等信息），仓库只提供 `app.example.yaml` 样本。

### 4. 自检（推荐）

```bash
python scripts/verify_mcp.py
```

看到 `[verify] OK` 即表示 MCP 握手与基础工具可用。

### 5. 启动

一般由 Cursor / Claude / Spring AI **自动拉起**，你不必长期手动跑。需要单独调试时：

```bash
python -m app
```

进程会阻塞等待客户端连接；诊断信息在 stderr。

---

## 🔌 接入客户端

把下面路径换成你本机的项目目录与 Python 路径。

### Cursor

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

未使用虚拟环境时，可将 `command` 改为 `python`（需保证该 `python` 已安装本项目依赖）。

配置后重启 Cursor 或刷新 MCP，即可在对话里调用工具。

### Claude Desktop

编辑 `claude_desktop_config.json`，`mcpServers` 写法与上方相同。

### Java Spring AI

以 **stdio 子进程**方式启动：

```text
command: <venv>\python.exe
args:    -m app
cwd:     <项目根目录>
```

由 Java 管理进程生命周期即可。

---

## 🧰 工具说明

列表类工具返回结构示例：

```json
{
  "ticker": "AAPL",
  "tab": "news",
  "count": 5,
  "articles": [
    {
      "id": "...",
      "title": "...",
      "summary": "...",
      "publisher": "...",
      "link": "...",
      "pubDate": "2026-08-04T10:00:00+08:00",
      "type": "STORY",
      "content": null
    }
  ]
}
```

失败时示例：

```json
{
  "ok": false,
  "error": "rate_limited",
  "message": "..."
}
```

| 工具 | 作用 | 常用参数 |
|------|------|----------|
| `search_news` | Yahoo 统一搜索 | `ticker`, `query`, `since`, `until`, `count`, `tab`, `market`, `with_content` |
| `get_cnbc_news` | CNBC 新闻 | `count`, `query`, `since`, `until`, `scope`（`wide` / `latest`） |
| `get_rss_news` | RSS 聚合 | `category`, `query`, `since`, `until`, `count`, `per_source` |
| `list_rss_categories` | 查看 RSS 分类 | — |
| `list_rss_sources` | 查看 RSS 源目录 | — |
| `get_cnyes_news` | 鉅亨網新闻 | `category`, `count`, `query`, `since`, `until`, `with_content` |
| `list_cnyes_categories` | 查看鉅亨網分类 | — |
| `fetch_article` | 抓取单篇正文 | `link` |
| `get_market_history` | Yahoo 历史行情 OHLCV（推荐） | `symbol`, `period`, `start`, `end`, `interval` |
| `get_investing_history` | Investing 行情（经 Yahoo 映射） | `symbol`, `period`, `start`, `end`, `interval` |
| `get_investing_history_native` | Investing 原生参数行情 | `product`, `name`, `country`, `period`… |
| `list_investing_mappings` | 查看 Yahoo→Investing 映射 | — |

### 调用示例

```text
search_news(ticker="AAPL", count=5)
search_news(query="AI chip", market="us", count=10)
get_rss_news(category="彭博社", count=10)
get_rss_news(category="中国经济,美国经济", query="美联储", count=20)
get_cnyes_news(category="headline", query="科技巨", count=10)
list_rss_categories()
fetch_article(link="https://finance.yahoo.com/...")
get_market_history(symbol="AAPL", period="1mo", interval="1d")
get_market_history(symbol="BTC-USD", start="2024-01-01", end="2024-06-30")
list_investing_mappings()
```

---

## ⚙️ 配置文件

| 文件 | 用途 |
|------|------|
| `config/app.example.yaml` | 代理配置样本；复制为 `app.yaml` 后按本机修改 |
| `config/rss_feeds.yaml` | RSS 分类与订阅源；分类名即 `get_rss_news` 的 `category` |
| `config/market_queries.json` | Yahoo 大盘头条关键词池 |
| `config/investing_symbol_map.yaml` | Yahoo → Investing 符号映射（第二行情源） |

不确定 RSS / 鉅亨網有哪些分类时，直接调用 `list_rss_categories`、`list_cnyes_categories`。

---

## ❓ 常见问题

**Cursor 里看不到工具？**  
检查 MCP 配置里的 `command`、`cwd` 是否指向本项目，且该 Python 已执行过 `pip install -r requirements.txt`。

**调用很慢？**  
多数是上游网站或代理延迟。尽量不要默认开 `with_content`；RSS 可缩小 `category` / `per_source`。

**返回限流错误？**  
降低调用频率，稍后再试。

**Investing 行情失败？**  
可能被网站风控拦截，请改用 `get_market_history`（Yahoo）。

**和 analyst-python 有什么区别？**  
那边是 HTTP API；这边是给 AI 客户端用的 MCP 工具。数据源能力相近，可按场景选用。

---

## 📜 License

[MIT](./LICENSE) © abbuibuibui
