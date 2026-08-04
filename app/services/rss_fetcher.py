import calendar
import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import feedparser
import yaml

from app.models import NewsArticle
from app.runtime_paths import get_config_dir
from app.services.common import CHINA_TZ, text_or_none
from app.services.news_fetcher import _parse_cnbc_filter_datetime
from app.services.rss_debug import (
    RSS_LOG,
    RssFetchTracer,
    RssSourceStatus,
    classify_fetch_error,
)

_RSS_CONTENT_TYPE = "RSS"


def _config_paths() -> tuple[Path, Path, Path]:
    config_dir = get_config_dir()
    return config_dir, config_dir / "rss_feeds.yaml", config_dir / "rss_feeds.json"
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)

_DEFAULT_PER_SOURCE_LIMIT = 5
_MAX_PER_SOURCE_LIMIT = 100
_MAX_RSS_COUNT = 5000
_RSS_FETCH_TIMEOUT_SECONDS = 15
_RSS_RETRY_DELAYS_SECONDS = (2,)
_RSS_MAX_WORKERS = 6


def _strip_html(value: str | None) -> str | None:
    text = text_or_none(value)
    if not text:
        return None
    cleaned = _HTML_TAG_RE.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _parse_source_entry(entry: object) -> tuple[str, str, str | None] | None:
    if isinstance(entry, dict):
        if "name" in entry and "url" in entry:
            name = text_or_none(entry.get("name"))
            url = text_or_none(entry.get("url"))
            note = text_or_none(entry.get("note"))
            if name and url:
                return name, url, note
            return None

        if len(entry) == 1:
            publisher, url = next(iter(entry.items()))
            if isinstance(publisher, str) and isinstance(url, str):
                publisher_name = publisher.strip()
                feed_url = url.strip()
                if publisher_name and feed_url:
                    return publisher_name, feed_url, None
    return None


def _parse_category_block(category: str, block: object) -> tuple[str, dict[str, str], str | None, list[dict[str, str | None]]] | None:
    category_name = category.strip()
    if not category_name:
        return None

    label: str | None = None
    sources_block: object = block
    if isinstance(block, dict) and "sources" in block:
        label = text_or_none(block.get("label"))
        sources_block = block.get("sources")

    normalized_sources: dict[str, str] = {}
    source_meta: list[dict[str, str | None]] = []

    if isinstance(sources_block, list):
        for item in sources_block:
            parsed = _parse_source_entry(item)
            if not parsed:
                continue
            name, url, note = parsed
            normalized_sources[name] = url
            source_meta.append({"name": name, "url": url, "note": note})
    elif isinstance(sources_block, dict):
        for publisher, url in sources_block.items():
            if publisher in {"label", "sources"}:
                continue
            if not isinstance(publisher, str) or not isinstance(url, str):
                continue
            publisher_name = publisher.strip()
            feed_url = url.strip()
            if publisher_name and feed_url:
                normalized_sources[publisher_name] = feed_url
                source_meta.append({"name": publisher_name, "url": feed_url, "note": None})
    else:
        return None

    if not normalized_sources:
        return None

    return category_name, normalized_sources, label, source_meta


def _parse_rss_config(raw: dict[str, Any]) -> tuple[dict[str, dict[str, str]], int, list[dict[str, Any]]]:
    categories = raw.get("categories")
    if not isinstance(categories, dict):
        raise ValueError("invalid categories")

    feeds: dict[str, dict[str, str]] = {}
    catalog: list[dict[str, Any]] = []

    for category, block in categories.items():
        if not isinstance(category, str):
            continue
        parsed = _parse_category_block(category, block)
        if not parsed:
            continue
        category_name, normalized_sources, label, source_meta = parsed
        feeds[category_name] = normalized_sources
        catalog.append(
            {
                "category": category_name,
                "label": label,
                "sources": source_meta,
            }
        )

    if not feeds:
        raise ValueError("no feeds")

    per_source_limit = raw.get("per_source_limit", _DEFAULT_PER_SOURCE_LIMIT)
    if not isinstance(per_source_limit, int) or per_source_limit < 1:
        per_source_limit = _DEFAULT_PER_SOURCE_LIMIT

    return feeds, min(per_source_limit, _MAX_PER_SOURCE_LIMIT), catalog


def _load_rss_config_raw() -> tuple[dict[str, Any], str, Path]:
    config_dir, yaml_path, json_path = _config_paths()

    if yaml_path.exists():
        loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return loaded, "yaml", yaml_path
        raise ValueError(f"invalid yaml root in {yaml_path}")

    if json_path.exists():
        loaded = json.loads(json_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return loaded, "json", json_path
        raise ValueError(f"invalid json root in {json_path}")

    raise FileNotFoundError(f"rss config not found under {config_dir}")


def _load_rss_config() -> tuple[dict[str, dict[str, str]], int]:
    raw, _, _ = _load_rss_config_raw()
    return _parse_rss_config(raw)[:2]


def _load_rss_catalog() -> tuple[dict[str, dict[str, str]], int, list[dict[str, Any]], str, Path, str | None]:
    raw, config_source, config_path = _load_rss_config_raw()
    feeds, per_source_limit, catalog = _parse_rss_config(raw)
    return feeds, per_source_limit, catalog, config_source, config_path, None


def _normalize_rss_pub_date(entry: feedparser.FeedParserDict) -> str | None:
    published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if published_parsed:
        ts = calendar.timegm(published_parsed)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.astimezone(CHINA_TZ).isoformat()

    for field in ("published", "updated"):
        text = text_or_none(entry.get(field))
        if not text:
            continue
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CHINA_TZ).isoformat()

    return None


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


def _fetch_feed_with_headers(url: str) -> feedparser.FeedParserDict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=_RSS_FETCH_TIMEOUT_SECONDS) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in {408, 429, 500, 502, 503, 504}:
            raise TimeoutError(f"RSS HTTP {exc.code}: {url}") from exc
        raise
    except TimeoutError:
        raise
    except Exception as exc:
        raise TimeoutError(f"RSS fetch timeout or network error: {url}") from exc

    return feedparser.parse(body.decode(charset, errors="replace"))


def _fetch_feed_with_retry(url: str, retries: int | None = None) -> tuple[feedparser.FeedParserDict | None, str | None]:
    attempts = len(_RSS_RETRY_DELAYS_SECONDS) + 1 if retries is None else max(1, retries)
    last_error: str | None = None
    for attempt in range(attempts):
        if attempt > 0:
            time.sleep(_RSS_RETRY_DELAYS_SECONDS[min(attempt - 1, len(_RSS_RETRY_DELAYS_SECONDS) - 1)])
        try:
            feed = _fetch_feed_with_headers(url)
            if feed and getattr(feed, "entries", None):
                return feed, None
            last_error = "feed has no entries"
        except TimeoutError as exc:
            last_error = str(exc)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    return None, last_error


def _collect_articles_from_feed(
    feed: feedparser.FeedParserDict,
    *,
    publisher: str,
    category_name: str,
    feed_url: str,
    source_limit: int,
    query_tokens: list[str],
    since_dt: datetime | None,
    until_dt: datetime | None,
) -> list[NewsArticle]:
    articles: list[NewsArticle] = []
    for entry in feed.entries[:source_limit]:
        article = _map_rss_entry(
            entry,
            publisher=publisher,
            category=category_name,
            feed_url=feed_url,
        )
        if not article or (not article.link and not article.title):
            continue

        article_dt = _article_datetime(article)
        if since_dt or until_dt:
            if article_dt is None:
                continue
            if since_dt and article_dt < since_dt:
                continue
            if until_dt and article_dt > until_dt:
                continue

        if query_tokens:
            haystack = " ".join(
                filter(
                    None,
                    [article.title, article.summary, article.publisher, category_name],
                )
            ).casefold()
            if not all(token in haystack for token in query_tokens):
                continue

        articles.append(article)
    return articles


def _fetch_source_articles(
    category_name: str,
    publisher: str,
    url: str,
    *,
    source_limit: int,
    query_tokens: list[str],
    since_dt: datetime | None,
    until_dt: datetime | None,
    tracer: RssFetchTracer | None = None,
) -> list[NewsArticle]:
    trace_index = tracer.source_start(category_name, publisher, url) if tracer else None
    try:
        feed, fetch_error = _fetch_feed_with_retry(url)
        if not feed:
            if tracer and trace_index is not None:
                tracer.source_finish(
                    trace_index,
                    status=classify_fetch_error(fetch_error),
                    error=fetch_error,
                )
            return []

        entry_count = min(len(feed.entries), source_limit)
        articles = _collect_articles_from_feed(
            feed,
            publisher=publisher,
            category_name=category_name,
            feed_url=url,
            source_limit=source_limit,
            query_tokens=query_tokens,
            since_dt=since_dt,
            until_dt=until_dt,
        )
        if tracer and trace_index is not None:
            tracer.source_finish(
                trace_index,
                status=RssSourceStatus.OK,
                entry_count=entry_count,
                matched_count=len(articles),
            )
        return articles
    except Exception as exc:
        if tracer and trace_index is not None:
            tracer.source_finish(
                trace_index,
                status=RssSourceStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
        raise


def _map_rss_entry(
    entry: feedparser.FeedParserDict,
    *,
    publisher: str,
    category: str,
    feed_url: str,
) -> NewsArticle | None:
    title = text_or_none(entry.get("title"))
    link = text_or_none(entry.get("link")) or text_or_none(entry.get("id"))
    if not title and not link:
        return None

    content_value: str | None = None
    raw_content = entry.get("content")
    if isinstance(raw_content, list) and raw_content:
        first = raw_content[0]
        if isinstance(first, dict):
            content_value = text_or_none(first.get("value"))

    summary = (
        _strip_html(text_or_none(entry.get("summary")))
        or _strip_html(text_or_none(entry.get("description")))
        or _strip_html(content_value)
    )
    article_id = text_or_none(entry.get("id")) or link or title

    return NewsArticle(
        id=article_id,
        title=title,
        summary=summary,
        publisher=publisher,
        link=link,
        pubDate=_normalize_rss_pub_date(entry),
        type=_RSS_CONTENT_TYPE,
        raw={
            "source": "rss",
            "category": category,
            "feed_url": feed_url,
            "guid": text_or_none(entry.get("guid")),
            "tags": [tag.get("term") for tag in entry.get("tags", []) if isinstance(tag, dict) and tag.get("term")],
        },
    )


def list_rss_categories() -> list[str]:
    feeds, _ = _load_rss_config()
    return sorted(feeds.keys())


def describe_rss_sources() -> dict[str, Any]:
    _, per_source_limit, catalog, config_source, config_path, warning = _load_rss_catalog()
    total_sources = sum(len(item["sources"]) for item in catalog)
    return {
        "config_path": str(config_path) if config_path else None,
        "config_source": config_source,
        "warning": warning,
        "per_source_limit": per_source_limit,
        "category_count": len(catalog),
        "source_count": total_sources,
        "categories": [item["category"] for item in catalog],
        "catalog": catalog,
    }


def _normalize_category_list(categories: list[str] | None) -> list[str] | None:
    if not categories:
        return None

    normalized: list[str] = []
    seen: set[str] = set()
    for item in categories:
        for part in item.split(","):
            name = part.strip()
            if name and name not in seen:
                seen.add(name)
                normalized.append(name)
    return normalized or None


def _find_categories_for_source_name(
    feeds: dict[str, dict[str, str]],
    name: str,
) -> list[str]:
    """当用户把「源名」误当成 category 时，反查所属分类。"""
    needle = name.strip().casefold()
    if not needle:
        return []

    matched: list[str] = []
    for category, sources in feeds.items():
        for publisher in sources:
            publisher_key = publisher.casefold()
            if (
                publisher_key == needle
                or publisher_key.startswith(f"{needle} - ")
                or publisher_key.startswith(f"{needle}-")
            ):
                matched.append(category)
                break
    return matched


def _resolve_selected_feeds(
    feeds: dict[str, dict[str, str]],
    categories: list[str] | None,
) -> dict[str, dict[str, str]]:
    if not categories:
        return feeds

    normalized: list[str] = []
    seen: set[str] = set()
    for item in categories:
        name = item.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        normalized.append(name)

    if not normalized:
        return feeds

    invalid = [name for name in normalized if name not in feeds]
    if invalid:
        allowed = ", ".join(sorted(feeds))
        hints: list[str] = []
        for name in invalid:
            source_cats = _find_categories_for_source_name(feeds, name)
            if source_cats:
                hints.append(
                    f"「{name}」是发布源名称，不是分类；请改用 category={','.join(source_cats)}"
                )
        joined = ", ".join(invalid)
        hint_text = (" " + "；".join(hints) + ".") if hints else ""
        raise ValueError(
            f"Invalid categor{'y' if len(invalid) == 1 else 'ies'}: {joined}.{hint_text} "
            f"可用分类: {allowed}"
        )

    return {name: feeds[name] for name in normalized}


def get_rss_news(
    count: int = 20,
    query: str | None = None,
    categories: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    per_source_limit: int | None = None,
) -> list[NewsArticle]:
    if count < 1 or count > _MAX_RSS_COUNT:
        raise ValueError(f"count must be between 1 and {_MAX_RSS_COUNT}")

    feeds, default_per_source_limit = _load_rss_config()
    source_limit = per_source_limit if per_source_limit is not None else default_per_source_limit
    if source_limit < 1 or source_limit > _MAX_PER_SOURCE_LIMIT:
        raise ValueError(f"per_source_limit must be between 1 and {_MAX_PER_SOURCE_LIMIT}")

    since_dt = _parse_cnbc_filter_datetime(since)
    until_dt = _parse_cnbc_filter_datetime(until, end_of_day=True)
    if since_dt and until_dt and since_dt > until_dt:
        raise ValueError("since 不能晚于 until")

    normalized_categories = _normalize_category_list(categories)
    selected_feeds = _resolve_selected_feeds(feeds, normalized_categories)

    query_tokens = [token.casefold() for token in query.split() if token] if query else []
    deduped: dict[str, NewsArticle] = {}

    jobs = [
        (category_name, publisher, url)
        for category_name, sources in selected_feeds.items()
        for publisher, url in sources.items()
    ]

    tracer = RssFetchTracer(total_sources=len(jobs)) if jobs else None
    if tracer:
        tracer.run_start(
            count=count,
            query=query,
            categories=normalized_categories,
            per_source_limit=source_limit,
            since=since,
            until=until,
        )
    early_stop = False

    def _merge_articles(batch: list[NewsArticle]) -> None:
        for article in batch:
            dedupe_key = article.link or article.id or article.title
            if not dedupe_key or dedupe_key in deduped:
                continue
            deduped[dedupe_key] = article

    if len(jobs) <= 1:
        for category_name, publisher, url in jobs:
            _merge_articles(
                _fetch_source_articles(
                    category_name,
                    publisher,
                    url,
                    source_limit=source_limit,
                    query_tokens=query_tokens,
                    since_dt=since_dt,
                    until_dt=until_dt,
                    tracer=tracer,
                )
            )
            if len(deduped) >= count:
                early_stop = True
                break
    else:
        with ThreadPoolExecutor(max_workers=min(_RSS_MAX_WORKERS, len(jobs))) as executor:
            futures = [
                executor.submit(
                    _fetch_source_articles,
                    category_name,
                    publisher,
                    url,
                    source_limit=source_limit,
                    query_tokens=query_tokens,
                    since_dt=since_dt,
                    until_dt=until_dt,
                    tracer=tracer,
                )
                for category_name, publisher, url in jobs
            ]
            for future in as_completed(futures):
                try:
                    _merge_articles(future.result())
                except Exception as exc:
                    RSS_LOG.info("失败 | 工作线程异常 | %s", exc)
                    continue
                if len(deduped) >= count:
                    early_stop = True
                    break

    articles = list(deduped.values())
    articles.sort(key=lambda article: article.pub_date or "", reverse=True)
    result = articles[:count]

    if tracer:
        tracer.run_finish(
            returned=len(result),
            deduped=len(deduped),
            target_count=count,
            early_stop=early_stop,
        )

    return result
