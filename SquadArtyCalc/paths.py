# -*- coding: utf-8 -*-
"""路径解析：源码运行与 PyInstaller 单文件（onefile）打包都适用。

- resource_dir(): 只读资源（data/ 里的地图与武器表）。打包后指向解包临时目录 sys._MEIPASS。
- app_dir():      程序所在目录。打包后是 exe 所在目录；源码运行时是 main.py 所在目录。
- config_path():  用户配置文件。优先放在程序目录（便携/绿色版），目录不可写
                  （例如放在 Program Files）时退回 %LOCALAPPDATA%\\SquadArtyCalc\\config.json。
- log_path():     运行日志。单文件 + 无控制台时 stdout/stderr 和崩溃堆栈都写这里。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CONFIG_NAME = "config.json"
LOG_NAME = "run.log"
FALLBACK_DIR_NAME = "SquadArtyCalc"


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """只读资源目录（data/ 的父目录）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent


def app_dir() -> Path:
    """exe（或 main.py）所在目录。"""
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".dsh_write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def user_dir() -> Path:
    """可写的用户目录：优先程序目录，失败退回 LOCALAPPDATA。"""
    d = app_dir()
    if _writable(d):
        return d
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    d2 = Path(base) / FALLBACK_DIR_NAME
    d2.mkdir(parents=True, exist_ok=True)
    return d2


def config_path() -> Path:
    return user_dir() / CONFIG_NAME


def log_path() -> Path:
    return user_dir() / LOG_NAME
