# -*- coding: utf-8 -*-
"""出货界面（opendesign 单文件）与后端之间的契约。

界面由外部设计工具产出、本仓库只负责端出来，所以最容易悄悄坏掉的就是「两边对不上」：
前端调了一个后端没有的路径、或者把主机名端口写死（打包版端口是动态选的）、
或者依赖了外部资源（离线打开就白屏）。这些都必须在这里钉死。

被检查的文件就是打包时同步成 ``web/dist/index.html`` 的那一份。
"""

from __future__ import annotations

import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as web_app  # noqa: E402
import desktop_app  # noqa: E402

FRONTEND = os.path.join(ROOT, "web", "opendesign", "chaoxing-assistant.html")
DIST_INDEX = os.path.join(ROOT, "web", "dist", "index.html")

pytestmark = pytest.mark.skipif(
    not os.path.exists(FRONTEND), reason="opendesign 界面缺失（web/opendesign/chaoxing-assistant.html）")


@pytest.fixture(scope="module")
def html() -> str:
    with open(FRONTEND, encoding="utf-8") as fp:
        return fp.read()


def _api_paths(source: str) -> set[str]:
    """抽出前端引用到的所有 ``/api/...`` 字面量（含 ``/api/task/`` 这种拼接前缀）。"""
    return set(re.findall(r"""["'](/api/[A-Za-z0-9_/-]*)["']""", source))


def _backend_paths() -> set[str]:
    rules = {r.rule for r in web_app.app.url_map.iter_rules() if r.rule.startswith("/api")}
    # 这两个由 desktop_app 在运行时注册（窗口活着吗）
    rules |= {"/api/heartbeat", "/api/window-closed"}
    return rules


def _resolves(path: str, rules: set[str]) -> bool:
    if path in rules:
        return True
    return any(rule.startswith(path) and rule[len(path):].startswith("<") for rule in rules)


def test_every_frontend_api_path_exists_in_backend(html):
    rules = _backend_paths()
    missing = sorted(p for p in _api_paths(html) if not _resolves(p, rules))
    assert missing == [], f"前端调用了后端没有的路径：{missing}"


def test_frontend_covers_the_core_flows(html):
    """核心流程不能少：登录/课程、盘点预览、执行、待审批、设置、日志、心跳。"""
    for path in ("/api/login", "/api/courses", "/api/fanya/preview", "/api/start",
                 "/api/fanya/approvals", "/api/fanya/approve", "/api/config",
                 "/api/ai/test", "/api/logs/", "/api/heartbeat", "/api/window-closed"):
        assert path in html, f"界面里找不到 {path} 的调用"


def test_no_hardcoded_host_or_port(html):
    """打包版端口是动态选的 —— 界面绝不能写死主机或端口。"""
    assert "http://localhost" not in html
    assert "http://127.0.0.1" not in html
    assert ":5000" not in html


def test_single_file_has_no_external_assets(html):
    """单文件自包含：不能有 src=/href= 指向外部资源，否则离线打开就白屏。"""
    refs = [m for m in re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", html) if m]
    assert refs == [], f"界面引用了外部资源：{refs}"


def test_demo_mode_is_kept(html):
    """没有后端时要进演示模式（评审靠它看界面），别被顺手删掉。"""
    assert "data-od-id" in html
    assert "演示" in html


def test_desktop_bridge_matches_shell_api(html):
    """界面通过 pywebview 桥开外链，方法名必须与桌面壳暴露的一致。"""
    assert "window.pywebview.api.open_external" in html
    assert hasattr(desktop_app.DesktopBridge, "open_external")


def test_window_close_beacon_present(html):
    """浏览器兜底模式靠 heartbeat + sendBeacon 判定窗口关闭。"""
    assert "sendBeacon" in html
    assert "/api/window-closed" in html


@pytest.mark.skipif(not os.path.exists(DIST_INDEX), reason="web/dist 未同步")
def test_dist_matches_source(html):
    """dist 是同步产物：内容应与源文件完全一致（打包前必须同步过）。"""
    with open(DIST_INDEX, encoding="utf-8") as fp:
        assert fp.read() == html


@pytest.mark.skipif(not os.path.exists(DIST_INDEX), reason="web/dist 未同步")
def test_dist_is_single_file():
    """同步后 dist 里只剩一个 index.html（旧 React 产物应被清掉）。"""
    dist = os.path.dirname(DIST_INDEX)
    assert sorted(os.listdir(dist)) == ["index.html"]


def test_execute_sends_concurrency_option(html):
    """界面上的「并发任务点」必须真的发到后端，否则输入框只是个摆设。"""
    assert 'id="fanya-concurrency"' in html, "找不到并发任务点输入框"
    block = re.search(r'/api/fanya/execute"[^;]*?options:\s*\{([^}]*)\}', html, re.S)
    assert block, "解析不到 /api/fanya/execute 的 options"
    assert "concurrency" in block.group(1), "执行时没有把 concurrency 传给后端"


def test_execute_sends_tiku_config(html):
    """执行任务点也必须带上 AI 接口配置。

    只带 options 不带 tiku_config 时，AI实践（上传作品）和主题讨论会静默
    降级成模板文本甚至直接失败 —— 用户明明在设置页配好了 AI 接口。
    """
    block = re.search(r'/api/fanya/execute"[^;]*?body:\s*\{(.*?)\}\s*\}\)',
                      html, re.S)
    assert block, "解析不到 /api/fanya/execute 的 body"
    assert "tiku_config" in block.group(1), "执行时没有把 tiku_config 传给后端"
