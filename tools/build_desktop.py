#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键打包桌面版（PyInstaller onedir）。

做的事情，顺序不能变：

1. 先跑 ``tools/patch_pywebview.py``：把 ``site-packages/webview/lib`` 里的 net462 版
   WebView2 托管程序集换成 .NET Core 版，并给 ``webview/platforms/winforms.py`` 打容错补丁。
   **必须在 PyInstaller 之前**，因为打进产物的是当时的 site-packages。
2. 把 ``web/opendesign/chaoxing-assistant.html``（opendesign 产出的**单文件**界面）同步成
   ``web/dist/index.html``，并清掉旧 React 构建留下的 ``web/dist/assets``；
3. 检查 ``web/dist`` 是否存在（缺失也能打包，但窗口会是空的）；
4. 调 PyInstaller 按 ``chaoxing-fanya.spec`` 打包到 ``dist/chaoxing-fanya/``。
5. 把 ``chaoxing.ico`` 复制到产物**根目录**（PyInstaller 6 只会把它塞进 ``_internal/``，
   而窗口/托盘图标要的是 exe 旁边那份真实文件，否则任务栏一块白、托盘用系统默认图标）。
6. 校验产物里确实带上了内嵌窗口需要的文件（webview 程序集、pythonnet 运行时、
   clr_loader、vendor/webview2、web/dist）。

用法::

    .venv\\Scripts\\python.exe tools\\build_desktop.py            # 打包
    .venv\\Scripts\\python.exe tools\\build_desktop.py --run      # 打包并直接启动验证
    .venv\\Scripts\\python.exe tools\\build_desktop.py --no-patch # 跳过补丁（不推荐）
    .venv\\Scripts\\python.exe tools\\build_desktop.py --no-sync  # 不覆盖 web/dist（调试用）
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Windows 控制台默认 GBK：个别符号会直接把脚本打死，这里放宽解码错误
try:  # pragma: no cover
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "chaoxing-fanya.spec"
DIST_DIR = ROOT / "dist" / "chaoxing-fanya"
EXE = DIST_DIR / "chaoxing-fanya.exe"


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def load_patch_module():
    path = ROOT / "tools" / "patch_pywebview.py"
    spec = importlib.util.spec_from_file_location("patch_pywebview", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载补丁模块: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def step_patch() -> bool:
    log("步骤 1/6：修补 pywebview 运行环境")
    os.chdir(ROOT)
    patch = load_patch_module()
    changed = patch.replace_webview2_assemblies(verbose=True)
    winforms = patch.patch_winforms(verbose=True)
    log(f"  WebView2 程序集替换: {'、'.join(changed) if changed else '无需替换'}")
    log(f"  winforms 补丁: {winforms}")
    if winforms == "unsupported":
        log("  [!] winforms 补丁未生效（内嵌窗口很可能不可用）")
        return False
    log("  [ok] 补丁已就绪")
    return True


OPENDESIGN_HTML = ROOT / "web" / "opendesign" / "chaoxing-assistant.html"
WEB_DIST = ROOT / "web" / "dist"


def step_sync_frontend() -> None:
    """把 opendesign 的单文件界面同步成 web/dist/index.html。

    这是「前端由外部设计产出、本仓库只负责端出来」这条约定的落地点：
    换界面只需要替换 ``web/opendesign/chaoxing-assistant.html`` 再打包，
    不必碰 Flask、也不必有 Node 工具链。
    """
    log("步骤 2/6：同步前端（opendesign 单文件 → web/dist/index.html）")
    if not OPENDESIGN_HTML.is_file():
        log(f"  [!] 缺少 {OPENDESIGN_HTML.relative_to(ROOT)} —— 沿用现有 web/dist")
        return
    WEB_DIST.mkdir(parents=True, exist_ok=True)
    data = OPENDESIGN_HTML.read_bytes()
    target = WEB_DIST / "index.html"
    before = target.read_bytes() if target.is_file() else None
    target.write_bytes(data)
    tail = "（内容无变化）" if before == data else ""
    log(f"  [ok] web/dist/index.html（{len(data)} 字节）{tail}")
    stale = WEB_DIST / "assets"
    if stale.is_dir():
        shutil.rmtree(stale, ignore_errors=True)
        log("  [ok] 清理旧的 web/dist/assets（单文件界面不再引用）")
    for name in ("vite.svg",):  # 旧 React 构建的残留
        leftover = WEB_DIST / name
        if leftover.is_file():
            leftover.unlink()
            log(f"  [ok] 清理旧的 web/dist/{name}")


def step_check_web() -> None:
    log("步骤 3/6：检查前端产物 web/dist")
    index = ROOT / "web" / "dist" / "index.html"
    if index.is_file():
        mtime = index.stat().st_mtime
        import datetime

        stamp = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        # opendesign 的导出会带 data-od-id 标记，用它区分「新单文件界面」与旧 React 构建
        try:
            text = index.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        kind = "opendesign 单文件版" if "data-od-id" in text else "未知来源"
        log(f"  [ok] web/dist/index.html（{stamp}，{kind}）")
    else:
        log("  [!] 缺少 web/dist/index.html —— 窗口会打开但页面为空")


def step_build() -> int:
    log("步骤 4/6：PyInstaller 打包（onedir）")
    os.chdir(ROOT)
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC),
        "--noconfirm",
        "--clean",
        "--log-level=WARN",
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build"),
    ]
    log("  " + " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(ROOT))
    return int(proc.returncode)


# 产物里必须存在的关键文件（相对 dist/chaoxing-fanya/_internal）
REQUIRED = [
    "web/dist/index.html",
    "vendor/webview2/Microsoft.Web.WebView2.Core.dll",
    "vendor/webview2/Microsoft.Web.WebView2.WinForms.dll",
    "webview/lib/Microsoft.Web.WebView2.Core.dll",
    "webview/lib/Microsoft.Web.WebView2.WinForms.dll",
    "pythonnet/runtime/Python.Runtime.dll",
    "clr_loader/ffi/dlls/amd64/ClrLoader.dll",
]


def step_icon() -> bool:
    """把窗口/托盘图标放进产物目录。

    PyInstaller 只把 ``chaoxing.ico`` 嵌进 exe 资源，**不会**在 onedir 产物里留一份独立文件；
    而 pywebview 的 WinForms 后端只有在 ``icon`` 指向真实文件时才直接 ``Form.Icon = Icon(path)``，
    否则退回 ``ExtractIconW``（有些机器返回 0 → 任务栏显示一块白），托盘也只能用系统默认图标。
    所以这里补一份 ``dist/chaoxing-fanya/chaoxing.ico``（安装包会原样带上）。
    """
    log("步骤 5/6：放窗口/托盘图标")
    src = ROOT / "chaoxing.ico"
    dst = DIST_DIR / "chaoxing.ico"
    if not src.is_file():
        log(f"  [xx] 缺少 {src}")
        return False
    if not DIST_DIR.is_dir():
        log(f"  [xx] 没有产物目录 {DIST_DIR}")
        return False
    shutil.copy2(src, dst)
    log(f"  [ok] {dst} ({dst.stat().st_size} 字节)")
    return True


def step_verify() -> bool:
    log("步骤 6/6：校验产物")
    if not EXE.is_file():
        log(f"  [xx] 没有生成 {EXE}")
        return False
    size = EXE.stat().st_size
    log(f"  [ok] {EXE.name} ({size} 字节)")
    internal = DIST_DIR / "_internal"
    missing = []
    for rel in REQUIRED:
        target = internal / rel
        if target.is_file():
            log(f"    [ok] {rel} ({target.stat().st_size} 字节)")
        else:
            missing.append(rel)
            log(f"    [xx] 缺少 {rel}")
    if missing:
        log(f"  [!] 产物缺少 {len(missing)} 个文件，内嵌窗口很可能不可用")
        return False
    log("  [ok] 内嵌窗口所需文件齐全")
    return True


def step_run() -> None:
    log(f"启动 {EXE} 验证（自己看窗口；进程可用 --stop 关闭）")
    subprocess.Popen([str(EXE)], cwd=str(DIST_DIR))


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 chaoxing-fanya 桌面版")
    parser.add_argument("--run", action="store_true", help="打包后直接启动做验证")
    parser.add_argument("--no-patch", action="store_true", help="跳过 pywebview 补丁")
    parser.add_argument("--skip-build", action="store_true", help="只做补丁与校验，不重新打包")
    parser.add_argument("--no-sync", action="store_true", help="不把 opendesign 界面同步进 web/dist")
    args = parser.parse_args()

    log(f"仓库根目录: {ROOT}")
    if not args.no_patch:
        step_patch()
    if not args.no_sync:
        step_sync_frontend()
    step_check_web()

    if not args.skip_build:
        code = step_build()
        if code != 0:
            log(f"[xx] PyInstaller 返回 {code}")
            return code

    icon_ok = step_icon()
    ok = step_verify() and icon_ok
    if args.run and ok:
        step_run()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
