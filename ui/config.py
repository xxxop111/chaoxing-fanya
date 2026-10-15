# -*- coding: utf-8 -*-
"""桌面版专属配置（与 Web 版共享的 ``web_config.json`` 分开存放）。

为什么分开
----------
``web_config.json`` 是 Web 界面在读写的，字段语义跟着前端走；桌面版的
窗口位置、托盘行为、定时任务这些**只对桌面版有意义**，混进去只会让
两边互相覆盖、越改越乱。

真正共享的配置（登录信息、AI 接口、课程选择、学习参数）仍然读写
``web_config.json``，这样 Web 版和桌面版看到的是同一份设置。
"""

from __future__ import annotations

import json
import os
from typing import Any

from api.paths import app_path

__all__ = ["DesktopConfig", "DEFAULTS", "CONFIG_PATH"]

CONFIG_PATH = app_path("desktop_config.json")

DEFAULTS: dict = {
    # 关闭窗口时：收进托盘继续跑 / 直接退出
    "close_action": "tray",
    "start_minimized": False,
    "autostart": False,
    # 文档类「累计阅读时长」任务点只能靠真实浏览器；默认隐藏运行（不弹窗）
    "browser_headless": True,
    # 定时自动跑（本地时间；scope: study / fanya / all）
    "schedule": {
        "enabled": False,
        "time": "08:30",
        "scope": "all",
        "approval": True,
        "last_run": "",
    },
    # 一键全自动的默认动作
    "auto": {
        "run_study": True,
        "run_fanya": True,
        "require_approval": True,
        "redo": False,
    },
    "window": {"width": 1220, "height": 840},
    "last_page": 0,
}


def _merge(defaults: dict, data: dict) -> dict:
    """浅层递归合并：缺的键补默认值，多的键保留（向前兼容）。"""
    out = dict(defaults)
    for key, value in (data or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class DesktopConfig:
    """``desktop_config.json`` 的读写（``get("schedule.time")`` 这样的点号路径）。"""

    def __init__(self, path: str = CONFIG_PATH) -> None:
        self.path = path
        self.data: dict = dict(DEFAULTS)
        self.load()

    # -- 读写 ---------------------------------------------------------
    def load(self) -> dict:
        if not os.path.exists(self.path):
            self.data = json.loads(json.dumps(DEFAULTS))
            return self.data
        try:
            # utf-8-sig：容忍 PowerShell 写出的 BOM，否则整份配置读不出来
            with open(self.path, "r", encoding="utf-8-sig") as fp:
                self.data = _merge(DEFAULTS, json.load(fp))
        except Exception:  # noqa: BLE001 - 配置坏了也要能启动
            self.data = json.loads(json.dumps(DEFAULTS))
        return self.data

    def save(self) -> bool:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self.data, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False

    # -- 取值 ---------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any, *, save: bool = True) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value
        if save:
            self.save()
