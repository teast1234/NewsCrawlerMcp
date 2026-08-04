import json
from datetime import timedelta, timezone

from app.models import NewsArticle
from app.runtime_paths import get_config_dir

VALID_TABS = {"news", "all", "press releases"}
SUPPORTED_MARKETS = {"us", "hk"}
RETRY_DELAYS_SECONDS = (3, 8, 15)
TAB_QUERY_REFS = {
    "all": "newsAll",
    "news": "latestNews",
    "press releases": "pressRelease",
}
MARKET_REGION_LANG = {
    "us": ("US", "en-US"),
    "hk": ("HK", "zh-Hant-HK"),
}
YAHOO_NEWS_URL = "https://finance.yahoo.com/xhr/ncp?queryRef={query_ref}&serviceKey=ncp_fin"
YAHOO_SEARCH_URL = (
    "https://query1.finance.yahoo.com/v1/finance/search"
    "?q={query}&newsCount={count}&quotesCount=0&lang={lang}&region={region}"
)

_BUILTIN_DEFAULT_MARKET_QUERIES = (
    "stock market",
    "finance",
    "economy",
    "business news",
    "global markets",
    "financial markets",
    "investing",
    "market outlook",
    "economic growth",
    "recession",
    "interest rates",
    "central bank",
    "federal reserve",
    "inflation",
    "jobs report",
    "treasury yields",
    "bond market",
    "currency market",
    "us dollar",
    "commodities",
    "oil",
    "natural gas",
    "gold",
    "technology stocks",
    "semiconductor",
    "magnificent 7",
    "artificial intelligence",
    "earnings",
    "guidance",
    "mergers and acquisitions",
    "ipo",
    "stock futures",
    "s&p 500",
    "nasdaq",
    "dow jones",
)
_BUILTIN_MARKET_TIME_RANGE_QUERIES = (
    "latest market news",
    "today market news",
    "breaking finance news",
    "market update",
    "weekly market recap",
    "pre market movers",
    "after hours stocks",
    "economic calendar",
    "fed meeting",
    "cpi report",
    "pce inflation",
    "nonfarm payrolls",
)

_CONFIG_PATH = get_config_dir() / "market_queries.json"


def _normalize_queries(raw_value: object, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(raw_value, list):
        return fallback

    deduped: list[str] = []
    seen: set[str] = set()
    for item in raw_value:
        if not isinstance(item, str):
            continue
        query = item.strip()
        if not query:
            continue
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(query)

    if not deduped:
        return fallback
    return tuple(deduped)


def _load_market_query_pools() -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _BUILTIN_DEFAULT_MARKET_QUERIES, _BUILTIN_MARKET_TIME_RANGE_QUERIES

    if not isinstance(raw, dict):
        return _BUILTIN_DEFAULT_MARKET_QUERIES, _BUILTIN_MARKET_TIME_RANGE_QUERIES

    default_queries = _normalize_queries(raw.get("default_market_queries"), _BUILTIN_DEFAULT_MARKET_QUERIES)
    time_range_queries = _normalize_queries(raw.get("market_time_range_queries"), _BUILTIN_MARKET_TIME_RANGE_QUERIES)
    return default_queries, time_range_queries


def resolve_market_region(market: str | None) -> tuple[str, str, str]:
    market_key = (market or "us").strip().lower()
    if market_key not in SUPPORTED_MARKETS:
        raise ValueError(f"Invalid market '{market}'. Choose from: {', '.join(sorted(SUPPORTED_MARKETS))}")
    region, lang = MARKET_REGION_LANG[market_key]
    return market_key, region, lang


DEFAULT_MARKET_QUERIES, MARKET_TIME_RANGE_QUERIES = _load_market_query_pools()
CHINA_TZ = timezone(timedelta(hours=8))


class RateLimitError(Exception):
    pass


def text_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def extract_url(value: object) -> str | None:
    if isinstance(value, dict):
        return text_or_none(value.get("url"))
    return text_or_none(value)


def dedup_articles(articles: list[NewsArticle]) -> list[NewsArticle]:
    deduped: dict[str, NewsArticle] = {}
    for article in articles:
        key = article.id or article.link or article.title
        if not key:
            continue
        if key in deduped:
            continue
        deduped[key] = article
    return list(deduped.values())
