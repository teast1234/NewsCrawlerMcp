from app.models import NewsArticle
from app.services.article_content import fetch_article_content
from app.services.common import RateLimitError
from app.services.news_fetcher import get_market_news, get_news


def get_news_with_content(
    ticker: str | None = None,
    *,
    count: int = 10,
    tab: str = "news",
    query: str | None = None,
    market: bool = False,
    max_articles: int = 5,
) -> list[NewsArticle]:
    if market:
        articles = get_market_news(count=count, query=query)
    else:
        if not ticker:
            raise ValueError("ticker is required when market=False")
        articles = get_news(ticker, count=count, tab=tab)

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
