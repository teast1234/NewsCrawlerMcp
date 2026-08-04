import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import yfinance as yf
import ycnbc
from yfinance.exceptions import YFRateLimitError

from app.models import NewsArticle
from app.services.common import (
    CHINA_TZ,
    DEFAULT_MARKET_QUERIES,
    RETRY_DELAYS_SECONDS,
    TAB_QUERY_REFS,
    VALID_TABS,
    YAHOO_NEWS_URL,
    YAHOO_SEARCH_URL,
    RateLimitError,
    extract_url,
    resolve_market_region,
    text_or_none,
)

_CNBCCONTENT_TYPE = "STORY"
_RELATIVE_TIME_RE = re.compile(
    r"^(?:(\d+)|(a|an))\s+(sec|secs|second|seconds|min|mins|minute|minutes|hr|hrs|hour|hours|day|days)\s+ago$",
    re.I,
)
_CNBC_SCOPE_METHODS: dict[str, tuple[str, ...]] = {
    "latest": ("latest",),
    "wide": (
        "latest",
        "trending",
        "technology",
        "business",
        "finance",
        "world_economy",
        "politics",
        "economy",
        "energy",
    ),
}


def _map_article(item: dict) -> NewsArticle:
    content = item.get("content") or {}
    provider = content.get("provider") or {}
    link = (
        extract_url(content.get("canonicalUrl"))
        or extract_url(content.get("clickThroughUrl"))
        or text_or_none(content.get("url"))
    )
    return NewsArticle(
        id=text_or_none(item.get("id")),
        title=text_or_none(content.get("title")),
        summary=text_or_none(content.get("summary")) or text_or_none(content.get("description")),
        publisher=text_or_none(provider.get("displayName")) or text_or_none(content.get("publisher")),
        link=link,
        pubDate=text_or_none(content.get("pubDate")),
        type=text_or_none(content.get("contentType")),
        raw=item,
    )


def _map_search_news_item(item: dict) -> NewsArticle:
    pub_ts = item.get("providerPublishTime")
    pub_date: str | None = None
    if isinstance(pub_ts, (int, float)):
        from datetime import datetime, timezone

        pub_date = datetime.fromtimestamp(pub_ts, tz=timezone.utc).isoformat()
    return NewsArticle(
        id=text_or_none(item.get("uuid")),
        title=text_or_none(item.get("title")),
        summary=text_or_none(item.get("summary")) or text_or_none(item.get("publisher")),
        publisher=text_or_none(item.get("publisher")),
        link=text_or_none(item.get("link")),
        pubDate=pub_date,
        type=text_or_none(item.get("type")),
        raw=item,
    )


def _fetch_via_yfinance(symbol: str, count: int, tab: str) -> list[NewsArticle]:
    news = yf.Ticker(symbol).get_news(count=count, tab=tab)
    return [_map_article(item) for item in news if not item.get("ad")]


def _fetch_via_yahoo_api(symbol: str, count: int, tab: str) -> list[NewsArticle]:
    query_ref = TAB_QUERY_REFS[tab]
    payload = json.dumps({"serviceConfig": {"snippetCount": count, "s": [symbol]}}).encode("utf-8")
    request = urllib.request.Request(
        YAHOO_NEWS_URL.format(query_ref=query_ref),
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimitError("Yahoo Finance 请求过于频繁，已被限流。请等待 5-10 分钟后再试。") from exc
        raise RuntimeError(f"Yahoo API HTTP {exc.code}") from exc

    if "Will be right back" in body:
        raise RuntimeError("Yahoo Finance is temporarily unavailable")

    data = json.loads(body)
    stream = data.get("data", {}).get("tickerStream", {}).get("stream", [])
    return [_map_article(item) for item in stream if not item.get("ad")]


def _fetch_via_yfinance_search(query: str, count: int) -> list[NewsArticle]:
    search = yf.Search(query, news_count=count, include_research=False)
    news = search.news or []
    return [_map_search_news_item(item) for item in news if not item.get("ad")]


def _fetch_via_yahoo_search_api(query: str, count: int, *, region: str, lang: str) -> list[NewsArticle]:
    url = YAHOO_SEARCH_URL.format(
        query=urllib.parse.quote_plus(query),
        count=count,
        region=urllib.parse.quote_plus(region),
        lang=urllib.parse.quote_plus(lang),
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimitError("Yahoo Finance 请求过于频繁，已被限流。请等待 5-10 分钟后再试。") from exc
        raise RuntimeError(f"Yahoo Search HTTP {exc.code}") from exc

    if "Will be right back" in body:
        raise RuntimeError("Yahoo Finance is temporarily unavailable")

    data = json.loads(body)
    news_items = data.get("news") or []
    return [_map_search_news_item(item) for item in news_items if not item.get("ad")]


def _normalize_cnbc_pub_date(value: str | None) -> str | None:
    text = text_or_none(value)
    if not text:
        return None

    lowered = text.casefold()
    if lowered in {"moments ago", "just now", "now"}:
        return datetime.now(CHINA_TZ).isoformat()

    matched = _RELATIVE_TIME_RE.match(text)
    if matched:
        amount = int(matched.group(1)) if matched.group(1) else 1
        unit = matched.group(3).lower()
        if unit.startswith("sec"):
            delta = timedelta(seconds=amount)
        elif unit.startswith("min"):
            delta = timedelta(minutes=amount)
        elif unit.startswith("hour"):
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(days=amount)
        dt = datetime.now(CHINA_TZ) - delta
        return dt.isoformat()

    normalized_text = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", text, flags=re.I)

    try:
        dt = parsedate_to_datetime(normalized_text)
    except (TypeError, ValueError):
        dt = None

    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CHINA_TZ).isoformat()

    for fmt in ("%a, %b %d %Y", "%b %d %Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(normalized_text, fmt).replace(tzinfo=CHINA_TZ)
            return dt.isoformat()
        except ValueError:
            continue

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CHINA_TZ).isoformat()


def _parse_cnbc_filter_datetime(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    text = text_or_none(value)
    if not text:
        return None

    parsed_by_date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text))
    iso = text.replace("Z", "+00:00")

    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"无法解析时间 '{value}'，请使用 ISO8601 或 YYYY-MM-DD[ HH:MM[:SS]] 格式")

    if end_of_day and parsed_by_date_only:
        dt = dt + timedelta(days=1) - timedelta(microseconds=1)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CHINA_TZ)
    return dt.astimezone(CHINA_TZ)


def _article_cnbc_datetime(article: NewsArticle) -> datetime | None:
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


def _map_ycnbc_news_item(item: dict) -> NewsArticle:
    if not isinstance(item, dict):
        return NewsArticle(raw={"source": "ycnbc", "payload": item})

    title = text_or_none(item.get("headline") or item.get("title"))
    link = text_or_none(item.get("link") or item.get("url"))
    summary = text_or_none(item.get("summary") or item.get("description") or item.get("tag"))
    publisher = text_or_none(item.get("source") or item.get("publisher")) or "CNBC"
    raw_pub_date = text_or_none(item.get("published_at") or item.get("publishedAt") or item.get("time"))
    pub_date = _normalize_cnbc_pub_date(raw_pub_date)
    article_id = text_or_none(item.get("id")) or link or title

    return NewsArticle(
        id=article_id,
        title=title,
        summary=summary,
        publisher=publisher,
        link=link,
        pubDate=pub_date,
        type=_CNBCCONTENT_TYPE,
        raw={"source": "ycnbc", "raw_time": raw_pub_date, **item},
    )


def get_cnbc_news(
    count: int = 20,
    query: str | None = None,
    scope: str = "wide",
    since: str | None = None,
    until: str | None = None,
) -> list[NewsArticle]:
    if count < 1 or count > 200:
        raise ValueError("count must be between 1 and 200")

    normalized_scope = (scope or "wide").strip().lower()
    if normalized_scope not in _CNBC_SCOPE_METHODS:
        allowed_scopes = ", ".join(sorted(_CNBC_SCOPE_METHODS))
        raise ValueError(f"Invalid scope '{scope}'. Choose from: {allowed_scopes}")

    since_dt = _parse_cnbc_filter_datetime(since)
    until_dt = _parse_cnbc_filter_datetime(until, end_of_day=True)
    if since_dt and until_dt and since_dt > until_dt:
        raise ValueError("since 不能晚于 until")

    news_client = ycnbc.News()
    items: list[dict] = []

    for method_name in _CNBC_SCOPE_METHODS[normalized_scope]:
        fetcher = getattr(news_client, method_name, None)
        if not callable(fetcher):
            continue
        try:
            batch = fetcher()
        except Exception:
            continue
        if isinstance(batch, list):
            items.extend(batch)

    query_tokens = [token.casefold() for token in query.split() if token] if query else []
    deduped: dict[str, NewsArticle] = {}

    for item in items:
        article = _map_ycnbc_news_item(item)
        if not article.link and not article.title:
            continue

        article_dt = _article_cnbc_datetime(article)
        if since_dt or until_dt:
            if article_dt is None:
                continue
            if since_dt and article_dt < since_dt:
                continue
            if until_dt and article_dt > until_dt:
                continue

        if query_tokens:
            haystack = f"{article.title or ''} {article.summary or ''} {article.publisher or ''}".casefold()
            if not all(token in haystack for token in query_tokens):
                continue
        dedupe_key = article.link or article.id or article.title
        if not dedupe_key:
            continue
        if dedupe_key in deduped:
            continue
        deduped[dedupe_key] = article

    articles = list(deduped.values())
    articles.sort(key=lambda article: article.pub_date or "", reverse=True)
    return articles[:count]


def get_news(ticker: str, count: int = 10, tab: str = "news") -> list[NewsArticle]:
    normalized_tab = tab.lower()
    if normalized_tab not in VALID_TABS:
        raise ValueError(f"Invalid tab '{tab}'. Choose from: {', '.join(sorted(VALID_TABS))}")

    symbol = ticker.strip().upper()
    if not symbol:
        raise ValueError("ticker cannot be empty")
    yfinance_rate_limited = False
    for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
        if attempt > 0:
            time.sleep(RETRY_DELAYS_SECONDS[attempt - 1])
        try:
            return _fetch_via_yfinance(symbol, count, normalized_tab)
        except YFRateLimitError:
            yfinance_rate_limited = True

    if yfinance_rate_limited:
        try:
            return _fetch_via_yahoo_api(symbol, count, normalized_tab)
        except RateLimitError:
            raise
        except Exception as exc:
            raise RateLimitError(
                "Yahoo Finance 请求过于频繁，已被限流。请等待 5-10 分钟后再试，并避免在 Swagger 中连续点击 Execute。"
            ) from exc

    raise RateLimitError("Yahoo Finance 请求过于频繁，已被限流。请等待 5-10 分钟后再试。")


def get_market_news(count: int = 20, query: str | None = None, market: str = "us") -> list[NewsArticle]:
    if count < 1 or count > 200:
        raise ValueError("count must be between 1 and 200")

    _, region, lang = resolve_market_region(market)

    queries: tuple[str, ...] | list[str]
    if query:
        queries = [query]
    else:
        queries = DEFAULT_MARKET_QUERIES

    aggregated: dict[str, NewsArticle] = {}
    last_error: Exception | None = None
    yfinance_rate_limited = False
    per_query = max(5, count // max(1, len(queries)))

    for q in queries:
        articles: list[NewsArticle] | None = None
        for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
            if attempt > 0:
                time.sleep(RETRY_DELAYS_SECONDS[attempt - 1])
            try:
                articles = _fetch_via_yfinance_search(q, per_query)
                break
            except YFRateLimitError:
                yfinance_rate_limited = True
            except AttributeError as exc:
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                break

        if articles is None:
            try:
                articles = _fetch_via_yahoo_search_api(q, per_query, region=region, lang=lang)
            except RateLimitError:
                raise
            except Exception as exc:
                last_error = exc
                continue

        for article in articles:
            key = article.id or article.link or article.title
            if key and key not in aggregated:
                aggregated[key] = article
                if len(aggregated) >= count:
                    break

        if len(aggregated) >= count:
            break

    if not aggregated:
        if last_error is not None:
            raise RuntimeError(f"Failed to fetch market news: {last_error}") from last_error
        if yfinance_rate_limited:
            raise RateLimitError("Yahoo Finance 请求过于频繁，已被限流。请等待 5-10 分钟后再试。")
        return []

    results = list(aggregated.values())
    results.sort(key=lambda article: article.pub_date or "", reverse=True)
    return results[:count]
