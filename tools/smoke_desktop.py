#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""桌面版冒烟测试：确认 exe 真能起来、真能服务前端与桌面壳端点，然后把它关掉。

它验证的正是「打包/安装是否可用」这条链上最容易悄悄坏掉的几件事：

1. 进程能启动、端口能监听（``--no-window`` 模式，不会弹窗打扰人）；
2. ``/api/health`` 通（说明 Flask 与整个 ``api/`` 包在冻结环境里导入成功）；
3. ``/`` 返回前端 HTML（说明 ``web/dist`` 被正确收进了包）；
4. 桌面壳端点 ``/api/heartbeat``、``/api/window-closed`` 已注册
   （前端靠它们实现「关窗即退出」，缺了就会关不掉）；
5. ``--stop`` 能把它干净地停下来。

用法::

    .venv\\Scripts\\python.exe tools\\smoke_desktop.py
    .venv\\Scripts\\python.exe tools\\smoke_desktop.py --exe "<安装目录>\\chaoxing-fanya.exe"
    .venv\\Scripts\\python.exe tools\\smoke_desktop.py --keep-running   # 测完不关，留给人工看界面
    .venv\\Scripts\\python.exe tools\\smoke_desktop.py --window         # 用真实原生窗口测（会弹窗）

退出码 0 = 全过，1 = 有检查失败（详情见输出与壳日志 ``desktop_app.log``）。
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

try:  # Windows 控制台默认 GBK，别为一个小符号崩掉
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
PROBE_PORTS = range(5000, 5012)
DETACHED_PROCESS = 0x00000008


def log(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


def _session():
    import requests

    s = requests.Session()
    s.trust_env = False  # 本机代理会把 127.0.0.1 也劫走
    return s


def find_exe(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    candidates = [ROOT / "dist" / "chaoxing-fanya" / "chaoxing-fanya.exe"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "ChaoxingFanya" / "chaoxing-fanya.exe")
    for path in candidates:
        if path.is_file():
            return path
    return None


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def find_running(base_urls: list[str]) -> tuple[int, str] | None:
    s = _session()
    for port in PROBE_PORTS:
        if not port_open(port):
            continue
        url = f"http://127.0.0.1:{port}"
        try:
            r = s.get(url + "/api/health", timeout=3)
            if r.status_code == 200 and r.json().get("status") is True:
                return port, url
        except Exception:
            continue
    return None


def wait_health(url: str, timeout: float) -> bool:
    s = _session()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = s.get(url + "/api/health", timeout=3)
            if r.status_code == 200 and r.json().get("status") is True:
                return True
        except Exception:
            pass
        time.sleep(0.8)
    return False


def check(label: str, fn) -> bool:
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001 - 冒烟测试要把任何异常都变成一条失败
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    log(f"{'[ok]' if ok else '[xx]'} {label}{('  ' + detail) if detail else ''}")
    return ok


def tail_log(exe: Path, lines: int = 12) -> None:
    path = exe.parent / "desktop_app.log"
    if not path.is_file():
        log(f"[!] 没有壳日志 {path}")
        return
    log(f"---- {path} 末尾 {lines} 行 ----")
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        log(f"[!] 读不到日志：{exc}")
        return
    for line in text[-lines:]:
        log("   " + line)


def kill_pid(pid: int) -> bool:
    try:
        proc = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return proc.returncode == 0
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="桌面版 exe 冒烟测试")
    parser.add_argument("--exe", default=None, help="chaoxing-fanya.exe 路径（默认自动找 dist 或安装目录）")
    parser.add_argument("--timeout", type=float, default=90.0, help="等待服务就绪的秒数（默认 90）")
    parser.add_argument("--window", action="store_true", help="用真实原生窗口启动（会弹窗），默认 --no-window")
    parser.add_argument("--keep-running", action="store_true", help="测完不停止服务，留给人看界面")
    args = parser.parse_args()

    exe = find_exe(args.exe)
    if exe is None:
        log("[xx] 找不到 chaoxing-fanya.exe，先跑 tools/build_desktop.py 或指定 --exe")
        return 1
    log(f"exe: {exe}（{exe.stat().st_size} 字节）")

    running = find_running([])
    started: subprocess.Popen | None = None
    if running is not None:
        port, url = running
        log(f"已有实例在 {url}，直接对它做检查（不会启动第二个进程）")
    else:
        argv = [str(exe), "--window"] if args.window else [str(exe), "--no-window"]
        log("启动：" + " ".join(argv))
        started = subprocess.Popen(  # noqa: S603 - exe 路径由本脚本自己解析
            argv,
            cwd=str(exe.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS,
        )
        port, url = 0, ""
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            found = find_running([])
            if found is not None:
                port, url = found
                break
            if started.poll() is not None:
                log(f"[xx] 进程在就绪前退出，返回码 {started.returncode}")
                tail_log(exe)
                return 1
            time.sleep(1.0)
        if not url:
            log(f"[xx] {args.timeout:.0f} 秒内没等到服务就绪")
            tail_log(exe)
            return 1
        log(f"服务就绪：{url}（pid {started.pid}）")

    s = _session()
    failures = 0

    def http(method: str, path: str, *, expect_json: bool = True, contains: str = ""):
        def _run():
            r = s.request(method, url + path, timeout=20)
            ctype = (r.headers.get("Content-Type") or "").lower()
            if r.status_code != 200:
                return False, f"HTTP {r.status_code} {r.text[:120]!r}"
            if expect_json:
                if "json" not in ctype:
                    return False, f"Content-Type={ctype!r}（应为 JSON）"
                payload = r.json()
                if payload.get("status") is not True:
                    return False, f"status 非 true：{payload}"
                return True, ""
            if contains and contains not in r.text:
                return False, f"响应里找不到 {contains!r}"
            return True, f"{len(r.text)} 字节 HTML"

        return _run

    def raw(
        path: str,
        *,
        accept: str = "",
        expect_status: int = 200,
        contains: str = "",
        want_json: bool = False,
    ):
        """按状态码/内容类型校验的通用探针（前端托管的契约靠它守着）。"""

        def _run():
            headers = {"Accept": accept} if accept else {}
            r = s.request("GET", url + path, headers=headers, timeout=20)
            if r.status_code != expect_status:
                return False, f"HTTP {r.status_code}（期望 {expect_status}）{r.text[:120]!r}"
            ctype = (r.headers.get("Content-Type") or "").lower()
            if want_json and "json" not in ctype:
                return False, f"Content-Type={ctype!r}（应为 JSON）"
            if contains and contains not in r.text:
                return False, f"响应里找不到 {contains!r}"
            return True, f"HTTP {r.status_code}（{ctype or '无 Content-Type'}）"

        return _run

    # 前端出货件是 opendesign 的单文件 HTML（web/opendesign/chaoxing-assistant.html
    # → web/dist/index.html），根节点是 <div class="app">，没有 React 时代的 id="root"。
    # 换界面时同步改这里：它是「/ 真的返回了打包进包的界面」的判据。
    FRONTEND_MARKER = '<div class="app">'

    checks = [
        ("GET /api/health", http("GET", "/api/health")),
        ("GET /api/heartbeat（桌面壳）", http("GET", "/api/heartbeat")),
        ("POST /api/heartbeat（桌面壳）", http("POST", "/api/heartbeat")),
        ("GET /api/version", http("GET", "/api/version")),
        ("GET /（前端已收进包）", http("GET", "/", expect_json=False, contains=FRONTEND_MARKER)),
        ("GET /tasks（客户端路由兜底到 index.html）", raw("/tasks", accept="text/html", contains=FRONTEND_MARKER)),
        (
            "GET /assets/missing.js（缺失资源如实 404）",
            raw("/assets/missing.js", accept="*/*", expect_status=404, want_json=True),
        ),
        (
            "GET /api/nope（未知接口 JSON 404）",
            raw("/api/nope", accept="application/json", expect_status=404, want_json=True),
        ),
    ]
    for label, fn in checks:
        if not check(label, fn):
            failures += 1

    # window-closed 必须放最后：真窗口模式下它会让服务进入退出倒计时
    if not check("POST /api/window-closed（桌面壳）", http("POST", "/api/window-closed")):
        failures += 1

    if args.keep_running:
        log(f"[i] --keep-running：保留 {url} 继续运行")
    else:
        log("停止：" + f'"{exe}" --stop')
        try:
            subprocess.run(
                [str(exe), "--stop"],
                cwd=str(exe.parent),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=40,
            )
        except subprocess.TimeoutExpired:
            # --stop 自己卡住不该让冒烟测试失败：端口是否真的关掉由下面这一步判定
            log("[i] --stop 未在 40 秒内返回，继续等端口释放")
        except Exception as exc:  # noqa: BLE001
            log(f"[i] --stop 调用失败：{exc}（继续等端口释放）")
        deadline = time.time() + 25
        while time.time() < deadline and port_open(port):
            time.sleep(0.7)
        if port_open(port):
            log(f"[!] 端口 {port} 仍在监听，改用 taskkill")
            if started is not None and kill_pid(started.pid):
                time.sleep(1.5)
        check("服务已停止", lambda: (not port_open(port), ""))

    if failures:
        log(f"[xx] {failures} 项检查失败")
        tail_log(exe)
        return 1
    log("[ok] 全部检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
