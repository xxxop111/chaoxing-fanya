# -*- coding: utf-8 -*-
"""文档浏览器通道的两条关键行为：**能被停止** 与 **滚到底之后会重开阅读页**。

为什么必须钉住这两条（都是真机上踩出来的）：

1. 阅读器滚到底之后服务端就不再计入时长 —— 实测原地空转 160 秒 Δ0，
   一个要求 10 分钟的文档会一直空转到 1800 秒预算耗尽。重开阅读页会开始
   新的阅读会话、触发下一段结算（实测每轮 +3~5 秒），是不伪造上报前提下
   唯一还能推进的做法。
2. 阅读中途点「停止」必须立刻收手，否则用户看到的「正在停止…」会被一个
   长文档卡住（界面上的停止只会在任务点之间生效）。

这里用假的 playwright 跑真实的分支逻辑，不启动浏览器。
"""

import sys
import types

import pytest

from api import fanya_browser


class _FakeFrame:
    url = "https://pan-yz.chaoxing.com/screen/v2/file_1"

    def evaluate(self, script):  # noqa: D401 - 记录滚动即可
        pass


class _FakeMouse:
    def wheel(self, x, y):
        pass


class _FakePage:
    def __init__(self):
        self.frames = []
        self.mouse = _FakeMouse()
        self.gotos = 0

    def goto(self, url, **kwargs):
        self.gotos += 1
        self.frames = [_FakeFrame()]

    def wait_for_timeout(self, ms):
        pass

    def title(self):
        return "文档学习 - 学习通"


class _FakeContext:
    def __init__(self, page):
        self.page = page

    def add_cookies(self, cookies):
        pass

    def new_page(self):
        return self.page


class _FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_context(self, **kwargs):
        return _FakeContext(self.page)

    def close(self):
        self.closed = True

    def is_connected(self):
        return not self.closed


class _FakeChromium:
    def __init__(self, browser):
        self.browser = browser

    def launch(self, **kwargs):
        return self.browser


class _FakePlaywright:
    def __init__(self, browser):
        self.chromium = _FakeChromium(browser)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def fake_browser(monkeypatch):
    """把 playwright 换成假的；返回 (page, browser) 供断言。"""
    page = _FakePage()
    browser = _FakeBrowser(page)
    stub = types.ModuleType("playwright.sync_api")
    stub.sync_playwright = lambda: _FakePlaywright(browser)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", stub)
    return page, browser


def _required():
    return 600_000  # 10 分钟


def test_reopens_reader_when_time_stops_growing(fake_browser):
    """滚到底之后不再入账 → 重开阅读页（而不是原地空转到预算耗尽）。"""
    page, browser = fake_browser
    state = {"opens": 0}

    def read_state():
        # 第一次会话只记到 570 秒；重开阅读页（第 2 次 goto）后才结算到达标
        if page.gotos >= 2:
            return (True, 600_000, 100)
        return (False, 570_000, 100)

    ok, message = fanya_browser.read_document_in_browser(
        session=None,
        learn_page_url="https://mooc1.chaoxing.com/document/learn?id=1",
        required_ms=_required(),
        read_state=read_state,
        poll_seconds=1.0,
        stall_rounds_limit=2,
        max_reopens=3,
    )

    assert ok is True, message
    assert "阅读完成" in message
    assert page.gotos == 2, "没有在时长停滞时重开阅读页"
    assert browser.closed


def test_stop_request_aborts_midway(fake_browser):
    """阅读中途收到停止请求要立刻收手，并如实说明任务点未完成。"""
    page, browser = fake_browser
    asked = {"n": 0}

    def should_stop():
        asked["n"] += 1
        return True

    ok, message = fanya_browser.read_document_in_browser(
        session=None,
        learn_page_url="https://mooc1.chaoxing.com/document/learn?id=1",
        required_ms=_required(),
        read_state=lambda: (False, 570_000, 100),
        poll_seconds=1.0,
        should_stop=should_stop,
    )

    assert ok is False
    assert "停止" in message and "未完成" in message
    assert asked["n"] == 1, "应该在进入每一轮循环时就检查停止请求"
    assert page.gotos == 1, "停止后不应再重开阅读页"
    assert browser.closed


def test_gives_up_after_max_reopens(fake_browser):
    """重开多次仍然不涨时，如实失败并写明重开了几次（不假称成功）。"""
    page, browser = fake_browser

    ok, message = fanya_browser.read_document_in_browser(
        session=None,
        learn_page_url="https://mooc1.chaoxing.com/document/learn?id=1",
        required_ms=_required(),
        read_state=lambda: (False, 570_000, 100),
        poll_seconds=1.0,
        stall_rounds_limit=2,
        max_reopens=2,
    )

    assert ok is False
    assert "重开阅读页 2 次" in message, message
    assert page.gotos == 3, f"应为 1 次首开 + 2 次重开，实际 {page.gotos}"
    assert browser.closed
