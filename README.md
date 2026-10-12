# 超星学习通课程自动化助手（Web + CLI + 桌面版）

<p align="center">
  <a href="https://github.com/xxxop111/chaoxing-fanya" target="_blank">
    <img src="https://img.shields.io/github/stars/xxxop111/chaoxing-fanya" alt="GitHub Stars" />
  </a>
  <a href="https://github.com/xxxop111/chaoxing-fanya" target="_blank">
    <img src="https://img.shields.io/github/forks/xxxop111/chaoxing-fanya" alt="GitHub Forks" />
  </a>
  <a href="https://github.com/xxxop111/chaoxing-fanya" target="_blank">
    <img src="https://img.shields.io/github/license/xxxop111/chaoxing-fanya" alt="License" />
  </a>
  <a href="https://github.com/xxxop111/chaoxing-fanya" target="_blank">
    <img src="https://img.shields.io/github/languages/code-size/xxxop111/chaoxing-fanya" alt="Code Size" />
  </a>
  <a href="https://github.com/xxxop111/chaoxing-fanya/actions/workflows/tests.yml" target="_blank">
    <img src="https://github.com/xxxop111/chaoxing-fanya/actions/workflows/tests.yml/badge.svg" alt="Tests" />
  </a>
</p>

> 基于 [Samueli924/chaoxing](https://github.com/Samueli924/chaoxing) 二次开发：补全 Web 可视化、OCR、题库、外部通知与便携打包能力，向原作者致敬。

## 界面预览

**课程管理** —— 勾选课程，再选择走哪条流程。同一门课教多个班时标题会重复，
界面自动补上教师名区分（截图中的两门「数据结构与算法」）：

![课程管理](docs/images/course-list.png)

**自动完成任务** —— 盘点结果会摊开每个任务点的**类型**和**能做到什么程度**。
请留意那些 `需先备材料`、`暂未支持` 的标注：工具会直接告诉你它哪里做不到，
而不是假装全能：

> 这张截图摄于「AI剧情」识别之前，图中那一行当时显示为 `未知模式(7)`；现在它会
> 显示 `AI剧情型：协议已完整逆向…评分需浏览器会话，拿不到分数，需手动完成`。
> 其余内容未变。
>
> 说明这个项目的取舍：**协议探明 ≠ 功能可用**。AI剧情 的接口、参数、章节结构都
> 逆向出来了（见 [协议文档](docs/FANYA_TASK_API.md)），发消息、交卷也都能跑通，
> 但**评分环节需要浏览器建立的会话**，纯 HTTP 拿不到分数，所以这里标的是
> 「需手动」而不是「已实现」。**宁可标低，不标高。**

![任务盘点结果](docs/images/tasks-scan.png)

## 我在这个 fork 里做了什么

> 上游是 `Samueli924/chaoxing` → `ymylive/chaoxing-fanya` → `sweetcornna/chaoxing-fanya`。
> 核心的 `api/` 逆向实现、题库抽象、章节任务处理来自上游；下面是我这个 fork 的增量。

**1. 接入「任务引擎」子系统（`api/fanya_*.py`）**

超星有两套互不相通的系统：`mooc1` 的**章节任务点**（上游已支持）和
`task.chaoxing.com` 的**「任务」标签**（上游完全没有）。后者是课程页左侧那个「任务」，
里面的任务点类型和前者完全不同。我补了整层：

- 任务点发现：按课程/班级遍历「任务」树，逐个取任务点与完成状态
- 类型适配器注册表 + 12 个处理器：视频、音频、文档、章节、作业、测验、自测、
  主题讨论、分组任务、AI实践（四种模式）
- `effective_status()` 防呆：任何在界面上标成「可自动」但**没有对应处理器**的类型，
  会被自动降级并说明原因 —— 避免"界面承诺了、执行却跳过"

**2. 「生成后暂停、逐条审批」流程**

会写入账号的内容（作业、讨论回复、AI实践作答…）默认**只生成、不提交**，
在界面「待审批」面板里摊开给你看，逐条批准后才真正提交。
动机很实际：早期版本直接把内容交上去过，出过事故。

**3. AI实践四种子模式的协议实现**

`data.type` 区分多种模式，接口与提交格式完全不同，逐一逆向：

| 模式 | 状态 |
| --- | --- |
| 代码实践（type=4） | 实测完成，得分 76 |
| 上传作品（type=1） | 实测完成，得分 93 |
| 思维阶梯（type=2） | **实测完成，得分 85**（2026-10-12：拆题 → 8 轮逐题作答含多选 → 交卷 → 结题报告出分，平台判定任务点完成）|
| 情景对话（type=0） | 已实现，本账号无未过期样本，未实测 |
| AI剧情（type=7） | 协议已**完整逆向**（章节结构 / 参数 / 37 个端点，见 [协议文档](docs/FANYA_TASK_API.md)）；发消息与章节交卷实测可用，但**评分必须由浏览器建立对答会话**（纯 HTTP 调 `evaluate/stream` 返回「会话不存在」），故自动作答拿不到分数 → **需人工**。实测人工粘贴高质量内容可得 **100 分** |

> 现在只剩「情景对话」没有真机验证；「AI剧情」则更进一步 —— 协议虽已完整探明，
> 但写入链路实测不通，因此按「需人工」标注。「AI剧情」无法试错的
> 原因写在协议文档里：该实例的作答记录 `enableReset=false`（**不可重置**），
> 试错的代价是永久污染用户真实作答 —— 宁可标注「未验证」也不猜着实现。

**4. Windows 桌面版（`desktop_app.py`）**

一键打包、无控制台窗口、关闭窗口即退出。顺带做了启动性能优化：
原来用 `urllib` 探测端口判断"是否已有实例"，而本机装了代理时 `127.0.0.1`
也会走代理，每次探测干等 1 秒超时 —— 12 个端口白等 **12 秒**。
改成裸 socket 并行探测 + 绕过代理的 opener 后降到 **0.14 秒**。

**5. 协议文档 [docs/FANYA_TASK_API.md](docs/FANYA_TASK_API.md)**

把实测出来的任务引擎接口写成了文档，并**逐条标注「已验证 / 推断 / 未验证」**。
这是仓库里最"别人没有"的部分。

**6. 修掉的静默失败（这类 bug 最危险）**

「报告成功、其实做错」比崩溃更糟，因为你会信任它。已修：

- 课程作业**判断题**必须发 `true`/`false`，原来发「对/错」→ 服务端直接拒绝整份提交
- 上传作品的附件载荷是**扁平数组**而非卡片结构，发错时服务端返回
  `{"status":true}` 却静默忽略 → 交出没有附件的空记录。现在**交卷前回读校验**，
  挂不上就中止
- 配置文件带 UTF-8 BOM 时整份读不出（连带 `KeyError`），改用 `utf-8-sig` 读取
- 前端轮询无失败处理 → 后端重启后永久转圈；现在有失败计数与超时上限

## 已知限制（诚实清单）

- **未实测**：主题讨论的实际发帖、自测/测验的正式交卷、上传作品的多附件
- **不支持**：作业/测验的自动重做（平台的重做是自有 UI 流程，请求格式未探明）；
  分组任务拒绝覆盖组员已有内容
- **解析脆弱**：所有接口都是逆向来的，平台改版会**静默失效**。解析层已有 17 个
  单元测试（`tests/`，零依赖可跑），但**接口层没有自动化测试** —— 平台改版仍需人工发现
- **AI 生成内容的边界**：部分题目原文要求「用自己的话整理、禁止复制 AI 输出」，
  工具对此默认只出草稿。请自行判断是否符合你所在课程的学术规范

## 功能亮点

- **可视化覆盖 100% 功能**：React + TailwindCSS 前端，桌面/移动自适应，实时日志与进度
- **「任务」模块自动完成**：补全原项目只刷章节的空白 —— 盘点课程页「任务」标签下的
  AI实践 / 主题讨论 / 作业 / 文档 / 视频 / 自测 / 测验等任务点，可自动化的直接完成，
  不可自动化的明确标注原因（详见 [协议文档](docs/FANYA_TASK_API.md)）
- **一键上手**：Windows `start.bat` 检查依赖后即启动前后端；支持 Docker 与便携打包
- **题库全家桶**：Yanxi / LIKE / TikuAdapter / AI / SiliconFlow，可调覆盖率与自动提交
- **OCR 多方案**：内置 PaddleOCR 或外部大模型（OpenAI / Claude / Qwen / SiliconFlow 等）
- **通知渠道**：Server酱 / Qmsg / Bark / Telegram 等完成 & 错误推送
- **CLI 同步**：命令行模式与 Web 功能一致，便于集成与自动化

## 快速开始（推荐一键）

```bash
git clone --depth=1 https://github.com/xxxop111/chaoxing-fanya
cd chaoxing-fanya
start.bat  # Windows 双击或命令行运行
```

启动后浏览器会自动打开 `http://localhost:3000`，按界面提示登录并开始学习。

### 桌面版（Windows，推荐）

一条命令构建（会先构建前端，再用 PyInstaller 打包）：

```bat
build_desktop_app.bat            :: 只构建到 dist\chaoxing-fanya\
build_desktop_app.bat deploy     :: 构建并部署到桌面
```

产物是 `dist\chaoxing-fanya\chaoxing-fanya.exe`，双击即用：

* **无控制台黑框**（`console=False`）
* 只监听 `127.0.0.1:5000`，**关闭 Flask debug**（避免 debugger 暴露）
* 运行数据就在 exe 同目录：`cookies.txt`（登录态）、`web_config.json`（题库配置）、`cache.json`

停止运行中的实例：

```bat
chaoxing-fanya.exe --stop
```

> ⚠️ **覆盖升级时注意**：`build_desktop_app.bat` 的 deploy 步骤会先
> `rd /s /q` 删掉整个目标目录 —— 那会**连同你的 `cookies.txt`（登录态）和
> `web_config.json`（API 密钥）一起删掉**。手工升级请只替换 `_internal`
> 和 exe，保留这几个运行时文件。

#### 启动体验（打包版 `chaoxing-fanya.exe`）

打包为 `--noconsole`，行为对齐普通桌面程序：

* **没有黑框**：不再弹控制台窗口，出问题改为弹 Windows 消息框；
* **启动快**：正常**约 1 秒**就绪并弹出窗口（全新部署首次约 2.5 秒）。
  > 关键修复：原先用 `urllib` 探测 12 个端口来判断「是否已有实例」，
  > 而本机若装了代理（如 Clash `127.0.0.1:7890`），`urllib` 会把 `127.0.0.1`
  > 的请求也走代理，每个端口干等 1 秒超时 —— 白白多花 **12 秒**。
  > 现改为**裸 socket 并行探测 + 绕过代理的 opener**，实测 12.15s → 0.14s。
* **再点一次图标是秒开**：已检测到运行中的实例时，直接开窗口后立即退出（约 0.2 秒）；
* **关掉窗口就退出**：页面每 5 秒心跳，关闭窗口时用 `sendBeacon` 通知后端，
  安静 15 秒后自动退出；若浏览器被杀/崩溃（收不到关闭信号），
  心跳消失 180 秒也会退出。刷新页面不会误杀（心跳会立刻恢复）。
  阈值取得比浏览器后台限流（约 1 次/分钟）宽松，最小化窗口不会误退出。
* 窗口仍是 Edge 的 `--app` 模式（无地址栏、无标签栏）。
  > 曾尝试用 pywebview 内嵌 WebView2 做「真原生窗口」，但本机走不通：
  > pythonnet 在 .NET Framework 与 CLR 两种运行时下都无法解析
  > `Python.Runtime.Loader.Initialize`；改用 coreclr 后，pywebview 的 WinForms
  > 宿主又依赖 .NET Core 已移除的 `System.Windows.Forms.ContextMenu`。
  > 因此默认不再尝试（省掉每次约 2 秒的无效探测），
  > 换机器想试可设 `CHAOXING_USE_WEBVIEW=1`。

### 其他运行方式

**手动启动（前后端分开）**
```bash
# 后端
pip install -r requirements.txt
python app.py        # 默认 http://localhost:5000

# 前端（新终端）
cd web
npm install
npm run dev          # 默认 http://localhost:3000
```

**Docker**
```bash
docker build -t chaoxing .

# 使用默认模板
docker run -it chaoxing

# 挂载自定义配置
docker run -it -v /本地路径/config.ini:/config/config.ini chaoxing
```
- 首次运行会将 `config_template.ini` 复制到 `/config/config.ini`，可自行覆盖挂载。

**便携打包**
```bash
clean_and_build_portable.bat
```
生成 `chaoxing_portable` 目录，免安装 Python 直接分发。

**命令行模式**
```bash
python main.py                               # 使用默认配置模板
python main.py -c config.ini                 # 指定配置
python main.py -u 手机号 -p 密码 -l 课程ID1,课程ID2 -a [retry|ask|continue]

# 「任务」模块（任务引擎）：先只读盘点，确认后再执行
python main.py --fanya-tasks                 # 只盘点，打印可自动化清单
python main.py --fanya-tasks --fanya-execute # 真正执行自动完成
python main.py --fanya-tasks --fanya-execute --fanya-plans 8138339,8138404
```

## 「任务」模块自动完成

原项目的「开始学习」只处理**章节任务点**；课程页左侧「任务」标签由独立的任务引擎
（`task.chaoxing.com`）提供，两者互不相通。本功能专门处理后者。

Web 端流程（刻意分成三步，避免误触改写作业状态）：

1. 课程管理页点击 **自动完成任务（任务标签）**；
2. 点 **开始盘点任务** —— 只读取数据，列出每门课的每个任务点、类型、完成条件与
   能否自动完成；
3. 勾选要处理的任务点，点 **确认执行** —— 但**默认不会直接提交**，见下节。

### 审批模式（默认开启）：生成 → 暂停 → 逐条批准

会**写入你账号**的内容一律先进待审批队列，你核对过才提交：

| 类型 | 审批模式下的行为 |
| --- | --- |
| 作业 | 只读题 + 查题库，**不保存草稿、不提交**（实测审批模式下写请求数为 0）；批准后才提交 |
| 主题讨论 | 只生成回复正文并展示，批准后才发帖 |
| AI实践·代码实践 | 只生成代码并跑完自测，批准后才建卷交卷 |
| AI实践·思维阶梯 / 情景对话 | 暂不支持「生成后暂停」，默认**拒绝直接交卷**；要直接交需开「允许 AI实践直接提交」 |
| 自测 / 测验 | 默认只逐题暂存，不交卷；开「考试交卷」才交 |
| 分组任务 | 默认只生成，不提交；且小组已有内容时仍拒绝覆盖 |
| 视频 / 文档 / 章节 / 知识点 / 音频 | **不进审批**：它们只是学习进度上报，不产生可审阅的内容 |

界面上的「待审批」面板会列出每一条的**完整生成内容**，可逐条「批准并提交」或「拒绝」。
待审批项只存在内存里，进程重启即失效（草稿不该跨进程长期挂着）。

> 另有一条硬性防线：题库偶尔会把「我看不到题目，请提供文字描述」这类**拒答/元回复**
> 当成答案返回。这类文本会被识别并按「未命中」处理 —— 绝不会被写进作业
> （曾经真的发生过一次，把这种话交上去了）。全部未命中时直接拒绝提交。

可自动化程度（全库实测，`planType` 见 `docs/FANYA_TASK_API.md`）：

| 任务类型 | 状态 | 说明 |
| --- | --- | --- |
| 主题讨论 | ✅ 自动（已实测） | 用已配置的 AI 接口生成回复并提交；提交前先检测是否已回复，避免重复 |
| 文档 | ✅ 自动（已实测） | 上报 `documentStudy/readEnd`，任务点由未完成变为已完成 |
| 章节 / 知识点 | ✅ 自动 | `externalDataId` 等于 mooc1 章节 `point["id"]`，复用既有章节学习流程 |
| 视频 | ✅ 自动 | 任务引擎自有播放页 `videoStudy/learnPage`：默认一次到位上报 `videoDataLog`；被拒时按真实时间模拟播放 |
| 音频 | ✅ 自动（无样本未实测） | 与视频同构（`audioStudy/learnPage` + `audioDataLog`），本账号无该类型任务点 |
| 作业 | ✅ 自动（审批后提交） | mooc2 课程作业（`mooc2/work/task` → `addStudentWorkNewWeb`）：题库作答，**默认只准备不提交**，批准后才提交 |
| 自测 / 测验 | ⚠️ 半自动 | 题库作答；**默认只暂存不交卷**，需显式勾选「考试交卷」；过期的**无法补完成**（`scoreRefresh` 不是补完成按钮），本账号缺未过期样本故未验证 |
| AI实践·代码实践 | ✅ 自动（已实测完成） | 第 4 种模式（`data.type==4`，本账号数量最多）：AI 生成代码 → `procedural-run` 自测校验 → 建卷交卷 → SSE 评估出分。实测 planId 4107919 `isFinish` false→true、得分 **76**（阈值 60） |
| AI实践·思维阶梯 | ✅ 自动（已实测完成） | `data.type==2`：`think/main-talk` 逐轮作答 → 交卷 → `think/end-report` 出分。**2026-10-12 真机验证**：planId 7881197「静电场环路定理闯关」题目 243 字拆出 8 个小问、逐题作答（含多选）、交卷、结题报告出分，得分 **85**（目标 80），平台 `isFinish` **false→true**。也顺带验证了容错分支：建新卷失败时回退为「续做旧记录」 |
| AI实践·上传作品 | ⚠️ 半自动 | `data.type==1`：按题目要求生成作品（要求「对话实录」的会**真的**和 AI 聊够轮次，文本作品型生成达标字数）→ 渲染成 PNG → 传超星网盘 → 挂附件 → 交卷 → AI 评分。**若题目写明「禁止直接大段复制 AI 输出，用自己的话整理」，默认只出草稿不提交**，需显式勾选「允许上传 AI 原文作品」覆盖 |
| AI实践·情景对话 | ⚠️ 半自动 | `data.type==0`：生成场景 → 多轮对话（默认 5 轮）→ 交卷 → 评估出分。已实现且带中止保护；`/ai-ans` **并非**独立鉴权后端（带 `aiEnc` 时返回 200 + SSE，此前的 401 是缺 `aiEnc`），本账号无未过期样本故未真机验证 |
| 分组任务 | ⚠️ 半自动 | 可用 AI 生成小组答案后提交；**默认不提交**，且默认**拒绝覆盖组员已写好的内容**（需再显式开「强制覆盖」）；完成取决于评分 ≥60 |
| 问卷 | ⚠️ 需人工 | 实际走超星 OA 表单，需管理员侧登录 |
| 线下课堂 / AI课堂 | ⚠️ 需人工 | 由教师记录 / 第三方课堂，客户端不上报泛雅 |

> 只有「处理器真的存在」的类型才会显示为可勾选（见 `effective_status()`），
> 因此界面不会把未实现的类型伪装成可用。任务点有**两级时间窗口**（任务级与任务点级），
> 未到时间的会被自动跳过并在日志中说明。

### 本账号实测覆盖情况

用 `python main.py --use-cookies --fanya-tasks` 在全库盘点（26 门课）：

```
含任务的课程 7｜任务 28｜待完成任务点 127 —— 可勾选自动完成 122（96%），暂未支持 5
```

| 类型 | 待完成 | 其中可勾选 |
| --- | --- | --- |
| 视频 | 42 | 42 |
| 章节 | 25 | 25 |
| 主题讨论 | 14 | 14 |
| 文档 | 14 | 14 |
| 测验 | 11 | 11 |
| 作业 | 6 | 6 |
| AI实践 | 13 | 8（代码实践 5 + 思维阶梯 1 + 上传作品 2；另有 5 个学习窗口未开放读不到子模式。其中 1 个上传作品题目声明「禁止复制 AI 输出」，默认只出草稿） |
| 分组任务 | 2 | 2（可勾选，但运行时会被闸门拒绝：一个已结束、另一个组员已写完答案） |

### 单元测试

**不登录、不联网、不写入任何账号数据**，可安全随时运行（CI 上也在跑）：

```bash
.venv\Scripts\python.exe tests\test_parsers.py     # 直接运行（内置 runner）
.venv\Scripts\python.exe -m pytest tests -q        # 或装了 pytest 时
```

17 个用例覆盖的是**平台一改版就会静默失效**的解析层：选择题 JSON 解析、
题库拒答识别、题干要求识别（字数/轮次/禁止复制）、进度格式化，以及
课程列表去重与同名课程区分。

不需要测试框架（内置 runner，pytest 可选）；但会用到项目本身的运行库，
CI 里装的是实测出的最小集：

```
requests beautifulsoup4 lxml loguru openai httpx tqdm urllib3 fonttools pyaes
```

### 端到端冒烟测试

改动后端或重新打包后，可用它确认 Web 全链路没坏（会**真实执行**你给的任务点）：

```bash
chaoxing-fanya.exe --no-window          # 或：python desktop_app.py --no-window
python tools/smoke_test.py 5000 <planId>
```


## 配置要点（config.ini）

- **登录**：支持账号密码或 cookies.txt（同后端配置说明）
- **题库 `[tiku]`**：`provider=Yanxi|Like|TikuAdapter|AI|SiliconFlow`；`cover_rate=0.0-1.0`；`submit=true|false`
- **未开放任务处理 `[common]`**：`notopen_action=retry|ask|continue`（命令行可用 `-a/--notopen-action` 覆盖）
- **通知 `[notify]`**：`provider=ServerChan|Qmsg|Bark|Telegram`，按注释填写 `url` / `token` / `chat_id` 等
- **OCR**：
  - 本地 PaddleOCR：安装 `paddlepaddle`、`paddlex`，并设置 `CHAOXING_ENABLE_OCR=1`
  - 外部大模型（推荐）：
    ```bash
    export CHAOXING_VISION_OCR_PROVIDER=openai
    export CHAOXING_VISION_OCR_KEY=sk-your-api-key
    export CHAOXING_VISION_OCR_MODEL=gpt-4o
    # 可选：CHAOXING_VISION_OCR_ENDPOINT, CHAOXING_VISION_OCR_PROMPT
    ```

更多详细说明见 [WEB_FRONTEND_GUIDE.md](WEB_FRONTEND_GUIDE.md)、[QUICKSTART.md](QUICKSTART.md)、
[FEATURE_COMPARISON.md](FEATURE_COMPARISON.md)、[WEB_FEATURES.md](WEB_FEATURES.md)。

## 使用流程

1) 登录：手机号+密码，或勾选「使用 Cookie 登录」（需 `cookies.txt`）  
2) 选课：多选或默认全部课程  
3) 配置：倍速、并发、题库、通知、OCR  
4) 开始：一键启动任务，实时查看进度/日志  
5) 监控：完成/异常自动推送（如启用通知）

## 目录速览

```
chaoxing-fanya/
├── app.py                      # Flask 后端入口（Web 版）
├── desktop_app.py              # 桌面版入口：起服务 → 等就绪 → 开无地址栏窗口
├── main.py                     # 命令行入口（章节任务点：视频/文档/作业/章节测验）
├── start.bat                   # Windows 一键启动（Web 版）
├── build_desktop_app.bat       # 构建桌面版（PyInstaller）
├── config.ini.example          # 配置模板（复制为 config.ini）
├── NOTICE                      # 来源与修改声明（GPL-3.0 要求）
├── api/                        # 后端接口
│   ├── fanya_task.py           # 「任务」模块：任务引擎客户端 / 盘点 / 执行器 / 12 个类型处理器
│   ├── fanya_approval.py       # 待审批队列（生成 → 暂停 → 批准才提交）
│   ├── fanya_upload.py         # AI实践「上传作品」模式
│   ├── fanya_ladder.py         # AI实践「思维阶梯」模式
│   ├── fanya_work.py           # 课程作业（含判断题 true/false 格式修正）
│   ├── fanya_discuss.py        # 主题讨论回复
│   └── ...
├── docs/
│   ├── FANYA_TASK_API.md       # 「任务」模块接口说明（逐条标注 已验证/推断/未验证）
│   └── images/                 # README 截图
├── tests/test_parsers.py       # 单元测试（零依赖，17 个用例）
├── tools/smoke_test.py         # 端到端冒烟测试
├── web/                        # 前端（React 18 + Vite + TailwindCSS）
└── resource/                   # 静态资源、模型等
```

## 常见问题

- 端口被占用：`netstat -ano | findstr :5000` / `:3000`，结束占用进程后重试
- 依赖安装失败：后端 `pip install -r requirements.txt --force-reinstall`；前端删除 `node_modules` 与锁文件后重新 `npm install`
- 浏览器未自动打开：手动访问 `http://localhost:3000`，查看启动脚本输出
- Docker 配置未生效：确认挂载路径正确，或在容器内检查 `/config/config.ini`

## 致谢

- 原项目：[Samueli924/chaoxing](https://github.com/Samueli924/chaoxing)
- 社区贡献者与所有用户

## 许可与声明

- 许可证：GPL-3.0，仅允许在相同许可证下开源免费使用与再分发，禁止闭源商业化及任何盈利行为
- 本项目仅供学习交流，使用者需自行承担法律与合规责任


