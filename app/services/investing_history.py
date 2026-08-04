"""Investing.com 历史行情（investpy / investingpy）。

两层 API：
1. 原生：按 Investing product + name/country 拉 OHLCV
2. Yahoo 映射：读 config/investing_symbol_map.yaml，用 Yahoo 符号走同一套响应
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.models import HistoryBar, HistoryResponse
from app.runtime_paths import get_config_dir
from app.services.common import RateLimitError

# investpy 仅支持这些周期（大小写按其约定）
_INVESTPY_INTERVALS = {
    "1d": "Daily",
    "daily": "Daily",
    "1wk": "Weekly",
    "1w": "Weekly",
    "weekly": "Weekly",
    "1mo": "Monthly",
    "monthly": "Monthly",
}

_VALID_PERIODS = {
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

_PRODUCT_QUOTE_TYPE = {
    "stocks": "EQUITY",
    "indices": "INDEX",
    "cryptos": "CRYPTOCURRENCY",
    "commodities": "FUTURE",
    "bonds": "INDEX",
    "etfs": "ETF",
    "funds": "MUTUALFUND",
    "currency_crosses": "CURRENCY",
    "certificates": "EQUITY",
}

_VALID_PRODUCTS = set(_PRODUCT_QUOTE_TYPE)


def _import_investpy():
    """优先 investingpy，回退 investpy（PyPI 上 investingpy 多为 investpy 衍生）。"""
    try:
        import investingpy as lib  # type: ignore

        return lib
    except ImportError:
        pass
    try:
        import investpy as lib  # type: ignore

        return lib
    except ImportError as exc:
        raise RuntimeError(
            "未安装 investpy/investingpy。请执行: pip install investpy"
        ) from exc


def _parse_ymd(value: str, field: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field} cannot be empty")
    try:
        if "T" in text or " " in text:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.date().isoformat()
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD (or ISO datetime), got: {value!r}") from exc


def _ymd_to_investpy(ymd: str) -> str:
    """YYYY-MM-DD -> DD/MM/YYYY"""
    y, m, d = ymd.split("-")
    return f"{d}/{m}/{y}"


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


def _period_to_start(today: date, normalized_period: str) -> date:
    if normalized_period == "ytd":
        return date(today.year, 1, 1)
    if normalized_period == "max":
        return date(1970, 1, 1)
    if normalized_period.endswith("mo"):
        months = int(normalized_period[:-2])
        year, month = today.year, today.month - months
        while month <= 0:
            month += 12
            year -= 1
        return date(year, month, min(today.day, 28))
    if normalized_period.endswith("y"):
        years = int(normalized_period[:-1])
        return date(today.year - years, today.month, min(today.day, 28))
    if normalized_period.endswith("d"):
        return today - timedelta(days=int(normalized_period[:-1]))
    return today - timedelta(days=30)


def _resolve_range(
    *,
    period: str | None,
    start: str | None,
    end: str | None,
) -> tuple[str, str, str | None, bool]:
    """返回 (start_ymd, end_ymd, normalized_period_or_none, use_absolute)."""
    start_ymd = _parse_ymd(start, "start") if start else None
    end_ymd = _parse_ymd(end, "end") if end else None
    if start_ymd and end_ymd and start_ymd > end_ymd:
        raise ValueError("start must be <= end")

    normalized_period: str | None = None
    if period is not None and str(period).strip():
        normalized_period = str(period).strip().lower()
        if normalized_period not in _VALID_PERIODS:
            raise ValueError(
                f"Invalid period '{period}'. Choose from: {', '.join(sorted(_VALID_PERIODS))}"
            )

    use_absolute = bool(start_ymd or end_ymd)
    today = date.today()
    if use_absolute:
        if not end_ymd:
            end_ymd = today.isoformat()
        if not start_ymd:
            start_ymd = (today - timedelta(days=30)).isoformat()
        return start_ymd, end_ymd, None, True

    if not normalized_period:
        normalized_period = "1mo"
    start_d = _period_to_start(today, normalized_period)
    return start_d.isoformat(), today.isoformat(), normalized_period, False


def _normalize_interval(interval: str) -> str:
    key = (interval or "1d").strip().lower()
    if key not in _INVESTPY_INTERVALS:
        raise ValueError(
            "Investing 历史接口仅支持 interval=1d|1wk|1mo "
            f"(got {interval!r})"
        )
    return _INVESTPY_INTERVALS[key]


def _frame_to_bars(frame: Any) -> list[HistoryBar]:
    if frame is None or getattr(frame, "empty", True):
        return []

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
    return bars


def _raise_investing_error(exc: BaseException, *, context: str) -> None:
    msg = str(exc)
    lower = msg.lower()
    if "403" in msg or "err#0015" in lower or "too many" in lower:
        raise RateLimitError(
            "Investing.com 请求被拒绝或限流（常见 403）。请检查代理或稍后再试。"
        ) from exc
    raise RuntimeError(f"Investing 拉数失败 ({context}): {exc}") from exc


def get_investing_history_native(
    *,
    product: str,
    name: str,
    country: str | None = None,
    period: str | None = None,
    start: str | None = None,
    end: str | None = None,
    interval: str = "1d",
    stock_exchange: str | None = None,
) -> HistoryResponse:
    """
    原生 Investing 历史行情。

    product: stocks/indices/cryptos/commodities/bonds/etfs/...
    name: Investing 名称（如 Apple / Nasdaq 100 / Bitcoin / Gold / U.S. 10Y）
    country: 股票/指数/ETF 等需要；加密货币可不传
    """
    product_key = (product or "").strip().lower()
    if product_key not in _VALID_PRODUCTS:
        raise ValueError(
            f"Invalid product '{product}'. Choose from: {', '.join(sorted(_VALID_PRODUCTS))}"
        )
    investing_name = (name or "").strip()
    if not investing_name:
        raise ValueError("name cannot be empty")

    investpy_interval = _normalize_interval(interval)
    start_ymd, end_ymd, normalized_period, use_absolute = _resolve_range(
        period=period, start=start, end=end
    )
    from_date = _ymd_to_investpy(start_ymd)
    to_date = _ymd_to_investpy(end_ymd)

    lib = _import_investpy()
    country_norm = (country or "").strip().lower() or None

    try:
        if product_key == "stocks":
            if not country_norm:
                raise ValueError("stocks 需要 country，例如 united states")
            frame = lib.get_stock_historical_data(
                stock=investing_name,
                country=country_norm,
                from_date=from_date,
                to_date=to_date,
                interval=investpy_interval,
            )
        elif product_key == "indices":
            if not country_norm:
                raise ValueError("indices 需要 country，例如 united states")
            frame = lib.get_index_historical_data(
                index=investing_name,
                country=country_norm,
                from_date=from_date,
                to_date=to_date,
                interval=investpy_interval,
            )
        elif product_key == "cryptos":
            frame = lib.get_crypto_historical_data(
                crypto=investing_name,
                from_date=from_date,
                to_date=to_date,
                interval=investpy_interval,
            )
        elif product_key == "commodities":
            frame = lib.get_commodity_historical_data(
                commodity=investing_name,
                from_date=from_date,
                to_date=to_date,
                country=country_norm,
                interval=investpy_interval,
            )
        elif product_key == "bonds":
            frame = lib.get_bond_historical_data(
                bond=investing_name,
                from_date=from_date,
                to_date=to_date,
                interval=investpy_interval,
            )
        elif product_key == "etfs":
            if not country_norm:
                raise ValueError("etfs 需要 country，例如 united states")
            frame = lib.get_etf_historical_data(
                etf=investing_name,
                country=country_norm,
                from_date=from_date,
                to_date=to_date,
                stock_exchange=stock_exchange,
                interval=investpy_interval,
            )
        elif product_key == "funds":
            if not country_norm:
                raise ValueError("funds 需要 country")
            frame = lib.get_fund_historical_data(
                fund=investing_name,
                country=country_norm,
                from_date=from_date,
                to_date=to_date,
                interval=investpy_interval,
            )
        elif product_key == "currency_crosses":
            frame = lib.get_currency_cross_historical_data(
                currency_cross=investing_name,
                from_date=from_date,
                to_date=to_date,
                interval=investpy_interval,
            )
        else:
            raise ValueError(f"暂未封装 product={product_key}")
    except (ValueError, RateLimitError):
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_investing_error(exc, context=f"{product_key}:{investing_name}")

    bars = _frame_to_bars(frame)
    if not bars:
        raise ValueError(
            f"No Investing history for {product_key} name={investing_name!r} "
            f"country={country_norm!r} ({start_ymd}..{end_ymd})"
        )

    # 对外 interval 仍用入参规范化小写（1d/1wk/1mo）
    out_interval = (interval or "1d").strip().lower()
    if out_interval in {"daily"}:
        out_interval = "1d"
    elif out_interval in {"weekly", "1w"}:
        out_interval = "1wk"
    elif out_interval in {"monthly"}:
        out_interval = "1mo"

    return HistoryResponse(
        symbol=investing_name,
        interval=out_interval,
        period=None if use_absolute else normalized_period,
        start=start_ymd if use_absolute else None,
        end=end_ymd if use_absolute else None,
        quote_type=_PRODUCT_QUOTE_TYPE.get(product_key),
        count=len(bars),
        bars=bars,
    )


def _mapping_path() -> Path:
    return get_config_dir() / "investing_symbol_map.yaml"


@lru_cache(maxsize=1)
def _load_symbol_map() -> dict[str, dict[str, Any]]:
    path = _mapping_path()
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mappings = raw.get("mappings") if isinstance(raw, dict) else None
    if not isinstance(mappings, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in mappings.items():
        if not isinstance(value, dict):
            continue
        out[str(key).strip().upper()] = value
    return out


def reload_symbol_map() -> None:
    _load_symbol_map.cache_clear()


def list_yahoo_investing_mappings() -> list[dict[str, Any]]:
    """列出 Yahoo→Investing 映射（供排查/前端展示）。"""
    items: list[dict[str, Any]] = []
    for yahoo, meta in sorted(_load_symbol_map().items()):
        items.append(
            {
                "yahooSymbol": yahoo,
                "enabled": meta.get("enabled", True) is not False,
                "product": meta.get("product"),
                "country": meta.get("country"),
                "investingId": meta.get("investing_id"),
                "investingSymbol": meta.get("investing_symbol"),
                "investingName": meta.get("investing_name"),
                "tag": meta.get("tag"),
                "quoteType": meta.get("quote_type"),
                "note": meta.get("note"),
            }
        )
    return items


def resolve_yahoo_investing_mapping(symbol: str) -> dict[str, Any]:
    ticker = (symbol or "").strip().upper()
    if not ticker:
        raise ValueError("symbol cannot be empty")
    meta = _load_symbol_map().get(ticker)
    if meta is None:
        raise ValueError(
            f"Yahoo 符号 '{ticker}' 未配置 Investing 映射。"
            f"请编辑 {_mapping_path()}"
        )
    if meta.get("enabled", True) is False:
        note = meta.get("note") or "映射未启用"
        raise ValueError(f"Yahoo 符号 '{ticker}' 的 Investing 映射已禁用：{note}")
    product = str(meta.get("product") or "").strip().lower()
    name = str(meta.get("investing_name") or meta.get("investing_symbol") or "").strip()
    if not product or not name:
        raise ValueError(f"映射不完整：{ticker} 缺少 product/investing_name")
    return meta


def _fetch_via_search_obj(
    meta: dict[str, Any],
    *,
    from_date: str,
    to_date: str,
    investpy_interval: str,
) -> Any | None:
    """若映射含 id/tag，尝试 SearchObj 拉历史；失败返回 None 以回退原生 get_*。"""
    investing_id = meta.get("investing_id")
    tag = meta.get("tag")
    if investing_id is None or not tag:
        return None

    lib = _import_investpy()
    try:
        from investpy.utils.search_obj import SearchObj  # type: ignore
    except Exception:
        try:
            from investingpy.utils.search_obj import SearchObj  # type: ignore
        except Exception:
            return None

    product = str(meta.get("product") or "stocks")
    pair_type = product if product != "cryptos" else "cryptos"
    # investpy pair_type 常见：stocks/indices/etfs/funds/commodities/currencies/cryptos/bonds
    obj = SearchObj(
        id_=int(investing_id),
        name=str(meta.get("investing_name") or meta.get("investing_symbol") or ""),
        symbol=str(meta.get("investing_symbol") or ""),
        tag=str(tag),
        country=str(meta.get("country") or ""),
        pair_type=pair_type,
        exchange=str(meta.get("exchange") or ""),
    )
    try:
        # 部分版本 retrieve_historical_data 无 interval 参数
        try:
            return obj.retrieve_historical_data(
                from_date=from_date,
                to_date=to_date,
                interval=investpy_interval,
            )
        except TypeError:
            return obj.retrieve_historical_data(from_date=from_date, to_date=to_date)
    except Exception:  # noqa: BLE001
        return None


def get_investing_history_by_yahoo(
    symbol: str,
    *,
    period: str | None = None,
    start: str | None = None,
    end: str | None = None,
    interval: str = "1d",
) -> HistoryResponse:
    """用 Yahoo 符号（经映射表）拉 Investing OHLCV，响应形态对齐 get_market_history。"""
    ticker = (symbol or "").strip().upper()
    meta = resolve_yahoo_investing_mapping(ticker)

    investpy_interval = _normalize_interval(interval)
    start_ymd, end_ymd, normalized_period, use_absolute = _resolve_range(
        period=period, start=start, end=end
    )
    from_date = _ymd_to_investpy(start_ymd)
    to_date = _ymd_to_investpy(end_ymd)

    frame = _fetch_via_search_obj(
        meta,
        from_date=from_date,
        to_date=to_date,
        investpy_interval=investpy_interval,
    )

    if frame is None or getattr(frame, "empty", True):
        # 股票 get_stock_historical_data 的 stock 参数通常是 symbol（AAPL），不是全名
        product = str(meta["product"]).strip().lower()
        if product == "stocks":
            name_for_api = str(meta.get("investing_symbol") or meta.get("investing_name"))
        else:
            name_for_api = str(meta.get("investing_name") or meta.get("investing_symbol"))

        response = get_investing_history_native(
            product=product,
            name=name_for_api,
            country=meta.get("country"),
            start=start_ymd,
            end=end_ymd,
            interval=interval,
            stock_exchange=meta.get("exchange"),
        )
        return HistoryResponse(
            symbol=ticker,
            interval=response.interval,
            period=None if use_absolute else normalized_period,
            start=start_ymd if use_absolute else None,
            end=end_ymd if use_absolute else None,
            quote_type=(
                str(meta.get("quote_type") or response.quote_type or "").upper() or None
            ),
            count=response.count,
            bars=response.bars,
        )

    bars = _frame_to_bars(frame)
    if not bars:
        raise ValueError(f"No Investing history for Yahoo symbol '{ticker}'")

    out_interval = (interval or "1d").strip().lower()
    if out_interval in {"daily"}:
        out_interval = "1d"
    elif out_interval in {"weekly", "1w"}:
        out_interval = "1wk"
    elif out_interval in {"monthly"}:
        out_interval = "1mo"

    return HistoryResponse(
        symbol=ticker,
        interval=out_interval,
        period=None if use_absolute else normalized_period,
        start=start_ymd if use_absolute else None,
        end=end_ymd if use_absolute else None,
        quote_type=str(meta.get("quote_type") or _PRODUCT_QUOTE_TYPE.get(str(meta.get("product")), "")).upper()
        or None,
        count=len(bars),
        bars=bars,
    )
