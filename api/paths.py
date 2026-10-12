# -*- coding: utf-8 -*-
"""便携版 / 打包版（PyInstaller）下的路径处理。

两种运行模式：

* **源码运行**：所有可写文件（`cookies.txt` / `cache.json` / `chaoxing.log` /
  `web_config.json`）就在项目根目录。
* **打包运行**：代码与静态资源被解压到临时目录（``sys._MEIPASS``，只读），
  而可写文件必须放在 **exe 所在目录**，否则重启就丢、甚至因只读而报错。
"""

from __future__ import annotations

import os
import sys


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的环境里。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> str:
    """可写数据目录：打包后为 exe 所在目录，否则为项目根目录。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bundle_dir() -> str:
    """只读资源目录：打包后为解压目录，否则与项目根目录相同。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", app_dir())
    return app_dir()


def app_path(*parts: str) -> str:
    """拼接一个位于可写目录下的路径。"""
    return os.path.join(app_dir(), *parts)


def bundle_path(*parts: str) -> str:
    """拼接一个位于只读资源目录下的路径。"""
    return os.path.join(bundle_dir(), *parts)
