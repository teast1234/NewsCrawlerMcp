"""从 config/app.yaml 加载并应用 HTTP(S) 代理（对齐 Java poe.proxy）。"""

from __future__ import annotations

import os
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from app.runtime_paths import get_config_dir

_APPLIED = False
_LAST_STATUS: str = "not applied"


def _log(message: str) -> None:
    # MCP stdio 占用 stdout，运行日志统一走 stderr
    print(message, file=sys.stderr)


def ensure_local_app_config() -> Path | None:
    """
    若缺少本机 app.yaml，则从 app.example.yaml 复制一份。
    返回最终应使用的 app.yaml 路径（可能仍不存在）。
    """
    config_dir = get_config_dir()
    app_yaml = config_dir / "app.yaml"
    example_yaml = config_dir / "app.example.yaml"

    if app_yaml.is_file():
        return app_yaml

    if example_yaml.is_file():
        try:
            shutil.copyfile(example_yaml, app_yaml)
            _log(f"[runtime] 已从样本生成本机配置: {app_yaml}")
            _log("[runtime] 请按需编辑 proxy 后重启")
            return app_yaml
        except OSError as exc:
            _log(f"[runtime] 无法生成 app.yaml: {exc}")
            return None

    return None


def _load_proxy_settings() -> dict[str, Any]:
    ensure_local_app_config()
    config_path = get_config_dir() / "app.yaml"
    if not config_path.is_file():
        return {"enabled": False}

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"enabled": False}

    if not isinstance(raw, dict):
        return {"enabled": False}

    proxy = raw.get("proxy")
    if not isinstance(proxy, dict):
        return {"enabled": False}
    return proxy


def get_proxy_status() -> str:
    return _LAST_STATUS


def apply_proxy_settings(*, force: bool = False) -> str:
    """
    安装全局 urllib 代理，并设置 HTTP_PROXY / HTTPS_PROXY。
    幂等：默认只应用一次。
    """
    global _APPLIED, _LAST_STATUS
    if _APPLIED and not force:
        return _LAST_STATUS

    settings = _load_proxy_settings()
    enabled = bool(settings.get("enabled"))
    host = str(settings.get("host") or "").strip()
    port = settings.get("port")

    if not enabled:
        _APPLIED = True
        _LAST_STATUS = "proxy disabled"
        return _LAST_STATUS

    if not host or port is None:
        _APPLIED = True
        _LAST_STATUS = "proxy enabled but host/port missing"
        return _LAST_STATUS

    try:
        port_num = int(port)
    except (TypeError, ValueError):
        _APPLIED = True
        _LAST_STATUS = f"proxy invalid port: {port!r}"
        return _LAST_STATUS

    proxy_url = f"http://{host}:{port_num}"
    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url
    os.environ["http_proxy"] = proxy_url
    os.environ["https_proxy"] = proxy_url

    # 本机服务不要走代理
    existing_no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    local_hosts = "127.0.0.1,localhost,::1"
    merged = ",".join(
        part for part in [existing_no_proxy.strip(), local_hosts] if part
    )
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged

    handler = urllib.request.ProxyHandler(
        {
            "http": proxy_url,
            "https": proxy_url,
        }
    )
    opener = urllib.request.build_opener(handler)
    urllib.request.install_opener(opener)

    _APPLIED = True
    _LAST_STATUS = f"proxy enabled -> {proxy_url}"
    return _LAST_STATUS
