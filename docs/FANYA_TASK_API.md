# 泛雅「任务」模块接口说明（任务引擎）

> 本文记录 `api/fanya_task.py` 所依赖的接口，全部来自浏览器实测（2026-10）。
> 标注 **[已验证]** 的条目在本机真实账号上跑通过；标注 **[推断]** 的条目来自前端 JS 静态分析，未实际调用。

## 1. 为什么需要单独一套接口

原项目的「开始学习」只处理**章节任务点**（`mooc1.chaoxing.com/mooc-ans/knowledge/cards`）。
而课程页左侧「任务」标签由另一套服务提供：

```
课程页「任务」入口  →  https://mobilelearn.chaoxing.com/page/active/stuActiveList
                       └─ 若存在任务引擎任务，则内嵌 task.chaoxing.com 的页面
任务引擎（taskDomain）→  https://task.chaoxing.com
```

`ServiceDomain` 里 `taskDomain = task.chaoxing.com`，`moocAnsDomain = mooc2-ans.chaoxing.com`。

## 2. 任务引擎接口（`task.chaoxing.com`）

> 注意：这些 POST 接口**只接受 JSON**（`Content-Type: application/json`）。
> 用表单提交会返回 `415 Unsupported Media Type`。

| 用途 | 方法与路径 | 关键参数 |
| --- | --- | --- |
| 课程下的任务列表 | `POST /userStudyTask/getData` | JSON: `page, searchKey, pageSize, courseId, moocClassId` |
| 任务进度 | `POST /userStudyTask/getTaskSpeedOfProgress` | query: `encryTaskUserId` |
| 是否在可学习时间 | `POST /userStudyTask/isAllowStudyTask` | query: `encryTaskUserId` |
| 任务分组 | `POST /userStudyPlan/getGroupData` | query: `encryTaskUserId` |
| 分组下任务点 | `POST /userStudyPlan/getPlanDataByGroupId` | query: `encryTaskUserId, encryGroupId` |
| 任务点跳转信息 | `POST /userStudyPlan/getToStudyUrl` | query: `encryptPlanId, encryTaskUserId, studyJumpType=0, isInterface=false` |
| 刷新任务点成绩 | `POST /userStudyPlan/scoreRefresh` | query: `encryTaskUserId, encryptPlanId` |
| 是否还存在任务 | `GET /api/v1/taskEngineApi/existTask` | query: `courseId, classId` |
| 学生任务页地址 | `GET /api/v1/userStudyApi/getMyTaskUrl` | query: `courseId, moocClassId, from=moocCourse` |

**[已验证]** `existTask` **不可靠**：`COM0018A面向对象程序设计与实践` 的
`existTask` 返回无任务，但 `getData` 实际返回 11 个任务。
因此本项目**不依赖 `existTask`**，直接调用 `getData`。

**[已验证]** 返回字段要点：

* 任务：`taskId` / `name` / `encryTaskUserId` / `taskStudyProgress`（0~1）。
* 任务点：`planId` / `encryptPlanId` / `name` / `planType` / `planTypeName` /
  `isFinish` / `planAllowStudy` / `chooseStudy` / `score` / `planBreakthroughSet`。
* **完成判定用 `isFinish`**。`score` 会在「分组任务」等类型上提前出现
  （组内已有部分得分），不能作为完成依据；仅当接口完全没有 `isFinish` 时才退化为
  `score != null`。
* `externalDataId` 在 AI实践 上是 AI 评估的 **libraryUuid**，不是 `publishRelationUuid`。

**[已验证]** 时间窗口有两级，且都会让接口直接失败：

* 任务级：`isAllowStudyTask` → `任务学习时间：… 当前不在学习时间内`。
* 任务点级：`getToStudyUrl` → `任务点学习时间：… 当前不在学习时间内`。

## 3. 任务点类型（全库实测分布）

对本人 26 门课程盘点，7 门存在任务引擎任务，共 28 个任务、127 个待完成任务点。

| planType | 名称 | 完成条件标志 | 处理方式 |
| --- | --- | --- | --- |
| 10 | 视频 | `enableVideoComplated` / `enableVideoWatchDuration` | **任务引擎自有播放页**（见 §6.3）✅ |
| 11 | 文档 | `enableCompleteRead` / `enableDocumentWatchDuration` | **任务引擎自有阅读页**（见 §6.1）✅ |
| 4 | 作业 | `enableSubmitWork` | **mooc2 课程作业**，需单独适配器（见 §6.2） |
| 8 | 章节 | `enableChapterFinishPercent` | 复用章节流程（见 §6.4）✅ |
| 1 | 知识点 | `enableFinishPercent` | 复用章节流程 ✅ |
| 22 | 音频 | — | 与视频同构（未验证） |
| 14 | 主题讨论 | `enableReplyDiscuss` | 见 §4 ✅ |
| 15 | AI实践 | `enableFinishAicoach` / `enableAicoach` | 见 §5 |
| 17 | 自测 | `enableNewQuiztestFinish` | 考试子系统，见 §6.5 |
| 5 | 测验 | `enableSubmitExam` / `enableExamScore` | 考试子系统，见 §6.5 |
| 6 | 问卷 | `enableFinishQuestionnaire` | **需人工**（走超星 OA 表单，需管理员侧登录） |
| 18 | 分组任务 | `enableGroupTaskScore` | 半自动：可提交，完成取决于评分 ≥60 |
| 20 | 线下课堂 | 无 `enable*` | **需人工**（由教师记录） |
| 10042 | AI课堂 | 无 `enable*` | **需人工**（第三方棱镜课堂，不上报泛雅） |

> ⚠️ 早期版本这里写的是「视频/文档/作业统一复用章节流程」，**这是错的**：
> 只有 `planType=8`（章节）的 `externalDataId` 等于 mooc1 的 `point["id"]`；
> `11/10/4` 的 `externalDataId` 都是 32 位 UUID，指向任务引擎自有的资源体系。

## 4. 主题讨论（planType 14）

**[已验证]** 回复接口：

```
POST https://groupweb.chaoxing.com/pc/invitation/{topicUuid}/addReplys
Content-Type: application/x-www-form-urlencoded; charset=UTF-8

courseId=..&classId=..&replyId=-1&uuid=<新 uuid4>&topic_content=<二次编码文本>
&files_url=&files_attr=&anonymous=&urlToken=<32位hex>&bbsid=..
```

三个必须注意的点：

1. **`topic_content` 需要二次 URL 编码**：页面 JS 先 `encodeURIComponent(text)`，
   jQuery 的表单序列化再编一次。直接在 Python 里传明文会把字面的 `%E4%BD%A0…`
   存进帖子。
2. **`urlToken` 必须现抓**：服务端把它写进 `window.obj.urlToken`，不是 JS 算出来的签名，
   但每次打开详情页都会变。抓取来源：
   `GET /pc/topic/jumpToTopicDetail?bbsid=&uuid=&classId=`（**GBK 编码**），
   正则 `urlToken\s*:\s*'([0-9a-fA-F]{16,64})'`。
3. **是否已回复**：`GET /pc/invitation/getReplyList`（`order=2` 时间倒序）翻页查找
   `createrPuid == 当前 puid`（是 **puid**，不是 uid）。顶层回复（`replyId=-1`）
   就足以让任务点 `isFinish` 变为 true，无需嵌套回复。

**[已验证]** 本机实测：已完成话题 `has_replied=True`（第54楼，2 页）；
未完成话题正确识别为未回复，`urlToken` 抓取长度 32。

## 5. AI实践（planType 15）

入口与读取：

```
入口  https://mooc2-ans.chaoxing.com/ai-evaluate/v2/answer?courseid=&clazzid=&cpi=
      &publishRelationUuid=<从 getToStudyUrl 的 url 解析>&__from=taskEngine
读取  GET /mooc2-ans/ai-evaluate/v2/answer/load-data
      ?courseid=&clazzid=&cpi=&publishRelationUuid=&recordUuid=
```

`data.type` 决定子模式：

| type | 模式 | 说明 |
| --- | --- | --- |
| 0 | 情景对话 | 与 AI 角色对话 |
| 1 | 上传作品 | 上传图片/文档作为作品后提交评估 |
| 2 | 思维阶梯 | 多步骤引导式作答（Vue SPA） |

**[已验证]** 本账号实测分布：可解析的 14 个 AI实践中，`type=1` 有 3 个、
`type=2` 有 11 个，**没有 `type=0`**（唯一名为「情景对话」的任务点处于关闭的学习窗口，
无法读取模式）。

上传作品型（type 1）的调用顺序 **[推断，来自前端 JS]**：

```
1) 上传附件（超星网盘）
   POST https://mooc1.chaoxing.com/upload-ans/upload/uploadNew?t=&enc2=&userId=
   multipart 字段： file, name, t, enc2, userId, fromType=27
   → 响应里的 url 即 fileObjectId
2) 创建答卷
   POST /mooc2-ans/ai-evaluate/v2/answer/init?courseid=&clazzid=&cpi=&publishRelationUuid=
   body: { type: 1, files: JSON.stringify([{fileCardId, files:[{fileType, fileObjectId, name}]}]) }
   → 返回 recordUuid
3) 提交
   POST /mooc2-ans/ai-evaluate/v2/answer/submit?courseid=&clazzid=&cpi=&recordUuid=
4) 轮询评估结果（约 30 次）
   GET /mooc2-ans/ai-evaluate/v2/answer/evaluate-result?courseid=&clazzid=&cpi=&recordUuid=
```

* `t` / `enc2` / `userId` 来自入口页的三个隐藏输入：
  `#uploadTimestamp` / `#uploadEnc` / `#uploadUid`。
* **`answer/init` 会消耗作答次数**，`remainScoreCount` 只在评分后才减少。
* 上传规则示例（`data.rules`）：
  `image: jpg/jpeg/png 最多 9 个 / 10MB`；`document: pdf/doc/docx/ppt/pptx 最多 1 个 / 10MB`。

情景对话型（type 0）**[此前的「401 不可用」结论已推翻]**：

早期用**空 `aiEnc`** 探测 `/ai-ans/.../evaluate/stream` 得到 HTTP 401，据此判断
「`/ai-ans` 是独立后端、需要单独鉴权」。2026-10 复测（真实 `aiEnc` + **假**
`recordUuid`，不产生任何记录）表明这是误判：

| 请求 | `aiEnc` | 结果 |
| --- | --- | --- |
| `/ai-ans/ai-evaluate/v2/evaluate/stream`（假 recordUuid） | **空** | **HTTP 401** |
| 同上 | **真实** | **HTTP 200 + `text/event-stream`**，`event:error data:{"content":"会话不存在"}` |

即 `/ai-ans` **会正常鉴权**（并下发 `k8s-ai` cookie），业务错误以 SSE `event:error`
返回，而不是 401。401 的成因是缺少有效 `aiEnc`，不是缺少独立授权。

因此「情景对话被 401 挡住」**不成立**；目前它未实现的原因只是**本账号没有未过期的
情景对话样本**可用于验证。协议本身（`scene-chat`、`init(scene)`、`submit`、
`evaluate/stream`）已记录在 `_findings_ai.md` §4。

思维阶梯（type 2）与代码实践（type 4）见 `_findings_ladder.md` 与 `api/fanya_ladder.py`。

## 6. 章节类任务点（文档 / 视频 / 作业 / 章节 / 知识点）

**重要纠正**：这些任务点**不都是**章节附件，必须逐类看待 —— 早期「统一复用章节流程」的
假设是错的。

### 6.1 文档（planType 11）——[已验证] 单次上报即可完成

```
跳转  POST /userStudyPlan/getToStudyUrl  → data.url =
      https://task.chaoxing.com/documentStudy/learnPage?encryPlanUserId=<EPU>
完成  GET  https://task.chaoxing.com/documentStudy/readEnd?encryPlanUserId=<EPU>
```

任务引擎有自己的文档学习页（云盘阅读器）。阅读器上报 `isFinished=1` 时页面会调用
`/documentStudy/readEnd`，这就是服务端认可的「读完了」。

**实测**：`planId=7453880`「9-4 静电场环路定理与电势自学任务清单」调用前
`isFinish=False`，调用后 `isFinish=True`。

### 6.2 作业（planType 4）——课程作业，与章节检测不同

```
跳转  → https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task
        ?courseId=..&classId=..&cpi=..&workId=56508602&enc=..
```

带的是 `workId` + `enc`，属于**课程作业**，和章节检测作业（`api/base.py:study_work`
用的 `/mooc-ans/api/work?...&jobid=work-...`）不是同一套入口，暂未接入。

### 6.3 视频（planType 10）——[已验证] 一次到位上报即可完成

任务引擎有**自己的视频学习页**（不走 mooc1 的 video 模块）：

```
跳转  POST /userStudyPlan/getToStudyUrl → data.url =
      https://task.chaoxing.com/videoStudy/learnPage?encryPlanUserId=<EPU>
```

页面内嵌播放参数（`videoLearnVo`）：

```js
const videoLearnVo = {
  encryId: "BEDCjrlChRDhp@…",     // 上报用的加密 id
  resourceId: "03788546aa…",
  currentTime: 0, farthestTimeValue: 0,
  enableDrag: true,                // 允许拖拽（关键）
  enableTripleSpeed: false,
  videoInfo: { videoSource: "https://s2.cldisk.com/…/sd.mp4?at_=…&ak_=…&ad_=…",
               duration: 808 }     // 时长（秒）
};
```

进度上报由两个 videojs 插件完成（`/videoPlayback/plugins/…`）：

```
心跳  GET /videoDataLog/dataLog/{duration}/{currentTime}/{status}?encryId=<encryId>
        status: 0=开始播放  1=播放中（每 6 秒）  2=暂停/播放结束
记录  GET /planUserSchedule/lastCurrentTime/{currentTime}?encryId=<encryId>
```

要点：

* `duration` / `currentTime` 都取整（`Math.floor`）。
* **关键实测结论**：服务端**接受一次到位的进度上报**，不需要按真实时长等待。

  | planId | 时长 | 调用 | isFinish |
  | --- | --- | --- | --- |
  | 6797890（已完成项，对照组） | 808s | `ct=0,status=0` → `result:true`；`ct=808,status=2` → `result:true` | 保持 true |
  | **4097446（未完成项）** | **1362s** | 同上 | **false → true** |
  | **4097447（未完成项）** | **643s** | 走 `handle_video` 处理器 | **false → true** |

* 适配器实现：抓 `videoLearnVo` → 上报「开始播放」(`ct=0,status=0`) →
  上报「播放结束」(`ct=duration,status=2`) → 记录播放位置 → 复查 `isFinish`。
  若服务端拒绝跳跃（`result:false`，例如该任务点要求真实观看时长），
  处理器会**自动回退为分段上报**；若分段仍被拒，会明确报错并提示改用
  「开始学习」按真实速度播放 —— 不会静默失败。
* **音频（planType 22）** 与视频同构（`audioStudy/learnPage` + `audioDataLog`），
  已按同一逻辑实现，但本账号没有音频任务点，**未经验证**。

### 6.5 自测（17）/ 测验（5）——[接口已定位，未实现；且样本已过期]

**不能复用 `study_work`**：自测/测验完全走考试子系统，与章节检测不同。

* 自测(17) 真正的入口要先向任务引擎取：
  ```
  POST /userStudyPlan/getSelttestTemplateStatus?encryTaskUserId=&encryptPlanId=   (注意拼写 Selttest)
    → data.data == 1 表示试卷已生成，data.url 给出 lookPaper 页
    → 从 lookPaper 页取 examId(=examRelationId)/paperId/openc
    → GET mooc1.chaoxing.com/exam/test/examcode/examnotes?courseId=&classId=&cpi=&examId=
  ```
* 测验(5)：`getToStudyUrl` 直接给 `…/exam/test/examcode/examnotes?…&examId=<examRelationId>`，
  `mooc-ans` 前缀会 302 到 `exam-ans`。
* 作答页**题目是服务端渲染进 HTML**（非 JSON），单选/多选/判断在 `.questionLi` /
  `.mark_name` DOM 里；**公式是图片，LaTeX 在 `img[data]`（URL 编码）**，入库匹配前需
  `urllib.parse.unquote` 后再拼进题干，否则公式会整体丢失。
* 交卷：`POST <页面隐藏域 #saveUrl>&pos=<sig>&version=1`，body 为 `#submitTest` 表单序列化，
  `#tempSave=false` 表示交卷（`true` 为暂存）。

> **重要运行事实（实测）**：`scoreRefresh` **不是「补完成」按钮** —— 它只把 mooc 侧
> 已合格的成绩同步回任务引擎。本账号 `高等数学II` 的两个未完成自测
> （`齐次方程` planId 4549581、`可分离变量的微分方程` planId 4549553）**考试时间已过期**
> （2026-06-13 / 2026-06-10），调用后 `isFinish` 仍为 `false`、`score` 仍为 `null`，
> **无法通过任何接口补完成**，需联系老师处理。

#### 6.5.1 为什么还没实现自动作答

不是「没想到」，而是**目前不具备安全实现的条件**：

1. **没有可验证的样本**：全库扫描后，唯一未完成的自测就是上面两个已过期的，
   没有任何「窗口内」的自测/测验可供真机跑通。交卷接口参数一旦用错，
   影响的是真实考试成绩 —— 这比「没实现」严重得多。
2. **`pos` 签名未知**：交卷 URL 需要 `pos=<sig>`（鼠标轨迹类签名），
   由客户端 JS 的 `getEnc()` 生成，本仓库尚未复刻。
   `_quiz_probe/pub/core/api__xuexitong__XueXiTongExamApi.go`（第三方 yatori-go-core
   实现）里有完整字段与算法，但**未在本机执行验证过**。
3. **一题一页 + 暂存**：作答页每题一页，需要逐题 `tempSave=true` 暂存，
   最后一题再 `tempSave=false` 交卷；中途放弃会在账号里留下「进行中」记录。

#### 6.5.2 实现时要读的字段（从作答页自身取，不要硬编码）

| 字段 | 位置 |
| --- | --- |
| 交卷地址 | `#saveUrl` 的 value（**每次都要现读**） |
| 试卷/关系 id | `#testPaperId`（= examRelationId）、`#testUserRelationId`（= examAnswerId） |
| 暂存开关 | `#tempSave`（`true` 暂存 / `false` 交卷） |
| 单选/判断 | `input[name=type<qid>]`、`#answer<qid>` |
| 多选 | `input[name=answers<qid>]`（**是 answers，不是 answer**） |
| 填空/简答 | `#answerEditor<qid><n>` |
| 签名 | `pos=getEnc()` —— **当前唯一硬缺口** |

### 6.4 章节 / 知识点（planType 8 / 1）——[已验证]

`plan.externalDataId` 是小整数，**精确等于 mooc1 章节列表里 `point["id"]`**
（JSON 中是字符串）。因此定位方式为：

```
externalDataId → chaoxing.get_course_point(...) 里匹配 point["id"]
              → chaoxing.get_job_list(course, point) → process_job(...)
```

完成后章节侧通过 `JOB_FINISH_INFO{module:"KNOWLEDGE"}` →
`POST /userStudyPlan/autoPullChapterScore`（`knowledgeId/jobCount/finishCount/clazzId/enc`）
把成绩同步回任务引擎。

> 注意：`planType ∈ {4,10,11}` 的 `externalDataId` 是 **32 位 UUID**
> （`4` 还是不带连字符的 32 位 hex），与 mooc1 的 `jobid`/`objectId`/`workId`
> 全不相等，**不能**走这条映射。

### 6.6 分组任务（planType 18）——[接口已定位]

```
提交  POST https://mobilelearn.chaoxing.com/widget/apis/renwu/studentSaveRenwu
      表单字段： forceSave / title / content / groupId / editVersion / activeId / courseId
```

**[已验证，只读]** `getMyGroupInfo` 显示本账号已在组内（`groupId` 非 0、
`submitOrNotStuForAnswerPage=1`、`onlyLeaderSubmit=0`）→ **任意组员都能提交**，
不需要凑齐队友。

**⚠️ 提交不等于完成**：该任务点的完成判据是 `totalScore ≥ 60`（教师评分 / 组内互评），
所以提交后任务引擎仍可能显示 `isFinish:false`。界面上如实标注了这一点。

### 6.7 AI实践·思维阶梯（planType 15, `data.type == 2`）

渲染器是 Vue SPA（`mooc2-ans-vue`），经典页面的 `answer-common.js` 里**没有**
type-2 分支；作答接口在 SPA 的懒加载 chunk 里。与另外两种模式的关系：
`type=0` 情景对话依赖独立后端 `/ai-ans`（本账号实测 401），`type=1` 上传作品
需要本人作品。详见 `_findings_ai.md` 与 `_findings_ladder.md`。

## 7. 免责声明

本模块仅用于学习自动化技术研究。使用者需自行承担因自动化操作带来的
学术诚信与合规风险；请遵守所在学校的规定。


---

## AI实践子模式（`ai-evaluate`）—— 含「AI剧情」(type=7)

子模式由 `GET /mooc2-ans/ai-evaluate/v2/answer/load-data` 返回的 `data.type` 决定：

| type | 名称 | 自动化状态 |
| --- | --- | --- |
| 0 | 情景对话 | 已实现（**未真机验证**，本账号无未过期样本）|
| 1 | 上传作品 | 已实现（**实测得分 93**）|
| 2 | 思维阶梯 | 已实现（**实测得分 85**，2026-10-12 planId 7881197）|
| 4 | 代码实践 | 已实现（**实测得分 76**）|
| **7** | **AI剧情** | 链路已打通：发言 → 章节交卷 → 评分（见[下方更正](#启动任务链的两条路-已验证)）。此前写的「评分必须由浏览器建立对答会话」**是错的**，根因只是 `evaluate/stream` 的 `recordUuid` 传错记录。⚠️ 交卷不可逆，故默认不代提交。实测人工粘贴高质量内容可得 **100 分** |

### type=7「AI剧情」是什么 [已验证]

来源：前端 i18n 的 `aiStoryEditor` 与页面组件（模块 72208，`name:"TaskChainChat"`）。

* 教师端把内容编排成**章节 + 节点**，每个节点是「情景对话」或「上传作品」
* 学生侧作答页路由：`/taskChain/evaluate`（`meta.title = "AI实践-作答"`）
* 实例：`离散型随机变量 AI实践（审计与风控案例）`，要求原文写
  "前 4 个节点通过与 AI 角色对话完成建模校验、数值计算和案例分析，
  第 5 个节点上传综合小结报告"
* `load_data` 返回 `storyExtraData.role`（角色阵容：旁白 / 统计专家 /
  资深审计经理 / 行业顾问 / 学生）与 `requirement`（剧情说明）

### HTTP 层（`index.js` 模块 40898）[已验证]

| 导出 | 语义 |
| --- | --- |
| `lx` | **GET**（参数走 query）|
| `zA` | POST（`application/x-www-form-urlencoded`）|
| `aW` | POST（`responseType: blob`，下载用）|

### type=7 用到的端点 —— **与模式 0 / 模式 1 完全复用** [已验证]

| 用途 | 端点 | 项目内已有实现 |
| --- | --- | --- |
| 读取剧情 | `GET /mooc2-ans/ai-evaluate/v2/answer/load-data` | `fanya_ai.load_data` |
| 发一轮对话 | `POST /ai-ans/ai-evaluate/v2/scene-chat` | `fanya_chat.send_turn` |
| 生成场景 | `POST /ai-ans/ai-evaluate/v2/scene-chat/generate/scene-by-library` | `fanya_chat.generate_scene` |
| 建作答记录 | `POST /mooc2-ans/ai-evaluate/v2/answer/init` | `fanya_chat.init_scene_record` |
| 交卷 | `POST /mooc2-ans/ai-evaluate/v2/answer/submit` | `fanya_ai.answer_submit` |
| 评估出分 | `POST /ai-ans/ai-evaluate/v2/evaluate/stream`（SSE）| `fanya_ai.evaluate_stream` |
| 上传报告 | `POST https://mooc1.chaoxing.com/upload-ans/upload/uploadNew` | `fanya_upload.PAN_UPLOAD` |

**与模式 0 的两点差异**：

1. `load_data` 可带 `chapterRecordUuid` 参数；
2. type=7 **没有 `scene` 字段**，背景必须从 `requirement` / `storyExtraData` 取，
   **不能调用 `generate_scene`**（那是模式 0 的接口）。

### 学生作答流程 [已验证 · 2026-10-12 真机探测]

从客户端 JS（分块 5116 / 1745 / 3826）与真机会话探测共同确认。**注意：剧情接口的参数
是 camelCase（`courseId`/`classId`），而 `load-data`/`answer/init` 用全小写
（`courseid`/`clazzid`）—— 传错会返回「课程id为空」。**

```
1) GET  /mooc2-ans/ai-evaluate/story/go-home
        params: courseId, classId, cpi, libraryUuid, publishRelationUuid, recordUuid
        返回: { chapterUuid, title, content, objectId }      ← 当前章节

2) GET  /mooc2-ans/ai-evaluate/story/go-chapter
        params: 同上 + chapterUuid
        返回: { condition:["分数达到90分"], relatedRole:"uuidA,uuidB",
                title, content, chapterTestSwitch, chapterRecapSwitch }

3) GET  /mooc2-ans/ai-evaluate/story/get-nodes
        params: 同上 + chapterUuid
        返回: [{ chat:[], relatedRole, chapterUuid, title,
                 conditions:["分数达到90分"], content, resourceList }]
        ⚠️ **必须先 go-chapter，否则 data 为空数组**（不是报错，容易误判）
```

**每个「章节」就是一个节点**，完成条件写在 `conditions` 里（实测第 1 章是
「分数达到 **90** 分」，而任务级达标线只有 60 分）。

### 剧情专用接口全集（37 个）[已验证存在性]

`/ai-ans/ai-evaluate/story/`
`background`、`get-achievement-card`、`get-chain-chat-monologue`、
**`get-chain-chat-scene`**（对话节点的场景）、**`get-chain-file-score-standard`**
（上传节点的评分标准）、`get-chain-scene-tip`、`get-chain-score-standard`、
`get-chat-score-standard`、`get-score-standard`、`getRoleAvatar`、
`improve-monologue`、`improve-plot`

`/mooc2-ans/ai-evaluate/story/`
`add-chat`（发一轮）、`answer-again`（再次作答）、`get-achievement`(+`-pre`/`-review`)、
`get-chat-pre`、`get-chats`、`get-file-info`、`get-node-pre`、**`get-nodes`**、
`get-progress`(+`-preview`/`-review`)、`get-roles`(+`-pre`)、`go-chapter`(+`-pre`)、
`go-home`(+`-pre`)、`init-chain-answer`(+`-preview`)、`reset`(+`-pre`)、`update-score`

### 启动任务链的两条路 [已验证]

* **新建**：`POST /mooc2-ans/ai-evaluate/v2/answer/init`，参数
  `courseid, clazzid, cpi, publishRelationUuid`，**`type=6`**（注意：初始化用 6，
  而 `load-data` 报的 `type` 是 7）。
  ⚠️ 已有未完成记录时会被拒：`{"status":false,"msg":"已存在未完成的作答记录"}`
  （`type=6` 放 query 或 body 都一样被拒）。
* **★ 更正（2026-10-12）**：本文件此前在此处写过「用纯 HTTP 建出的章节记录调
  `evaluate/stream` 会返回『会话不存在』，永远拿不到分数，因此无法纯 HTTP 自动化」
  —— **该结论是错的，已作废**。

  真正的原因是**参数传错**，不是缺浏览器会话：

  | 传给 `recordUuid` 的值 | 服务端回答 |
  | --- | --- |
  | **外层记录**（`load-data` 返回的 `recordUuid`）✅ | `任务未提交，无法开始评估` |
  | 章节记录（`chapterRecordUuid`）❌ | `会话不存在` |

  判据来自对话页 `TaskChainChat.startEvaluate()`：

  ```javascript
  var a = {courseId, clazzId, cpi, aiEnc,
           recordUuid: e.currentRecordUuid,      // ★ 外层记录
           chapterRecordUuid: e.chapterRecordUuid,
           isPreview, useJsonScheme: true};
  e.evaluateEventSource = qX(a, {...});          // 内部是 EventSource => GET
  ```

  也就是说「会话不存在」只是**参数对不上的症状**。传对之后服务端立刻给出
  真实业务状态，例如「**任务未提交，无法开始评估**」—— 那说明它已经认得这条记录，
  只差 `answer/submit` 那一步。**不存在"必须用 WebSocket/浏览器才能评分"这回事。**

* **沿用**：直接把 `load-data` 返回的 `recordUuid` 传给 story 系列接口即可
  —— 实测 `go-home` / `go-chapter` / `get-nodes` 都正常工作。

### 节点类型判据 [已验证]

教师端编辑器里：`1 === subtype ? "uploadWork" : "sceneDialog"`
—— 即 **subtype=1 是「上传作品」节点，其余是「情景对话」节点**。
配合任务要求原文「前 4 个节点对话 + 第 5 个节点上传综合小结报告」，
可知该实例是 4 个对话节点 + 1 个上传节点。

### [已探明] 章节遍历与对话作答 —— 完整的可自动化路径

**① 章节总表：`GET /story/get-progress`**

```json
{ "needInit": false, "chapterCompleted": false,
  "progress": [{
    "uuid": "<chapterUuid>",
    "title": "审计抽样建模校验",
    "modeType": 1,                       // 节点类型（对应 subtype：1=上传，其余=对话）
    "requirement": "业务背景：…提交物：文本作答 + AI对话文本/截图",
    "answerRecord": [{ "uuid": "<chapterRecordUuid>", "score": 0.0, "status": 0 }]
  }, …] }
```

*`answerRecord[].uuid` **就是** `add-chat` 需要的 `chapterRecordUuid`* —— 这是之前一直缺的一环。
`chapterCompleted` 是章节级完成标志（JS 里正是用它判断并触发 `fetchAchievement`）。

**② 章节对话结构：`GET /story/get-chats`（带 `chapterUuid`）**

```json
{ "chat": [{
    "condition": ["分数达到90分"],        // 完成条件（分数阈值）
    "maxRound": 5,                        // 该节点最多 5 轮
    "userRound": 0,
    "chat": [{ "type": 1, "roleUuid": "39c54efd…",
               "content": "请复制任务要求中的提示词发起提问，智能体只响应输入，不要主动发起对话。" }],
    "chapterUuid": "…", "title": "审计抽样建模校验" }] }
```

**③ 发一轮对话：`POST /story/add-chat`（表单编码，`zA` helper）**

| 参数 | 来源 |
| --- | --- |
| `courseId` / `classId` / `cpi` | study_url（注意 **camelCase**）|
| `libraryUuid` / `publishRelationUuid` / `recordUuid` | `load-data` |
| `chapterRecordUuid` | `get-progress.progress[].answerRecord[].uuid` |
| `roleUuid` | `load-data.startRoleUuid`（或 `get-chats.chat[].chat[].roleUuid`）|
| `content` | 要发送的文本 |

**④ 角色表：`GET /story/get-roles`** → `[{name:"旁白",type:2,uuid:…}, {name:"统计专家",type:1,…}, {name:"学生",type:0,uuid:…}]`
（`type`：2=旁白、1=智能体、0=学生）

**⑤ 评分/成绩**：`GET /story/get-achievement` 在未提交时返回
`{"status":true,"msg":"任务尚未提交"}`；提交后才有数据。

### 实测第 1 章的真实要求（供实现参考）

```
业务背景：注册会计师开展凭证审计抽样，总体凭证错报率 p=0.02，抽取样本 n=100 份凭证。
设随机变量 X 为样本里面错报凭证的份数。请完成：
1. 判断 X 是离散还是连续型；2. 写出 X 服从的概率分布与记号；
3. 写出期望、方差计算公式。把以上整理成文字，复制下方提示词发给 AI 校验，保存 AI 回复。
提交物：文本作答 + AI对话文本/截图
```

条件：`分数达到90分`，`maxRound: 5`。

> ⚠️ 探测副作用（如实记录）：调用 `go-chapter` 会让服务端初始化一条**空**对话
> （`storyExtraData.chat` 由 0 条变 1 条）；`answer/init` 在已有未完成记录时被拒，
> 故未新建作答卷。**`add-chat` 一旦调用就会写入真实对话内容，不可撤销。**
