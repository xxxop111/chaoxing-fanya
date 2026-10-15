# -*- coding: utf-8 -*-
"""静态资源与 SPA 兜底的行为契约。

前端由外部设计产出（最终只替换 ``web/dist``），所以「后端怎么把这份前端端出来」
是一条必须钉死的契约：

* ``/`` 回 ``index.html``；
* 客户端路由（``/tasks`` 之类、浏览器导航带 ``Accept: text/html``）兜底回 ``index.html``；
* 缺失的静态资源（``.js`` / ``.css`` / 图片）如实 404，不能回 200 + HTML；
* ``/api/`` 命名空间下任何未注册路径都是 JSON 404（前端按 ``status`` 字段判断）。

这些用例之所以存在：``Flask(static_folder=..., static_url_path='')`` 会自动注册一条
``/<path:filename>`` 静态路由，它比 ``app.serve_static`` 更早注册且形状相同，
曾让客户端路由直接吃到 Flask 的 HTML 404 页（打包版实测如此）。
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as web_app  # noqa: E402

HAVE_DIST = os.path.exists(web_app.STATIC_DIR)
needs_dist = pytest.mark.skipif(not HAVE_DIST, reason="web/dist 未构建")


def _index_bytes() -> bytes:
    """``web/dist/index.html`` 的原字节。

    界面是外部设计产出、由 ``tools/build_desktop.py`` 原样拷进 ``web/dist`` 的单文件，
    所以这里断言「端出来的就是盘上那份」，而不是断言某个界面内部标记 ——
    界面换版（React 版曾用 ``id="root"``，opendesign 版用 ``<div class="app">``）
    不该让这条契约测试跟着改。打包件里是不是真界面，由 ``tools/smoke_desktop.py`` 判。
    """
    with open(os.path.join(web_app.STATIC_DIR, "index.html"), "rb") as fh:
        return fh.read()


@pytest.fixture()
def client():
    web_app.app.config.update(TESTING=True)
    return web_app.app.test_client()


@needs_dist
def test_root_serves_index(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["Content-Type"]
    assert resp.get_data() == _index_bytes()


@needs_dist
def test_client_route_falls_back_to_index(client):
    """/tasks 这种前端路由必须回 index.html，而不是 Flask 的 404 页。"""
    resp = client.get("/tasks", headers={"Accept": "text/html,application/xhtml+xml"})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["Content-Type"]
    assert resp.get_data() == _index_bytes()


@needs_dist
def test_nested_client_route_falls_back(client):
    resp = client.get("/courses/12345/detail", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert resp.get_data() == _index_bytes()


@needs_dist
def test_client_route_with_json_accept_is_404(client):
    """fetch() 默认 Accept 不是 text/html，此时不该兜底成页面。"""
    resp = client.get("/tasks", headers={"Accept": "application/json"})
    assert resp.status_code == 404
    assert resp.get_json()["status"] is False


@needs_dist
def test_missing_asset_is_json_404(client):
    """缺 .js 要点名 404 —— 回 HTML 会让浏览器报 MIME 错，掩盖真正的 404。"""
    resp = client.get("/assets/does-not-exist.js", headers={"Accept": "*/*"})
    assert resp.status_code == 404
    assert "application/json" in resp.headers["Content-Type"]
    assert resp.get_json()["status"] is False


@needs_dist
def test_existing_asset_is_served(client):
    resp = client.get("/index.html")
    assert resp.status_code == 200
    assert resp.get_data() == _index_bytes()


def test_unknown_api_path_is_json_404(client):
    resp = client.get("/api/nope", headers={"Accept": "application/json"})
    assert resp.status_code == 404
    assert resp.get_json() == {"status": False, "msg": "Not Found"}


def test_unknown_api_path_under_html_accept_still_json(client):
    """整页导航到 /api/xxx 也只给 JSON，不给前端页面。"""
    resp = client.get("/api/nope", headers={"Accept": "text/html"})
    assert resp.status_code == 404
    assert "application/json" in resp.headers["Content-Type"]
