"""财经新闻 + 行情 MCP Server（stdio）。

工具面覆盖 Yahoo / CNBC / RSS / 鉅亨網 / 正文抓取 / OHLCV。
不含 DuckDuckGo。
"""

from __future__ import annotations

import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.models import HistoryResponse, NewsArticle, NewsResponse
from app.news_service import (
    RateLimitError,
    describe_rss_sources,
    fetch_article_content,
    get_cnbc_news,
    get_cnyes_news,
    get_investing_history_by_yahoo,
    get_investing_history_native,
    get_market_history,
    get_rss_news,
    list_cnyes_categories,
    list_rss_categories,
    list_yahoo_investing_mappings,
    search_news,
)
from app.services.proxy_config import apply_proxy_settings, ensure_local_app_config
from app.services.rss_fetcher import _normalize_category_list

mcp = FastMCP(
    "analyst-news",
    instructions=(
        "财经新闻与行情 MCP：Yahoo / CNBC / RSS / 鉅亨網 / OHLCV。"
        "新闻优先用 search_news；行情优先用 get_market_history（Yahoo）；"
        "RSS 的 category 必须是分类名而非源名；with_content 会显著变慢。"
    ),
)


def _ok_response(*, ticker: str, tab: str, articles: list[NewsArticle]) -> dict[str, Any]:
    payload = NewsResponse(
        ticker=ticker,
        tab=tab,
        count=len(articles),
        articles=articles,
    )
    return payload.model_dump(by_alias=True)


def _ok_history(response: HistoryResponse) -> dict[str, Any]:
    data = response.model_dump(by_alias=True)
    data["ok"] = True
    return data


def _err(message: str, *, code: str = "error") -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message}


@mcp.tool(name="search_news")
def search_news_tool(
    ticker: str | None = None,
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    count: int = 20,
    tab: str = "news",
    market: str = "us",
    with_content: bool = False,
    max_articles: int = 5,
) -> dict[str, Any]:
    """统一搜索 Yahoo 财经新闻（个股 / 关键词 / 大盘头条 / 时间区间 / 美港股）。

    Args:
        ticker: 股票代码，如 AAPL、0700.HK；可空
        query: 关键词（空格分词 AND）
        since: 起始时间 YYYY-MM-DD / YYYY-MM-DD HH:MM / ISO8601
        until: 结束时间，格式同 since
        count: 返回条数 1–200
        tab: news / all / press releases
        market: us（默认）或 hk
        with_content: 是否抓取正文（变慢）
        max_articles: 启用 with_content 时最多抓正文条数 1–20
    """
    try:
        articles = search_news(
            ticker=ticker,
            query=query,
            since=since,
            until=until,
            count=count,
            tab=tab,
            market=market,
            with_content=with_content,
            max_articles=max_articles,
        )
    except ValueError as exc:
        return _err(str(exc), code="bad_request")
    except RateLimitError as exc:
        return _err(str(exc), code="rate_limited")
    except Exception as exc:  # noqa: BLE001
        return _err(f"search_news failed: {exc}", code="upstream_error")

    label = (ticker or query or market or "MARKET").upper()
    tab_label = tab
    if query:
        tab_label = f"{tab}|q={query}"
    if since or until:
        tab_label = f"{tab_label}|t={since or ''}:{until or ''}"
    return _ok_response(ticker=label, tab=tab_label, articles=articles)


@mcp.tool(name="get_cnbc_news")
def get_cnbc_news_tool(
    count: int = 20,
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    scope: str = "wide",
) -> dict[str, Any]:
    """拉取 CNBC 新闻（多栏目聚合 + 关键词/时间过滤）。

    Args:
        count: 返回条数 1–200
        query: 关键词过滤
        since: 起始时间
        until: 结束时间
        scope: wide（多栏目）或 latest（最新）
    """
    try:
        articles = get_cnbc_news(
            count=count,
            query=query,
            since=since,
            until=until,
            scope=scope,
        )
    except ValueError as exc:
        return _err(str(exc), code="bad_request")
    except RateLimitError as exc:
        return _err(str(exc), code="rate_limited")
    except Exception as exc:  # noqa: BLE001
        return _err(f"get_cnbc_news failed: {exc}", code="upstream_error")

    tab = f"cnbc|{scope}"
    if query:
        tab = f"{tab}|q={query}"
    return _ok_response(ticker="CNBC", tab=tab, articles=articles)


@mcp.tool(name="get_rss_news")
def get_rss_news_tool(
    category: str | None = None,
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    count: int = 20,
    per_source: int = 5,
) -> dict[str, Any]:
    """按 config/rss_feeds.yaml 聚合 RSS 新闻。

    Args:
        category: 分类名（如 彭博社、中国经济），可逗号分隔多个；不是源名
        query: 关键词过滤
        since: 起始时间
        until: 结束时间
        count: 返回条数 1–200
        per_source: 每个源最多取几条
    """
    try:
        categories = _normalize_category_list([category] if category else None)
        articles = get_rss_news(
            count=count,
            categories=categories,
            query=query,
            since=since,
            until=until,
            per_source_limit=per_source,
        )
    except ValueError as exc:
        return _err(str(exc), code="bad_request")
    except Exception as exc:  # noqa: BLE001
        return _err(f"get_rss_news failed: {exc}", code="upstream_error")

    tab = "rss"
    if category:
        tab = f"rss|c={category}"
    if query:
        tab = f"{tab}|q={query}"
    return _ok_response(ticker="RSS", tab=tab, articles=articles)


@mcp.tool(name="list_rss_categories")
def list_rss_categories_tool() -> dict[str, Any]:
    """列出 RSS 可用分类名（即 get_rss_news 的 category 参数取值）。"""
    try:
        categories = list_rss_categories()
        meta = describe_rss_sources()
    except Exception as exc:  # noqa: BLE001
        return _err(f"list_rss_categories failed: {exc}", code="config_error")
    return {
        "ok": True,
        "categories": categories,
        "count": len(categories),
        "config_source": meta.get("config_source"),
        "config_path": str(meta.get("config_path") or ""),
        "warning": meta.get("warning"),
    }


@mcp.tool(name="list_rss_sources")
def list_rss_sources_tool() -> dict[str, Any]:
    """列出 RSS 完整源目录（分类、源名、URL 等）。"""
    try:
        return {"ok": True, **describe_rss_sources()}
    except Exception as exc:  # noqa: BLE001
        return _err(f"list_rss_sources failed: {exc}", code="config_error")


@mcp.tool(name="get_cnyes_news")
def get_cnyes_news_tool(
    category: str = "headline",
    count: int = 20,
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    with_content: bool = False,
    max_articles: int = 5,
) -> dict[str, Any]:
    """拉取鉅亨網（cnYES）新闻。

    Args:
        category: 分类 slug，如 headline / tw_stock / wd_stock / forex / crypto
        count: 返回条数 1–200
        query: 关键词（标题/摘要，空格分词 AND）
        since: 起始时间；默认最近 2 天
        until: 结束时间
        with_content: 是否填充正文
        max_articles: with_content 时最多填充条数
    """
    try:
        articles = get_cnyes_news(
            count=count,
            category=category,
            query=query,
            since=since,
            until=until,
            with_content=with_content,
            max_articles=max_articles,
        )
    except ValueError as exc:
        return _err(str(exc), code="bad_request")
    except RateLimitError as exc:
        return _err(str(exc), code="rate_limited")
    except Exception as exc:  # noqa: BLE001
        return _err(f"get_cnyes_news failed: {exc}", code="upstream_error")

    tab = f"cnyes|c={category}"
    if query:
        tab = f"{tab}|q={query}"
    return _ok_response(ticker="CNYES", tab=tab, articles=articles)


@mcp.tool(name="list_cnyes_categories")
def list_cnyes_categories_tool() -> dict[str, Any]:
    """列出鉅亨網常用分类 slug 与中文标签。"""
    try:
        return {"ok": True, **list_cnyes_categories()}
    except Exception as exc:  # noqa: BLE001
        return _err(f"list_cnyes_categories failed: {exc}", code="error")


@mcp.tool(name="fetch_article")
def fetch_article_tool(link: str) -> dict[str, Any]:
    """抓取单篇文章正文（Yahoo CaaS → trafilatura → HTML 启发式）。

    Args:
        link: 文章 URL（http/https）
    """
    try:
        content = fetch_article_content(link)
    except RateLimitError as exc:
        return _err(str(exc), code="rate_limited")
    except ValueError as exc:
        return _err(str(exc), code="bad_request")
    except Exception as exc:  # noqa: BLE001
        return _err(f"fetch_article failed: {exc}", code="upstream_error")

    text = content or ""
    return {
        "ok": True,
        "link": link,
        "content": content,
        "length": len(text),
    }


@mcp.tool(name="get_market_history")
def get_market_history_tool(
    symbol: str,
    period: str | None = None,
    start: str | None = None,
    end: str | None = None,
    interval: str = "1d",
) -> dict[str, Any]:
    """拉取历史行情 OHLCV（Yahoo 正规报价符号，推荐）。

    Args:
        symbol: Yahoo 符号，如 AAPL / BTC-USD / 0700.HK
        period: 相对跨度 1d/5d/1mo/3mo/6mo/1y/2y/5y/10y/ytd/max；与 start/end 同时传时以日期为准
        start: 起始日期 YYYY-MM-DD
        end: 结束日期 YYYY-MM-DD
        interval: K 线周期，默认 1d（也支持 1h/1wk 等）
    """
    try:
        result = get_market_history(
            symbol,
            period=period,
            start=start,
            end=end,
            interval=interval,
        )
    except ValueError as exc:
        return _err(str(exc), code="bad_request")
    except RateLimitError as exc:
        return _err(str(exc), code="rate_limited")
    except Exception as exc:  # noqa: BLE001
        return _err(f"get_market_history failed: {exc}", code="upstream_error")
    return _ok_history(result)


@mcp.tool(name="get_investing_history")
def get_investing_history_tool(
    symbol: str,
    period: str | None = None,
    start: str | None = None,
    end: str | None = None,
    interval: str = "1d",
) -> dict[str, Any]:
    """拉取历史行情 OHLCV（Yahoo 符号 → Investing.com 映射表）。

    需在 config/investing_symbol_map.yaml 中有对应映射。
    Investing 线上接口可能因风控失败，优先用 get_market_history。

    Args:
        symbol: Yahoo 符号，如 AAPL
        period: 相对跨度
        start: 起始日期 YYYY-MM-DD
        end: 结束日期 YYYY-MM-DD
        interval: 仅支持 1d / 1wk / 1mo
    """
    try:
        result = get_investing_history_by_yahoo(
            symbol,
            period=period,
            start=start,
            end=end,
            interval=interval,
        )
    except ValueError as exc:
        return _err(str(exc), code="bad_request")
    except RateLimitError as exc:
        return _err(str(exc), code="rate_limited")
    except Exception as exc:  # noqa: BLE001
        return _err(f"get_investing_history failed: {exc}", code="upstream_error")
    return _ok_history(result)


@mcp.tool(name="get_investing_history_native")
def get_investing_history_native_tool(
    product: str,
    name: str,
    country: str | None = None,
    period: str | None = None,
    start: str | None = None,
    end: str | None = None,
    interval: str = "1d",
    stock_exchange: str | None = None,
) -> dict[str, Any]:
    """拉取历史行情 OHLCV（Investing 原生参数）。

    Args:
        product: stocks / indices / cryptos / commodities / bonds / etfs / funds / currency_crosses
        name: Investing 名称或代码，如 AAPL / Bitcoin / Gold
        country: 国家（小写），股票/指数等需要，如 united states
        period: 相对跨度
        start: 起始日期 YYYY-MM-DD
        end: 结束日期 YYYY-MM-DD
        interval: 1d / 1wk / 1mo
        stock_exchange: ETF 可选交易所过滤
    """
    try:
        result = get_investing_history_native(
            product=product,
            name=name,
            country=country,
            period=period,
            start=start,
            end=end,
            interval=interval,
            stock_exchange=stock_exchange,
        )
    except ValueError as exc:
        return _err(str(exc), code="bad_request")
    except RateLimitError as exc:
        return _err(str(exc), code="rate_limited")
    except Exception as exc:  # noqa: BLE001
        return _err(f"get_investing_history_native failed: {exc}", code="upstream_error")
    return _ok_history(result)


@mcp.tool(name="list_investing_mappings")
def list_investing_mappings_tool() -> dict[str, Any]:
    """列出 Yahoo → Investing 符号映射表（排查 / 查看可用标的）。"""
    try:
        items = list_yahoo_investing_mappings()
    except Exception as exc:  # noqa: BLE001
        return _err(f"list_investing_mappings failed: {exc}", code="config_error")
    return {"ok": True, "count": len(items), "items": items}


def main() -> None:
    ensure_local_app_config()
    proxy_status = apply_proxy_settings()
    # stdio 模式下禁止往 stdout 打日志，否则会破坏 MCP 协议
    print(f"[news-mcp] {proxy_status}", file=sys.stderr)
    print("[news-mcp] transport=stdio", file=sys.stderr)
    print(
        "[news-mcp] tools: search_news, get_cnbc_news, get_rss_news, "
        "list_rss_categories, list_rss_sources, get_cnyes_news, "
        "list_cnyes_categories, fetch_article, get_market_history, "
        "get_investing_history, get_investing_history_native, list_investing_mappings",
        file=sys.stderr,
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
