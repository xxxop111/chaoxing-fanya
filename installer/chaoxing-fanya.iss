; Inno Setup 脚本：把 dist\chaoxing-fanya（PyInstaller onedir 产物）打成安装向导。
;
; 关键选择（理由）：
;   * PrivilegesRequired=lowest + DefaultDirName={localappdata}\Programs\... —— 装到用户目录，
;     全程不需要管理员（不弹 UAC），而且程序要在自己目录里写 desktop_config.json、
;     web_config.json、desktop_app.log、.webview\（WebView2 用户数据），
;     放 Program Files 会被权限拦下。
;   * 不做「单文件 exe」：onedir 才能让 .NET/WebView2 的原生 dll 直接躺在磁盘上，
;     单文件模式每次启动都要解压上千个文件，且 pythonnet/clr_loader 在临时目录里定位
;     自带 dll 很容易失败。
;   * 语言只用 Inno 自带的英文消息表（官方包不含简体中文 isl），中文文案写在 AppName /
;     自定义标签里，界面文字仍是中文。
;
; 编译（需先安装 Inno Setup 6，见 tools\build_installer.py）：
;   ISCC.exe installer\chaoxing-fanya.iss

#define AppName "超星学习通助手"
#define AppVersion "1.0.0"
#define AppPublisher "chaoxing-fanya"
#define AppExeName "chaoxing-fanya.exe"
#define SourceDir "..\dist\chaoxing-fanya"

[Setup]
AppId={{8F3A1C6E-2B47-4E9D-9C51-7A0E5D3B6F12}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\ChaoxingFanya
DisableDirPage=no
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
OutputBaseFilename=chaoxing-fanya-setup-{#AppVersion}
SetupIconFile=..\chaoxing.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
AllowNoIcons=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式（可选）"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
; 排除运行期产物：它们由程序首次启动时生成，而且 web_config.json / desktop_config.json
; 里可能有账号与 AI 密钥，绝不能打进安装包；.webview 是 WebView2 的用户数据目录（几十 MB）。
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: ".webview\*,*.log,desktop_app.log,chaoxing.log,desktop_config.json,web_config.json,config.ini,config.ini.bak,.chaoxing_app.pid,pythonnet.runtimeconfig.json"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 让卸载也收拾掉运行期产物（配置、日志、WebView2 用户数据）
Type: filesandordirs; Name: "{app}\webview-data"
Type: filesandordirs; Name: "{app}\.webview"
Type: files; Name: "{app}\desktop_config.json"
Type: files; Name: "{app}\desktop_app.log"
Type: files; Name: "{app}\.chaoxing_app.pid"
