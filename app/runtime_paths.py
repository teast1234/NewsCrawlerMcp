"""开发 / PyInstaller 打包环境下的资源路径解析。"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def get_resource_root() -> Path:
    """打包后返回 `_MEIPASS`；开发时返回项目根目录。"""
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1]


def get_exe_dir() -> Path | None:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def get_config_dir() -> Path:
    """
    配置目录查找顺序：
    1. exe 同级的 config/（便于用户改 yaml，优先）
    2. 打包进 exe 的 `_MEIPASS/config/`
    3. 开发环境的项目根 config/
    """
    exe_dir = get_exe_dir()
    if exe_dir is not None:
        beside_exe = exe_dir / "config"
        if beside_exe.is_dir():
            return beside_exe
        bundled = get_resource_root() / "config"
        if bundled.is_dir():
            return bundled
        return beside_exe

    return get_resource_root() / "config"
