import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from email.utils import parsedate_to_datetime

from app.models import NewsArticle
from app.services.article_content import fetch_article_content
from app.services.common import (
    CHINA_TZ,
    DEFAULT_MARKET_QUERIES,
    MARKET_TIME_RANGE_QUERIES,
    RateLimitError,
    dedup_articles,
)
from app.services.news_fetcher import get_market_news, get_news

def _parse_datetime(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if value is None:
        return None
    text = value.strip()
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
    return dt.astimezone(timezone.utc)


def _article_datetime(article: NewsArticle) -> datetime | None:
    if not article.pub_date:
        return None

    text = article.pub_date.strip()
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
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _article_haystack(article: NewsArticle) -> str:
    parts = [
        article.title or "",
        article.summary or "",
        article.publisher or "",
        article.content or "",
    ]
    related_tickers = article.raw.get("relatedTickers") if isinstance(article.raw, dict) else None
    if isinstance(related_tickers, list):
        parts.extend(str(item) for item in related_tickers if item is not None)
    return " ".join(parts).lower()


def _matches_query(article: NewsArticle, query: str) -> bool:
    haystack = _article_haystack(article)
    tokens = [token for token in query.lower().split() if token]
    if not tokens:
        return True
    return all(token in haystack for token in tokens)


def _ticker_queries(ticker: str) -> list[str]:
    cleaned = ticker.strip().upper()
    if not cleaned:
        return []
    aliases: list[str] = [cleaned]
    compact = cleaned.replace(" ", "")
    if compact not in aliases:
        aliases.append(compact)
    if compact.startswith("HK"):
        digits = "".join(ch for ch in compact[2:] if ch.isdigit())
        if digits:
            if digits not in aliases:
                aliases.append(digits)
            normalized_4 = digits.lstrip("0")
            if normalized_4:
                normalized_4 = normalized_4.zfill(4)
                hk_symbol = f"{normalized_4}.HK"
                if hk_symbol not in aliases:
                    aliases.append(hk_symbol)
    return aliases


def _matches_ticker(article: NewsArticle, ticker_aliases: list[str]) -> bool:
    if not ticker_aliases:
        return True
    haystack = _article_haystack(article).upper()
    related_tickers = article.raw.get("relatedTickers") if isinstance(article.raw, dict) else None
    related_set: set[str] = set()
    if isinstance(related_tickers, list):
        related_set = {str(item).upper() for item in related_tickers if item is not None}
    for alias in ticker_aliases:
        alias_upper = alias.upper()
        if alias_upper in related_set:
            return True
        if alias_upper in haystack:
            return True
    return False


def _is_hk_link(article: NewsArticle) -> bool:
    if not article.link:
        return False
    try:
        host = urlsplit(article.link).netloc.lower()
    except ValueError:
        return False
    return host.endswith("hk.finance.yahoo.com")


def _ticker_relevance_score(article: NewsArticle, ticker_aliases: list[str]) -> int:
    score = 0
    haystack = _article_haystack(article).upper()
    related_tickers = article.raw.get("relatedTickers") if isinstance(article.raw, dict) else None
    related_set: set[str] = set()
    if isinstance(related_tickers, list):
        related_set = {str(item).upper() for item in related_tickers if item is not None}

    for alias in ticker_aliases:
        alias_upper = alias.upper()
        if alias_upper in related_set:
            score += 30
        if article.title and alias_upper in article.title.upper():
            score += 12
        if alias_upper in haystack:
            score += 6

    if _is_hk_link(article):
        score += 2
    if article.type and article.type.upper() == "STORY":
        score += 1
    return score


def _merge_hk_ticker_articles(
    *,
    ticker_aliases: list[str],
    market_articles: list[NewsArticle],
    ticker_articles: list[NewsArticle],
) -> list[NewsArticle]:
    merged = dedup_articles([*ticker_articles, *market_articles])
    merged.sort(
        key=lambda article: (
            _ticker_relevance_score(article, ticker_aliases),
            article.pub_date or "",
        ),
        reverse=True,
    )
    return merged


def _gather_market_news_for_time_range(*, target_count: int, market: str) -> list[NewsArticle]:
    queries = [
        *DEFAULT_MARKET_QUERIES,
        *MARKET_TIME_RANGE_QUERIES,
    ]
    aggregated: list[NewsArticle] = []
    per_query = min(200, max(20, target_count // max(1, len(queries)) + 8))

    for query in queries:
        try:
            batch = get_market_news(count=per_query, query=query, market=market)
        except Exception:
            continue
        aggregated.extend(batch)

    return dedup_articles(aggregated)


def search_news(
    *,
    ticker: str | None = None,
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    count: int = 20,
    tab: str = "news",
    with_content: bool = False,
    max_articles: int = 5,
    market: str = "us",
) -> list[NewsArticle]:
    if count < 1 or count > 200:
        raise ValueError("count must be between 1 and 200")

    since_dt = _parse_datetime(since)
    until_dt = _parse_datetime(until, end_of_day=True)
    if since_dt and until_dt and since_dt > until_dt:
        raise ValueError("since 不能晚于 until")

    fetch_count = min(200, max(count * 3, count + 20)) if (since_dt or until_dt) else count

    effective_query = query
    ticker_filter_aliases: list[str] | None = None
    hk_ticker_fallback_articles: list[NewsArticle] | None = None
    if ticker:
        normalized_ticker = ticker.strip()
        if not normalized_ticker:
            raise ValueError("ticker cannot be empty")
        if market.strip().lower() == "hk":
            ticker_aliases = _ticker_queries(normalized_ticker)
            market_aggregated: list[NewsArticle] = []
            for ticker_query in ticker_aliases:
                try:
                    batch = get_market_news(count=fetch_count, query=ticker_query, market=market)
                except Exception:
                    continue
                market_aggregated.extend(batch)

            ticker_articles: list[NewsArticle] = []
            for ticker_query in ticker_aliases:
                if not ticker_query.endswith(".HK"):
                    continue
                try:
                    ticker_articles = get_news(ticker_query, count=fetch_count, tab=tab)
                    break
                except Exception:
                    continue

            market_articles = dedup_articles(market_aggregated)
            hk_ticker_fallback_articles = market_articles
            articles = _merge_hk_ticker_articles(
                ticker_aliases=ticker_aliases,
                market_articles=market_articles,
                ticker_articles=ticker_articles,
            )
            ticker_filter_aliases = ticker_aliases
            effective_query = query
        else:
            articles = get_news(normalized_ticker, count=fetch_count, tab=tab)
    elif query:
        articles = get_market_news(count=fetch_count, query=query, market=market)
    else:
        if since_dt or until_dt:
            articles = _gather_market_news_for_time_range(target_count=max(fetch_count, count * 6), market=market)
        else:
            articles = get_market_news(count=fetch_count, market=market)

    if ticker_filter_aliases:
        filtered_by_ticker = [article for article in articles if _matches_ticker(article, ticker_filter_aliases)]
        if filtered_by_ticker:
            articles = filtered_by_ticker
        elif hk_ticker_fallback_articles is not None:
            articles = hk_ticker_fallback_articles

    if effective_query:
        articles = [article for article in articles if _matches_query(article, effective_query)]

    if since_dt or until_dt:
        filtered: list[NewsArticle] = []
        for article in articles:
            dt = _article_datetime(article)
            if dt is None:
                continue
            if since_dt and dt < since_dt:
                continue
            if until_dt and dt > until_dt:
                continue
            filtered.append(article)
        articles = filtered

    articles.sort(key=lambda article: article.pub_date or "", reverse=True)
    articles = articles[:count]

    if with_content and articles:
        enrich_count = max(0, min(max_articles, len(articles)))
        for article in articles[:enrich_count]:
            if not article.link:
                continue
            try:
                article.content = fetch_article_content(article.link)
            except RateLimitError:
                break
            except Exception:
                continue

    return articles
