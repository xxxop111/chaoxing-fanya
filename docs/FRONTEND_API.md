# 前端接口规范（超星学习通助手 · 桌面版）

这份文档是**前端的唯一契约**。后端是单进程 Flask 应用（`app.py`），前端产物放在
`web/dist`，由**同一个** Flask 进程以静态文件方式对外提供；桌面版把它打包成 exe，
用 pywebview / WebView2 开一个**原生窗口**加载 `http://127.0.0.1:<运行时端口>/`。

换前端（重新设计界面）时，这份文档描述的部分**不需要任何后端改动**即可对接。
19 个业务接口（第 3 节表格 1–19）全部**已实现**；另有 2 个由桌面窗口外壳提供的接口
（`/api/heartbeat`、`/api/window-closed`，表格 20–21），**前端必须调用**，否则关窗后进程不退出。

---

## 0. 先看这 7 条（最容易踩）

1. **一律用相对路径** `/api/...`，绝对不要写死 `http://127.0.0.1:5000`。端口在运行时
   从 5000 起挑第一个空闲端口，写死端口会连不上（开发模式单独跑 `python app.py`
   时才是固定的 5000）。
2. **统一信封**：所有接口返回 `{"status": bool, "msg": str, "data": any}`。
   `status === false` 时 `msg` 是服务端给用户看的中文原话，**原样显示**，不要再包一层
   「操作失败」（后端已经把真实原因写在里面了）。
3. **后端无会话**：没有 token、没有 cookie 会话。每个需要账号的 POST 都要**重复**带上
   `username` / `password`，或 `"use_cookies": true`（复用本机 `cookies.txt`，
   必须先成功密码登录过一次）。
4. **只有轮询**，没有 WebSocket / SSE。任务状态 1 秒一次，日志 1 秒一次。
5. **日志是队列消费**：`GET /api/logs/<task_id>` 一次取走并清空。同一任务**只能有一个
   消费方**，两个界面同时轮询会把日志劈成两半。
6. 失败响应的 HTTP 状态码是 400 / 401 / 404 / 500，但**不要只看状态码**：有的失败是
   HTTP 200 + `status:false`（例如审批被拒绝）。以 `status` 字段为准。
7. **桌面模式要打心跳**：每 5 秒 `GET /api/heartbeat`，并在 `pagehide` 时
   `navigator.sendBeacon("/api/window-closed", "")`（§4.20 / §4.21）。这两个接口
   只在桌面壳里存在，浏览器/开发模式会 404 —— 要写成「有就调、没有就算了」。

---

## 1. 运行方式与基址

| 场景 | 基址 | 谁启动 |
| --- | --- | --- |
| 桌面版（打包后） | `http://127.0.0.1:<动态端口>/` | 桌面入口在后台线程里 `make_server`，窗口加载该地址 |
| 开发（只想调前端） | `http://127.0.0.1:5000/` | 手动 `python app.py`（读 `CHAOXING_PORT`） |
| 开发（Vite HMR） | 后端 `5000` + 前端 `5173` | 前端在 vite 里配 `/api` 代理到 5000 |

* 只监听 `127.0.0.1`，不对外服务，因此**没有鉴权**，也不需要加。
* CORS 已对全部来源开放（`flask_cors`），所以 Vite 开发模式下跨端口直接调也可以。
* 桌面版窗口不显示地址栏；前端不要依赖 `window.location.reload()` 之类的浏览器能力，
  也不要用 `target="_blank"` 打开外链（窗口没有新标签页），需要跳外链请用
  `/api/...` 拿数据后自己渲染，或调桌面壳提供的外链接口（见 §7.3）。

## 2. 通用约定

### 2.1 信封

```json
{ "status": true,  "msg": "登录成功", "data": { "username": "2023xxxx" } }
{ "status": false, "msg": "用户名或密码不能为空" }
```

* `data` 可以是对象、数组，也可以缺失。
* `msg` 缺失时按「成功/失败」给一个通用文案，但不要覆盖服务端返回的 `msg`。

### 2.2 状态码

| 码 | 含义 |
| --- | --- |
| 200 | 正常（含「业务失败但 HTTP 200」的情况，看 `status`） |
| 400 | 参数缺失 / 状态不允许（例如任务不在 running 时请求停止、重复审批） |
| 401 | 登录失败（`/api/login`、`/api/courses`） |
| 404 | `task_id` 不存在（任务只在内存里，进程重启即丢） |
| 500 | 服务端异常，`msg` 里是异常原话 |

### 2.3 时间与进度

* 所有时间戳都是 **Unix 秒（float）**，不是毫秒。
* 进度类字段含义在各接口里写明（`progress` 在「学习」任务里是**已处理课程数**）。

### 2.4 轮询节奏（建议值）

| 内容 | 接口 | 间隔 | 停止条件 |
| --- | --- | --- | --- |
| 任务状态 | `GET /api/task/<task_id>` 或 `GET /api/fanya/result/<task_id>` | 1000 ms | `data.status` ∈ `completed / stopped / error` |
| 日志 | `GET /api/logs/<task_id>` | 1000 ms | 任务结束后再取一次收尾 |
| 盘点结果 | `GET /api/fanya/preview/<task_id>` | 1500 ms | `data.task.status` ∈ 同上，且取到 `preview` |
| 待审批 | `GET /api/fanya/approvals` | 2000 ms（执行中） | 任务结束后停 |
| 窗口心跳 | `GET /api/heartbeat` | 5000 ms | **永不停止**（关窗前一直打；仅桌面壳提供） |

**任务状态机**：`running → completed | stopped | error`，终态不再变化。
`stopped` 表示用户点了停止（当前章节收尾后退出），`error` 时 `error` 字段有原因。

### 2.5 静态托管

* `GET /` → `web/dist/index.html`。
* **客户端路由可用**：`GET /<任意非 api 路径>` 先当成真实文件去 `web/dist` 里找（命中就返回，
  哈希过的资源可以正常缓存），找不到时按下面规则处理 —— 这决定了你的路由该怎么写：
  * `Accept` 含 `text/html` **且**路径末段没有扩展名（「整页导航」的特征）
    → 返回 `index.html`（200）。所以 `BrowserRouter` 那种 `/tasks`、`/courses/123/detail`
    直接刷新也能正常打开；
  * 其余（`.js` / `.css` / 图片等缺失资源，或 `fetch()` 默认的 `Accept: */*`）
    → **JSON 404** `{"status": false, "msg": "Not Found"}`，
    不会回 200 + HTML（那会让浏览器报 MIME 错误，掩盖真正的 404）。
* `GET /api/...` 永远不会命中静态回退：未注册的 `/api/` 路径**一律 JSON 404**，
  即使整页导航过去也只给 JSON。
* 已知接口内部找不到资源（例如 `task_id` 过期）→ 同样是 JSON
  `{"status": false, "msg": ...}`（HTTP 400 / 401 / 404 / 500）。
  历史坑：早期版本里「路径没注册」会落到 Flask 默认的 HTML 404
  （`<!doctype html>…<title>404 Not Found</title>`），现在已统一成 JSON；
  仍然建议取响应体前看一眼 `Content-Type`，别无条件 `response.json()`。
* HTML 响应带 `Cache-Control: no-store`；哈希过的静态资源可以正常缓存。
* **不要假设后端有前端构建**：没有 `web/dist` 时 `/` 返回 404 JSON
  `{"status": false, "msg": "前端未构建…"}`。

---

## 3. 接口总览

| # | 方法 | 路径 | 用途 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | GET | `/api/health` | 存活探测（桌面壳启动时用） | 已实现 |
| 2 | GET | `/api/version` | 前端构建时间戳 | 已实现 |
| 3 | POST | `/api/login` | 校验账号密码 | 已实现 |
| 4 | POST | `/api/courses` | 登录并取课程列表 | 已实现 |
| 5 | GET | `/api/config` | 读本地配置 | 已实现 |
| 6 | POST | `/api/config` | 覆盖写本地配置 | 已实现 |
| 7 | POST | `/api/ai/test` | 测试 AI 接口连通性 | 已实现 |
| 8 | POST | `/api/start` | 开始「章节任务点」自动学习 | 已实现 |
| 9 | GET | `/api/task/<task_id>` | 学习任务状态 | 已实现 |
| 10 | GET | `/api/task/<task_id>/details` | 学习任务明细（课程/章节） | 已实现 |
| 11 | POST | `/api/task/<task_id>/stop` | 优雅停止（学习、任务引擎通用） | 已实现 |
| 12 | GET | `/api/logs/<task_id>` | 取日志（队列消费） | 已实现 |
| 13 | POST | `/api/fanya/preview` | 阶段一：只读盘点课程「任务」 | 已实现 |
| 14 | GET | `/api/fanya/preview/<task_id>` | 盘点进度 + 结果 | 已实现 |
| 15 | POST | `/api/fanya/execute` | 阶段二：确认后执行 | 已实现 |
| 16 | GET | `/api/fanya/result/<task_id>` | 执行进度 + 明细 | 已实现 |
| 17 | GET | `/api/fanya/approvals` | 待审批列表 | 已实现 |
| 18 | POST | `/api/fanya/approve` | 批准/拒绝一条 | 已实现 |
| 19 | POST | `/api/fanya/approvals/clear` | 清空待审批队列 | 已实现 |
| 20 | GET/POST | `/api/heartbeat` | 窗口存活心跳（**前端必须定时打**） | 桌面壳提供 |
| 21 | GET/POST | `/api/window-closed` | 关窗通知（**前端卸载时打**） | 桌面壳提供 |

> 「章节任务点」= 原 `/api/start` 的学习流程；「任务」= 课程页左侧「任务」标签背后
> `task.chaoxing.com` 的任务点（AI实践、主题讨论、作业、文档…）。两条流程互不影响，
> **可以同时跑**，各自有独立 `task_id`。

---

## 4. 逐条接口

### 4.1 `GET /api/health`

```json
{ "status": true, "msg": "OK" }
```

无 `data`。桌面壳用它在启动时确认服务就绪（每个 0.2 秒探一次）。

### 4.2 `GET /api/version`

```json
{ "status": true, "data": { "webBuild": 1760000000 } }
```

`webBuild` = `web/dist/index.html` 的 mtime（秒）。界面底部可以显示它，用于确认
「加载的是不是最新构建」。

### 4.3 `POST /api/login`

请求：

```json
{ "username": "2023xxxx", "password": "***", "use_cookies": false }
```

响应 `data`：`{ "username": "2023xxxx" }`。

* 缺用户名/密码 → 400 `{"status": false, "msg": "用户名或密码不能为空"}`。
* 登录被超星拒绝 → 401，`msg` 是原因（可能要求短信验证码/滑块）。
* 登录成功会把 cookies 落到本机 `cookies.txt`，之后可用 `use_cookies: true` 免密。

### 4.4 `POST /api/courses`

请求 body 与 `/api/login` 相同。响应 `data` 是**课程数组**：

```json
[{
  "id": "…", "info": "…", "roleid": "…",
  "clazzId": "107515845", "courseId": "246831735",
  "cpi": "338350298",
  "title": "数据结构与算法",
  "desc": "…", "teacher": "张三",
  "displayTitle": "数据结构与算法（张三）"
}]
```

* `displayTitle` 是**展示用**标题（同名课程用教师名区分，否则等于 `title`）。
  界面展示一律用 `displayTitle`，**业务请求里传 `courseId`**，不要传标题。
* 登录失败 → 401。
* 这个接口每次都重新登录 + 拉列表，**慢**（数秒到十几秒），不要频繁调。

### 4.5 `GET /api/config`

```json
{ "status": true, "data": { "speed": 1.0, "tiku_config": { … } } }
```

文件不存在时 `data` 为 `{}`（不是错误）。配置存在可写的 `web_config.json`。

### 4.6 `POST /api/config`

请求 = **整份配置对象**（不是补丁，后端是覆盖写）：

```json
{
  "speed": 1.0, "jobs": 4, "notopen_action": "retry", "max_chapters": 0,
  "tiku_config": { … }, "notification_config": { … }, "ocr_config": { … }
}
```

响应 `{"status": true, "msg": "保存成功"}`。body 不是对象 → 400；写盘失败 → 500。

**完整配置结构（前端照这个读写）**

```jsonc
{
  "speed": 1.0,                 // 视频倍速，后端会夹到 [1.0, 2.0]
  "jobs": 4,                    // 并发视频数
  "notopen_action": "retry",    // 未开放章节：retry（重试几次）/ continue（直接跳过）
  "max_chapters": 0,            // 每门课只刷前 N 章，0 = 不限制
  "tiku_config": {
    "provider": "AI",           // AI / SiliconFlow / TikuAdapter / 空
    "tokens": "",               // 题库 token（非 AI 供应商用）
    "url": "",                  // TikuAdapter 地址
    "endpoint": "https://…/v1/chat/completions",  // AI 的 OpenAI 兼容地址
    "key": "sk-…",              // AI 密钥
    "model": "deepseek-chat",   // AI 模型名
    "submit": "false",          // 是否自动提交答题（字符串 "true"/"false"）
    "cover_rate": 0.9,          // 覆盖率阈值
    "delay": 1.0,               // 查询延迟（秒）
    "ai_concurrency": 3,
    "min_interval_seconds": 1.0
  },
  "notification_config": {
    "provider": "",             // ServerChan / Qmsg / Bark / Telegram
    "url": "",                  // 各家的 send 地址（含 key）
    "tg_chat_id": ""            // 仅 Telegram
  },
  "ocr_config": {
    "provider": "",             // siliconflow / openai / claude / qwen / openai_compatible
    "key": "", "endpoint": "", "model": ""
  }
}
```

* `tiku_config` 里 `endpoint` / `key` / `model` 三项**同时**非空才算配好 AI；
  没配好时，需要文字作答的任务点会回退模板文本（日志里会警告）。
* 前端可以在配置里多存自己的字段（后端不校验、原样保存），但**不要**改名上面这些。

### 4.7 `POST /api/ai/test` — 测试 AI 接口

请求可以只带 `tiku_config`：

```json
{ "tiku_config": { "provider": "deepseek", "endpoint": "https://api.deepseek.com/v1/chat/completions",
                   "key": "sk-…", "model": "deepseek-chat" } }
```

* **不带**（或传空对象）时，后端用本地已保存的 `settings.tiku_config`
  —— 前端可以「读完配置直接点测试」。
* 后端真发一条极短请求（温度 0、最多 16 token），所以能验证 key/model 是否真可用。
* 成功：`data.latency` 是毫秒（含重试），`data.sample` 是模型回复（截断 200 字）。

```json
{ "status": true, "data": { "ok": true, "model": "deepseek-chat", "latency": 812, "sample": "OK" } }
```

失败：`status:false`，`msg` 是模型侧原话（`HTTP 401: {"error":…}`）；
此时 `data` 里仍有 `model` 与 `latency`，界面可显示「发往哪个模型、等了多久」。

```json
{ "status": false, "msg": "HTTP 401: {\"error\":{\"message\":\"Authentication Fails\"}}",
  "data": { "ok": false, "model": "deepseek-chat", "latency": 433 } }
```

### 4.8 `POST /api/start` — 章节任务点自动学习

请求：

```json
{
  "username": "2023xxxx", "password": "***",
  "course_list": ["246831735", "246831736"],
  "speed": 1.0, "jobs": 4, "notopen_action": "retry",
  "max_chapters": 0,
  "tiku_config": { … }, "notification_config": { … }, "ocr_config": { … }
}
```

* `course_list` 传 **courseId 字符串数组**；空数组 = 全部课程。
* `notopen_action`：`"retry"`（默认，未开启章节重试几次后放弃）/ `"continue"`
  （遇到未开启章节直接跳过）。界面建议给「未开启章节：重试 / 跳过」两个选项。
* `ocr_config.provider` + `key` 会写进进程环境变量并立即生效（用于公式图片 OCR）。
* 响应 `data`：`{ "task_id": "2023xxxx_1760000000" }`。

**注意**：`course_list` 为空是「全部课程」，界面上要让用户明确看到这一点。

### 4.9 `GET /api/task/<task_id>`

```json
{ "status": true, "data": {
  "status": "running",          // running / completed / stopped / error
  "progress": 1,                // 已处理课程数
  "total": 3,                   // 选中课程总数
  "current_course": "数据结构与算法",
  "current_chapter": "第 3 章 栈与队列",
  "current_task": "",
  "logs": [],
  "start_time": 1760000000.12,
  "stats": {
    "total_chapters": 42, "completed_chapters": 17,
    "total_tasks": 88, "completed_tasks": 31,
    "failed_tasks": 0, "skipped_tasks": 2
  },
  "stop_requested": true,       // 仅在请求过停止后出现
  "error": "登录失败"            // 仅 status=error 时出现
} }
```

`task_id` 不存在 → 404。

### 4.10 `GET /api/task/<task_id>/details`

```json
{ "status": true, "data": {
  "courses": [{
    "id": "246831735", "title": "数据结构与算法",
    "status": "running",        // pending / running / completed / error
    "start_time": 1760000000.5, "end_time": null,
    "error": "…",               // 仅 error 时
    "chapters": [
      { "id": "…", "title": "第 1 章 绪论",
        "status": "completed",  // pending / completed
        "has_finished": true, "jobCount": 3 }
    ]
  }],
  "active_jobs": {              // 正在跑的视频任务，键是超星 jobid
    "work-abc123": {
      "course_name": "数据结构与算法",
      "job_name": "3.2 栈的应用",
      "current_time": 120, "duration": 600,
      "progress": 20,           // 百分数 0~100
      "timestamp": 1760000012.3 // 最后更新时间，超过 10 秒无更新会被后端移除
    }
  }
} }
```

`active_jobs` 用来画「当前视频进度」条；条目在**超过 10 秒没有心跳**时由后端自动清理，
所以前端不要缓存它，每次以最新响应为准。

### 4.11 `POST /api/task/<task_id>/stop`

* 任务不存在 → 404；任务不是 `running` → 400
  `{"status": false, "msg": "任务当前状态为 completed，无需停止"}`。
* 成功 → `data: {"task_id": "…", "stop_requested": true}`。
* 语义是**优雅停止**：不再开始新章节，当前章节收尾后退出（不会硬杀线程）。
  界面文案建议写「停止（当前章节收尾后退出）」。
* 对「任务引擎」任务同样适用。

### 4.12 `GET /api/logs/<task_id>`

```json
{ "status": true, "data": [
  { "message": "开始学习课程：数据结构与算法", "level": "info", "timestamp": 1760000001.02 },
  { "message": "章节完成", "level": "success", "timestamp": 1760000003.4 }
] }
```

* **队列消费**：取走的条目不再返回；`data` 可能是空数组。
* `level` 由后端按关键词判定，可能取 `info / success / warning / error`
  —— 前端按这四色渲染即可，不要自己再猜级别。
* 建议前端保留自己的历史缓冲（后端只保留最近一次未读）。
* 任务不存在时返回 200 + 空数组（日志队列不校验 task_id 是否存在）。

### 4.13 `POST /api/fanya/preview` — 阶段一（只读盘点）

请求：

```json
{
  "username": "…", "password": "…", "use_cookies": true,
  "course_list": ["246831735"],
  "tiku_config": { … },
  "options": { "redo": false }
}
```

* `use_cookies` **默认 true**（前端不传就是复用 cookies）；为 true 时
  `username/password` 可以不传。两者都没有 → 400。
* `options.redo: true` = 连已完成的任务点也盘点出来（用于重做）。
* 响应 `data`：`{ "task_id": "fanya-preview-2023xxxx-1760000000" }`。

**盘点绝不修改任何学习记录**，可以放心让用户点。

### 4.14 `GET /api/fanya/preview/<task_id>`

```json
{ "status": true, "data": {
  "task": { "status": "running", "progress": 1, "current_course": "…",
            "stats": { "pending": 0, "done": 0, "failed": 0, "skipped": 0 } },
  "preview": { … 盘点完成后才有，否则 null … }
} }
```

`preview` 结构（这是「任务点清单」页面主要渲染的数据）：

```jsonc
{
  "summary": {
    "courseCount": 2,        // 有任务引擎的课程数
    "taskCount": 5,
    "pendingCount": 12,      // 未完成的任务点数
    "completableCount": 9,   // 其中可自动完成
    "manualCount": 2,        // 需人工（考试未到时间、需老师评分…）
    "unsupportedCount": 1    // 类型暂不支持
  },
  "courses": [{
    "courseId": "246831735", "classId": "107515845", "cpi": "338350298",
    "title": "审计学", "hasTaskEngine": true, "error": "",
    "tasks": [{
      "taskId": "…", "name": "第三章 综合练习", "progress": 0.0,
      "allowStudy": true, "allowStudyMessage": "",
      "encryTaskUserId": "…",
      "pendingCount": 3, "completableCount": 2,
      "groups": [ /* 原始分组数据，前端一般不用 */ ],
      "items": [{
        "planId": 12345,             // ★ 执行时用它指定要跑哪些任务点
        "name": "综合实践小结上传",
        "planType": 7,
        "typeLabel": "AI剧情",        // 展示用类型名
        "isFinish": false,
        "isLock": false,
        "chooseStudy": false,
        "status": "auto",            // auto / partial / manual / pending
        "handler": "ai_story",       // 后端处理器名，仅供排查
        "supported": true,           // == status ∈ {auto, partial}
        "note": "…给用户看的一句话说明…",
        "skipReason": "",            // 跳过原因（可能为空）
        "aiMode": "story",           // AI实践子模式，可能 null
        "aiModeLabel": "AI剧情",
        "aiRules": ["…"],
        "entryUrl": "https://…",     // 任务点入口（可作为「打开看看」）
        "quizExpired": false,
        "quizWindow": "2026-10-01 08:00 ~ 2026-10-08 20:00"
      }]
    }]
  }]
}
```

`status` 取值含义：

| status | 含义 | 界面建议 |
| --- | --- | --- |
| `auto` | 可全自动完成 | 默认勾选 |
| `partial` | 能自动做一部分（如视频只看时长） | 勾选，提示 |
| `manual` | 需人工（考试窗口未开、需老师评分） | 禁选 + 显示 `note` |
| `pending` | 未能识别子模式 / 处理器尚未实现（计入 `summary.unsupportedCount`） | 禁选 + 显示 `note` |

### 4.15 `POST /api/fanya/execute` — 阶段二（真正写入）

请求：

```json
{
  "username": "…", "password": "…", "use_cookies": true,
  "course_list": ["246831735"],
  "plan_ids": [12345, 12346],
  "tasks_by_course": { "246831735": ["taskId1"] },
  "tiku_config": { … },
  "options": {
    "redo": false,
    "require_approval": true,
    "allow_ai_submit": false,
    "exam_submit": false,
    "exam_max_questions": 50,
    "group_submit": false,
    "group_force_overwrite": false,
    "ladder_threshold": 80,
    "ladder_attempts": 3,
    "video_speed": 1.0,
    "video_max_seconds": 7200,
    "doc_read_max_seconds": 1800,
    "browser_headless": true
  }
}
```

* `plan_ids` 为空 = 处理所有可自动化任务点；非空 = **只跑这些**（从盘点结果里勾选）。
* `tasks_by_course` 一般不用（留空即可）。
* `require_approval` 默认 **true**：所有会写入账号的内容都**先生成、不提交**，
  登记到待审批队列，等用户在界面上逐条批准。想全自动就显式传 `false`。
* `allow_ai_submit: true` 时，AI实践类的作答**不再逐条等待审批**（其余类型仍按
  `require_approval`）。
* `exam_submit` / `group_submit` 分别控制**考试/测验**与**分组任务**是否真的交卷，
  默认 false（只做不打分/不交卷的部分）。
* `browser_headless` 默认 **true**：需要真实浏览器的任务点（阅读时长等）用系统
  Edge/Chrome **后台隐藏运行**（playwright `headless=True`），用户看不到窗口；
  只有排查问题时才传 `false` 让窗口可见。
* 响应 `data`：`{ "task_id": "fanya-run-2023xxxx-1760000000" }`。

### 4.16 `GET /api/fanya/result/<task_id>`

```json
{ "status": true, "data": {
  "task": {
    "status": "running", "kind": "fanya_execute", "progress": 0,
    "current_course": "审计学", "current_task": "第三章 综合练习",
    "stats": { "pending": 3, "done": 2, "failed": 0, "skipped": 1 },
    "start_time": 1760000000.0, "error": "…"
  },
  "result": { "results": [ … 明细，执行结束后才有 … ] }
} }
```

明细每一项：

```jsonc
{
  "courseId": "246831735", "courseTitle": "审计学",
  "taskId": "…", "taskName": "第三章 综合练习",
  "planId": 12345, "name": "综合实践小结上传",
  "typeLabel": "AI剧情", "planType": 7, "handler": "ai_story",
  "ok": true,               // 是否成功
  "skipped": false,         // 是否被跳过（此时 ok=false 且 message 有原因）
  "verified": true,         // 完成后回查任务点状态：true/false/null(未知)
  "message": "任务点完成: …（任务点状态已确认完成）"
}
```

* 统计口径：`stats.done` = `ok:true`；`stats.skipped` = `skipped:true`；其余算 `failed`。
* `verified: false` 时 `message` 里会带「注意：任务点状态仍为未完成，可能需要等待老师
  评分或平台同步」——这是**正常**情况（例如老师关闭了 AI 批阅），界面不要标红成失败。

### 4.17 `GET /api/fanya/approvals?include_decided=1`

```json
{ "status": true, "data": {
  "pending": 2,
  "items": [{
    "id": "ap-7",
    "kind": "discussion",        // discussion / course_work / ai_code / ai_upload / ai_ladder …
    "label": "审计学 / 第三章 综合练习 / 主题讨论",
    "preview": "生成的回答正文（可能上千字）",
    "created": 1760000012.3,
    "status": "pending",         // pending / approved / rejected / failed
    "result": "",                // 决策后的结果文案
    "extra": { }                 // 附加信息（如题目 id，前端可忽略）
  }]
} }
```

* 不传 `include_decided` 只返回 `pending`；传 `1` 返回全部（含已决策）。
* 队列**只在内存**里：进程重启即失效（此时审批会返回「不存在」）。

### 4.18 `POST /api/fanya/approve`

```json
{ "id": "ap-7", "approve": true }
```

* 缺 `id` → 400。成功 → `data: {"id": "ap-7", "approve": true}`。
* 批准会**立刻执行提交动作**（可能占满几秒到几十秒，前端按钮要转圈禁用）。
* 拒绝 → 丢弃，不写入任何内容。
* 每条只能决策一次：重复点会 400 `{"status": false, "msg": "该项已经是 approved 状态…"}`。

### 4.19 `POST /api/fanya/approvals/clear`

```json
{ "keep_decided": false }
```

响应 `data: { "remaining": 0 }`（清空后剩下的条目数）。
`keep_decided: true` 时保留已决策的记录，只扔掉 `pending`。

### 4.20 `GET|POST /api/heartbeat` — 窗口心跳（桌面壳提供）

**这两个端点不在 `app.py` 里，而是桌面壳 `desktop_app.py` 注册的**，所以只在桌面模式存在，
纯浏览器 / Vite 开发模式下会 404 —— 前端要容忍（用 `?.` 或 catch 吞掉）。

```json
{ "status": true }
```

* 前端**每 5 秒**打一次，`GET`、`POST` 都行，无参数、无请求体。
* 心跳到达会**撤销**「关窗判定」：刷新页面（`pagehide` → 重新加载 → 心跳恢复）不会误杀进程。
* 不实现心跳的后果：点关闭后程序仍常驻后台（下次启动命中单实例逻辑，只是把窗口再开出来）。

### 4.21 `GET|POST /api/window-closed` — 关窗通知（桌面壳提供）

```json
{ "status": true }
```

* 前端在 `pagehide` / `beforeunload` 里 `navigator.sendBeacon("/api/window-closed", "")` 打一次。
  `sendBeacon` 固定是 `POST`，所以该端点必须同时接受 `GET` 和 `POST`。
* 收到后桌面壳等 15 秒：期间没有新心跳（真关窗）就退出；有新心跳（刷新/跳转）就撤销，继续运行。
* 不打这个端点也不会立刻出问题：壳还有「心跳安静 180 秒」的兜底判定
  （阈值远大于浏览器后台标签页的定时器限流间隔，避免最小化时被误杀）。

---

## 5. 前端需要的 6 个界面（建议信息架构）

接口已经支撑下面这套，重做界面时可以自由拆分，但**建议保留语义**：

1. **账号页**：`/api/login`（可选）、`/api/courses`；`use_cookies` 开关。
2. **课程与任务点列表**：`/api/courses` → 勾选课程；`/api/fanya/preview` +
   `/api/fanya/preview/<id>` → 任务点树（课程 → 任务 → 任务点），按 `status` 决定
   可勾选性；显示 `summary` 统计卡。
3. **运行页（实时进度 + 日志）**：`/api/fanya/execute` 或 `/api/start` →
   `/api/fanya/result/<id>` 或 `/api/task/<id>`（1 s）+ `/api/logs/<id>`（1 s）+
   `/api/task/<id>/stop`。日志用等宽字体、四色级别、自动滚底（用户手动上滚时暂停自动滚）。
4. **审批页**：`/api/fanya/approvals`（2 s）→ 列表；每条显示 `label` + `preview`
   （长文可折叠/滚动）+ 「批准 / 拒绝」；顶部显示 `pending` 数量角标。
5. **AI 接口配置页**：`/api/config` 读写 `tiku_config`（provider/endpoint/key/model/…）
   + 「测试连接」按钮 → `/api/ai/test`（§7.1）；顺手放 `notification_config`、
   `ocr_config`、`speed` / `jobs` / `max_chapters`。
6. **关于/诊断页**：`/api/health`、`/api/version`（显示 `webBuild`），
   以及当前配置文件路径说明。

---

## 6. 已知坑（照抄结论即可）

1. **端口是动态的** —— 只用相对路径。
2. **`/api/logs` 是破坏性读取** —— 一个任务一个消费方；切换页面时把已收到的日志留在
   前端 store 里，不要指望后端重放。
3. **所有任务状态只在内存** —— 窗口/进程重启后 `task_id` 全部失效，前端要能优雅处理
   404（提示「任务已结束或程序重启」并回到列表页）。
4. **审批队列同样在内存** —— 重启后 `pending` 清空。
5. **`status:false` 也可能 HTTP 200**（审批决策失败就是这种），判断成功必须看 `status`。
6. **`/api/courses` 很慢**（要真登录），不要放在输入框 `onChange` 之类的地方；
   加按钮触发 + loading 态。
7. **`msg` 直接展示**：里面是超星/模型的原话（例如「任务点学习时间：… 当前不在学习时间内」），
   这是排查问题最快的线索，别用「操作失败」盖掉。
8. **`preview.courses[].error` 非空**表示这门课的任务列表没读到（网络/权限），
   不是整体失败：其余课程照常显示。
9. **超星同名课程**：展示用 `displayTitle`，请求用 `courseId` / `clazzId`。

---

## 7. 可选增强与桌面壳能力

### 7.1 `POST /api/ai/test`

**已实现**，契约见 §4.7（不再在这里重复描述，避免两处漂移）。

### 7.2 `GET /api/fanya/approvals/stream`（可选，SSE）

若希望审批不需要轮询，可加 SSE。**当前不实现**，前端按 2 秒轮询写就行。

### 7.3 桌面壳能力（窗口进程提供）

1. **窗口上的注入对象**：外链、打开日志目录、最小化到托盘这类需求，桌面壳会在 `window` 上
   注入 `window.desktop`（例如 `window.desktop.openExternal(url)`）。前端**必须**先判断存在性
   （`window.desktop?.openExternal?.(url)`），这样浏览器/开发模式下同一份前端也能跑。
2. **必须实现的两个回执**：`/api/heartbeat`（每 5 秒）与 `/api/window-closed`（`sendBeacon`），
   契约见 §4.20 / §4.21。桌面壳靠它们做到「关掉窗口就退出」，没有它们进程会常驻。

---

## 8. 一页速查

```
基址        http://127.0.0.1:<port>/      ← 用相对路径
信封        {status, msg, data}          ← status=false 时原样显示 msg
鉴权        无（仅回环）；每个 POST 带 username/password 或 use_cookies:true
任务        POST /api/start               → task_id（章节任务点）
            POST /api/fanya/preview       → task_id（只读盘点）
            POST /api/fanya/execute       → task_id（写入执行）
轮询        GET  /api/task/<id> | /api/fanya/result/<id>   1 s
            GET  /api/logs/<id>（队列消费）                1 s
停止        POST /api/task/<id>/stop
审批        GET  /api/fanya/approvals     → 列表
            POST /api/fanya/approve       {id, approve}
配置        GET/POST /api/config          ← 整份覆盖写
窗口心跳    GET/POST /api/heartbeat       ← 每 5 s（仅桌面壳提供）
关窗通知    POST /api/window-closed       ← pagehide 时 sendBeacon（仅桌面壳提供）
```
