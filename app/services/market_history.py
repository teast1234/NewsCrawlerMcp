"""Yahoo Finance 历史行情（yfinance OHLCV）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import yfinance as yf
from yfinance.exceptions import YFRateLimitError

from app.models import HistoryBar, HistoryResponse
from app.services.common import RateLimitError

VALID_PERIODS = {
    "1d",
    "5d",
    "1mo",
    "3mo",
    "6mo",
    "1y",
    "2y",
    "5y",
    "10y",
    "ytd",
    "max",
}
VALID_INTERVALS = {
    "1m",
    "2m",
    "5m",
    "15m",
    "30m",
    "60m",
    "90m",
    "1h",
    "1d",
    "5d",
    "1wk",
    "1mo",
    "3mo",
}


def _parse_ymd(value: str, field: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field} cannot be empty")
    # 允许 YYYY-MM-DD 或带时间的 ISO；统一截到日期给 yfinance
    try:
        if "T" in text or " " in text:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.date().isoformat()
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD (or ISO datetime), got: {value!r}") from exc


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    if number is None:
        return None
    return int(number)


def _resolve_quote_type(ticker_obj: yf.Ticker) -> str | None:
    """从 fast_info（优先）或 info 读取 Yahoo 交易类型，如 CRYPTOCURRENCY / EQUITY。"""
    try:
        fast = getattr(ticker_obj, "fast_info", None)
        if fast is not None:
            value = None
            if hasattr(fast, "get"):
                value = fast.get("quoteType") or fast.get("quote_type")
            elif hasattr(fast, "quote_type"):
                value = getattr(fast, "quote_type", None)
            elif isinstance(fast, dict):
                value = fast.get("quoteType") or fast.get("quote_type")
            if value:
                return str(value).strip().upper() or None
    except Exception:  # noqa: BLE001
        pass

    try:
        info = ticker_obj.info or {}
        value = info.get("quoteType") or info.get("quote_type")
        if value:
            return str(value).strip().upper() or None
    except Exception:  # noqa: BLE001
        pass
    return None


def get_market_history(
    symbol: str,
    *,
    period: str | None = None,
    start: str | None = None,
    end: str | None = None,
    interval: str = "1d",
) -> HistoryResponse:
    """
    按 Yahoo 正规报价符号拉取历史 OHLCV。

    时间筛选二选一优先：
    - 传 start / end：按绝对日期过滤（可不传 period）
    - 只传 period：相对跨度（如 1y / max）
    - 都不传：默认 period=1mo
    """
    ticker = (symbol or "").strip().upper()
    if not ticker:
        raise ValueError("symbol cannot be empty")

    normalized_interval = (interval or "1d").strip().lower()
    if normalized_interval not in VALID_INTERVALS:
        raise ValueError(
            f"Invalid interval '{interval}'. Choose from: {', '.join(sorted(VALID_INTERVALS))}"
        )

    start_ymd = _parse_ymd(start, "start") if start else None
    end_ymd = _parse_ymd(end, "end") if end else None
    if start_ymd and end_ymd and start_ymd > end_ymd:
        raise ValueError("start must be <= end")

    normalized_period: str | None = None
    if period is not None and str(period).strip():
        normalized_period = str(period).strip().lower()
        if normalized_period not in VALID_PERIODS:
            raise ValueError(
                f"Invalid period '{period}'. Choose from: {', '.join(sorted(VALID_PERIODS))}"
            )

    # 有绝对日期时以 start/end 为准；否则用 period（默认 1mo）
    use_absolute = bool(start_ymd or end_ymd)
    if not use_absolute and not normalized_period:
        normalized_period = "1mo"

    history_kwargs: dict[str, Any] = {
        "interval": normalized_interval,
        # False：保留 Close + Adj Close 两列（auto_adjust=True 时 Adj Close 常被合并掉）
        "auto_adjust": False,
        "actions": False,
    }
    if use_absolute:
        if start_ymd:
            history_kwargs["start"] = start_ymd
        if end_ymd:
            # yfinance 的 end 为左闭右开；对外按「含结束日」理解，这里 +1 天
            end_exclusive = (date.fromisoformat(end_ymd) + timedelta(days=1)).isoformat()
            history_kwargs["end"] = end_exclusive
    else:
        history_kwargs["period"] = normalized_period

    ticker_obj = yf.Ticker(ticker)
    try:
        frame = ticker_obj.history(**history_kwargs)
    except YFRateLimitError as exc:
        raise RateLimitError(
            "Yahoo Finance 请求过于频繁，已被限流。请等待 5-10 分钟后再试。"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to fetch history for {ticker}: {exc}") from exc

    if frame is None or getattr(frame, "empty", True):
        raise ValueError(
            f"No history data for symbol '{ticker}' "
            f"(interval={normalized_interval}, period={normalized_period}, "
            f"start={start_ymd}, end={end_ymd}). Check the Yahoo quote symbol."
        )

    quote_type = _resolve_quote_type(ticker_obj)

    # 列名兼容：有的版本只有 Close
    columns = {str(c).strip(): c for c in frame.columns}
    open_col = columns.get("Open")
    high_col = columns.get("High")
    low_col = columns.get("Low")
    close_col = columns.get("Close")
    adj_col = columns.get("Adj Close") or columns.get("AdjClose")
    volume_col = columns.get("Volume")

    bars: list[HistoryBar] = []
    for idx, row in frame.iterrows():
        if hasattr(idx, "date"):
            bar_date = idx.date().isoformat()
        else:
            bar_date = str(idx)[:10]

        close_val = _to_float(row[close_col]) if close_col is not None else None
        adj_val = _to_float(row[adj_col]) if adj_col is not None else close_val

        bars.append(
            HistoryBar(
                date=bar_date,
                open=_to_float(row[open_col]) if open_col is not None else None,
                high=_to_float(row[high_col]) if high_col is not None else None,
                low=_to_float(row[low_col]) if low_col is not None else None,
                close=close_val,
                adj_close=adj_val,
                volume=_to_int(row[volume_col]) if volume_col is not None else None,
            )
        )

    return HistoryResponse(
        symbol=ticker,
        interval=normalized_interval,
        period=None if use_absolute else normalized_period,
        start=start_ymd,
        end=end_ymd,
        quote_type=quote_type,
        count=len(bars),
        bars=bars,
    )
