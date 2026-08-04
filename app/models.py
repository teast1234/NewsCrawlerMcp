"""新闻 MCP 数据模型（与 analyst-python HTTP 端字段对齐）。"""

from typing import Any

from pydantic import BaseModel, Field


class NewsArticle(BaseModel):
    id: str | None = None
    title: str | None = None
    summary: str | None = None
    publisher: str | None = None
    link: str | None = None
    pub_date: str | None = Field(default=None, alias="pubDate")
    type: str | None = None
    content: str | None = None
    raw: dict[str, Any] | None = None

    model_config = {"populate_by_name": True, "serialize_by_alias": True}


class NewsResponse(BaseModel):
    ticker: str
    tab: str
    count: int
    articles: list[NewsArticle]


class HistoryBar(BaseModel):
    """单根 K 线 / 历史价。"""

    date: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    adj_close: float | None = Field(default=None, alias="adjClose")
    volume: int | None = None

    model_config = {"populate_by_name": True, "serialize_by_alias": True}


class HistoryResponse(BaseModel):
    """历史行情响应。"""

    symbol: str
    interval: str
    period: str | None = None
    start: str | None = None
    end: str | None = None
    quote_type: str | None = Field(default=None, alias="quoteType")
    count: int
    bars: list[HistoryBar]

    model_config = {"populate_by_name": True, "serialize_by_alias": True}
