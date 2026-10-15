#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证安装包的**载荷与自启动**：不依赖「装到用户目录」也能证明包里的东西对不对。

为什么需要它：完整静默安装要写 ``HKCU`` 卸载注册表项、要往 ``%APPDATA%`` 建快捷方式，
在受限环境（DSH 会话、CI、沙箱）里这些写入会被拒，安装器会整体回滚 —— 于是
「安装包到底装得对不对」就永远验不到。本脚本派生一份**测试用 .iss**，只去掉那两处
必然被拒的动作，其余（解包、文件落盘、``[Run]`` 自启动、程序真的能起来）全部保留：

* ``Uninstallable=no``：不写卸载注册表项、不生成 ``unins000.dat``；
* 清空 ``[Icons]``：不建开始菜单/桌面快捷方式；
* ``[Run]`` 改成 ``--no-window`` 且不 ``skipifsilent``：静默装完即自动启动，但不弹窗。

用法::

    .venv\\Scripts\\python.exe tools\\verify_installer.py
    .venv\\Scripts\\python.exe tools\\verify_installer.py --keep   # 保留测试安装目录，便于人工看

退出码 0 = 载荷、自启动、冒烟测试全通过。产物默认在结束时清理。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SRC_ISS = ROOT / "installer" / "chaoxing-fanya.iss"
PROBE_ISS = ROOT / "installer" / "_probe_install.iss"
SETUP = ROOT / "dist" / "installer" / "_probe-setup.exe"
INSTALL_DIR = ROOT / "_probe_install_dir"
TMP_DIR = ROOT / "_probe_tmp"
SETUP_LOG = ROOT / "_probe_setup.log"
PROBE_PORTS = range(5000, 5012)

FAILS: list[str] = []


def log(msg: str) -> None:
    print(f"[verify] {msg}", flush=True)


def check(label: str, ok: bool, detail: str = "") -> bool:
    log(f"{'[ok]' if ok else '[xx]'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(label)
    return ok


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def find_running() -> tuple[int, str] | None:
    import requests

    s = requests.Session()
    s.trust_env = False
    for port in PROBE_PORTS:
        if not port_open(port):
            continue
        try:
            r = s.get(f"http://127.0.0.1:{port}/api/health", timeout=3)
            if r.status_code == 200 and r.json().get("status") is True:
                return port, f"http://127.0.0.1:{port}"
        except Exception:
            continue
    return None


def make_probe_iss() -> None:
    text = SRC_ISS.read_text(encoding="utf-8")
    text = text.replace(
        "PrivilegesRequiredOverridesAllowed=dialog",
        "PrivilegesRequiredOverridesAllowed=dialog\nUninstallable=no",
    )
    text = text.replace(
        'OutputBaseFilename=chaoxing-fanya-setup-{#AppVersion}',
        'OutputBaseFilename=_probe-setup',
    )
    # [Icons] 整段清空（要写 %APPDATA%，受限会话必被拒）
    text = re.sub(
        r"\[Icons\]\n(?:[^\[]*?)(?=\n\[Run\])",
        "[Icons]\n; probe: 快捷方式已禁用\n\n",
        text,
    )
    text = text.replace(
        'Filename: "{app}\\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent',
        'Filename: "{app}\\{#AppExeName}"; Parameters: "--no-window"; Flags: nowait postinstall',
    )
    PROBE_ISS.write_text(text, encoding="utf-8")
    log(f"已生成测试脚本 {PROBE_ISS.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="验证安装包载荷与自启动（受限环境可用）")
    parser.add_argument("--keep", action="store_true", help="保留测试安装目录与测试 .iss，便于人工查看")
    args = parser.parse_args()

    if not SRC_ISS.is_file():
        log(f"[xx] 找不到 {SRC_ISS}")
        return 1

    sys.path.insert(0, str(ROOT / "tools"))
    from build_installer import find_iscc  # noqa: PLC0415

    iscc = find_iscc(None)
    if iscc is None:
        log("[xx] 找不到 ISCC.exe")
        return 1

    for path in (INSTALL_DIR, TMP_DIR):
        shutil.rmtree(path, ignore_errors=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    for stale in (SETUP, SETUP_LOG):
        stale.unlink(missing_ok=True)

    existing = find_running()
    if existing is not None:
        log(f"[!] 已有实例在 {existing[1]}，先停掉它")
        subprocess.run(
            [str(ROOT / "dist" / "chaoxing-fanya" / "chaoxing-fanya.exe"), "--stop"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=40,
        )
        time.sleep(3)

    make_probe_iss()
    log("编译测试安装包 ...")
    code = subprocess.run(
        [str(iscc), str(PROBE_ISS)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    log(f"ISCC 返回 {code.returncode}：{(code.stdout or '').strip().splitlines()[-1] if code.stdout else ''}")
    if not check("测试安装包编译成功", code.returncode == 0 and SETUP.is_file()):
        return 1
    log(f"安装包 {SETUP.name}：{SETUP.stat().st_size} 字节")

    log("静默安装（TEMP 指向工作区，避开会话对 %TEMP% 限制的干扰）...")
    env = dict(os.environ)
    env.update({"TEMP": str(TMP_DIR), "TMP": str(TMP_DIR)})
    proc = subprocess.run(
        [
            str(SETUP),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            f"/DIR={INSTALL_DIR}",
            f"/LOG={SETUP_LOG}",
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=600,
    )
    check("安装器退出码为 0", proc.returncode == 0, f"rc={proc.returncode}")
    if proc.returncode != 0 and SETUP_LOG.is_file():
        tail = SETUP_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-6:]
        for line in tail:
            log("   " + line.strip())

    exe = INSTALL_DIR / "chaoxing-fanya.exe"

    def installed(rel: str) -> Path | None:
        """PyInstaller 6 的 onedir 把 datas 放在 ``_internal\\`` 下，两种布局都接受。"""
        for base in (INSTALL_DIR, INSTALL_DIR / "_internal"):
            candidate = base / rel
            if candidate.exists():
                return candidate
        return None

    check("主程序已安装", exe.is_file(), str(exe))
    check("onedir 依赖目录已安装", (INSTALL_DIR / "_internal").is_dir())
    for label, rel in (
        ("前端 web/dist 已安装", "web/dist/index.html"),
        ("resource 资源已安装", "resource/font_map_table.json"),
        ("WebView2 补丁程序集已安装", "vendor/webview2/Microsoft.Web.WebView2.Core.dll"),
        ("补丁脚本已安装", "tools/patch_pywebview.py"),
    ):
        found = installed(rel)
        check(label, found is not None, str(found or f"未找到 {rel}"))
    if not installed("web/dist/index.html"):
        log("安装目录顶层：" + ", ".join(sorted(p.name for p in INSTALL_DIR.iterdir())[:12]))
        internal = INSTALL_DIR / "_internal"
        if internal.is_dir():
            log("_internal 顶层：" + ", ".join(sorted(p.name for p in internal.iterdir())[:12]))

    leaked = [
        name
        for name in ("web_config.json", "desktop_config.json", "config.ini", "desktop_app.log", "chaoxing.log")
        if (INSTALL_DIR / name).exists()
    ]
    check("没有把运行期配置/日志/凭据打进包", not leaked, ", ".join(leaked))
    check("没有把 WebView2 用户数据打进包", not (INSTALL_DIR / ".webview").exists())

    log("等 [Run] 自启动的服务就绪 ...")
    running = None
    deadline = time.time() + 90
    while time.time() < deadline:
        running = find_running()
        if running is not None:
            break
        time.sleep(1.0)
    if not check("[Run] 让安装好的程序自动起来了", running is not None):
        log("（看壳日志）" + str(INSTALL_DIR / "desktop_app.log"))
        return 1
    port, url = running
    log(f"服务：{url}")

    smoke = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "smoke_desktop.py"), "--exe", str(exe)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    for line in (smoke.stdout or "").splitlines():
        log("   " + line)
    check("安装目录里的 exe 通过冒烟测试", smoke.returncode == 0, f"rc={smoke.returncode}")

    # 不论是否 --keep，装出来的程序都要停掉（留着会占 5000 端口，影响后续验证）
    if port_open(port):
        subprocess.run(
            [str(exe), "--stop"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=40,
        )
        time.sleep(3)

    if args.keep:
        log(f"[i] --keep：保留 {INSTALL_DIR}、{PROBE_ISS.name}、{SETUP.name} 供人工查看")
    else:
        log("清理 ...")
        PROBE_ISS.unlink(missing_ok=True)
        SETUP.unlink(missing_ok=True)
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        SETUP_LOG.unlink(missing_ok=True)

    if FAILS:
        log(f"[xx] {len(FAILS)} 项失败：{'；'.join(FAILS)}")
        return 1
    log("[ok] 安装包载荷与自启动全部验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
