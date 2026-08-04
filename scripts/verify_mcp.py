"""验证新闻 + 行情 MCP：stdio 握手 + 列出 tools + 调用轻量工具。

用法（在项目根目录）:
  python scripts/verify_mcp.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _text(result) -> str:
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _parse(result) -> dict:
    raw = _text(result)
    try:
        data = json.loads(raw) if raw.strip().startswith("{") else {"raw": raw}
    except json.JSONDecodeError:
        data = {"raw": raw[:500]}
    if not isinstance(data, dict):
        return {"ok": False, "error": "invalid_payload", "message": str(data)[:200]}
    return data


async def _run() -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app"],
        cwd=str(ROOT),
        env=None,
    )

    print(f"[verify] starting: {sys.executable} -m app", file=sys.stderr)
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("[verify] tools:", ", ".join(names))

            expected = {
                "search_news",
                "get_cnbc_news",
                "get_rss_news",
                "list_rss_categories",
                "list_rss_sources",
                "get_cnyes_news",
                "list_cnyes_categories",
                "fetch_article",
                "get_market_history",
                "get_investing_history",
                "get_investing_history_native",
                "list_investing_mappings",
            }
            missing = expected - set(names)
            if missing:
                print(f"[verify] FAIL missing tools: {sorted(missing)}", file=sys.stderr)
                return 1

            checks = [
                ("list_rss_categories", {}),
                ("list_cnyes_categories", {}),
                ("get_cnyes_news", {"count": 2, "category": "headline"}),
                ("get_market_history", {"symbol": "AAPL", "period": "5d", "interval": "1d"}),
                ("list_investing_mappings", {}),
            ]
            for label, args in checks:
                result = await session.call_tool(label, arguments=args)
                data = _parse(result)
                if data.get("ok") is False:
                    print(f"[verify] FAIL {label}: {data}", file=sys.stderr)
                    return 1
                if label == "get_market_history":
                    print(
                        f"[verify] {label}: symbol={data.get('symbol')} "
                        f"count={data.get('count')}"
                    )
                    if not data.get("count"):
                        print(f"[verify] FAIL {label}: empty bars", file=sys.stderr)
                        return 1
                elif label == "list_investing_mappings":
                    print(f"[verify] {label}: count={data.get('count')}")
                elif label == "get_cnyes_news":
                    print(f"[verify] {label}: count={data.get('count')}")
                elif label == "list_rss_categories":
                    print(f"[verify] {label}: categories={data.get('count')}")
                else:
                    cats = data.get("categories") or []
                    print(f"[verify] {label}: categories={len(cats)}")

            # Investing 线上常被风控；映射表可用即可，线上失败只告警不阻断
            inv = await session.call_tool(
                "get_investing_history",
                arguments={"symbol": "AAPL", "period": "1mo", "interval": "1d"},
            )
            inv_data = _parse(inv)
            if inv_data.get("ok") is False:
                print(
                    f"[verify] WARN get_investing_history unavailable: "
                    f"{inv_data.get('message')}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[verify] get_investing_history: "
                    f"symbol={inv_data.get('symbol')} count={inv_data.get('count')}"
                )

            print("[verify] OK")
            return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
