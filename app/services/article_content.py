import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

from app.services.common import RateLimitError

YAHOO_CAAS_URL = "https://finance.yahoo.com/caas/content/article?uuid={uuid}&serviceKey=ncp_fin"
_MIN_CONTENT_LENGTH = 80
_HTML_BODY_PATTERNS = (
    re.compile(r'<div[^>]+class="[^"]*caas-body[^"]*"[^>]*>(.*?)</div>\s*</article>', re.S),
    re.compile(r"<article\b[^>]*>(.*?)</article>", re.S),
    re.compile(r'<div[^>]+(?:class|id)="[^"]*(?:article-body|articleBody|post-content|entry-content|story-body)[^"]*"[^>]*>(.*?)</div>', re.S | re.I),
)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.S | re.I)
_META_DESC_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']+)["\']',
    re.I,
)
_META_DESC_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\'](?:description|og:description)["\']',
    re.I,
)


def _strip_html(raw_html: str) -> str:
    cleaned = _SCRIPT_STYLE_RE.sub(" ", raw_html)
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned


def _normalize_text(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = _WHITESPACE_RE.sub(" ", text).strip()
    return cleaned or None


def _is_private_host(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"Unable to resolve host: {hostname}") from exc

    for info in infos:
        ip_text = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def _validate_public_http_url(link: str) -> str:
    if not link or not isinstance(link, str):
        raise ValueError("link is required")

    parsed = urlparse(link.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https links are supported")
    if not parsed.hostname:
        raise ValueError("Invalid link: missing host")
    if _is_private_host(parsed.hostname):
        raise ValueError("Private/internal URLs are not allowed")
    return link.strip()


def _http_get(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/json,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            _validate_public_http_url(final_url)
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimitError("Upstream request rate limited. Try again later.") from exc
        raise


def _extract_uuid_from_link(link: str) -> str | None:
    match = re.search(
        r"/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/",
        link,
    )
    return match.group(1) if match else None


def _fetch_article_via_caas(uuid: str) -> tuple[str | None, dict | None]:
    url = YAHOO_CAAS_URL.format(uuid=uuid)
    body = _http_get(url)
    data = json.loads(body)

    items = data.get("items") or data.get("data", {}).get("contents") or []
    if not items:
        return None, data

    first = items[0] if isinstance(items, list) else items
    content_node = first.get("content") if isinstance(first, dict) else None
    if not content_node:
        return None, data

    body_html = (
        content_node.get("body")
        or content_node.get("articleBody")
        or content_node.get("articleBodyHtml")
    )
    if isinstance(body_html, dict):
        body_html = body_html.get("data") or body_html.get("html")

    if not isinstance(body_html, str) or not body_html.strip():
        return None, data

    return _normalize_text(_strip_html(body_html)), data


def _extract_with_trafilatura(html_body: str, url: str) -> str | None:
    try:
        import trafilatura
    except ImportError:
        return None

    extracted = trafilatura.extract(
        html_body,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    return _normalize_text(extracted)


def _extract_with_regex(html_body: str) -> str | None:
    for pattern in _HTML_BODY_PATTERNS:
        matched = pattern.search(html_body)
        if matched:
            text = _normalize_text(_strip_html(matched.group(1)))
            if text and len(text) >= _MIN_CONTENT_LENGTH:
                return text

    for pattern in (_META_DESC_RE, _META_DESC_RE_ALT):
        matched = pattern.search(html_body)
        if matched:
            text = _normalize_text(html.unescape(matched.group(1)))
            if text and len(text) >= 40:
                return text
    return None


def _fetch_article_via_html(link: str) -> str | None:
    body = _http_get(link)
    if "Will be right back" in body:
        raise RuntimeError("Yahoo Finance is temporarily unavailable")

    text = _extract_with_trafilatura(body, link)
    if text and len(text) >= _MIN_CONTENT_LENGTH:
        return text

    text = _extract_with_regex(body)
    if text:
        return text

    # Last resort: whole-page text after stripping scripts/styles.
    fallback = _normalize_text(_strip_html(body))
    if fallback and len(fallback) >= _MIN_CONTENT_LENGTH:
        # Avoid returning huge chrome-heavy pages; keep a reasonable chunk.
        return fallback[:20000]
    return None


def fetch_article_content(link: str) -> str:
    normalized_link = _validate_public_http_url(link)
    uuid = _extract_uuid_from_link(normalized_link) if "finance.yahoo.com" in normalized_link else None
    last_error: Exception | None = None

    if uuid:
        try:
            text, _raw = _fetch_article_via_caas(uuid)
            if text and len(text) >= _MIN_CONTENT_LENGTH:
                return text
            if text:
                return text
        except RateLimitError:
            raise
        except Exception as exc:
            last_error = exc

    try:
        text = _fetch_article_via_html(normalized_link)
        if text:
            return text
    except RateLimitError:
        raise
    except Exception as exc:
        last_error = exc

    if last_error is not None:
        raise RuntimeError(f"Failed to extract article content: {last_error}") from last_error
    raise RuntimeError("Article body not found in HTML")
