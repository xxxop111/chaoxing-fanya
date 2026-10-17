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
    """单文件自包含：不能有 src=/href= 指向外部资源，否则离线打开就白屏。

    ``data:`` 内联 URI 不算外部资源（站点图标就是内联的，不额外带文件）。
    """
    refs = [m for m in re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", html)
            if m and not m.startswith("data:")]
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

def test_preview_surfaces_below_threshold(html):
    """低分（未达标）必须在盘点里看得见。

    任务点「未完成」和「已交但没达标」是两件不同的事：后者（AI实践 < aicoachScore、
    作业 < workScore）平台不会算完成，需要重做。前端要给出汇总格、状态标签与
    任务级计数，否则用户只能看到「未完成」，不知道低分还要处理。
    """
    assert "belowThresholdCount" in html, "汇总里没有未达标计数"
    assert "belowThreshold" in html, "状态标签没有用未达标标记"
    assert "未达标" in html, "界面上看不到「未达标」字样"
    assert "expiredCount" in html, "窗口已过的计数被改掉了"


def test_preview_surfaces_submit_allowance(html):
    """作业「还能提交几次」也要看得见（只有一次机会的作业不能显示成随便交）。

    数据来自作答页的 ``limitWorkSubmitTimes``/``addTimes``（见 docs §6.2.3）：
    后端把 ``submitRemaining`` 放进盘点结果，前端据此给出「不可重交」/「可重交 N 次」。
    """
    assert "submitRemaining" in html, "前端没有读盘点的 submitRemaining"
    assert "不可重交" in html and "可重交" in html, "作业的提交次数没有做成标签"


def test_expired_items_are_collapsed_by_default(html):
    """窗口已过的条目默认收起。

    窗口一过平台就不让进了（补不了），把它们和能做的任务点混在一起，用户只看到
    一片「窗口已过」。默认收起、工具栏给开关展开，计数仍在汇总和任务头上。
    """
    assert 'id="preview-show-expired"' in html, "找不到「显示窗口已过」开关"
    assert "showExpired" in html, "前端没有记住开关状态"
    assert "项已收起" in html, "全部收起后没有给出提示"
    assert "it.expired" in html, "收起逻辑没有按 expired 判定"


def test_finished_items_are_collapsed_until_redo_is_on(html):
    """已完成的条目也不该默认摆成「可勾选项」。

    真实反馈（2026-10-16）：数学建模里 9 个已完成项被预勾选（status 是 auto/partial
    就算可勾），点执行后 runner 按 ``is_finish`` 丢弃它们，日志只剩
    「待完成 0 个」+「成功 0 / 失败 0 / 跳过 0」——用户以为工具没检查出过期。
    现在：已完成项由「包含已完成」（``#preview-redo``，本来就是这个意思）开关展开。
    """
    assert "showFinished" in html, "前端没有按「包含已完成」开关决定是否列已完成项"
    assert "it.isFinish" in html, "收起逻辑没有按 isFinish 判定"
    assert 'id="preview-redo"' in html and '$("#preview-redo").addEventListener' in html, \
        "开关变了不重渲染，收起状态不会跟着变"


def test_ai_direct_submit_is_a_switch_that_reaches_the_runner(html):
    """「AI实践直接提交」必须是界面上的开关，不能只会改配置文件。

    以前 execute 把 ``allow_ai_submit`` 写死成 false，用户想交卷只能去改
    tiku 配置（还得知道键名）。现在工具栏有开关，值直接进 ``options``；
    而且它写平台、部分不可撤销，所以默认关闭并记在本机。
    """
    assert 'id="preview-ai-submit"' in html, "找不到「AI实践直接提交」开关"
    assert "AI实践直接提交" in html, "开关没有可见文案"
    assert 'allow_ai_submit: $("#preview-ai-submit").checked' in html, \
        "开关没有接到执行请求的 options.allow_ai_submit 上"
    assert "od-cx-ai-submit" in html, "开关状态没有存在本机（刷新会丢）"
    assert "已完成 " in html and "项已收起" in html, "全部收起后没有给出提示"


def test_settings_page_shows_why_ocr_is_off(html):
    """14 分事故之后：OCR 到底有没有生效，必须能在设置页直接看见。

    以前的 OCR 配置是「填了表单就算配了」——用户把模型名 ``deepseek-flash``
    写进「密钥」、接口地址留空，界面上一切正常，等到交卷时才发现整卷是瞎猜的。
    """
    assert 'data-od-id="settings-ocr-status"' in html, "设置页没有 OCR 状态那一行"
    assert 'id="ocr-status"' in html, "OCR 状态没有可以写入的元素"
    assert "renderOcrStatus" in html, "OCR 状态没有渲染逻辑"
    assert "未启用：题干是公式图片的题会一律留空" in html, "OCR 状态没有说清楚后果"
    assert "DeepSeek" in html and "读不了公式图" in html, "没有说明为什么 DeepSeek 不行"


def test_brand_mark_matches_the_windows_icon(html):
    """侧栏 logo 与 Windows 图标（chaoxing.ico）用同一套造型：蓝渐变圆角块 + 白「学」。

    2026-10-16 起统一：此前侧栏是线条书页 SVG、任务管理器里是蓝底白三角，两套视觉。
    """
    assert '<span class="brand-mark" aria-hidden="true">学</span>' in html, \
        "侧栏 logo 还是旧造型"
    assert "linear-gradient(180deg, #2563EB" in html, "侧栏 logo 不是图标那套蓝渐变"
    assert 'rel="icon"' in html and "%E5%AD%A6" in html, "浏览器标签页没有同款站点图标"


def test_app_icon_carries_every_windows_size():
    """``chaoxing.ico`` 是 exe / 任务管理器 / 任务栏 / 托盘 / 快捷方式 / 安装包共用的那一个。

    少了 16/32 这些小尺寸，Windows 会把大图硬缩，任务栏和任务管理器里就糊了。
    """
    Image = pytest.importorskip("PIL.Image")
    ico = os.path.join(ROOT, "chaoxing.ico")
    assert os.path.exists(ico), "根目录的 chaoxing.ico 丢了（spec/iss/desktop_app 都指着它）"
    with Image.open(ico) as im:
        sizes = set(im.info.get("sizes", []))
    missing = {(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)} - sizes
    assert not missing, f"chaoxing.ico 缺尺寸：{sorted(missing)}"


