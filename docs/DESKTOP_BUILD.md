# 桌面版构建与安装（2026-10-16 实测）

本文记录「怎么把 chaoxing-fanya 打包成独立桌面软件」，以及其中**必须知道的两个环境补丁**。
面向以后重新打包的人（包括未来的我）。

## 1. 成品长什么样

| 层 | 内容 |
| --- | --- |
| 界面 | `web/dist`（React 构建产物），由 Flask 静态托管 |
| 外壳 | `desktop_app.py`：后台线程起 Flask（`werkzeug.make_server`，只监听 `127.0.0.1`，端口从 5000 起找空闲），主线程用 **pywebview / WebView2 开原生窗口**加载它 |
| 兜底 | 原生窗口真的起不来时，才退回 `Edge/Chrome --app=` 无地址栏窗口（**用户会看到「跳转网页」**，属于失败态，不是设计） |
| 打包 | PyInstaller **onedir** → `dist/chaoxing-fanya/chaoxing-fanya.exe` |
| 安装 | Inno Setup → `dist/installer/chaoxing-fanya-setup-1.0.0.exe`（当前用户安装，不需要管理员） |

开关（环境变量）：`CHAOXING_NO_WEBVIEW=1` 强制走浏览器兜底；`--no-window` 只起服务不开窗；
`--stop` 关掉已在跑的实例。

## 2. 两个必须打的环境补丁（`tools/patch_pywebview.py`）

本机没有可用的 .NET Framework pythonnet 运行时（pip 的 pythonnet 3.x 只带 coreclr 版
`Python.Runtime.dll`，走 netfx 会 `Failed to resolve Python.Runtime.Loader.Initialize`），
所以 pywebview **必须跑在 .NET 8 上**。而 pywebview 6.2.1 自带的两样东西是按 .NET Framework
世界准备的，于是有两处不兼容：

1. `webview/lib/Microsoft.Web.WebView2.WinForms.dll` 是 NuGet 包的 **net462** 版，引用了
   `System.Windows.Forms.ContextMenu`；该类在 .NET 7+ 已删除 →
   `TypeLoadException: Could not load type 'System.Windows.Forms.ContextMenu'`。
   **修法**：用 `vendor/webview2/` 里的 `lib_manual/netcoreapp3.0` 版**原地覆盖**（Core + WinForms 两个）。
   不能自己先 `clr.AddReference` —— pywebview 之后还会按自己的路径再加载一次同名程序集，
   .NET 会直接 `FileLoadException: Assembly with same name is already loaded`。
2. `webview/platforms/winforms.py` 的 `class OpenFolderDialog` 类体在 import 时就解析
   `System.Windows.Forms.FileDialogNative+IFileDialog` 等私有成员，这些在 .NET 7+ 也已移除 →
   `AttributeError: 'NoneType' object has no attribute 'GetMethod'`。
   **修法**：把该类的属性区换成容错版本（缺失成员变成「一调用就报清晰错误」的占位），让 import 通过。
   本程序不用「选择文件夹」对话框，无功能损失。

```powershell
.venv\Scripts\python.exe tools\patch_pywebview.py          # 打补丁（幂等）
.venv\Scripts\python.exe tools\patch_pywebview.py --check   # 只检查
```

> **打包前必须打**：PyInstaller 会把 `winforms.py` 编译进 PYZ，冻结之后无法再改源码。
> `tools/build_desktop.py` 已把这一步自动化（除非显式 `--no-patch`）。

## 3. 构建

前置：`chaoxing-fanya\.venv`（Python 3.12）里有 `pywebview 6.2.1`、`pythonnet 3.2.1`、
`clr-loader 0.3.1`、`pyinstaller 6.22.3`；系统装有 **WebView2 Runtime**。
（PySide6、PyQt5 都不需要，spec 里已排除。）

```powershell
cd D:\03-开发\项目\ds\chaoxing-fanya
.venv\Scripts\python.exe tools\build_desktop.py            # 补丁 → 检查 web/dist → PyInstaller → 产物校验
.venv\Scripts\python.exe tools\build_desktop.py --run      # 再顺带启动一次做人工确认
```

参数：`--no-patch` 跳过补丁（不推荐）、`--skip-build` 只补丁 + 校验。

产物：`dist\chaoxing-fanya\chaoxing-fanya.exe`（约 18 MB）+ `_internal\`（onedir）。
`chaoxing-fanya.spec` 里 `datas` 必须包含 `web/dist`（界面）、`resource`（字体混淆映射表等）、
`vendor/webview2`（补丁用的程序集）、`tools/patch_pywebview.py`；
`hiddenimports` 要带上 `webview` / `clr_loader` / `pythonnet` 与 `clr`。

## 4. 打安装包

前置：**Inno Setup 6**（`ISCC.exe`）。本机装在
`C:\Users\35278\AppData\Local\Programs\Inno Setup 6\ISCC.exe`。

```powershell
.venv\Scripts\python.exe tools\build_installer.py                       # 自动找 ISCC
.venv\Scripts\python.exe tools\build_installer.py --iscc "C:\...\ISCC.exe"
```

产物：`dist\installer\chaoxing-fanya-setup-1.0.0.exe`（约 63 MB）。
脚本：`installer\chaoxing-fanya.iss`。

* 安装到 **`%LOCALAPPDATA%\Programs\ChaoxingFanya`**（当前用户，`PrivilegesRequired=lowest`，
  **不弹 UAC**）；开始菜单建「超星学习通助手」文件夹；桌面图标为可选任务（默认不勾）。
* **不打包运行时数据/凭据**：`data\*`、`.webview\*`、`*.log`、`web_config.json`、`config.ini`、
  `desktop_config.json`、`.chaoxing_app.pid`、`pythonnet.runtimeconfig.json` 都在 `[Files]` 里排除，
  升级安装不会覆盖用户的登录缓存与配置。
* 卸载：Windows「应用」列表，或安装目录里的 `unins000.exe`；卸载会清掉运行时文件（`[UninstallDelete]`）。
* 支持 `/DIR="..."` 指定安装目录、`/VERYSILENT /LOG="..."` 静默安装（用于自动化验证）。

## 5. 实测结论（2026-10-16）

| 项 | 结果 |
| --- | --- |
| 原生窗口 | ✅ 窗口标题「超星学习通助手」，`/api/health` 返回 `{"status":true,"msg":"OK"}`，`/` 200，前端资源正常加载 |
| 打包产物 | ✅ `dist\chaoxing-fanya\chaoxing-fanya.exe` 独立运行（含 WebView2/CLR 加载），窗口与源码模式一致 |
| 安装包编译 | ✅ `Successful compile`，`chaoxing-fanya-setup-1.0.0.exe` 生成 |
| 安装到用户目录 | ⚠️ **本会话内无法完成**，见下 |

**为什么本会话装不了**：这台机器的 DSH 会话把「由我的进程再拉起的子进程」限制在受限令牌里，
子进程对 `%LOCALAPPDATA%\Programs` 与 `HKCU` 的写入会被拒（实测 Python 写 HKCU 报
`[WinError 5] 拒绝访问`，安装器日志报
`Setup was unable to create the directory "C:\Users\35278\AppData\Local\Programs\ChaoxingFanya". Error 5`）。
Inno 安装必须先写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\{AppId}_is1`，
所以**必须由用户自己在资源管理器里双击安装包**（用户会话不受该限制）。
这属于会话环境限制，不是安装包缺陷；安装到工作区目录时，文件安装阶段是成功的
（`Successfully installed the file` ×N），只在写卸载注册表项时被拒并回滚。

## 6. 报错速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `[pywebview] pythonnet cannot be loaded` / `cannot import name 'SystemEvents'` | 没预加载 WindowsDesktop 程序集 | `desktop_app._ensure_dotnet_runtime()` 会 `clr.AddReference('Microsoft.Win32.SystemEvents'/'System.Windows.Forms'/'System.Drawing.Common')`；确认 `PYTHONNET_RUNTIME=coreclr` |
| `FileLoadException: Assembly with same name is already loaded` | 自己先把 WebView2 程序集 `AddReference` 了 | 别抢注：用 `tools/patch_pywebview.py` 原地覆盖 `webview/lib` |
| `TypeLoadException: ... ContextMenu` | `webview/lib` 里还是 net462 版程序集 | 跑补丁（`vendor/webview2` 必须存在） |
| `AttributeError: 'NoneType' object has no attribute 'GetMethod'` | `winforms.py` 的 `OpenFolderDialog` 在 .NET 8 上解析失败 | 跑补丁 |
| 启动后出现带地址栏的 Edge/Chrome 窗口 | 走了浏览器兜底分支 | 看 `desktop_app.log` 里 webview 的报错；确认打包版没排除 `webview`/`clr_loader`/`pythonnet` |
| `RedirectionGuard status ... Enabled in enforcing mode` | Win11 24H2+ 的进程缓解策略，正常 | 无需处理 |

## 7. 历史遗留（已不用）

`build_desktop_app.bat`、`build_portable.bat`、`clean_and_build_portable.bat`、`quick_build.bat`
是「内嵌 Python + 便携版」时代的老脚本，已被 `tools\build_desktop.py` + `installer\*.iss` 取代。
