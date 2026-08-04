"""鉅亨網（cnYES / Anue）新闻 Provider。

列表走官方 JSON API（非 RSS）；可选抓正文。
只负责查询并映射为 NewsArticle，不落库。
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from app.models import NewsArticle
from app.services.article_content import fetch_article_content
from app.services.common import CHINA_TZ, RateLimitError, dedup_articles, text_or_none
from app.services.news_fetcher import _parse_cnbc_filter_datetime

_SUMMARY_MAX_CHARS = 280

# 常用分类（可传任意 category slug；未知分类由上游返回错误）
CNYES_CATEGORIES: dict[str, str] = {
    "headline": "焦点要闻",
    "tw_stock": "台股",
    "tw_stock_news": "台股新闻",
    "tw_money": "台股理财",
    "wd_stock": "国际股市",
    "wd_money": "国际理财",
    "forex": "外汇",
    "future": "期货",
    "crypto": "加密货币",
    "fund": "基金",
    "cnyeshouse": "房市",
}

_LIST_URL = (
    "https://api.cnyes.com/media/api/v1/newslist/category/{category}"
    "?startAt={start_at}&endAt={end_at}&limit={limit}&page={page}"
)
_ARTICLE_URL = "https://news.cnyes.com/news/id/{news_id}"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def list_cnyes_categories() -> dict[str, Any]:
    return {
        "source": "cnyes",
        "publisher": "鉅亨網",
        "categories": [
            {"id": key, "label": label} for key, label in CNYES_CATEGORIES.items()
        ],
        "note": "category 也可传上游其它 slug；默认 headline（焦点，相对少台股碎闻）",
    }


def _strip_html(text: str | None) -> str | None:
    cleaned = text_or_none(text)
    if not cleaned:
        return None
    # 上游常把 HTML 实体编码后塞进 JSON（&lt;p&gt;...），先还原再去标签
    cleaned = html.unescape(cleaned)
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    return cleaned or None


def _short_summary(*, summary_raw: Any, content_raw: Any) -> str | None:
    """优先用官方 summary；没有则截取正文前一段，避免把整篇 HTML 塞进 summary。"""
    summary = _strip_html(text_or_none(summary_raw))
    if summary:
        return summary[:_SUMMARY_MAX_CHARS] + ("…" if len(summary) > _SUMMARY_MAX_CHARS else "")

    body = _strip_html(text_or_none(content_raw))
    if not body:
        return None
    if len(body) <= _SUMMARY_MAX_CHARS:
        return body
    return body[:_SUMMARY_MAX_CHARS].rstrip() + "…"


def _collect_tags(item: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()

    def _add(value: str | None) -> None:
        text = text_or_none(value)
        if not text:
            return
        key = text.upper()
        if key in seen:
            return
        seen.add(key)
        tags.append(text)

    _add(item.get("categoryName") if isinstance(item.get("categoryName"), str) else None)

    stock = item.get("stock")
    if isinstance(stock, list):
        for s in stock:
            if isinstance(s, str):
                # "US-AAPL" -> AAPL
                code = s.split("-")[-1] if "-" in s else s
                _add(code)

    markets = item.get("market")
    if isinstance(markets, list):
        for m in markets:
            if isinstance(m, dict):
                _add(text_or_none(m.get("code")))
            elif isinstance(m, str):
                _add(m)
    return tags


def _slim_raw_item(item: dict[str, Any]) -> dict[str, Any]:
    """raw.item 去掉超长 HTML content，避免与 summary/content 重复占响应。"""
    slim = dict(item)
    slim.pop("content", None)
    return slim


def _http_get_json(url: str, *, timeout: int = 25) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Origin": "https://news.cnyes.com",
            "Referer": "https://news.cnyes.com/",
            "x-cnyes-app": "fe-desktop",
            "x-platform": "WEB",
            "x-system-kind": "NEWS_DESKTOP",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in {429, 503}:
            raise RateLimitError(f"鉅亨網限流或暂不可用 HTTP {exc.code}") from exc
        raise RuntimeError(f"鉅亨網 HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"鉅亨網网络错误: {exc}") from exc

    if status != 200:
        raise RuntimeError(f"鉅亨網 HTTP {status}: {url}")

    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("鉅亨網返回非 JSON") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("鉅亨網 JSON 结构异常")
    return payload


def _to_china_iso_from_ts(publish_at: Any) -> str | None:
    try:
        ts = int(publish_at)
    except (TypeError, ValueError):
        return None
    if ts > 10_000_000_000:
        ts = ts // 1000
    dt = datetime.fromtimestamp(ts, tz=CHINA_TZ)
    return dt.isoformat()


def _article_datetime(article: NewsArticle) -> datetime | None:
    text = text_or_none(article.pub_date)
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CHINA_TZ)
    return dt.astimezone(CHINA_TZ)


def _map_cnyes_item(item: dict[str, Any], *, category: str) -> NewsArticle:
    news_id = item.get("newsId") or item.get("id") or item.get("news_id")
    news_id_str = str(news_id).strip() if news_id is not None else None
    title = text_or_none(item.get("title") or item.get("headline"))
    summary = _short_summary(summary_raw=item.get("summary"), content_raw=item.get("content"))
    # 列表接口常带全文 HTML；先清洗暂存，是否暴露给调用方由 with_content 决定
    body = _strip_html(text_or_none(item.get("content")))
    link = text_or_none(item.get("link") or item.get("url"))
    if not link and news_id_str:
        link = _ARTICLE_URL.format(news_id=news_id_str)

    pub_date = _to_china_iso_from_ts(item.get("publishAt") or item.get("publishedAt"))
    if not pub_date:
        pub_date = text_or_none(item.get("publishAt") or item.get("publishedAt"))

    tags = _collect_tags(item)
    article_id = f"cnyes_{news_id_str}" if news_id_str else (link or title)

    return NewsArticle(
        id=article_id,
        title=title,
        summary=summary,
        publisher="鉅亨網",
        link=link,
        pubDate=pub_date,
        type="STORY",
        # 暂存列表正文；get_cnyes_news 会在 with_content=false 时清掉
        content=body,
        raw={
            "source": "cnyes",
            "category": category,
            "newsId": news_id_str,
            "tags": tags,
            "item": _slim_raw_item(item),
        },
    )


def _match_query(article: NewsArticle, query: str | None) -> bool:
    q = text_or_none(query)
    if not q:
        return True
    tokens = [t for t in re.split(r"\s+", q.lower()) if t]
    if not tokens:
        return True
    hay = " ".join(
        filter(
            None,
            [
                (article.title or "").lower(),
                (article.summary or "").lower(),
                (article.publisher or "").lower(),
            ],
        )
    )
    return all(token in hay for token in tokens)


def get_cnyes_news(
    count: int = 20,
    *,
    category: str = "headline",
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    with_content: bool = False,
    max_articles: int = 5,
    page_size: int = 30,
) -> list[NewsArticle]:
    """
    拉取鉅亨网新闻列表并映射为 NewsArticle。

    category: 上游分类 slug，默认 headline
    since/until: 与 CNBC/RSS 相同时间格式；不传则默认最近 2 天
    """
    if count < 1 or count > 200:
        raise ValueError("count must be between 1 and 200")
    if page_size < 1 or page_size > 50:
        raise ValueError("page_size must be between 1 and 50")
    if max_articles < 1 or max_articles > 20:
        raise ValueError("max_articles must be between 1 and 20")

    category_key = (category or "headline").strip()
    if not category_key:
        raise ValueError("category cannot be empty")

    since_dt = _parse_cnbc_filter_datetime(since)
    until_dt = _parse_cnbc_filter_datetime(until, end_of_day=True)
    now = datetime.now(CHINA_TZ)
    if until_dt is None:
        until_dt = now
    if since_dt is None:
        since_dt = until_dt - timedelta(days=2)
    if since_dt > until_dt:
        raise ValueError("since must be <= until")

    start_at = int(since_dt.timestamp())
    end_at = int(until_dt.timestamp())

    collected: list[NewsArticle] = []
    page = 1
    last_page = 1
    fetch_budget = max(count * 3, count)

    while page <= last_page and len(collected) < fetch_budget:
        url = _LIST_URL.format(
            category=urllib.parse.quote(category_key, safe=""),
            start_at=start_at,
            end_at=end_at,
            limit=page_size,
            page=page,
        )
        payload = _http_get_json(url)
        status_code = payload.get("statusCode")
        if status_code not in (None, 200, "200"):
            message = payload.get("message") or "unknown error"
            raise RuntimeError(f"鉅亨網业务错误: {message}")

        items_block = payload.get("items") or {}
        if not isinstance(items_block, dict):
            break
        try:
            last_page = int(items_block.get("last_page") or page)
        except (TypeError, ValueError):
            last_page = page

        rows = items_block.get("data") or []
        if not isinstance(rows, list) or not rows:
            break

        for row in rows:
            if not isinstance(row, dict):
                continue
            article = _map_cnyes_item(row, category=category_key)
            if not _match_query(article, query):
                continue
            pub_dt = _article_datetime(article)
            if pub_dt is not None:
                if pub_dt < since_dt or pub_dt > until_dt:
                    continue
            collected.append(article)
            if len(collected) >= fetch_budget:
                break

        page += 1
        if page <= last_page and len(collected) < fetch_budget:
            time.sleep(0.35)

    articles = dedup_articles(collected)
    articles.sort(
        key=lambda a: _article_datetime(a) or datetime.min.replace(tzinfo=CHINA_TZ),
        reverse=True,
    )
    articles = articles[:count]

    if with_content and articles:
        for idx, article in enumerate(articles):
            if idx >= max_articles:
                article.content = None
                continue
            # 列表已有正文则直接用，避免再抓一次页面造成“内容重复 / 变慢”
            if text_or_none(article.content):
                continue
            link = text_or_none(article.link)
            if not link:
                continue
            try:
                content = fetch_article_content(link)
            except Exception:  # noqa: BLE001
                content = None
            article.content = content
    else:
        for article in articles:
            article.content = None

    return articles
