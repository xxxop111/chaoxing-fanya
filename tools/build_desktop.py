#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键打包桌面版（PyInstaller onedir）。

做的事情，顺序不能变：

1. 先跑 ``tools/patch_pywebview.py``：把 ``site-packages/webview/lib`` 里的 net462 版
   WebView2 托管程序集换成 .NET Core 版，并给 ``webview/platforms/winforms.py`` 打容错补丁。
   **必须在 PyInstaller 之前**，因为打进产物的是当时的 site-packages。
2. 检查 ``web/dist`` 是否存在（前端由 opendesign 产出，缺失也能打包，但窗口会是空的）。
3. 调 PyInstaller 按 ``chaoxing-fanya.spec`` 打包到 ``dist/chaoxing-fanya/``。
4. 校验产物里确实带上了内嵌窗口需要的文件（webview 程序集、pythonnet 运行时、
   clr_loader、vendor/webview2、web/dist）。

用法::

    .venv\\Scripts\\python.exe tools\\build_desktop.py            # 打包
    .venv\\Scripts\\python.exe tools\\build_desktop.py --run      # 打包并直接启动验证
    .venv\\Scripts\\python.exe tools\\build_desktop.py --no-patch # 跳过补丁（不推荐）
"""

from __future__ import annotations

import argparse
import importlib.util
import os
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
    log("步骤 1/4：修补 pywebview 运行环境")
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


def step_check_web() -> None:
    log("步骤 2/4：检查前端产物 web/dist")
    index = ROOT / "web" / "dist" / "index.html"
    if index.is_file():
        mtime = index.stat().st_mtime
        import datetime

        stamp = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        log(f"  [ok] web/dist/index.html（{stamp}）")
    else:
        log("  [!] 缺少 web/dist/index.html —— 窗口会打开但页面为空")


def step_build() -> int:
    log("步骤 3/4：PyInstaller 打包（onedir）")
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


def step_verify() -> bool:
    log("步骤 4/4：校验产物")
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
    args = parser.parse_args()

    log(f"仓库根目录: {ROOT}")
    if not args.no_patch:
        step_patch()
    step_check_web()

    if not args.skip_build:
        code = step_build()
        if code != 0:
            log(f"[xx] PyInstaller 返回 {code}")
            return code

    ok = step_verify()
    if args.run and ok:
        step_run()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
