# -*- coding: utf-8 -*-
"""桌面版入口 —— 用一个**原生窗口**承载界面，而不是「控制台 + 另开浏览器」。

体验目标（对齐普通桌面程序）：

* 双击图标**不弹黑框**（打包为 --noconsole）；
* 立刻出现应用窗口，先显示「正在启动…」，服务就绪后自动切到界面；
* 窗口就是应用本身（内嵌 WebView2，无地址栏、无标签栏），关掉窗口即退出；
* 浏览器存储持久化（``private_mode=False``），「记住密码」和盘点缓存不会丢。

实现要点：

* Flask 跑在后台线程（用 ``werkzeug.make_server`` 以便优雅关闭），
  GUI 必须占主线程 —— 这是 pywebview/WebView2 的要求；
* 原生窗口（pywebview + WebView2）是**默认**方式；只有它真的起不来时，才退回
  「Edge/Chrome --app 窗口」方案保证功能不丢（那种方式会另起浏览器进程，观感差）。
  想强制走浏览器可设 ``CHAOXING_NO_WEBVIEW=1``。
* .NET 8（coreclr）上 pywebview 6.2.1 有两处不兼容，启动时由
  ``tools/patch_pywebview.py`` 修好，见 ``_run_pywebview_patches()`` 的说明。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# ⚠️ 必须在本进程加载 pythonnet / pywebview 之前设置。
# pythonnet 3.x 在 Windows 上默认优先使用 .NET Framework，而本机的
# Python.Runtime.dll 在 CLR 下解析不到 Python.Runtime.Loader.Initialize，
# 表现为「原生窗口不可用，退回浏览器」。改用 coreclr（.NET 8/9/10）即可。
os.environ.setdefault("PYTHONNET_RUNTIME", "coreclr")

HOST = "127.0.0.1"
DEFAULT_PORT = 5000
HEALTH_PATH = "/api/health"
WINDOW_TITLE = "超星学习通助手"


def _base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _base_dir()
LOG_PATH = os.path.join(BASE_DIR, "desktop_app.log")
PID_PATH = os.path.join(BASE_DIR, ".chaoxing_app.pid")
WEBVIEW_STORAGE = os.path.join(BASE_DIR, ".webview")
BROWSER_PROFILE = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "chaoxing-fanya-app", "browser-profile")

BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

SPLASH_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{height:100%;margin:0;background:#f2f4f7;
  font-family:"Microsoft YaHei",-apple-system,Segoe UI,sans-serif;color:#334}
.box{height:100%;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:18px}
.spin{width:34px;height:34px;border:3px solid #c9d4e5;border-top-color:#2563eb;
  border-radius:50%;animation:r .9s linear infinite}
@keyframes r{to{transform:rotate(360deg)}}
.t{font-size:16px;font-weight:600}.s{font-size:12px;color:#8a8b99}
</style></head><body><div class="box">
<div class="spin"></div><div class="t">正在启动…</div>
<div class="s">首次启动需要几秒，请稍候</div>
</div></body></html>"""


def log(message: str) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fp:
            fp.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
    except OSError:
        pass


def message_box(title: str, text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40 | 0x1000)
    except Exception:  # noqa: BLE001
        pass


def health_url(port: int) -> str:
    return f"http://{HOST}:{port}{HEALTH_PATH}"


# ⚠️ 本机装了代理（如 127.0.0.1:7890）时，urllib 会把 127.0.0.1 的请求也走代理，
# 于是每个探测都要干等一个超时 —— 探测 12 个端口就白等 12 秒，启动自然「不丝滑」。
# 因此：先用裸 socket 快速判活（毫秒级），再用**绕过代理**的 opener 做 HTTP 检查。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _port_open(port: int, timeout: float = 0.3) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((HOST, port)) == 0


def is_running(port: int, timeout: float = 1.0) -> bool:
    if not _port_open(port, timeout=min(timeout, 0.3)):
        return False
    try:
        with _OPENER.open(health_url(port), timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def find_running_port(ports, timeout: float = 0.12) -> int | None:
    """找出本程序已在监听的端口。

    本机若装了代理软件（Clash 之类），连本机**未监听**的端口不会立刻被拒绝、
    而是要等到超时，所以这里并行探测 + 很短的超时，把总耗时压到一次超时量级。
    """
    from concurrent.futures import ThreadPoolExecutor

    port_list = list(ports)
    if not port_list:
        return None
    with ThreadPoolExecutor(max_workers=len(port_list)) as pool:
        opened = list(pool.map(lambda p: (p, _port_open(p, timeout)), port_list))
    for port, is_open in opened:
        if is_open and is_running(port, timeout=0.5):
            return port
    return None


def pick_port(start: int = DEFAULT_PORT, tries: int = 12) -> int:
    import socket

    for offset in range(tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((HOST, port))
            except OSError:
                continue
            return port
    return start


# ----------------------------------------------------------------------
# 兜底方案：Edge/Chrome 的 --app 窗口（pywebview 不可用时）
# ----------------------------------------------------------------------


def open_app_window(url: str) -> bool:
    os.makedirs(BROWSER_PROFILE, exist_ok=True)
    for exe in BROWSER_CANDIDATES:
        if not os.path.exists(exe):
            continue
        try:
            subprocess.Popen(
                [exe, f"--app={url}", f"--user-data-dir={BROWSER_PROFILE}",
                 "--no-first-run", "--no-default-browser-check"],
                close_fds=True)
            log(f"[兜底] 已用 --app 窗口打开: {exe} -> {url}")
            return True
        except OSError as exc:
            log(f"[兜底] 打开 {exe} 失败: {exc}")
    try:
        import webbrowser
        webbrowser.open(url)
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"[兜底] 打开浏览器失败: {exc}")
        return False


# ----------------------------------------------------------------------
# 停止
# ----------------------------------------------------------------------


def _write_pid() -> None:
    try:
        with open(PID_PATH, "w", encoding="utf-8") as fp:
            fp.write(str(os.getpid()))
    except OSError:
        pass


def stop_running() -> int:
    if not os.path.exists(PID_PATH):
        message_box(WINDOW_TITLE, "没有找到正在运行的实例。")
        return 0
    try:
        pid = int(open(PID_PATH, encoding="utf-8").read().strip())
    except (OSError, ValueError):
        pid = -1
    if pid <= 0:
        try:
            os.remove(PID_PATH)
        except OSError:
            pass
        message_box(WINDOW_TITLE, "记录进程号的文件已损坏，已清理。")
        return 0

    result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                            check=False, capture_output=True)
    # 进程早已退出时 taskkill 会失败 —— 这不算错误，顺手清掉残留的 pid 文件即可
    alive = result.returncode == 0
    try:
        os.remove(PID_PATH)
    except OSError:
        pass
    if alive:
        message_box(WINDOW_TITLE, "已关闭。")
        return 0
    message_box(WINDOW_TITLE, "没有正在运行的实例（已清理残留记录）。")
    return 0


# ----------------------------------------------------------------------
# Flask 后台服务
# ----------------------------------------------------------------------


def _start_server(port: int):
    """在后台线程启动 Flask，返回 (server, thread)。"""
    from werkzeug.serving import make_server

    import app as flask_app

    server = make_server(HOST, port, flask_app.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="flask-server")
    thread.start()
    return server


def _wait_ready(port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_running(port, timeout=0.6):
            return True
        time.sleep(0.25)
    return False


def _icon_path() -> str | None:
    for name in ("chaoxing.ico", "resource/chaoxing.ico"):
        candidate = os.path.join(BASE_DIR, name)
        if os.path.exists(candidate):
            return candidate
    bundled = os.path.join(getattr(sys, "_MEIPASS", BASE_DIR), "resource", "chaoxing.ico")
    return bundled if os.path.exists(bundled) else None


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def _ensure_dotnet_runtime() -> None:
    """让 pythonnet 使用**带 WinForms 的 .NET 桌面运行时**。

    pywebview 的 EdgeChromium 后端建窗口时依赖 ``System.Windows.Forms``，
    而默认的 coreclr 只加载 ``Microsoft.NETCore.App``（里面没有 WinForms），
    会报 ``Could not load file or assembly 'System.Windows.Forms'``。
    这里写一个指向 ``Microsoft.WindowsDesktop.App`` 的 runtimeconfig 并指定使用它。
    """
    os.environ.setdefault("PYTHONNET_RUNTIME", "coreclr")
    try:
        import clr_loader
        from pythonnet import set_runtime

        cfg_path = os.path.join(BASE_DIR, "pythonnet.runtimeconfig.json")
        if not os.path.exists(cfg_path):
            import json
            with open(cfg_path, "w", encoding="utf-8") as fp:
                json.dump({
                    "runtimeOptions": {
                        "tfm": "net8.0",
                        "framework": {
                            "name": "Microsoft.WindowsDesktop.App",
                            "version": "8.0.0",
                        },
                        # 机器上装了 8/9/10 的桌面运行时都能向上兼容
                        "rollForward": "latestMinor",
                    }
                }, fp, indent=2)
        set_runtime(clr_loader.get_coreclr(runtime_config=cfg_path))
        log("已指定 .NET 桌面运行时（coreclr + WindowsDesktop）")

        # pywebview 的 winforms 后端开头就执行 ``from Microsoft.Win32 import SystemEvents``，
        # 而 pythonnet 只会把**已经加载过**的程序集解析成真实类型：SystemEvents 住在
        # Microsoft.Win32.SystemEvents.dll（属于 WindowsDesktop 框架，默认并不会被载入），
        # 于是这行拿到的是个空命名空间，报
        # ``ImportError: cannot import name 'SystemEvents' from 'Microsoft.Win32'``，
        # 随后 pywebview 统一报「pythonnet cannot be loaded」，窗口悄悄退化成浏览器。
        # 这里显式把三个必需程序集先 AddReference 进来。
        import clr

        for asm in ("Microsoft.Win32.SystemEvents", "System.Windows.Forms",
                    "System.Drawing.Common"):
            try:
                clr.AddReference(asm)
            except Exception as exc:  # noqa: BLE001 - 缺哪个由后面报错指出
                log(f"预加载程序集 {asm} 失败：{exc}")
        log("已预加载 WinForms / SystemEvents 程序集")
    except Exception as exc:  # noqa: BLE001 - 失败则退回默认，由 webview 报错
        log(f"设置 .NET 桌面运行时失败（将尝试默认）：{exc}")


def _run_pywebview_patches() -> None:
    """修好 pywebview 在 .NET 8（coreclr）上的两处不兼容 —— 详见补丁脚本。

    补丁实体在 ``tools/patch_pywebview.py``（唯一实现，打包前也由它施工）：

    1. ``webview/lib/Microsoft.Web.WebView2.{Core,WinForms}.dll`` 得换成 NuGet 包里
       ``lib_manual/netcoreapp3.0`` 的版本 —— 自带的 net462 版引用了
       ``System.Windows.Forms.ContextMenu``，该类型在 .NET 7+ 已删除。
    2. ``webview/platforms/winforms.py`` 的 ``OpenFolderDialog`` 类体在 import 时会解析
       同样被删除的 ``FileDialogNative``，必须换成容错解析。

    原地替换比自行 ``clr.AddReference`` 正确：后者会让 pywebview 之后按自己的路径
    再加载一次同名程序集，.NET 报 ``Assembly with same name is already loaded``。
    任何失败都只告警（窗口还有浏览器兜底）。
    """
    tools_dir = os.path.join(getattr(sys, "_MEIPASS", BASE_DIR), "tools")
    if not os.path.isdir(tools_dir):
        tools_dir = os.path.join(BASE_DIR, "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    try:
        import patch_pywebview as patcher
    except Exception as exc:  # noqa: BLE001
        log(f"找不到补丁脚本（原生窗口可能不可用）：{exc}")
        return
    try:
        changed = patcher.replace_webview2_assemblies(verbose=False)
        if changed:
            log(f"已替换 WebView2 程序集为 .NET Core 版：{'、'.join(changed)}")
        result = patcher.patch_winforms(verbose=False)
        if result == "unsupported":
            log("winforms 补丁未生效（原生窗口可能不可用）")
        elif result == "patched":
            log("已给 pywebview 的 winforms.py 打补丁")
    except Exception as exc:  # noqa: BLE001
        log(f"应用 pywebview 补丁失败：{exc}")


def _native_window_disabled() -> bool:
    """是否按环境变量强制走浏览器窗口。

    * ``CHAOXING_NO_WEBVIEW=1`` —— 显式禁用原生窗口；
    * ``CHAOXING_USE_WEBVIEW=0`` —— 同上（兼容旧写法）；
    * 其它情况一律**优先原生窗口**：那才是「独立软件」该有的样子。
    """
    if os.environ.get("CHAOXING_NO_WEBVIEW") == "1":
        return True
    return os.environ.get("CHAOXING_USE_WEBVIEW") == "0"


def _run_with_webview(url: str, server) -> int:
    """原生窗口模式（默认）。"""
    _ensure_dotnet_runtime()
    _run_pywebview_patches()
    import webview

    os.makedirs(WEBVIEW_STORAGE, exist_ok=True)
    window = webview.create_window(
        WINDOW_TITLE, html=SPLASH_HTML,
        width=1200, height=840, min_size=(980, 640),
        background_color="#f2f4f7",
    )

    def _go_live() -> None:
        if _wait_ready(int(url.rsplit(":", 1)[1])):
            try:
                window.load_url(url)
            except Exception as exc:  # noqa: BLE001
                log(f"加载界面失败: {exc}")
                message_box(WINDOW_TITLE, f"界面加载失败：{exc}")
        else:
            message_box(WINDOW_TITLE, f"服务启动超时，请查看日志：\n{LOG_PATH}")

    threading.Thread(target=_go_live, daemon=True).start()

    # private_mode=False + storage_path：保证 localStorage 持久化
    # （「记住密码」与盘点缓存都依赖它）
    webview.start(private_mode=False, storage_path=WEBVIEW_STORAGE,
                  icon=_icon_path(), debug=False)
    try:
        server.shutdown()
    except Exception:  # noqa: BLE001
        pass
    return 0


def _register_window_routes(flask_app) -> dict:
    """注册「窗口还活着吗」的两个端点，用于「关掉窗口即退出」。

    页面每 5 秒心跳一次；关闭窗口时用 ``sendBeacon`` 打一个 ``window-closed``。
    服务端只有在「已收到关闭信号」且「安静了 15 秒」后才退出 —— 这样刷新页面
    （pagehide → 立刻重新加载 → 心跳恢复）不会误杀进程。
    """
    from flask import jsonify, request

    state = {"last_beat": time.time(), "closed_at": None}

    def heartbeat():
        state["last_beat"] = time.time()
        state["closed_at"] = None      # 有新页面连上，撤销关闭判定
        return jsonify({"status": True})

    def window_closed():
        state["closed_at"] = time.time()
        return jsonify({"status": True})

    for rule in ("/api/heartbeat", "/api/window-closed"):
        flask_app.app.add_url_rule(
            rule, endpoint=rule.strip("/").replace("/", "_"),
            view_func=heartbeat if rule.endswith("heartbeat") else window_closed,
            methods=["POST", "GET"])
    return state


def _run_with_browser(url: str, server, state: dict) -> int:
    """浏览器窗口模式（**兜底**：原生窗口起不来时才会走到这里）。

    * 窗口**并行**启动：不等服务就绪就先拉起浏览器，总耗时取两者较大值；
    * **关掉窗口即退出**（心跳安静 15 秒后），行为与普通桌面程序一致，
      也避免进程常驻、下次启动端口被占。
    """
    open_app_window(url)
    log("窗口已启动，等待页面心跳（关闭窗口后本程序会自动退出）")

    grace = 15.0        # 收到「关窗」信号后的宽限期（刷新页面会在这期间恢复心跳）
    silent_limit = 180.0  # 心跳彻底消失的兜底：浏览器被杀/崩溃时也要能退出
    try:
        while True:
            time.sleep(2.0)
            now = time.time()
            quiet = now - state.get("last_beat", now)
            closed_at = state.get("closed_at")
            if closed_at and (now - closed_at) > grace and quiet > grace:
                log("窗口已关闭，退出")
                break
            if quiet > silent_limit:
                # 注意：后台标签页的定时器会被浏览器限流（约每分钟一次），
                # 所以这个阈值必须远大于 60s，否则最小化窗口会误杀进程。
                log(f"心跳已消失 {int(quiet)} 秒，判定窗口已关闭，退出")
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
    return 0


def main() -> int:
    argv = [a.lower() for a in sys.argv[1:]]
    if "--stop" in argv:
        return stop_running()
    headless = "--no-window" in argv

    os.makedirs(BASE_DIR, exist_ok=True)
    try:
        os.chdir(BASE_DIR)
    except OSError:
        pass
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)

    ports = range(DEFAULT_PORT, DEFAULT_PORT + 12)
    running = find_running_port(ports)
    if running is not None:
        # 已在运行：直接开个窗口指过去，不再起第二个服务
        url = f"http://{HOST}:{running}"
        log(f"检测到已在运行的实例（端口 {running}），直接打开窗口")
        if not headless:
            reused = False
            if not _native_window_disabled():
                try:
                    _ensure_dotnet_runtime()
                    _run_pywebview_patches()
                    import webview
                    os.makedirs(WEBVIEW_STORAGE, exist_ok=True)
                    webview.create_window(WINDOW_TITLE, url, width=1200, height=840,
                                          min_size=(980, 640))
                    webview.start(private_mode=False, storage_path=WEBVIEW_STORAGE,
                                  icon=_icon_path())
                    reused = True
                except Exception as exc:  # noqa: BLE001
                    log(f"复用窗口失败，改用浏览器窗口: {exc}")
            if not reused:
                # 服务已在运行 → 开个窗口就立刻退出，所以「再点一次图标」是秒开
                open_app_window(url)
        return 0

    port = pick_port()
    url = f"http://{HOST}:{port}"
    _write_pid()
    log(f"启动服务: {url}（数据目录 {BASE_DIR}）")

    try:
        server = _start_server(port)
    except Exception as exc:  # noqa: BLE001
        import traceback
        log("启动失败:\n" + traceback.format_exc())
        message_box(WINDOW_TITLE + " —— 启动失败", f"{exc}\n\n日志：\n{LOG_PATH}")
        return 1

    import app as flask_app

    state = _register_window_routes(flask_app)

    try:
        if headless:
            log("--no-window：仅启动服务")
            while True:
                time.sleep(3600)
        # 原生窗口（pywebview + WebView2）是默认方式：那才像「一个独立软件」。
        # .NET 8 上 pywebview 的两处不兼容已由 tools/patch_pywebview.py 修好；
        # 若仍失败（例如机器没装 WebView2 运行时），退回 Edge/Chrome --app 窗口，
        # 保证功能不受影响。想强制走浏览器：CHAOXING_NO_WEBVIEW=1。
        if not _native_window_disabled():
            try:
                return _run_with_webview(url, server)
            except Exception as exc:  # noqa: BLE001
                log(f"原生窗口不可用（{exc}），改用浏览器窗口")
        else:
            log("已按环境变量要求改用浏览器窗口")
        return _run_with_browser(url, server, state)
    except KeyboardInterrupt:
        log("收到中断，退出")
        return 0
    finally:
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        if os.path.exists(PID_PATH):
            try:
                os.remove(PID_PATH)
            except OSError:
                pass


if __name__ == "__main__":
    # 无控制台打包时，任何未捕获异常都会「双击没反应」——
    # 因此这里兜住并弹窗，至少让用户知道去哪看日志。
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        import traceback
        detail = traceback.format_exc()
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fp:
                fp.write(detail)
        except OSError:
            pass
        try:
            ctypes.windll.user32.MessageBoxW(
                None, f"程序启动失败：{exc}\n\n日志：\n{LOG_PATH}", "超星学习通助手",
                0x10 | 0x1000)
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
