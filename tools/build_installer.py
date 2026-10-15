#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用 Inno Setup 把 dist\\chaoxing-fanya 打成带安装向导的安装包。

先跑 ``tools/build_desktop.py`` 生成 onedir 产物，再跑本脚本。

ISCC.exe 的查找顺序：

1. ``--iscc`` 显式路径
2. 注册表 ``HKLM/HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Inno Setup 6_is1``
   的 ``InstallLocation``（Inno Setup 自己写的）
3. 常见安装位置（当前用户优先，因为本机就是 ``/CURRENTUSER`` 装的）

用法::

    .venv\\Scripts\\python.exe tools\\build_installer.py
    .venv\\Scripts\\python.exe tools\\build_installer.py --iscc "C:\\path\\to\\ISCC.exe"
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

try:  # Windows 控制台默认 GBK，别为一个符号崩掉
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
ISS = ROOT / "installer" / "chaoxing-fanya.iss"
DIST_APP = ROOT / "dist" / "chaoxing-fanya"
OUT_DIR = ROOT / "dist" / "installer"


def log(msg: str) -> None:
    print(f"[installer] {msg}", flush=True)


def find_iscc(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None

    import winreg  # noqa: PLC0415 - 只有 Windows 才有

    candidates: list[Path] = []
    for hive, flag in ((winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, 0)):
        for view in (0, winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(
                    hive,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
                    0,
                    winreg.KEY_READ | flag | view,
                ) as key:
                    loc = winreg.QueryValueEx(key, "InstallLocation")[0]
                    if loc:
                        candidates.append(Path(loc) / "ISCC.exe")
            except OSError:
                continue

    local = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    for base in filter(None, (local and str(Path(local) / "Programs"), program_files, program_files_x86)):
        candidates.append(Path(base) / "Inno Setup 6" / "ISCC.exe")

    for path in candidates:
        if path.is_file():
            return path
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="打包安装向导（Inno Setup）")
    parser.add_argument("--iscc", default=None, help="ISCC.exe 路径")
    args = parser.parse_args()

    os.chdir(ROOT)
    if not ISS.is_file():
        log(f"[xx] 找不到脚本 {ISS}")
        return 1
    if not (DIST_APP / "chaoxing-fanya.exe").is_file():
        log(f"[xx] 找不到产物 {DIST_APP / 'chaoxing-fanya.exe'}，先跑 tools/build_desktop.py")
        return 1

    iscc = find_iscc(args.iscc)
    if iscc is None:
        log("[xx] 找不到 ISCC.exe。安装 Inno Setup 6：winget install JRSoftware.InnoSetup")
        log("     （本机 GitHub 直连不通，可用镜像：")
        log("       https://ghproxy.net/https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe")
        log("       下载后 /VERYSILENT /CURRENTUSER 静默安装）")
        return 1
    log(f"ISCC: {iscc}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [str(iscc), str(ISS)]
    log(" ".join(cmd))
    code = int(subprocess.run(cmd, cwd=str(ROOT)).returncode)
    if code != 0:
        log(f"[xx] ISCC 返回 {code}")
        return code

    setups = sorted(OUT_DIR.glob("*.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
    if setups:
        newest = setups[0]
        size = newest.stat().st_size
        log(f"[ok] {newest}（{size} 字节 / {size / 1048576:.1f} MB）")
    else:
        log(f"[!] ISCC 成功但 {OUT_DIR} 里没有 exe")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
