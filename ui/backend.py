# -*- coding: utf-8 -*-
"""桌面版的后端控制与本地 API 客户端。

设计要点
--------
桌面版**不复制**任何业务逻辑：登录、章节任务点、任务引擎、AI剧情、审批队列
仍然住在 ``api/`` 与 ``app.py`` 里，Web 版与桌面版共用同一份实现，
否则两边会各修各的、行为逐渐漂移。

界面是纯 PySide6 控件，用户看不到浏览器、也看不到地址栏；Flask 只是
**进程内**的一条本地 HTTP 通道：只监听 ``127.0.0.1``、端口从 5000 起找空闲，
不对外提供服务。

两个必须记住的坑
----------------
* ``requests`` 必须关掉 ``trust_env``：本机如果装了代理（Clash 之类），
  连 ``127.0.0.1`` 的请求也会被塞进代理，表现为「每个请求都等到超时」，
  桌面版会显得又慢又卡。
* ``import app``（连带整个 ``api/`` 包）要好几秒，所以由界面在**后台线程**
  里调用 :meth:`ServiceController.start`，主线程先把窗口显示出来。
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any, Callable, Optional

import requests

__all__ = [
    "ApiError",
    "ApiClient",
    "ServiceController",
    "pick_port",
    "unwrap",
]

HOST = "127.0.0.1"
DEFAULT_PORT = 5000
PORT_TRIES = 12


class ApiError(RuntimeError):
    """本地接口返回失败或无法连接时抛出（带服务端原话，便于界面直接展示）。"""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def _port_free(port: int, host: str = HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(start: int = DEFAULT_PORT, tries: int = PORT_TRIES) -> int:
    """从 ``start`` 起找第一个空闲端口（找不到就退回 ``start``）。"""
    for offset in range(tries):
        port = start + offset
        if _port_free(port):
            return port
    return start


def _new_session() -> requests.Session:
    """本地回环专用会话：绕开系统代理，避免本机代理把 127.0.0.1 也劫走。"""
    session = requests.Session()
    session.trust_env = False
    return session


def unwrap(payload: Any) -> Any:
    """校验 ``status`` 并取出 ``data``。

    服务端的约定是 ``{"status": bool, "msg": str, "data": ...}``；
    ``status`` 为 False 时把 ``msg`` 原样抛给界面，绝不吞掉。
    """
    if not isinstance(payload, dict):
        raise ApiError("本地服务返回了意外的内容（不是 JSON 对象）")
    if payload.get("status") is False:
        raise ApiError(str(payload.get("msg") or "操作失败"))
    return payload.get("data")


# ----------------------------------------------------------------------
# 进程内 Flask 服务
# ----------------------------------------------------------------------


class ServiceController:
    """在本进程后台线程里跑 Flask，只监听回环地址。"""

    def __init__(self, on_log: Optional[Callable[[str], None]] = None) -> None:
        self._server: Any = None
        self._thread: Optional[threading.Thread] = None
        self._port: int = 0
        self._on_log = on_log or (lambda _m: None)

    # -- 状态 ---------------------------------------------------------
    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> str:
        return f"http://{HOST}:{self._port}"

    @property
    def running(self) -> bool:
        return self._server is not None

    def log(self, message: str) -> None:
        try:
            self._on_log(message)
        except Exception:  # noqa: BLE001 - 日志回调不该影响启动
            pass

    # -- 生命周期 -----------------------------------------------------
    def start(self, timeout: float = 180.0) -> str:
        """启动服务并等它真的能响应，返回 base_url。**调用方负责放到后台线程**。"""
        if self._server is not None:
            return self.base_url

        port = pick_port()
        self.log(f"正在启动本地服务（端口 {port}）…")

        from werkzeug.serving import make_server

        import app as flask_app

        self._server = make_server(HOST, port, flask_app.app, threaded=True)
        self._port = port
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="desktop-flask")
        self._thread.start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._probe():
                self.log(f"本地服务已就绪：{self.base_url}")
                return self.base_url
            time.sleep(0.2)

        raise ApiError(f"本地服务启动超时（{timeout:.0f} 秒）")

    def _probe(self, timeout: float = 1.0) -> bool:
        try:
            with _new_session() as session:
                resp = session.get(f"{self.base_url}/api/health", timeout=timeout)
                return resp.status_code == 200
        except requests.RequestException:
            return False

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is None:
            return
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001 - 退出路径不该再抛
            pass
        if thread is not None:
            thread.join(timeout=3.0)
        self.log("本地服务已停止")


# ----------------------------------------------------------------------
# 本地 API 客户端
# ----------------------------------------------------------------------


class ApiClient:
    """把 ``app.py`` 暴露的 HTTP 接口包成一句话调用。"""

    def __init__(self, base_url: str, service: Optional[ServiceController] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._service = service
        self._session = _new_session()

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass

    # -- 底层 ---------------------------------------------------------
    def request(self, method: str, path: str, *, json: Any = None,
                params: Optional[dict] = None, timeout: float = 60.0) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.request(method, url, json=json, params=params,
                                         timeout=timeout)
        except requests.Timeout as exc:
            raise ApiError(f"请求超时（{timeout:.0f} 秒）：{path}") from exc
        except requests.RequestException as exc:
            raise ApiError(f"无法连接本地服务：{exc}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ApiError(f"本地服务返回了非 JSON 内容（HTTP {resp.status_code}）") from exc

        if isinstance(payload, dict) and payload.get("status") is False:
            raise ApiError(str(payload.get("msg") or f"HTTP {resp.status_code}"),
                           resp.status_code)
        if resp.status_code >= 400:
            raise ApiError(f"HTTP {resp.status_code}", resp.status_code)
        return payload

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        """``request`` + ``unwrap``：直接拿到 ``data``。"""
        return unwrap(self.request(method, path, **kwargs))

    # -- 通用 ---------------------------------------------------------
    def health(self) -> Any:
        return self.call("GET", "/api/health", timeout=5.0)

    def version(self) -> Any:
        return self.call("GET", "/api/version", timeout=10.0)

    def login(self, username: str, password: str, use_cookies: bool = False) -> Any:
        return self.call("POST", "/api/login", json={
            "username": username, "password": password, "use_cookies": use_cookies,
        }, timeout=180.0)

    def courses(self, username: str, password: str, use_cookies: bool = False) -> Any:
        return self.call("POST", "/api/courses", json={
            "username": username, "password": password, "use_cookies": use_cookies,
        }, timeout=300.0)

    def get_config(self) -> Any:
        return self.call("GET", "/api/config", timeout=20.0)

    def save_config(self, data: dict) -> Any:
        return self.call("POST", "/api/config", json=data, timeout=20.0)

    def ai_test(self, tiku_config: dict) -> Any:
        return self.call("POST", "/api/ai/test",
                         json={"tiku_config": tiku_config}, timeout=150.0)

    # -- 章节任务点（学习） -------------------------------------------
    def start_study(self, payload: dict) -> Any:
        return self.call("POST", "/api/start", json=payload, timeout=60.0)

    def task_status(self, task_id: str) -> Any:
        return self.call("GET", f"/api/task/{task_id}", timeout=20.0)

    def task_details(self, task_id: str) -> Any:
        return self.call("GET", f"/api/task/{task_id}/details", timeout=20.0)

    def stop_task(self, task_id: str) -> Any:
        return self.call("POST", f"/api/task/{task_id}/stop", timeout=20.0)

    def logs(self, task_id: str) -> Any:
        return self.call("GET", f"/api/logs/{task_id}", timeout=20.0)

    # -- 任务引擎（泛雅「任务」模块） ---------------------------------
    def fanya_preview_start(self, payload: dict) -> Any:
        return self.call("POST", "/api/fanya/preview", json=payload, timeout=60.0)

    def fanya_preview_get(self, task_id: str) -> Any:
        return self.call("GET", f"/api/fanya/preview/{task_id}", timeout=30.0)

    def fanya_execute(self, payload: dict) -> Any:
        return self.call("POST", "/api/fanya/execute", json=payload, timeout=60.0)

    def fanya_result(self, task_id: str) -> Any:
        return self.call("GET", f"/api/fanya/result/{task_id}", timeout=30.0)

    # -- 待审批队列 ---------------------------------------------------
    def approvals(self, include_decided: bool = False) -> Any:
        params = {"include_decided": "1"} if include_decided else None
        return self.call("GET", "/api/fanya/approvals", params=params, timeout=30.0)

    def approve(self, entry_id: str, approve: bool = True) -> Any:
        return self.call("POST", "/api/fanya/approve",
                         json={"id": entry_id, "approve": approve}, timeout=180.0)

    def approvals_clear(self, keep_decided: bool = False) -> Any:
        return self.call("POST", "/api/fanya/approvals/clear",
                         json={"keep_decided": keep_decided}, timeout=30.0)
