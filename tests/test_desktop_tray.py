# -*- coding: utf-8 -*-
"""桌面外壳的可测契约：托盘常驻、原生桥、命令行开关。

托盘本身要点消息循环，自动化里起不来，所以这里只钉死「**决策**」——
关窗到底该缩到托盘还是真退出、页面调得到哪些原生能力、``--no-tray`` 是否生效。
真机上的观感由 ``tools/smoke_desktop.py`` 负责（它只验证能起来、能连上）。
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import desktop_app  # noqa: E402


class _FakeTray:
    def __init__(self, *, available: bool):
        self.available = available

        class _Ready:
            def wait(self, timeout=None):  # noqa: ARG002
                return True

        self.ready = _Ready()


# --- 关窗决策 ---------------------------------------------------------------


def test_no_tray_means_close_exits():
    """没有托盘（--no-tray / 复用窗口 / 非 Windows）→ 关窗就是退出。"""
    assert desktop_app._should_close_to_tray(None, {"quitting": False}) is False


def test_tray_catches_close_when_available():
    """托盘就绪 → 关窗被拦下，改成缩到托盘。"""
    tray = _FakeTray(available=True)
    assert desktop_app._should_close_to_tray(tray, {"quitting": False}) is True


def test_tray_failure_does_not_trap_user():
    """托盘创建失败 → 不能拦关闭，否则用户关不掉窗口。"""
    tray = _FakeTray(available=False)
    assert desktop_app._should_close_to_tray(tray, {"quitting": False}) is False


def test_quitting_short_circuits_tray():
    """托盘菜单点了「退出」→ 放行关闭（否则会自己把自己藏起来）。"""
    tray = _FakeTray(available=True)
    assert desktop_app._should_close_to_tray(tray, {"quitting": True}) is False


# --- 原生桥 -----------------------------------------------------------------


def test_bridge_rejects_non_http_links(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(desktop_app.webbrowser, "open", opened.append)
    bridge = desktop_app.DesktopBridge()
    assert bridge.open_external("javascript:alert(1)") is False
    assert bridge.open_external("file:///C:/Windows/System32") is False
    assert bridge.open_external("") is False
    assert opened == []


def test_bridge_opens_http_links_in_system_browser(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(desktop_app.webbrowser, "open", opened.append)
    bridge = desktop_app.DesktopBridge()
    assert bridge.open_external("  https://mooc1.chaoxing.com/visit  ") is True
    assert opened == ["https://mooc1.chaoxing.com/visit"]


def test_bridge_survives_browser_launch_failure(monkeypatch):
    def boom(_url):
        raise OSError("no browser")

    monkeypatch.setattr(desktop_app.webbrowser, "open", boom)
    assert desktop_app.DesktopBridge().open_external("https://example.com") is False


# --- 命令行开关 -------------------------------------------------------------


def _fake_main(monkeypatch, argv, tmp_path):
    captured: dict = {}

    class _Server:
        def shutdown(self):
            pass

    monkeypatch.setattr(desktop_app, "PID_PATH", str(tmp_path / "app.pid"))
    monkeypatch.setattr(desktop_app, "find_running_port", lambda ports, timeout=0.12: None)
    monkeypatch.setattr(desktop_app, "pick_port", lambda: 5000)
    monkeypatch.setattr(desktop_app, "_write_pid", lambda: None)
    monkeypatch.setattr(desktop_app, "_start_server", lambda port: _Server())
    monkeypatch.setattr(desktop_app, "_register_window_routes", lambda flask_app: {})
    monkeypatch.setattr(desktop_app, "_native_window_disabled", lambda: False)

    def fake_webview(url, server, *, close_to_tray=True):
        captured["url"] = url
        captured["close_to_tray"] = close_to_tray
        return 0

    monkeypatch.setattr(desktop_app, "_run_with_webview", fake_webview)
    monkeypatch.setattr(sys, "argv", ["desktop_app.py", *argv])
    rc = desktop_app.main()
    captured["rc"] = rc
    return captured


def test_tray_is_on_by_default(monkeypatch, tmp_path):
    got = _fake_main(monkeypatch, [], tmp_path)
    assert got["rc"] == 0
    assert got["close_to_tray"] is True


def test_no_tray_flag_disables_tray(monkeypatch, tmp_path):
    got = _fake_main(monkeypatch, ["--no-tray"], tmp_path)
    assert got["close_to_tray"] is False


@pytest.mark.parametrize("flag,expected", [("--stop", False), ("--stop --dialog", True)])
def test_stop_dialog_flag(monkeypatch, flag, expected):
    """--stop 默认静默；只有显式 --dialog 才允许弹系统模态框（那玩意会卡死调用方）。"""
    called: list[bool] = []
    monkeypatch.setattr(desktop_app, "stop_running",
                        lambda interactive=False: called.append(interactive) or 0)
    monkeypatch.setattr(sys, "argv", ["desktop_app.py", *flag.split()])
    desktop_app.main()
    assert called == [expected]


def test_icon_path_is_a_string():
    """图标路径只用于 pywebview/NotifyIcon；缺文件也得回个字符串不炸。"""
    assert isinstance(desktop_app._icon_path(), str)


def test_module_exposes_bridge_and_tray():
    assert hasattr(desktop_app, "DesktopBridge")
    assert hasattr(desktop_app, "_TrayIcon")
