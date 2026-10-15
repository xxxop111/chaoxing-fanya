# -*- coding: utf-8 -*-
"""修好 pywebview 在 .NET 8（coreclr）上的两处不兼容 —— 本项目的两处「环境补丁」。

背景：本机没有 .NET Framework 里那套可用的 pythonnet 运行时（pip 装的 pythonnet 3.x
只带 coreclr 版 ``Python.Runtime.dll``，走 netfx 会报
``Failed to resolve Python.Runtime.Loader.Initialize``），所以 pywebview 必须跑在
.NET 8 上。而 pywebview 6.2.1 自带的两样东西是按 .NET Framework 世界准备的：

1. ``webview/lib/Microsoft.Web.WebView2.WinForms.dll`` 是 NuGet 包里的 **net462** 版，
   它引用了 ``System.Windows.Forms.ContextMenu``；该类型在 .NET 7+ 已被删除，于是
   coreclr 上一解析 WebView2 类型就::

       TypeLoadException: Could not load type 'System.Windows.Forms.ContextMenu'
       from assembly 'System.Windows.Forms, Version=8.0.0.0'

   → 修法：用 ``vendor/webview2/`` 里同包的 ``lib_manual/netcoreapp3.0`` 版**原地覆盖**
   （不能自己先 AddReference：pywebview 之后还会按自己的路径再加载一次同名程序集，
   .NET 直接 ``FileLoadException: Assembly with same name is already loaded``）。

2. ``webview/platforms/winforms.py`` 里 ``class OpenFolderDialog`` 的**类体**在 import
   时就会解析 ``System.Windows.Forms.FileDialogNative+IFileDialog`` 等私有成员。这些类型
   在 .NET 7+ 已被移除（COM 互操作改成 CsWin32 生成的调用），于是::

       AttributeError: 'NoneType' object has no attribute 'GetMethod'

   → 修法：把这个类的属性区换成容错版本（缺失的成员变成「一调用就报清晰错误」的占位），
   让 import 通过。本程序不使用「选择文件夹」对话框，因此没有功能损失。

用法::

    python tools/patch_pywebview.py          # 打补丁（幂等）
    python tools/patch_pywebview.py --check  # 只检查，不改文件

打包前必须执行一次：PyInstaller 会把 ``winforms.py`` 编译进 PYZ，冻结后无法再改源码。
"""

from __future__ import annotations

import argparse
import py_compile
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR_WEBVIEW2 = ROOT / "vendor" / "webview2"

WEBVIEW2_ASSEMBLIES = (
    "Microsoft.Web.WebView2.Core.dll",
    "Microsoft.Web.WebView2.WinForms.dll",
)

SENTINEL = "# --- chaoxing-fanya patch: OpenFolderDialog (net7+ 无 FileDialogNative) ---"

# 覆盖 `class OpenFolderDialog` 属性区（从 class 行之后到第一个 @classmethod 之前）。
# 定义的名字必须与原版一致，show() 会用到。
GUARDED_ATTRIBUTES = '''\
    # --- chaoxing-fanya patch: OpenFolderDialog (net7+ 无 FileDialogNative) ---
    # 原版在这里直接 windowsFormsAssembly.GetType('...FileDialogNative+IFileDialog')，
    # .NET 7+ 已删除该类型 → None.GetMethod(...) 抛 AttributeError，import 直接失败，
    # pywebview 便认为 winforms 后端不可用。本程序不使用「选择文件夹」，改成容错解析。
    foldersFilter = 'Folders|\\n'
    flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic
    windowsFormsAssembly = Assembly.LoadWithPartialName('System.Windows.Forms')

    class _MissingFileDialogNative:
        """占位类型：让类体求值通过，真调用时才报错（.NET 7+ 已无 FileDialogNative）。

        注意这里必须**返回一个函数**而不是直接 raise：原版代码在类体里就调用了
        ``iFileDialogType.GetMethod('SetOptions', flags)``，导入期不允许抛异常。
        """

        @staticmethod
        def GetMethod(*_args, **_kwargs):
            def _fail(*_a, **_k):
                raise RuntimeError(
                    'pywebview 的「选择文件夹」对话框依赖 '
                    'System.Windows.Forms.FileDialogNative，而当前 .NET 运行时（.NET 7+）'
                    '已移除该类型；本程序不使用此功能。'
                )

            return _fail

        @staticmethod
        def GetField(*_args, **_kwargs):
            return None

    iFileDialogType = windowsFormsAssembly.GetType(
        'System.Windows.Forms.FileDialogNative+IFileDialog'
    ) or _MissingFileDialogNative()
    OpenFileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.OpenFileDialog')
    FileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.FileDialog')
    createVistaDialogMethodInfo = OpenFileDialogType.GetMethod('CreateVistaDialog', flags)
    onBeforeVistaDialogMethodInfo = OpenFileDialogType.GetMethod('OnBeforeVistaDialog', flags)
    getOptionsMethodInfo = FileDialogType.GetMethod('GetOptions', flags)
    setOptionsMethodInfo = iFileDialogType.GetMethod('SetOptions', flags)
    _fosType = windowsFormsAssembly.GetType('System.Windows.Forms.FileDialogNative+FOS')
    fosPickFoldersBitFlag = (
        _fosType.GetField('FOS_PICKFOLDERS').GetValue(None) if _fosType is not None else 0
    )
    vistaDialogEventsConstructorInfo = windowsFormsAssembly.GetType(
        'System.Windows.Forms.FileDialog+VistaDialogEvents'
    ).GetConstructor(flags, None, [FileDialogType], [])
    adviseMethodInfo = iFileDialogType.GetMethod('Advise')
    unadviseMethodInfo = iFileDialogType.GetMethod('Unadvise')
    showMethodInfo = iFileDialogType.GetMethod('Show')
    # --- chaoxing-fanya patch end ---
'''


def _webview_lib_dir() -> Path | None:
    try:
        import webview.util as wvutil  # noqa: PLC0415 - 只有装了 pywebview 才有
    except Exception:  # noqa: BLE001
        return None
    return Path(wvutil.__file__).resolve().parent / "lib"


def replace_webview2_assemblies(verbose: bool = False) -> list[str]:
    """把 webview/lib 里的 WebView2 托管程序集换成 .NET Core 版。返回已替换的文件名。"""
    lib_dir = _webview_lib_dir()
    if lib_dir is None or not lib_dir.is_dir():
        if verbose:
            print("[skip] 找不到 webview/lib 目录（未安装 pywebview？）")
        return []
    changed: list[str] = []
    for name in WEBVIEW2_ASSEMBLIES:
        src = VENDOR_WEBVIEW2 / name
        dst = lib_dir / name
        if not src.exists():
            print(f"[warn] 缺少 {src}（原生窗口会失败）")
            continue
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            if verbose:
                print(f"[ok]   {name} 已是 .NET Core 版")
            continue
        shutil.copyfile(src, dst)
        changed.append(name)
        print(f"[done] {name}: {dst.stat().st_size} 字节（.NET Core 版）")
    return changed


def patch_winforms(verbose: bool = False) -> str:
    """给 OpenFolderDialog 打容错补丁。返回 'patched' / 'already' / 'unsupported'。"""
    lib_dir = _webview_lib_dir()
    if lib_dir is None:
        if verbose:
            print("[skip] 未安装 pywebview")
        return "unsupported"
    target = lib_dir.parent / "platforms" / "winforms.py"
    if not target.is_file():
        if verbose:
            print(f"[skip] 找不到 {target}")
        return "unsupported"

    source = target.read_text(encoding="utf-8")
    if SENTINEL in source:
        if verbose:
            print("[ok]   winforms.py 已打过补丁")
        return "already"

    lines = source.splitlines(keepends=True)
    class_idx = next(
        (i for i, line in enumerate(lines) if line.startswith("class OpenFolderDialog:")), None
    )
    if class_idx is None:
        print("[warn] winforms.py 里找不到 class OpenFolderDialog（pywebview 版本变了？）")
        return "unsupported"
    attr_start = class_idx + 1
    attr_end = next(
        (i for i in range(attr_start, len(lines)) if lines[i].startswith("    @classmethod")),
        None,
    )
    if attr_end is None:
        print("[warn] 找不到 OpenFolderDialog 的属性区结尾（pywebview 版本变了？）")
        return "unsupported"

    patched = "".join(lines[:attr_start]) + GUARDED_ATTRIBUTES + "".join(lines[attr_end:])

    # 先写到临时文件编译，编译不过就不动原文件
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "winforms.py"
        probe.write_text(patched, encoding="utf-8")
        try:
            py_compile.compile(str(probe), cfile=str(Path(tmp) / "winforms.pyc"),
                               doraise=True)
        except py_compile.PyCompileError as exc:
            print(f"[fail] 补丁后语法不通过，已放弃：{exc}")
            return "unsupported"

    shutil.copyfile(target, target.with_suffix(".py.orig"))
    target.write_text(patched, encoding="utf-8")
    print(f"[done] winforms.py 已打补丁（原件备份为 {target.with_suffix('.py.orig').name}）")
    return "patched"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查，不修改文件")
    args = parser.parse_args()

    if args.check:
        lib_dir = _webview_lib_dir()
        target = (lib_dir.parent / "platforms" / "winforms.py") if lib_dir else None
        ok_win = bool(target and target.is_file()
                      and SENTINEL in target.read_text(encoding="utf-8"))
        ok_dll = True
        for name in WEBVIEW2_ASSEMBLIES:
            src = VENDOR_WEBVIEW2 / name
            dst = (lib_dir / name) if lib_dir else None
            if dst is None or not dst.exists() or dst.stat().st_size != src.stat().st_size:
                ok_dll = False
        print(f"winforms 补丁: {'已打' if ok_win else '未打'}")
        print(f"WebView2 程序集: {'已是 .NET Core 版' if ok_dll else '仍是 net462 版'}")
        return 0 if (ok_win and ok_dll) else 1

    replace_webview2_assemblies(verbose=True)
    result = patch_winforms(verbose=True)
    return 0 if result in ("patched", "already") else 1


if __name__ == "__main__":
    sys.exit(main())
