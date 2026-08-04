# 启动财经新闻 MCP（stdio）。供 Cursor / Claude Desktop / Java Spring AI 拉起。
# 用法: .\scripts\start_news_mcp.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (Test-Path "$Root\.venv\Scripts\python.exe") {
    $Python = "$Root\.venv\Scripts\python.exe"
} else {
    $Python = "python"
}

Write-Host "[news-mcp] root=$Root"
Write-Host "[news-mcp] python=$Python"
& $Python -m app
