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

**[已验证，2026-10-16]** 窗口关掉以后，服务端在 `getToStudyUrl` 的失败响应里
**什么 id 都不给**（实测 `{"code":400,"result":false,"message":"…"}`，没有 url /
workId / publishRelationUuid / topicId），所以依赖这次跳转的类型在窗口结束后
**没有任何入口**：作业拿不到 workId、AI实践拿不到 `publishRelationUuid`、讨论拿不到
topicId。已排除的旁路（都实测失败或不可达）：

* 作业「修改答案」入口：`/mooc-ans/mooc2/work/dowork?…&answerId=…` 直接返回
  **403「很抱歉，您没有权限访问这个页面！」**；且判定页里 `redoTimes=0`
  （`redoWork()` 的文案是「你还可以重做0次」）——平台自己也不允许重做。
* 作业列表接口：`/mooc-ans/mooc2/work/task` 是按 `workId` 打开的详情页，
  **不存在**「列出全部作业」的 HTTP 接口（前端从课程页 HTML 里拿 workId）。
  ⚠️ 但这条不是**硬**限制：mooc1 课程页 HTML 里带着每份作业的 `workId/enc`，
  所以「窗口关闭的课程作业」在原理上仍可从课程页 HTML 取 workId 后直达
  `mooc2/work/task` 答题（**尚未实测**，列为后续工作）。
* 文档/视频/AI实践/讨论：`externalDataId` 不是进入资源所需的那个 id（AI实践上它是
  评分 `libraryUuid`），借它拼阅读器/播放器 URL 都不成立。

**[已验证]** 唯一的例外是**章节类**（`planType=8`，handler `chapter`）：
`handle_chapter` 只用课程 id + `plan.externalDataId`（= 小整数，精确等于 mooc1 的
章节 point id），完全不碰任务引擎的跳转。实测 2026-10-16 在窗口已结束
（2026-09-20~09-30）的 plan 8110517「5.1 人口增长-马尔萨斯模型视频教学」上：
入口侧 `get_course_point` → `get_job_list` → `process_job` 正常播放并上报；
**完成侧也确实生效** —— 该任务点由 `isFinish=False` 变为 `isFinish=True`，
服务端生成评分记录（`createDate/updateDate = 2026-10-16T11:07:51Z`，
`jobCompleteRate=100.0`、`moocChapterFinish=1`、`moocChapterId=1222389482`）。
因此 `run_item` 里 `HANDLERS_WITHOUT_STUDY_URL = {"chapter"}`：窗口失败时只有
章节继续执行，其余类型如实记为跳过。

> ⚠️ **读回要留出延迟**：同一任务点刚跑完时立刻回读，`verify_plan_finished` 与
> 任务列表里的 `isFinish` 仍然是 `False`（实测跑完瞬间读到 False，几分钟后才是
> True）。判定章节类是否完成不能只看紧邻的那一次回读。

> ⚠️ **耗时**：该任务点的 `planBreakthroughSet` 里 `enableTripleSpeed=0`（平台禁倍速），
> 视频总时长 3398 秒，实测真实播放约 **18 分钟**才完成。窗口关闭的章节类「能做」
> 但**不便宜**，盘点里要给这个预期。

**[约定]** 盘点（`build_task_preview`）现在会为每个未完成任务点探测一次
`getToStudyUrl`，把窗口状态写成 `window_state`（`open` / `expired` / `not_open`），
并把「窗口已结束且处理器依赖跳转」的任务点（`item.unreachable`）从
「待完成 / 可自动完成」里挪到 `expiredCount`（前端标「窗口已过」，说明文字带上
窗口终点）。理由：不探测就只能把它们永远显示成能做，用户会一直等一个永远不会开的窗口。
章节类窗口关闭**不算** `unreachable`（它不依赖这次跳转，实测还能完成），
只是 `windowState="expired"` + `windowNote` 提示窗口已过。

**[约定]** 前端默认**收起** `item.expired`（= `unreachable`）的条目，工具栏给一个
「显示窗口已过」勾选展开（`web/opendesign/chaoxing-assistant.html` 的
`#preview-show-expired` / `state.showExpired`）。理由：窗口一过平台就不让进，补不了；
混在能做的任务点里只会把「能做的事」淹掉（实测 2026-10-16 数学建模：线性规划窗口已过 7 项、
马尔萨斯人口模型窗口已过 10 项，能做的只有 21 项）。汇总与任务头上的 `expiredCount`
照旧计数，一个数都不改——收起不等于隐藏事实。某个任务点的条目**全部**被收起时，
原地留一行「窗口已过 N 项已收起」。

**[约定]** 成绩与「未达标」：盘点里每个任务点还带 `score` / `threshold` /
`belowThreshold`，任务级与汇总各有一个 `belowThresholdCount`。

* `threshold` 取平台自己的达标线：**AI实践** 看 `planBreakthroughSet.aicoachScore`
  （`enableAicoach=1` 时），**作业** 看 `workScore`（`enableWorkScore=1` 时）；
  视频/文档/章节这类平台不给达标线，返回 `None`。
* 作业的 `score` 是**一整条评分记录**（dict，分数在 `totalScore` 里），其它类型
  直接是数值或 `None`；`item_score()` 负责归一化，`is_below_threshold()` 判低分。
* **低于达标线的 AI实践 不会翻成已完成**：实测 2026-10-16「线性与非线性回归
  AI实践作业」（`aicoachScore=60`）第一次作答 35 分 → `isFinish=False`，
  平台一直把它算作未完成（再跑一遍就是重答）；重做后 100 分 → `isFinish=True`。
  所以这类任务点界面要明说「未达标、需要重做」，不能只显示「未完成」。
* **低分作业会被标成「需人工」而不是「可自动完成」**：作业的重做不在本工具的
  自动重做范围内（见下），若继续显示成「可自动」，用户一跑只会得到一句
  「该作业已经提交过了，无需重复作答」。所以盘点会为**低分作业**多发两次只读
  请求（`getToStudyUrl` → 判定页），用 ``_parse_redo_times`` 从页面 JS 里解出
  ``redoTimes``（实测是算式 ``var redoTimes = 0 + 0 - 0;``），item 上给出
  `redoTimes`，并把 note 写成「已提交但得分 44.5，低于达标线 50；平台不给重做
  次数（redoTimes=0），只能请老师开启重做」。读不到次数时不猜（`redoTimes=None`，
  note 改为「请到课程页面手动操作」）。只对低分作业做，正常任务点一次都不多发。

**[已验证，2026-10-16] 重做的可行边界**：

* **AI实践 可以重做**（平台按 `remainScoreCount` 允许重练，本账号余量很大）：
  进入任务点作答页 → 重新上传作品（`fileModeType=0` 的挂附件载荷，实测可提交）
  → 交卷 → 平台 AI 评卷出分。实测同一天把上面那份 AI实践 从 35 分重做到 **100 分**，
  任务点随即 `isFinish=True`。
* **作业不可自动重做**：判定页里 `redoTimes=0`（`redoWork()` 文案「你还可以重做0次」），
  页面里也没有任何「修改 / 重新提交」入口，`modifyAnswer()` 指向的 `dowork` 页 403。
  低分作业只能请老师在作业设置里开启重做/允许修改。
* **文档 / 视频 / AI实践 之外的类型**：窗口结束即无法进入（见上），不存在重做问题。

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

### 6.1 文档（planType 11）——[已验证] `readEnd` 只解决「读完」，不解决「读够」

```
跳转  POST /userStudyPlan/getToStudyUrl  → data.url =
      https://task.chaoxing.com/documentStudy/learnPage?encryPlanUserId=<EPU>
完成  GET  https://task.chaoxing.com/documentStudy/readEnd?encryPlanUserId=<EPU>
```

任务引擎有自己的文档学习页。页面本身只是个**外壳**：它把

```
https://pan-yz.chaoxing.com/screen/v2/file_<objectId>?…&ext={"rwyq_doc":<EPU>,"pId":<planId>,…}
```

塞进 iframe，真正的阅读器在那个**独立域名**里跑。阅读器滚到底部时
`parent.postMessage({isFinished:1})`，外壳收到才调 `/documentStudy/readEnd`
（外壳页里只有 2 个 `requestGet`：`readEnd` 和 `downloadUrl`，**没有任何时长上报**）。

**实测**：`planId=7453880`「9-4 静电场环路定理与电势自学任务清单」调用前
`isFinish=False`，调用后 `isFinish=True` —— 那是一个**没设时长门槛**的文档。

#### ⚠️ 但设了 `enableDocumentWatchDuration` 时，`readEnd` 不够

服务端要的是 `score.totalTime >= documentWatchDuration × 60000` 毫秒（单位是**分钟**）。
`readEnd` 能让 `completeWeightRate` 变成 100（=「读完」），却**一分一秒都不计入**
`totalTime`。2026-10-15 受控实验：

| 实验 | 结果 |
| --- | --- |
| `learnPage` → 真等 40 秒 → `readEnd` | `{"result":true}`，`totalTime` 仍是 `None` |
| 再连打 3 次 `readEnd`（间隔 12 秒） | `totalTime` 仍是 `None` |
| `GET /documentStudy/dataLog/1/1/1` | **404** |
| `GET /documentStudy/readStart` / `readLog` / `lastCurrentTime/1` | **均 404** |

对照数据（全账号 15 个带时长要求的任务点）：`completeWeightRate=100.0` 但
`isFinish=False` 的有 8 个，全部满足 `totalTime < 要求`。

**结论**：文档时长由云盘阅读器 iframe 走一条**跨域埋点**通道上报：

```
https://data-xxt.aichaoxing.com/analysis/ac_mark?…
```

参数在 `addPoint.js`（jsjiami.com.v5 混淆，97 KB）里，纯 HTTP 复现不了。
因此 `handle_document` 在这种情况下**如实返回失败**并说明原因，而不是让外层
打一条「提交成功但状态仍为未完成」再算成功。

### 6.2 作业（planType 4）——课程作业，与章节检测不同

```
跳转  → https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task
        ?courseId=..&classId=..&cpi=..&workId=56508602&enc=..
```

带的是 `workId` + `enc`，属于**课程作业**，和章节检测作业（`api/base.py:study_work`
用的 `/mooc-ans/api/work?...&jobid=work-...`）不是同一套入口，暂未接入。

#### 6.2.1 答案来源与「大模型复核」（2026-10-16 实测）

链路：`handle_course_work` → `parse_work_page`（题干 + 带 `A. ` 前缀的选项）→
`build_submission(parsed, tiku, verifier=…)` → `submit_prepared`。其中 `tiku` 就是
`settings.tiku_config` 里配的**那一个**题库类（`Tiku.get_tiku_from_config` 按
`provider` 只选一个，不存在「多题库并联」）。

写进表单的取值：

| 题型 | 取值 | 备注 |
| --- | --- | --- |
| 单选 / 多选 | 选项字母 `"B"` / `"AB"` | 答案给的是选项原文时按 `_option_plain` 反查字母 |
| 判断题 | 字符串 `"true"` / `"false"` | 页面 JS `itemAnswer == "true" ? true : false`；写「对/错」服务端**拒绝整份提交**（实测提交请求 ~50ms 内失败） |

答案既不是字母、也对不上任何选项原文时：**该题留空**，明细里记
`reason="答案无法对应选项"`，命中数减一 —— 旧行为是把整段文本塞进单选字段，
服务端判错，用户看到的「我的答案」还会变成一段话。

⚠️ **为什么要复核**：网络题库（言溪 / LIKE / TikuAdapter）按题干模糊匹配，串题时会
回**语义完全不相干**的字母答案 —— 实测数学建模作业 8379428（9 道单选）：
问「Logistic 曲线形状」答「周期正弦曲线」、问「最小化哪个量」答「承载量 xₘ」，
而这类字母答案能通过 `check_answer` 的宽松校验，于是整份作业交成 **4/9**；同一个
模型对同一份卷子**三种提示词都是 9/9**（`_probe_work_quality.py`、`_probe_work_quality2.py`）。

所以当**主题库不是大模型、且 `tiku_config` 已配齐 endpoint/key/model** 时，选择题
（含判断题）会额外问一次 AI 复核：两边都规范成选项字母后不一致 → **采用 AI 的答案**，
并打 `WARNING`（「题库答案与大模型复核不一致…采用模型答案」）。判定入口
`_build_ai_verifier`（`api/fanya_task.py`）：provider 已是 `AI`/`SiliconFlow`、
配置不全、或实例化失败 → 返回 `None`，行为与之前**完全一致**（复核是纯增强）。

复核失败 / 模型回了一段对不上选项的话 / 主题库没命中 —— 三种情况都不改写答案、
也不抛异常。成本：每题一次额外 AI 调用（AI 内部有 3s 最小间隔），9 题约 +27 秒。
端到端复现：`.venv\Scripts\python.exe _probe_work_verify.py`（假网络题库复刻 4/9 →
加复核应回到 9/9）。

接线（`handle_course_work`，`api/fanya_task.py`）：复核器在 handler 里构造一次，
**两条路径都传**——免审批走 `complete_course_work(..., verifier=...)`；审批模式走
`prepare_course_work(..., verifier=...)`（答案在 prepare 阶段就定稿，提交只是把
准备好的载荷发出去，所以必须在这一步复核，`submit_prepared` 不需要）。本机配置的
`provider` 就是 `AI`，因此复核默认**关闭**（同一来源自问自答没有意义），这一步只在
用户改用网络题库时生效。回归保护：
`tests/test_parsers.py::test_handle_course_work_passes_ai_verifier_down`。

#### 6.2.2 一次性作业保护（2026-10-16 自查）

**背景（用户追问「为什么会出现只有一次机会、而且分数很低」）**：作业交上去之后
能不能改，取决于平台给的 `redoTimes`。它**只在已判分的作业详情页里**才有
（页面模板：`var redoTimes = 0 + 0 - 0;`，是个算式，必须用 `_parse_redo_times`
解析，不能 `int()`）。实测：全账号唯一一个「未完成 + 窗口开放」的作业就是
8379428（planId 8379428 已经交过、`redoTimes=0`、得分 44.5 < 达标线 50），其余作业
`getToStudyUrl` 全部报「不在学习时间内」（窗口已过）。

> 📌 2026-10-16 更正：**未提交**的作答页里确实还有一组次数隐藏域
> （`limitWorkSubmitTimes` / `addTimes`，只有 id 没有 name），见下面的
> §6.2.3「作业允许提交几次」。原先写的「交卷前不可知」只对
> **已判分**的作业成立。

**发现的洞**（`_audit_hole.py`，纯本地可复现）：

| 情况 | 加固前 | 加固后 |
| --- | --- | --- |
| `cover_rate` 没配（默认 0）+ 一题未命中 | `do_submit=True` —— **把空白卷交上去** | `do_submit=False`，连草稿都不保存 |
| `cover_rate=0.8` | 只保存草稿（阈值生效） | 不变 |
| 一次性作业（`redoTimes` 查不到或为 0） | 与前一种完全一样，照样自动交 | `do_submit=False`，只保存草稿 |

**加固规则**（`api/fanya_work.py`）：

* `submission_blockers()` 统一给出「不该自动交卷」的理由：一题未命中 → 绝不自动交；
  一次性作业 → 只有用户显式放行**且整卷都答上**才交；`cover_rate` 设了阈值时照旧。
* `build_prepared(..., single_shot=…, allow_single_shot=…)` 把判定写进载荷：
  `do_submit` / `blocked_reason` / `single_shot` 三个字段，日志里直接写清
  「本次不自动提交：<原因>（改为只保存草稿，可在课程页面核对后自己提交）」。
* `handle_course_work` 从 `ctx.item.extra["redo_times"]` 取次数：**查不到一律按
  「只有一次」处理**（`single_shot = redo_times is None or redo_times <= 0`），
  两条路径都传下去；审批预览里加一行「⚠️ …批准后会直接交卷、不可逆（交上去就不能
  改了）」，`extra["singleShot"]=true`，返回文案也会带「该作业不可重做」。
* 想恢复全自动：在 `settings.tiku_config` 里加 `allow_one_shot_submit: "true"`
  （`_opt_in` 只认 `1/true/yes/on`）；即便如此，仍有没答上的题时也只保存草稿。
* 审批路径不变：用户点「批准」仍会真正提交（`submit_prepared(force_submit=True)`）
  —— 那是用户看过预览后的显式决定。

回归保护：`tests/test_parsers.py` 的
`test_build_prepared_never_submits_an_empty_sheet`、
`test_build_prepared_single_shot_stays_draft_even_when_full`、
`test_build_prepared_single_shot_with_opt_in_needs_full_coverage`、
`test_opt_in_only_accepts_truthy_strings`、
`test_submission_blockers_reports_cover_rate`、
`test_handle_course_work_treats_unknown_redo_times_as_one_shot`、
`test_handle_course_work_warns_irreversible_in_approval_preview`。

**交卷后自检**（`_post_submit_score_check`，`api/fanya_task.py`）：真正交卷之后
（返回文案里含「已提交」才算，只保存草稿不算）立刻回读一次任务引擎里这个
`planId` 的 `score` 与达标线：

| 回读结果 | 行为 |
| --- | --- |
| 得分 < 达标线 | `WARNING` + 通知（`api/notification.py` 的 `Notification.send`）+ 返回文案里带「⚠️ 交卷后回读：得分 X，低于达标线 Y」 |
| 得分 ≥ 达标线 | 返回「交卷后回读：得分 X（达标线 Y，已达标）」 |
| 读不到分数（判分延迟） | 返回「暂未读到分数」，**绝不猜** |

回读本身只读两次（任务列表 + 任务点列表），任何异常都被吞掉并返回空串 ——
交卷已经成功，回读失败不能反过来把成功报成失败。
回归保护：`test_post_submit_score_check_warns_when_below_threshold`、
`test_post_submit_score_check_never_raises`、
`test_handle_course_work_only_checks_score_after_a_real_submit`。

**全部「写入平台」入口的审计结论**（2026-10-16，防「只有一次机会却被烧掉」）：

| 入口 | 平台是否可重来 | 交卷前的保护 |
| --- | --- | --- |
| 课程作业（`course_work`，mooc2） | **可能只有一次**（`redoTimes` 事前查不到） | 空卷绝不交；一次性作业默认只存草稿；审批预览写「不可逆」；交卷后回读分数 |
| 自测 / 测验（`quiz`） | 通常只有一次 | **默认只逐题暂存，不交卷**；需显式 `options["exam_submit"]`；任何一题题库没答案就中止（「按约定绝不提交白卷」）；题型无法识别/异常一律中止 |
| AI实践（`ai_upload` / `ai_ladder` / `ai_chat` / `ai_story` / `ai_code`） | **可重来**：`remainScoreCount`（实测 97）+ `answerScore` 取历次最高分 | 无需拦；低分可重传（实测 35 → 100） |
| 章节 / 文档 / 视频 / 讨论 | 可重复学 | 无一次性风险 |

> ⚠️ 同一个 `tests/test_parsers.py` 里还修掉一个测试基建问题：内置 runner
> （`if __name__ == "__main__"`）原来夹在文件中间，`_run_all()` 用 `globals()`
> 收集用例，于是**它下面定义的用例永远不会执行**（`pytest tests` 才发现）。
> runner 现在放在文件末尾。

#### 6.2.3 作业允许提交几次：作答页的隐藏域（2026-10-16 语料实测）

用户追问「你不知道哪次作业只有一次资格，但可以看别的作业推断出来吧」。于是把全账号
的作业都翻了一遍（**只读**，只发 GET），结论如下。

**两个字段**（未提交的作答页，`mooc2/work/dowork`）：

```html
<input id="limitWorkSubmitTimes" type="hidden" value="100"/>   <!-- 平台允许提交几次 -->
<input id="addTimes" type="hidden" value="0"/>                 <!-- 已经提交过几次 -->
```

⚠️ 它们**只有 `id`、没有 `name`** —— 按 `name` 收集表单字段的
`parse_work_page` 原本一个都抓不到（现在按 id 单独取，见 `_hidden_int`）。
页面自己的用法：

```js
var limitWorkSubmitTimes = $("#limitWorkSubmitTimes").val() || 100;
if (parseInt(addTimes) > parseInt(limitWorkSubmitTimes)) { showWorkVerify(); }
```

**语料（27 门课，18 个作业任务点，15 个已判分）**：

| 页面形态 | 数量 | 读到的字段 |
| --- | --- | --- |
| 已判分（→ `work/view`，隐藏域 8~11 个） | 8 个读到 `redoTimes` | `redoTimes = 0`（**6 个，一次性**）、`= 3`（2 个） |
| 已判分但页面结构不同 | 7 个 | 只有 `tasktips`「未达到及格线，请重做」，读不到次数 |
| 未提交（→ 作答页 `dowork`） | 3 个 | `limitWorkSubmitTimes = 100`、`addTimes = 0` |

**怎么用**（`api/fanya_work.py`）：

* 剩余 `<= 1` 次 → **坐实**是一次性作业（`single_shot=True`，只保存草稿）；
* 剩余 `>= 2` 次 → 页面说还能再交，但**「平台允许提交次数」与「教师给的重做次数」
  是不是同一件事，2026-10-16 还没验证过**（6 个 `redoTimes=0` 的作业与 3 个
  `limit=100` 的作业从未在同一份作业上同时观测到）。所以默认**仍按一次性处理**，
  只把这个数字写进日志、审批预览与盘点（`submitLimit`/`submitRemaining`）；
* 想让工具按页面上的次数直接交卷：`settings.tiku_config` 里加
  `allow_page_submit_times: "true"`（`_opt_in` 只认 `1/true/yes/on`）。此时
  剩余 `>= 2` 就按「交错了还能重做」处理（部分未答上只会打一条提醒日志）。
* 「查不到」时行为不变：按一次性处理（保守）。
* 盘点（`_probe_work_redo_times`）现在会看**未完成且还没有分数**的作业 ——
  这类作业的跳转就是作答页，顺手把次数记进 `item.extra`
  （`submit_limit`/`submit_count`/`submit_remaining`）；已达标、已完成的作业
  一个请求都不发。作答页写着「只能提交 1 次」的未完成作业，盘点里会直接给出
  「平台只允许提交 1 次（交卷后不能再改），本工具默认只保存草稿」的提示。

**怎么验证这套推断**（留给下次真机）：下一次出现「未完成 + 窗口开放」的作业时，
运行日志里会有 `作答页写着本作业可提交 N 次（已提交 M 次…）`；交卷后再回读该作业的
`redoTimes` —— 若 `N >= 2` 而 `redoTimes = 0`，说明两者不是一回事，就必须保持
「默认不信」；反之就可以把它当成可靠信号。

回归保护：`test_parse_work_page_reads_submit_limit_hidden_fields`、
`test_parse_submit_limit_html_tolerates_attribute_order`、
`test_build_prepared_distrusts_page_limit_by_default`、
`test_build_prepared_trusts_page_limit_when_opted_in`、
`test_build_prepared_page_limit_of_one_seals_single_shot`、
`test_summarize_prepared_tells_the_user_how_many_submissions_left`、
`test_probe_work_redo_times_reads_submit_limit_for_ungraded_work`。

### 6.3 视频（planType 10）——累计观看时长门槛

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
  enableDrag: true,                // 允许拖拽
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
* **没设 `enableVideoWatchDuration` 时**：服务端接受一次到位的进度上报。

  | planId | 时长 | 调用 | isFinish |
  | --- | --- | --- | --- |
  | 6797890（已完成项，对照组） | 808s | `ct=0,status=0` → `result:true`；`ct=808,status=2` → `result:true` | 保持 true |
  | **4097446（未完成项）** | **1362s** | 同上 | **false → true** |
  | **4097447（未完成项）** | **643s** | 走 `handle_video` 处理器 | **false → true** |

  这三个任务点的 `planBreakthroughSet` 里 `enableVideoWatchDuration=0`。

* **设了 `enableVideoWatchDuration` 时，一次到位就失效了**（旧结论的适用边界）。
  此时判据是 `score.totalTime >= videoWatchDuration × 60000`（毫秒 vs **分钟**）。
  2026-10-15 三条受控实验：

  | 实验 | 墙上用时 | `totalTime` 增量 | 结论 |
  | --- | --- | --- | --- |
  | 一次性 `ct=0,status=0` → `ct=duration,status=2` | ~0s | **0** | 跳播不给分 |
  | 大跳 `ct=1500`（不等待） | ~6s | **0** | 大跳不给分 |
  | 真实 6 秒心跳 ×7 | 42.6s | **+74.0s** | 按真实间隔计入 |
  | 10 倍速节奏（墙上 6s / `ct` +60） | 63.8s | **+18.0s** | 加速被折扣，不能用 |

  单位检验（`_probe_tt.py` 扫全库 15 个带时长要求的任务点）：

  | planId | 要求 | `totalTime` | 换算 | isFinish |
  | --- | --- | --- | --- | --- |
  | 6797876（唯一完成） | 5.0 min | 304000 | 304s ≥ 300s | **True** ✅ |
  | 8359996 | 13.0 min | 22000 | 22s < 780s | False |
  | 8359997 | 5.0 min | None | 0 < 300s | False |

  后者在 2026-10-15 被按真实时间补到 `totalTime=304000`（304s ≥ 300s）后
  `isFinish` **真的翻转成 True** —— 因果闭环。

  详见 `docs/观看时长门槛.md`（含 `_exp_proof.py` 级别的完整实验记录）。

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

#### 6.5.0 未完成测验的两种「进不去」状态（2026-10-15 全量实测）

COM0018A 的 11 个未完成测验，按进入后落到的页面分两类，**都不是页面结构问题**：

| 类别 | 数量 | 判定依据 | 页面原话 |
| --- | --- | --- | --- |
| 窗口已结束、从未参加 | 5 | 页面含「考试已过期」 | `考试已过期`；开始 2026-05-11 09:59，结束 2026-06-30 09:59 |
| 计划建立前就已交卷 | 6 | 落到 look 查看页 | `作答时间耗尽，试卷已提交`；提交时间 2026-03-20 ~ 2026-05-08 |

第二类的因果值得记下来：`planUser.createDate`（例如 `2026-05-11T01:58:46Z`）
**晚于**试卷提交时间（`2026-05-08 12:25`）。平台因此不认这次旧交卷，
`enableSubmitExam=1` 却仍 `isFinish=false`。

**重考不可用（已查证）**：重考端点确为
`GET mooc1.chaoxing.com/exam/test/reVersionReTest?courseId=&classId=&tId=<examId>&id=<examAnswerId>`，
但对从未参加的试卷实测返回 `{"msg":"学生未参加考试","status":false}`；
对已交卷的试卷，页面里 `retestClick()` / `retest()` / `reTestAction()` **整段被 `/* */` 注释掉**，
「确定重考」按钮的 `onclick=""` 是死代码。→ **两条路都走不通，确认无法自动补完成。**

实现上因此把旧的含糊报错
`作答页缺少必需隐藏域 saveUrl，拒绝继续（避免误提交）`
换成真实成因（`api/fanya_exam.py` 的 `describe_exam_page()`）。

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
| 1 | 上传作品 | 已实现（**实测得分 100**，2026-10-13 planId 8138339；更早一次为 93）|
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

---

## 8. AI剧情（type=7）做到全自动 —— 2026-10-15 实测补遗

以下每一条都是当天在本课程（`planId=8519975`，5 章：4 章情景对话 + 1 章上传）上
跑出来的，取代本文件前面若干"当时未能确定"的猜测。

### 8.1 推进任务链：`init-chain-answer` 的正解是 `nextChapterUuid`

| 传参 | 结果 |
| --- | --- |
| `chapterUuid=<目标章>`（旧实现） | **被忽略**，服务端退回"链上第一个未完成节点"（永远是第 1 章）—— run4 里它给"第 2 章"开的记录其实是**第 1 章的第 15 次作答**，把已到手的 100 分覆盖成 0 分 |
| **`nextChapterUuid=<目标章>`** ✅ | `{"data":"<章节记录 uuid>","status":true}`，定向开出目标章的记录（实测一次成功） |
| `nextChapterUuid=…&isAnswerAgain=true` | `{"msg":"当前作答状态不满足重新作答"}` —— `isAnswerAgain` 属于 `answer-again`，推进时**不能**带 |

* 断言归属**只能**看 `load-data(...).storyExtraData.title`：顶层 `title` 是任务名
  （「离散型随机变量 AI 实践（审计与风控案例）」），章名在 `storyExtraData.title`
  （「审计抽样建模校验」…）。写内容之前必须先校验，否则会把对话写到错章上。
* `answer/init`（本项目里的 `reopen_chapter`）会**额外留一行 `status=-1` 的同章记录**；
  而"链上第一个未完成节点"正是按这行定位的 ⇒ 它会**把自己堵死**。只在确实需要
  重开时才调，不要每次续跑都调。

### 8.2 推进规则与通过门槛只在 `progress[].payload.condition.rule`

`progress[].payload` 是**字符串形式的 JSON**，其中

```json
{ "condition": { "dimension": "总分",
    "rule": [ {"from": 0,  "to": 89,  "nextChapter": "<自己>"},
              {"from": 90, "to": 100, "nextChapter": "<下一章 uuid>"} ] } }
```

* `nextChapter != 本章` 的那条就是"前进口"，其 `from` 就是**通过门槛**（实测都是 90）。
* `progress[].condition[]` **形状不一致**：第 1 章只有 `name:"分数达到90分"`，
  第 2 章起只有 `from`/`to`/`dimension`。靠「…分」字面量解析的
  `condition_threshold()` 从第 2 章起恒为 `None` ⇒ "已通过就跳过"永远不成立，
  满分的章会被**完整重答一遍**（实测）。
* `dimension: "总分"` 指的是**链级总分**：`POST v2/answer/submit`（只带
  `courseid/clazzid/cpi/recordUuid`）+ `GET /ai-ans/ai-evaluate/v2/evaluate/stream`
  **不带 `chapterRecordUuid`** = 链级评估，返回
  `{"chainTitle":…,"totalScore":26,"completedNodeCount":1,"dimensionScores":[…]}`。
  它是**只读预览，不写库**（评估前后外层 `answerScore` 都是 0.0）。
  整链没做完时它必然 < 90，所以 `condition[].hit` 恒 `false` ——
  但**推进并不依赖 `hit`**，`nextChapterUuid` 照样能跳过去。

### 8.3 得 0 分还是 100 分：评卷器要的是**对话里的正文**

* 学生发言由 `story-host-agent`(SSE) 落库（**不要**再用 `add-chat` 重发一次，
  实测那样每轮会写两条一模一样的消息）。
* **智能体的回复正文必须自己补**：`story-host-agent` 只推角色元数据与 `intent`，
  真正的回复正文走语音 WebSocket；纯文字路径下没人落库。用
  `POST story/add-chat`（`chapterRecordUuid` + `roleUuid=<智能体 roleUuid>`）写进去。
  * 第 2 章缺它 → **0 分**，评语原文：「对话历史中仅包含学生发送的提示词，
    **缺失智能体（统计专家）的回复内容**…故判定为未完成」。
  * 补上后 → **100 分**：「完整获取了任务要求的所有统计量…无缺失或错误」。
* 题目里可能有**多段**「AI提示词」，必须**依次逐段**提问：
  * 第 4 章有两段（77 字 + 83 字），只发第一段 → **40 分**，评语原文：
    「**遗漏了关于泊松分布的第二组案例建模与解读**…每缺失1个要点扣20-30分」。
  * 轮流逐段发 → **100 分**。
  * 解析：`_PROMPT_RE.finditer(requirement)` 取全部段落（`extract_prompts()`），
    并把轮数下限抬到段数（平台 `maxRound=1` 会把它压到 1）。

### 8.4 上传章（`subtype=1`）

* 上传页 URL —— **`chapterUuid` 与 `chapterRecordUuid` 必须同时带**：

  ```
  GET /ai-evaluate/v2/answer?courseid=&clazzid=&cpi=&publishRelationUuid=
      &chapterUuid=<章>&chapterRecordUuid=<章节记录>&__from=taskEngine
  ```

  少任何一个都会被 302 回任务链 SPA 页（13563 字节，没有 `#uploadEnc`）。
  带上之后是 207987 字节的作答页，`parse_hidden_inputs` 能取到
  `currentTime` / `currentUploadEnc` / `currentUserId`。
* 传网盘：`POST https://mooc1.chaoxing.com/upload-ans/upload/uploadNew
  ?t=<currentTime>&enc2=<currentUploadEnc>&userId=<currentUserId>`，
  `files={"file": (name, blob, "image/png")}` → 返回 `url` 即 `fileObjectId`。
* 挂附件：`POST /mooc2-ans/ai-evaluate/v2/answer/update-file`
  **`recordUuid=<外层记录>` + `chapterRecordUuid=<章节记录>`**，
  `data={"type": 7, "files": JSON.stringify([{fileObjectId, fileType, fileName}])}`。
  * 传**章节记录**给 `recordUuid` → `{"msg":"作答记录不存在","status":false}`
    （与 `evaluate/stream` 同一套"外层=会话、章节=章节"约定）。
  * 扁平数组（`fileName`，不是 `name`）实测被接受并如期回显；
    `file-mode/answer.js` 发的是卡片式 `[{fileCardId, files:[…]}]`，可作兜底。
* **回读校验**：`load-data(<章节记录>)` 的 `files` / `fileObjectId` 有值才交卷 ——
  服务端对不认识的载荷是"静默成功"，交上去会是一条**没有附件的空记录**。
* 交卷、评分、门槛与对话章完全一致（门槛取该章 `rule.from` = 90）。

### 8.5 零碎但致命的坑

* 上传章**没有开场白角色**，`go-chapter` 会答「章节开场白角色为空」并抛错 ⇒
  `subtype` 判断必须放在 `enter_chapter`（`go-chapter`）**之前**。
  `go-chapter` 不返回 `subtype`，可靠的 `subtype` 在 `get-progress` 行或
  `load-data` 里。
* `api/fanya_story.py` 里的 `ld` 是 `api.fanya_ai`，**没有** `parse_hidden_inputs`
  （那个助手在 `api.fanya_ladder`）。run16 就死在
  `AttributeError: module 'api.fanya_ai' has no attribute 'parse_hidden_inputs'`。
* `go-home` 永远返回**链首**（第 1 章），不能拿它判断"平台当前章"；
  可靠的当前章是 `get-progress.progress[]` 里第一条 `status != 2` 的行。
* `visited` 防空转守卫：`get-progress` 有滞后，推进不顺时会原地打转。

## 9. 执行口径：已完成 / 窗口已过 / 待完成 必须逐类交代（2026-10-16 用户反馈后修正）

### 9.1 用户看到的问题

界面把**已完成**的任务点也当成可勾选项预勾上（`status` 为 `auto/partial`、且
`resolve_item()` 从不看 `is_finish`），用户勾着它们点「执行」，日志只有：

```
任务「线性规划」：共 16 个任务点，待完成 0 个，其中可自动化 0 个
执行结束：成功 0 个，失败 0 个，跳过 0 个
```

16 个任务点里 9 个已完成、7 个窗口已过，**一个都没说** —— 用户的原话是
「过期了，没检查出来」。

### 9.2 三类的判定（执行前一次性算清，全部只读）

| 类别 | 判据 | 处理 |
| --- | --- | --- |
| 已完成 | `item.is_finish and not redo` | 不重做；勾到它时逐条记「已完成（跳过）」 |
| 窗口已过 | `not item.is_finish and item.unreachable` | 不尝试（`getToStudyUrl` 必然拒绝）；勾到它时记「窗口已过（跳过）：<窗口终点>」 |
| 待完成 | 其余 | 正常执行 |

`unreachable` 依赖 `handled_by`，因此 `_probe_item_windows()` 必须在
`classify_items()` **之后**、选候选**之前**调用（只读 GET，失败不挡执行）。

### 9.3 输出契约

* `run_task` 的 `task_start` 事件带 `pending` / `finishedCount` / `expiredCount`，
  `app.py` 把它们累计进 `stats['pending'/'finished'/'expired']`；
* 逐类计数行：`任务「X」：共 N 个任务点 —— 已完成 a（不重做）、窗口已过 b（补不了）、未达标 c、待完成 d（其中可自动化 e）`；
* 结束行：`执行结束：成功 x 个，失败 y 个，跳过 z 个；本次共发现 已完成 a 个（不重做）、窗口已过 b 个（补不了）`；
* 跳过项同时进 `results`（`skipped=True` + 可读的 `message`），界面按明细显示；
* `only_plan_ids` 里**没有**任何本任务点 ⇒ `run_task` 直接静默返回（勾选执行会遍历
  所选课程的全部任务，不为无关任务刷日志）。

### 9.4 界面口径

* 「包含已完成」（`#preview-redo`）**默认关闭**：已完成项与窗口已过项都收起，只显示
  「已完成 N 项已收起（勾上「包含已完成」可展开）/ 窗口已过 M 项已收起（勾上「显示窗口已过」可展开）」；
* 任务标题右侧 meta 始终显示 `未完成 N · 窗口已过 M · 未达标 K · 已完成 J`（计数不受收起影响）；
* 打开「包含已完成」后这些条目才会出现并被勾选 —— 此时执行请求同时带
  `options.redo = true`，runner 会把它们当**重做**候选（口径自洽：要么不看，要么真重做）。

### 9.5 回归用例

`tests/test_fanya_window.py`：`test_run_task_accounts_for_finished_and_expired_items`、
`test_run_task_marks_expired_items_as_skipped_rows`、
`test_run_task_is_silent_for_tasks_outside_the_selection`；
`tests/test_frontend_contract.py`：`test_finished_items_are_collapsed_by_default`。


## 10. 2026-10-17 那轮反馈修的四件事

用户截图里「概率论与数理统计」15 条任务点，4 条跑完是红的。逐条查下来是四件事，
其中两件是「把不可自动化的东西报成失败」，一件是框架层的报错无信息，一件是提示误导。

### 10.1 知识点（微课）不是章节，判「需人工」

`externalDataId=239094628` 是**微课 topicId**，`get_course_point()` 的章节列表里
根本不会有它，所以原来报「章节列表中未找到 id=239094628 的章节（可能已调整或删除）」
—— 却把锅甩给了老师改课。实测取证（plan 9068620/9068621）：

* `study_emptypage?knowledgeid=<topicId>` 只把 `cpi`/`knowledgeid` 记成「已学」，
  `isFinish` 仍是 false，任务点不完成；
* 微课页 `mooc2-ans/coursetopic?…` 的隐藏域里 `microTopicId=239094628`、
  `chapterId=1222351333`，正文是 `/mooc2-ans/wk-view?courseId=&topicId=` 的 ajax；
  完成判据是 wk-view 里的**阅读计时上报**（`studentstudyAjax` 那条链），本工具没有。

所以新增 `page_kind`（`chapter` / `micro_topic` / `""`）：`_probe_item_windows()`
探到 `coursetopic` + `microTopicId` 就标 `micro_topic`，`resolve_item()` 直接给
`manual` + 「平台的完成判据是微课里的阅读计时上报，本工具没有这条链路；请手动打开
这一页完成」。`handle_chapter()` 也加了同样的兜底：真的走到那里也**跳过**而不是
报「章节未找到」。

**2026-10-17 又往下挖了一层（仍然做不成，但把判据钉死了）**：

* 用带 cookie 的真实浏览器（Playwright + 系统 Edge）打开微课页，抓到的链路是
  `topicdetailsmiddle` → `mycourse/stu?microTopicId=` → 真正的学习页
  `mycourse/studentstudy?chapterId=1222351333&microTopicId=239094628` →
  内容页 `mooc-ans/knowledge/cards?…&isMicroCourse=true`（里面是 **4 个 PDF**，
  由 `pan-yz.chaoxing.com/screen/v2/file_<objectid>` 渲染）。
* 整整 52 秒的停留 + 滚动之后，`isFinish` 仍是 false；会话上报只有
  `fystat-ans/log/setlog` 与 `stat2-ans/page/view/log`（page_enter / heartbeat），
  没有完成上报。
* `wk-view` 单独 GET 只返回 `{"status":true}`（只是浏览标记），
  `studentstudyAjax` 带 `micTopicId` 也只是把页面拿回来。
* 完成条量在任务点 JSON 里写着：`planBreakthroughSet.enableFinishPercent=1,
  finishPercent=80` → 要靠 `jobCompleteRate` ≥ 80。已完成的 8375328 是
  `jobCompleteRate=80.0`，这两个失败的恒为 `0.0`。
* `knowledge/cards` 的 `mArg.attachments[]` 里每个 PDF 都带
  `jobid / jtoken / enc / objectid / topicList`（`topicList` 列出该章节下 5 个
  知识点 id）。拿这些 id 走既有的「阅读任务」上报
  `ananas/job/readv2`（`Chaoxing.study_read`）→ 四个全部返回
  **「无效的请求参数」**：PDF 卡片的进度由 pan-yz 阅读器自己的通道上报，
  与 `readv2` 不是一套。

结论没变（判「需人工」），但**"平台判据在 PDF 阅读器里"这句话现在有证据**，
不再是猜测。要做成自动化，下一步是逆 pan-yz 阅读器的进度上报，属于另一件事。

### 10.2 AI实践 的「不能自动做」= 跳过，不是失败

三处闸门原来都 `return False, "…"`，界面上就是红色「失败」。现在改成
`raise TaskSkipped(...)`，落成黄色「跳过」：

| 处理器 | 条件 | 原文案 |
| --- | --- | --- |
| `ai_ladder` / `ai_chat` / `ai_story` | `require_approval` 且没开 `allow_ai_submit` | 请在任务点清单工具栏勾上「AI实践直接提交」 |
| `ai_upload` / `ai_story` | 最后一节是「上传报告/作品」，未自动化 | 同上 |
| `ai_*` | 未识别子模式（学习窗口可能关闭） | 已经由 `supported=False` 落成跳过 |

`TaskSkipped` 是 `api/fanya_task.py` 里的异常类型，`run_item` 捕它 → `skipped=True`，
计入 `stats['skipped']`。

**开关而不是改配置**：`options.allow_ai_submit` 由界面工具栏的
「AI实践直接提交」（`#preview-ai-submit`）控制，默认**关**、状态记在
`localStorage["od-cx-ai-submit"]`。以前 `execute` 把它写死 `false`，用户想交卷
只能去 tiku 配置里改键名，等于没有这个功能。关着时 AI实践 只出草稿并记为「跳过」，
在「待审批」里逐条放行；勾上后 `run_task` 直接把 `allow_ai_submit=True` 交给处理器，
由它 `createPaper → answerQuestion → submitPaper` 一次走完。

### 10.3 「TypeError: cannot unpack non-iterable NoneType object」的守卫

那一轮里「第二章章节测验」只有这一句，堆栈里只有 `run_item` 一帧，没有任何处理器
痕迹。用当前代码对同一个任务点（plan 9069293）以审批模式复跑：返回
「已生成 29 题答案（命中 4 题），待审批后提交 [id=ap-4]」，**无法复现** —— 说明它
来自当时那个更旧的构建。

为了下次不再只给一句 TypeError，`run_item` 改为经 `_handler_outcome(name, value)`
收口：不是 `(ok, message)` 二元组就抛
`处理器 <name> 返回了 <类型>（<值>），不是 (ok, message) 二元组`，并在 `except`
里落成 `异常: TypeError: …`。这样日志里直接点名是哪个处理器。

### 10.4 两处误导性提示

* **审批项被说成「没提交成功」**：审批模式下处理器什么都没写，回读任务点必然
  未完成，原来照样追加「（注意：任务点状态仍为未完成，可能需要等待老师评分或平台
  同步）」。现在 `_awaits_approval(ok, message)`（含 `[id=ap-`）为真就只记
  「任务点已登记待审批」，不回读、不加那句。
* **低命中率没有警告**：plan 9069293 是 29 题只命中 4 题，预览里却只有
  「已生成 29 题答案（命中 4 题）」—— 批准等于把 25 题空白交上去（这类作业往往
  一次机会）。新增 `_thin_hit_warning(total, found)`：命中不到一半时预览里追加
  「⚠️ 只答上 4/29 题（14%）：批准后其余 25 题会以空答案交卷…」，行文案也补
  「（只答上 4/29 题，建议核对后再批准）」。

## 11. 2026-10-17 14 分事故：整卷只有 4 题答上就交了

用户原话：*「我的章节测验怎么只有14分就交了，你必须帮我改过来」*。这不是显示
问题，是**真的把 25 道空题交上去了**，而且该作业不可重做。事故链逐环拆开如下，
每一环都改成「宁可停下，也不瞎交」。

### 11.1 现场（日志逐行）

```
14:14:28  任务点已登记待审批：[作业] 第二章章节测验 — 已生成 29 题答案（命中 4 题），
          待审批后提交 [id=ap-3]（该作业不可重做，批准即不可逆）
          （只答上 4/29 题，建议核对后再批准）
14:41:27  [作业] 审批通过，提交 29 题答案
14:41:29  [作业] 交卷后暂未读到分数
14:41:29  [ap-3] 已批准 -> 成功：作业已提交（29 题，命中 4 题）
```

界面把所有该拦的信号都摆出来了（「命中 4 题」「不可重做」「建议核对」），
**但没有一处是硬的**：用户点「批准」，工具就照单交卷。设计错误在于「警告 +
人工判断」被当成了安全措施。

### 11.2 为什么只有 4 题

1. 题库（`tiku`）查不到 → 29 题里 25 题没答案；
2. 题库的 AI 兜底其实答了（14:13 的 DEBUG：`从AI大模型答题获取答案 … -> A`），
   但 `build_submission` 把这 25 个答案全丢了，日志写明原因：
   `题目 215222536 的答案无法对应到选项字母，本题按未命中处理：A`；
3. 根因：这 25 题**选项本身是图片**（`p.cldisk.com`），解析器只认文字选项 → 选项
   列表为空 → 无法把「A」映射到具体选项值 → 全部判未命中；
4. 更深一层：题干/选项都是图片时本该先 OCR。用户配了 `ocr_config`
   （provider=openai_compatible），**接口地址留空、密钥栏填的是模型名
   `deepseek-flash`**，而 `_apply_ocr_to_title_if_needed` 只在本地 PaddleOCR
   （`CHAOXING_ENABLE_OCR`）时生效 —— 视觉 OCR 那条链在这个函数里根本没接上，
   日志只有一条 WARNING。于是 AI 全程在**没看过题干**的情况下猜答案。

### 11.3 改成了什么

* **整卷闸门**：`build_submission` 逐题记录 `found`；只要 `found < total`，
  `prepare_course_work` 直接返回失败，**不登记审批、不生成可交卷的东西**：
  「29 题只答上 4 题（14%）：缺 25 题，这种卷子不交」。原来的「⚠️ 命中不到一半
  建议核对」警告已删除 —— 有硬闸门就不该再留一条「你可以硬交」的路。
* **AI 补答**：题库没命中但 AI 给了答案的题，不再丢弃，直接写进 payload
  （`details[i]["source"] = "AI"`），日志 `AI 补答 25 题`。AI 答案同样过
  `_resolve_choice_answer`，能映射才收。
* **图片选项不再被吃掉**：`parse_work_page` 遇到图片选项保留占位
  `（图片选项）`，选项数量与真实题目一致；`_resolve_choice_answer` 在「选项
  不认识」且答案本身就是干净的 A–H 字母时，按字母收下（平台提交的就是字母）。
* **OCR 接线**：`_apply_ocr_to_title_if_needed` 现在同时认本地 PaddleOCR 和
  设置页的视觉 OCR（`is_vision_ocr_enabled`），并且**选项字段也 OCR**
  （原来只看题干）。
* **OCR 不可用就别交**：`ocr_gap()` 返回人话原因（没选供应商 / 密钥空 /
  openai_compatible 没填接口地址 / 模型空），`parse_work_page` 把它记在
  `parsed["ocrBlocked"]`；`submission_blockers` 首位检查 —— 只要卷子里有图片题
  而 OCR 不可用，直接失败：「这份卷子有 12 道题的题干是图片，但 OCR 没生效：
  …。DeepSeek 这类文字模型不行，要 qwen-vl / Qwen2-VL / gpt-4o 这类能看图的」。
* **设置页看得见**：设置页 OCR 卡片新增状态行（`#ocr-status`），按同一套规则
  实时算「已启用 / 未启用 + 缺哪一项」，并写明「题干是公式图片的题会一律留空，
  整卷不会自动交卷」。

### 11.4 这套闸门明确不做什么

* 不保证分数：OCR 认错公式、AI 答错，都会被如实记录，不会为了「看起来完成」
  去凑答案。
* 不自动降低标准：整卷不交 ≠ 帮你点「放弃」。作业窗口还在时，用户下次运行
  配好 OCR 后会重新生成答案；窗口已过的作业仍然是「补不了」。

### 11.5 回归用例

`tests/test_parsers.py`：
`test_build_submission_rejects_a_paper_that_is_mostly_blank`、
`test_build_submission_accepts_ai_answers_when_the_bank_misses`、
`test_image_options_do_not_swallow_the_letter_answer`、
`test_build_submission_refuses_image_paper_without_ocr`、
`test_build_submission_answers_image_paper_when_ocr_available`、
`test_submission_blockers_explain_ocr_gap`；
`tests/test_fanya_work_flow.py`：
`test_prepare_course_work_refuses_when_the_bank_only_covers_a_few_questions`、
`test_prepare_course_work_does_not_register_approval_for_a_thin_paper`；
`tests/test_frontend_contract.py`：`test_settings_page_shows_why_ocr_is_off`。

**Open objectives（截至本块）**：14 分已判、不可撤回（该作业无重做机会），只能
保证同类事故不再发生 + 让用户配好视觉 OCR 后重跑其余窗口未过的作业。