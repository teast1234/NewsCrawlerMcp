import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

RSS_LOG = logging.getLogger("rss.fetch")


class RssSourceStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    EMPTY = "empty"
    SKIPPED = "skipped"


@dataclass
class RssSourceFetchResult:
    index: int
    total: int
    category: str
    publisher: str
    url: str
    status: RssSourceStatus
    elapsed_ms: float
    entry_count: int = 0
    matched_count: int = 0
    error: str | None = None


@dataclass
class _InflightSource:
    category: str
    publisher: str
    url: str
    start: float


def _format_elapsed(elapsed_ms: float) -> str:
    return f"耗时 {elapsed_ms / 1000:.1f}s"


def _format_source_result(
    *,
    status: RssSourceStatus,
    elapsed_ms: float,
    entry_count: int,
    matched_count: int,
    error: str | None,
) -> str:
    elapsed = _format_elapsed(elapsed_ms)

    if status == RssSourceStatus.OK:
        if entry_count <= 0:
            return f"{elapsed}，未抓取到"
        if matched_count <= 0:
            return f"{elapsed}，抓取到 {entry_count} 条，未找到"
        return f"{elapsed}，抓取到 {entry_count} 条，匹配 {matched_count} 条"

    if status == RssSourceStatus.TIMEOUT:
        return f"{elapsed}，请求超时"

    if status == RssSourceStatus.EMPTY:
        return f"{elapsed}，未抓取到"

    if error:
        return f"{elapsed}，抓取失败：{error}"
    return f"{elapsed}，抓取失败"


def _format_status_label(status: RssSourceStatus) -> str:
    return {
        RssSourceStatus.OK: "完成",
        RssSourceStatus.FAILED: "失败",
        RssSourceStatus.TIMEOUT: "超时",
        RssSourceStatus.EMPTY: "空源",
        RssSourceStatus.SKIPPED: "跳过",
    }[status]


@dataclass
class RssFetchTracer:
    total_sources: int
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _index_counter: int = field(default=0, repr=False)
    _inflight: dict[int, _InflightSource] = field(default_factory=dict, repr=False)
    results: list[RssSourceFetchResult] = field(default_factory=list)
    run_started_at: float = field(default_factory=time.perf_counter, repr=False)
    completed_sources: int = 0
    early_stop: bool = False

    def run_start(
        self,
        *,
        count: int,
        query: str | None,
        categories: list[str] | None,
        per_source_limit: int,
        since: str | None,
        until: str | None,
    ) -> None:
        category_label = "全部"
        if categories:
            category_label = "、".join(categories)

        RSS_LOG.info(
            "开始抓取 | 目标 %d 条 | 关键词 %s | 分类 %s | 每源 %d 条 | 时间 %s ~ %s | 共 %d 个源",
            count,
            query or "无",
            category_label,
            per_source_limit,
            since or "不限",
            until or "不限",
            self.total_sources,
        )

    def source_start(self, category: str, publisher: str, url: str) -> int:
        with self._lock:
            self._index_counter += 1
            index = self._index_counter

        now = time.perf_counter()
        with self._lock:
            self._inflight[index] = _InflightSource(
                category=category,
                publisher=publisher,
                url=url,
                start=now,
            )

        RSS_LOG.info(
            "开始 [%d/%d] %s | %s",
            index,
            self.total_sources,
            category,
            publisher,
        )
        return index

    def source_finish(
        self,
        index: int,
        *,
        status: RssSourceStatus,
        entry_count: int = 0,
        matched_count: int = 0,
        error: str | None = None,
    ) -> None:
        with self._lock:
            inflight = self._inflight.pop(index, None)
            self.completed_sources += 1

        if inflight is None:
            category = "?"
            publisher = "?"
            url = "?"
            elapsed_ms = 0.0
        else:
            category = inflight.category
            publisher = inflight.publisher
            url = inflight.url
            elapsed_ms = (time.perf_counter() - inflight.start) * 1000

        result = RssSourceFetchResult(
            index=index,
            total=self.total_sources,
            category=category,
            publisher=publisher,
            url=url,
            status=status,
            elapsed_ms=elapsed_ms,
            entry_count=entry_count,
            matched_count=matched_count,
            error=error,
        )
        with self._lock:
            self.results.append(result)

        label = _format_status_label(status)
        detail = _format_source_result(
            status=status,
            elapsed_ms=elapsed_ms,
            entry_count=entry_count,
            matched_count=matched_count,
            error=error,
        )
        RSS_LOG.info(
            "%s [%d/%d] %s | %s | %s",
            label,
            index,
            self.total_sources,
            category,
            publisher,
            detail,
        )

    def run_finish(
        self,
        *,
        returned: int,
        deduped: int,
        target_count: int,
        early_stop: bool,
    ) -> dict[str, Any]:
        self.early_stop = early_stop
        total_elapsed = time.perf_counter() - self.run_started_at
        summary = summarize_rss_fetch(self.results)
        summary.update(
            {
                "total_elapsed_s": round(total_elapsed, 2),
                "completed_sources": self.completed_sources,
                "scheduled_sources": self.total_sources,
                "deduped_articles": deduped,
                "returned_articles": returned,
                "target_count": target_count,
                "early_stop": early_stop,
            }
        )

        RSS_LOG.info(
            "抓取结束 | 总耗时 %.1fs | 完成 %d/%d 个源 | 成功 %d 失败 %d 超时 %d 空源 %d "
            "| 匹配 %d 条 去重 %d 条 返回 %d 条 目标 %d 条 | 提前结束：%s",
            total_elapsed,
            self.completed_sources,
            self.total_sources,
            summary["ok"],
            summary["failed"],
            summary["timeout"],
            summary["empty"],
            summary["matched_articles"],
            deduped,
            returned,
            target_count,
            "是" if early_stop else "否",
        )
        failures = [item for item in self.results if item.status != RssSourceStatus.OK]
        if failures:
            RSS_LOG.info("失败明细（%d 个源）：", len(failures))
            for item in failures:
                RSS_LOG.info(
                    "  [%d] %s | %s | %s | %s",
                    item.index,
                    item.category,
                    item.publisher,
                    _format_status_label(item.status),
                    _format_source_result(
                        status=item.status,
                        elapsed_ms=item.elapsed_ms,
                        entry_count=item.entry_count,
                        matched_count=item.matched_count,
                        error=item.error,
                    ),
                )

        return summary


def summarize_rss_fetch(results: list[RssSourceFetchResult]) -> dict[str, Any]:
    counts = {status.value: 0 for status in RssSourceStatus}
    matched_articles = 0
    for item in results:
        counts[item.status.value] = counts.get(item.status.value, 0) + 1
        matched_articles += item.matched_count

    return {
        "ok": counts.get("ok", 0),
        "failed": counts.get("failed", 0),
        "timeout": counts.get("timeout", 0),
        "empty": counts.get("empty", 0),
        "skipped": counts.get("skipped", 0),
        "matched_articles": matched_articles,
        "sources": [
            {
                "index": item.index,
                "category": item.category,
                "publisher": item.publisher,
                "url": item.url,
                "status": item.status.value,
                "elapsed_ms": round(item.elapsed_ms, 1),
                "entry_count": item.entry_count,
                "matched_count": item.matched_count,
                "error": item.error,
            }
            for item in sorted(results, key=lambda row: row.index)
        ],
    }


def setup_rss_monitor_logging() -> None:
    if RSS_LOG.handlers:
        RSS_LOG.setLevel(logging.INFO)
        return

    # MCP stdio 占用 stdout，日志必须走 stderr，否则会污染 JSON-RPC
    stream = sys.stderr
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [RSS] %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    RSS_LOG.addHandler(handler)
    RSS_LOG.setLevel(logging.INFO)
    RSS_LOG.propagate = False


def classify_fetch_error(error: str | None) -> RssSourceStatus:
    if not error:
        return RssSourceStatus.EMPTY
    lowered = error.casefold()
    if "timeout" in lowered or "timed out" in lowered:
        return RssSourceStatus.TIMEOUT
    return RssSourceStatus.FAILED


setup_rss_monitor_logging()
