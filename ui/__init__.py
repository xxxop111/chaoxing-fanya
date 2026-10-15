# -*- coding: utf-8 -*-
"""桌面层：把已有的 Flask 后端与「新的网页前端」装进一个原生窗口。

分层（**界面用网页技术写，窗口是原生的**）：

* ``ui.backend`` —— 在本进程后台线程内拉起 Flask（仅 127.0.0.1，端口自动挑），
  并提供本地 API 客户端；同时由它把 ``web/dist`` 作为静态资源对外提供。
* ``ui.config``  —— 桌面版专属配置（窗口尺寸/托盘/定时/浏览器通道），
  与 Web 版共享的 ``web_config.json`` 分开存放，互不覆盖。
* 前端          —— 由设计侧产出（HTML/CSS/JS 或任意框架构建产物），
  只要把构建结果放进 ``web/dist``，改前端**不需要动后端**。
* 窗口壳        —— ``desktop_app.py``：pywebview/WebView2 原生窗口加载
  ``service.base_url``；缺 WebView2 时回退到 Edge/Chrome 的 ``--app`` 窗口
  （那是兜底，不是目标形态）。

对外契约见 ``docs/FRONTEND_API.md``：那份文档是前端与后端之间**唯一**的接口约定，
换前端时只依赖它。界面层不含任何超星协议代码 —— 协议都在 ``api/`` 与 ``app.py`` 里，
Web 版、桌面版、CLI 共用同一份实现，避免行为漂移。
"""

__all__ = ["backend", "config"]
