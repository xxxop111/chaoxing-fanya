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
| 冒烟测试 | ✅ `tools\smoke_desktop.py` 六项检查全过、`--stop` 干净退出，退出码 0（连跑两次，覆盖 dist 产物） |
| 安装包编译 | ✅ `Successful compile`，`chaoxing-fanya-setup-1.0.0.exe` 生成（60.5 MB） |
| **安装包载荷 + 自启动** | ✅ `tools\verify_installer.py` 全过：14 项检查 + 装出来的 exe 通过冒烟测试，退出码 0 |
| 装到 `%LOCALAPPDATA%\Programs` | ⚠️ **本会话内做不了**，需用户双击；见 5.1 |

### 5.1 装不了的环境里，怎么证明安装包是对的

完整静默安装要写 `HKCU\...\Uninstall\{AppId}_is1`（卸载项）与 `%APPDATA%`（开始菜单快捷方式），
而这台机器的 DSH 会话把「我的进程再拉起的子进程」限制在受限令牌里 —— 这两处写入必被拒，
Inno 于是整体回滚（日志：
`Setup was unable to create the directory "C:\Users\35278\AppData\Local\Programs\ChaoxingFanya". Error 5`）。

`tools\verify_installer.py` 绕开的正是这一点：它派生一份**测试用 .iss**，只砍掉上面两处必然被拒的动作
（`Uninstallable=no` + 清空 `[Icons]`），把 `[Run]` 改成 `--no-window`，其余流程原样跑：

```powershell
.venv\Scripts\python.exe tools\verify_installer.py          # 装到工作区目录，验完自动清理
.venv\Scripts\python.exe tools\verify_installer.py --keep   # 保留安装目录便于人工查看
```

它验证：静默安装退出码 0 → 主程序 / `_internal` / `web/dist` / `resource` / `vendor\webview2` /
补丁脚本都落盘 → **没有**把 `web_config.json`、`desktop_config.json`、`*.log`、`.webview` 打进包
→ `[Run]` 让装好的程序自动起来 → 对**安装目录里的 exe**跑一遍冒烟测试。

仍然需要用户自己做的两件事：写卸载注册表项（否则「应用」列表里没有它）与建快捷方式。
这两步在用户会话里没有任何限制，双击即可。

## 6. 冒烟测试（每次改完打包链必跑）

```powershell
.venv\Scripts\python.exe tools\smoke_desktop.py              # 自动找 dist\ 或安装目录里的 exe
.venv\Scripts\python.exe tools\smoke_desktop.py --window     # 用真实原生窗口测（会弹窗）
.venv\Scripts\python.exe tools\smoke_desktop.py --keep-running
```

它用 `--no-window` 把 exe 拉起来（不弹窗、不打扰人），逐项确认：

1. 进程能启动、端口能监听（5000 起自动探测）；
2. `GET /api/health` → `{"status": true, "msg": "OK"}`（证明 Flask 与整个 `api/` 包在冻结环境里导入成功）；
3. `GET|POST /api/heartbeat`、`POST /api/window-closed` 已注册（桌面壳的关窗协议，缺了会关不掉窗）；
4. `GET /api/version` 正常；
5. `GET /` 返回含 `id="root"` 的 HTML（证明 `web/dist` 被收进包，而不是只有一个后端）；
6. `GET /tasks`（带 `Accept: text/html`）也返回 `index.html` —— 前端用客户端路由时，直接刷新不能 404；
7. `GET /assets/missing.js` 与 `GET /api/nope` 都是 **JSON 404**，不是 200 + HTML；
8. `--stop` 能干净停下。

退出码 0 = 全过；失败时会打印 `desktop_app.log` 末尾若干行。已经跑着实例时它不会启第二个进程，
而是直接对现有实例做检查。

`--stop` 现在是**静默**的（结果写 `desktop_app.log`）：它原本会弹系统模态对话框报「已关闭。」，
而系统模态没人点就把调用方永远卡住 —— 实测让脚本里的 `subprocess.run(timeout=40)` 直接超时。
要回执弹窗就显式 `chaoxing-fanya.exe --stop --dialog`。

> 手动验证「原生窗口」时不要用本工具的 pwsh 会话 `Start-Process` —— GUI 子进程会继承管道，
> 让工具一直等下去（实测把整个调用卡成后台任务）。要么用 `smoke_desktop.py`，
> 要么用 WMI 分离启动：`([wmiclass]'Win32_Process').Create('"<exe>" --no-window')`。

## 7. 报错速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `[pywebview] pythonnet cannot be loaded` / `cannot import name 'SystemEvents'` | 没预加载 WindowsDesktop 程序集 | `desktop_app._ensure_dotnet_runtime()` 会 `clr.AddReference('Microsoft.Win32.SystemEvents'/'System.Windows.Forms'/'System.Drawing.Common')`；确认 `PYTHONNET_RUNTIME=coreclr` |
| `FileLoadException: Assembly with same name is already loaded` | 自己先把 WebView2 程序集 `AddReference` 了 | 别抢注：用 `tools/patch_pywebview.py` 原地覆盖 `webview/lib` |
| `TypeLoadException: ... ContextMenu` | `webview/lib` 里还是 net462 版程序集 | 跑补丁（`vendor/webview2` 必须存在） |
| `AttributeError: 'NoneType' object has no attribute 'GetMethod'` | `winforms.py` 的 `OpenFolderDialog` 在 .NET 8 上解析失败 | 跑补丁 |
| 脚本调 `--stop` 卡住 / `subprocess.run` 超时 | 旧版 `--stop` 会弹系统模态对话框，没人点就一直等 | 已修为默认静默（结果写 `desktop_app.log`）；要弹窗加 `--dialog` |
| 安装包里出现 `winforms.py.orig` | 补丁脚本留在 site-packages 的回滚备份被 `collect_all` 收走 | spec 已过滤 `*.py.orig`；`.iss` 的 `Excludes` 也加了它 |
| 启动后出现带地址栏的 Edge/Chrome 窗口 | 走了浏览器兜底分支 | 看 `desktop_app.log` 里 webview 的报错；确认打包版没排除 `webview`/`clr_loader`/`pythonnet` |
| `RedirectionGuard status ... Enabled in enforcing mode` | Win11 24H2+ 的进程缓解策略，正常 | 无需处理 |

## 8. 历史遗留（已不用）

`build_desktop_app.bat`、`build_portable.bat`、`clean_and_build_portable.bat`、`quick_build.bat`
是「内嵌 Python + 便携版」时代的老脚本，已被 `tools\build_desktop.py` + `installer\*.iss` 取代。
