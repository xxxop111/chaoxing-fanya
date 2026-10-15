# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir，入口 desktop_app.py）。

与旧版的差别（2026-10-16，为「原生窗口」而改）：

* **不再排除** ``webview`` / ``clr_loader`` / ``pythonnet`` —— 它们正是内嵌窗口
  （pywebview + WebView2 + .NET）的实现，旧配置把它们排除掉，打包版永远只能退回
  「另开浏览器窗口」，那也正是用户抱怨的「跳转网页」。
* 收集三者的数据文件与二进制：``collect_all`` 会把
  ``pythonnet/runtime/Python.Runtime.dll``、``clr_loader/ffi/dlls/*/ClrLoader.dll``、
  ``webview/lib/*.dll`` 一并带上（pywebview 与 pythonnet 都自带 PyInstaller hook）。
* 带上 ``vendor/webview2``（.NET Core 版 WebView2 托管程序集）与
  ``tools/patch_pywebview.py``：前者让冻结版仍能自我修复程序集，后者是补丁的唯一实现。
* **打包前必须先跑** ``python tools/patch_pywebview.py``：它会替换
  ``site-packages/webview/lib`` 里的 net462 程序集、并给 ``winforms.py`` 打容错补丁，
  冻结后再也改不了源码。``tools/build_desktop.py`` 已把这一步串起来。

用法::

    .venv\\Scripts\\python.exe tools\\build_desktop.py        # 推荐
    .venv\\Scripts\\pyinstaller.exe chaoxing-fanya.spec --noconfirm --clean
"""

from PyInstaller.utils.hooks import collect_all

datas = [
    ('web/dist', 'web/dist'),
    ('resource', 'resource'),
    ('vendor/webview2', 'vendor/webview2'),
    ('tools/patch_pywebview.py', 'tools'),
    ('chaoxing.ico', '.'),
]

binaries = []
hiddenimports = ['flask_cors', 'loguru', 'httpx', 'pyaes']

for pkg in ('webview', 'clr_loader', 'pythonnet'):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# `import clr` 用的是 site-packages 顶层模块 clr.py（pythonnet 的门面）
hiddenimports += ['clr', 'webview.platforms.winforms', 'webview.platforms.edgechromium']

a = Analysis(
    ['desktop_app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 这些是重依赖且本项目用不到（OCR/Paddle 走独立进程或可选导入）
    excludes=['paddleocr', 'paddle', 'ddddocr', 'matplotlib', 'PyQt5', 'PyQt6', 'PySide6'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='chaoxing-fanya',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='chaoxing.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='chaoxing-fanya',
)
