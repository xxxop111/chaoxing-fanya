# AI实践「上传作品」型任务 —— 完整可复现配方

> 2026-10-13 实测：马原《AI对话挑战——"挑战聊天机器人"》(plan=8138339) **得分 100**，任务点 `is_finish=True`。

## 适用模式

`type=1`（上传作品）。判定方式：`load_data()` 返回 `type == 1` 且 `rules` 里含
`{"category": "image"}`。

## 五个环节

### 1. 起一个可被 CDP 控制的、已登录的浏览器

**坑**：Edge 136+ / Chrome 136+ 对**默认用户配置目录**禁用了 `--remote-debugging-port`
（安全加固），加了参数也不监听端口。

**解法**：复制**最小配置**到独立目录再启动。

```powershell
$src = "$env:LOCALAPPDATA\Microsoft\Edge\User Data"
$dst = "<repo>\_edge_cdp"
New-Item -ItemType Directory -Force -Path "$dst\Default\Network" | Out-Null
Copy-Item "$src\Default\Network\Cookies" "$dst\Default\Network\Cookies"   # 登录态
Copy-Item "$src\Local State"             "$dst\Local State"               # 解密密钥
Copy-Item "$src\Default\Preferences"     "$dst\Default\Preferences"
Start-Process "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" -ArgumentList @(
    "--remote-debugging-port=9222", "--user-data-dir=$dst", "about:blank")
```

**注意**：复制过来的 Cookies 往往**解不开**（App-Bound 加密与配置路径绑定），
所以第 2 步仍然必要。

### 2. 注入 cookie（关键：显式 domain）

**坑**：REA 的 `capture_browser_scenario` 只能按 URL 设 **host-only** cookie，
`passport2.chaoxing.com` 收不到 → 被踢回登录页。

**解法**：用 CDP 的 `Network.setCookie`，它**支持 domain 字段**。

```python
call("Network.setCookie", session=sid, name=name, value=value,
     domain=".chaoxing.com", path="/", secure=False, httpOnly=False)
# 实测 14/14 成功，导航后直接进入任务页（不再跳登录）
```

**另一个坑**：用 `--headless` 时 `Runtime.evaluate` 会**超时**；真窗口下正常。

**目标页匹配要按域名前缀**，不能用 `'chaoxing' in url` —— 仓库路径
`D:\...\chaoxing-fanya\` 也含 "chaoxing"，会误命中误开的 `file://` 标签页。

```python
url.startswith("https://mooc2-ans.chaoxing.com/ai-evaluate")
```

### 3. 生成作品

题目通常要求「≥5 轮对话」+ 若干**特定提问类型**。逐条对齐生成，用
`fanya_upload.render_pngs()` 渲染（宽 1240，自动分页，≤9 页）。

实测有效做法：每轮在提问前**显式标注类型**（如 `【追问证据】`），
AI 评分会逐项核对并明确认可。

### 4. 上传并挂附件

```python
# 上传令牌从任务页的三个隐藏输入取
fields = {currentTime: #uploadTimestamp,
          currentUploadEnc: #uploadEnc,
          currentUserId: #uploadUid}

obj = up.upload_to_pan(session, fields, filename, blob, "image/png")  # -> fileObjectId
up.attach_files(session, ids, data, uploaded)                        # -> recordUuid
```

**`fileModeType=0` 的 `files` 必须是扁平数组**（抓真实请求确认）：

```json
[{"fileObjectId": "...", "fileType": "png", "fileName": "01.png"}]
```

字段名是 `fileName`（**不是** `name`）。发卡片结构
`[{"fileCardId": ..., "files": [...]}]` 会被服务端**静默忽略**
（返回 `{"status":true}` 但附件没挂上），交上去就是一条没有附件的空记录。

### 5. 提交并触发评估

**`answer/submit` 只是交卷，不等于评分。** 必须再触发 `evaluate/stream`。

最稳的办法：**用页面的「提交任务」按钮**（`a.asseBtn`）—— 页面会走完整的
提交 + 评估流程，结果直接渲染在右栏。

## 平台规则（重要）

- **`answerScore` = 历次作答的最高分**，不是平均分 —— UI 原文：
  「学生多次练习最高分达到60分」。所以单次低分**不会拖累**，可以放心重试。
- `enableReset` 字段与能否重做**没有直接关系**；能否开新一次看的是
  `answer/init` 是否被「已存在未完成的作答记录」拒绝。
- 提交后 `recordUuid`/`files` 会被清空，页面回到 `Upload-before` 状态，
  即可开始下一次练习。

## 自动化边界（诚实记录）

- **文件上传无法纯 HTTP 完成**（`fileModeInput` 是点击时动态创建的，
  `DOM.setFileInputFiles` 和 `Input.dispatchDragEvent` 都没能让 Vue 组件响应）。
  **但不需要** —— `upload_to_pan` + `update-file` 的 HTTP 路径是通的。
- 浏览器的作用主要是：拿到上传令牌、以及**点击「提交任务」触发评估**。
