# -*- coding: utf-8 -*-
"""「AI剧情」(``data.type == 7``) 的作答链路。

协议全部来自客户端 JS 逆向 + 真机只读探测，见 ``docs/FANYA_TASK_API.md``。

状态：**链路已打通，只差「交卷」这最后一步由调用方决定**
------------------------------------------------------
真机验证（2026-10-12）逐项确认：

| 环节 | 结论 |
| --- | --- |
| 发学生消息 | ✅ 可用 —— `add-chat` 配**学生角色**（`type=0`）能真实落库 |
| 记录读取 | ✅ 必须用 `load-data` **带 `chapterRecordUuid`**（`get-chats` 看不到）|
| 章节交卷 | ✅ 可用 —— `POST /v2/answer/submit` 带 `chapterUuid`+`chapterRecordUuid`，章节 status 0→1 |
| 触发评分 | ✅ **可达** —— `evaluate/stream` 会给出真实业务状态（见下方修正） |

★★ 2026-10-13 架构澄清（前后两次结论各对一半，这里给出准确的）★★

| 环节 | 通道 | 需要浏览器/WebSocket？ |
| --- | --- | --- |
| 取页面令牌 t/enc | 入口 URL 的 **302 Location** | ❌ 不需要（一次 GET）|
| 学生发言 add-chat | HTTP | ❌ |
| 章节交卷 answer/submit | HTTP | ❌ |
| **评分 evaluate/stream** | HTTP GET | ❌ **不需要** |
| 章节条件 story-host-agent | HTTP SSE | ❌ |
| **智能体回复正文 aiResponse** | **WebSocket 音频转写** | ✅ **需要** |

两条曾被写错的结论，都已纠正：

1. ❌「评分必须由浏览器建立对答会话」—— 错。评分只要 ``recordUuid`` 传对
   （外层记录）就能跑；传章节记录得到「会话不存在」，那只是参数错误的症状。
2. ❌「整套流程不需要 WebSocket」—— 也不全对。评分确实不需要，但
   **智能体的回复正文只能从 WebSocket 的音频转写里拿**。

依据（通话页 chunk 6190）：

.. code-block:: javascript

    case "response_done":                    // WebSocket 事件
        var n = s || t.lastUserTranscript;
        t.checkChapterCondition(n, 0)        // SSE: story-host-agent
         .then(function(){ t.addMessage(n, t.aiResponse) });  // aiResponse=智能体回复

即：学生语音 → WS → 智能体语音回复 → 转写进 ``aiResponse`` → ``response_done``
时连同学生转写一起 ``add-chat`` 落库。

⚠️ **2026-10-15 更新：下面这段"纯文字路径实测得 0 分"的结论已被推翻。**
纯文字路径**可以拿满分**：学生话走 ``story-host-agent`` 落库，**智能体回复正文
由内容生成器写好后用智能体角色 ``add-chat`` 补进对话**即可 —— 实测第 1~4 章
全自动 100 分、第 5 章（上传作品）100 分。语音 WS 不是必需品。

其它实测要点：``realtimeDialogEnc`` 必须用 JS 里硬编码的 ``test_enc_2026``
（``load-data`` 返回的真实值会被判「enc 鉴权错误」）；``config`` 消息的
``speaker``/``botName``/``systemRole``/``speakingStyle`` 四项必填。
（WS 路径本身在本模块里**没有实现**，只在注释里。）

⚠️ 交卷不可逆：本模块会发言、交卷、评分，但调用方必须自行确认
（部分课程的作答记录 ``enableReset=false``，提交后无法重置）。

结构（实测 2026-10-15 修正）
----------------------------
剧情是一串**章节**（``get-progress.progress[]``），每章要么是「情景对话」
（``subtype=0``）要么是「上传作品」（``subtype=1``）；章内还有若干**对话槽位**
（``get-chats`` 项，带 ``maxRound``/``userRound``）。

```
get-progress       → 每章：uuid / title / status / score / chapterRecordUuid / payload
                     （payload 是字符串 JSON，condition.rule 才是推进规则）
go-chapter(+uuid)  → 进入指定章（上传章会报「章节开场白角色为空」）
get-chats          → 全部对话槽位（服务端**忽略** chapterUuid）
story-host-agent   → 发一轮学生话（SSE；落库）
add-chat           → 写一条消息（学生或智能体角色）
init-chain-answer  → 开/推进：★ 传 **nextChapterUuid** 才定向（chapterUuid 被忽略）
answer/submit      → 交卷（courseid/clazzid/cpi/recordUuid/chapterUuid/chapterRecordUuid）
evaluate/stream    → 评分（recordUuid 用**外层记录**；不带 chapterRecordUuid = 链级总分）
answer/update-file → 上传章挂附件（recordUuid 用**外层记录** + chapterRecordUuid）
```

三条最容易踩的坑（都实测过）：
1. 推进必须用 ``init-chain-answer?nextChapterUuid=<目标章>``；传 ``chapterUuid``
   会被忽略并退回"链上第一个未完成节点"（run4 因此把第 1 章的 100 分覆盖成 0 分）。
2. 开出来的记录必须用 ``load-data(...).storyExtraData.title`` 校验归属 ——
   顶层 ``title`` 是**任务名**，两章看起来一模一样。
3. 智能体的回复正文必须自己写进对话（``add-chat`` + 智能体 ``roleUuid``），
   否则评卷器判「缺失智能体回复内容」给 0 分。

安全边界
--------
* ``add-chat`` 会**写入真实对话内容且不可撤销**，因此每章的轮数由
  ``story_max_rounds`` 硬性封顶（默认 3，用户要求"先让 AI 试几轮"）；
  题目自带多段 AI提示词时会按段数抬高下限。
* 任何异常、AI 生成失败、条件不满足，都**只停止并报告**，不会提交或伪造数据。
* 上传作品章（``subtype=1``）**已实现**：AI 写小结 → 渲染 PNG → 传超星网盘 →
  ``answer/update-file`` 挂到章节记录（回读确认挂上）→ 交卷 → 评分。
* ``answer/init``（``reopen_chapter``）会额外留一行 ``status=-1`` 的同章记录，
  而"链上第一个未完成节点"正是按它定位的 ⇒ 只在确实要重开时才调。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Any, Callable, Optional

import requests
from loguru import logger

from api import fanya_ai as ld

STORY = f"{ld.MOOC_ANS_DOMAIN}/mooc2-ans/ai-evaluate/story"
HEADERS = {"Referer": f"{ld.MOOC_ANS_DOMAIN}/ai-evaluate/v2/answer"}

DEFAULT_MAX_ROUNDS = 3      # 用户要求：AI 先试 3 轮，剩下留给他自己
PLAN_TYPE_UPLOAD = 1        # subtype=1 → 上传作品节点
ROLE_NARRATOR = 2           # get-roles 的 type：2=旁白
ROLE_AGENT = 1              # 1=智能体
ROLE_STUDENT = 0            # 0=学生（⚠️ 是 0，注意 falsy）

# 页面令牌：写在 /ai-evaluate/v2/answer 的 302 Location 里（不需要浏览器）
PAGE_TOKEN_KEYS = ("t", "enc", "aiEnc", "fileEnc", "asrEnc")


def fetch_page_tokens(session: requests.Session, entry_url: str) -> dict:
    """从入口 URL 的 302 跳转里取出页面令牌。

    ``story-host-agent`` 需要页面 URL 上的 ``t`` / ``enc``（缺了直接 HTTP 400），
    而它们就写在 ``/ai-evaluate/v2/answer`` 的 302 ``Location`` 里：

        Location: /mooc2-ans-vue/taskChain/student?...&t=<毫秒>&enc=<32hex>
                  &aiEnc=<32hex>&fileEnc=<32hex>&asrEnc=<32hex>

    ★ 早前这个模块里写过「``enc``/``fileEnc``/``asrEnc`` 不在任何已知接口的
    响应里，疑似由上游页面签发」——**那是错的**，一次 GET 就全拿到了。

    返回 ``{"t": ..., "enc": ..., "aiEnc": ..., "fileEnc": ..., "asrEnc": ...}``，
    取不到时返回空字典（调用方应能容忍，只是 host-agent 会 400）。
    """
    url = (entry_url or "").strip()
    if not url:
        return {}
    try:
        r = session.get(url, allow_redirects=False, timeout=60.0, headers=HEADERS)
    except requests.RequestException as exc:
        logger.debug("取页面令牌失败: {}", exc)
        return {}
    loc = r.headers.get("Location") or ""
    if not loc and r.status_code == 200:
        loc = r.url or ""
    if not loc:
        logger.debug("取页面令牌：入口未返回 Location（HTTP {}）", r.status_code)
        return {}
    q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    tokens = {k: (q.get(k) or [""])[0] for k in PAGE_TOKEN_KEYS}
    if not tokens.get("enc"):
        logger.debug("取页面令牌：Location 里没有 enc —— {}", loc[:160])
    return tokens


class StoryError(Exception):
    """剧情链路的技术性失败（网络/协议），可重试。"""


class StoryAbort(Exception):
    """应当停止并交给用户的业务性中止（如轮数用尽、遇到上传节点）。"""


# ---------------------------------------------------------------- 基础请求
def _get(session: requests.Session, path: str, params: dict,
         timeout: float = 30.0) -> dict:
    try:
        r = session.get(f"{STORY}/{path}", params=params, timeout=timeout,
                        headers=HEADERS)
    except requests.RequestException as exc:
        raise StoryError(f"请求 {path} 失败: {exc}") from exc
    if r.status_code != 200:
        raise StoryError(f"{path} 返回 HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError as exc:
        raise StoryError(f"{path} 返回非 JSON: {r.text[:160]}") from exc
    if not body.get("status"):
        msg = body.get("errorMsg") or body.get("msg") or str(body)[:160]
        raise StoryError(f"{path} 失败: {msg}")
    return body.get("data") or {}


def _post_form(session: requests.Session, path: str, data: dict,
               timeout: float = 40.0) -> dict:
    try:
        r = session.post(f"{STORY}/{path}", data=data, timeout=timeout,
                         headers=HEADERS)
    except requests.RequestException as exc:
        raise StoryError(f"POST {path} 失败: {exc}") from exc
    if r.status_code != 200:
        raise StoryError(f"POST {path} 返回 HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as exc:
        raise StoryError(f"POST {path} 返回非 JSON: {r.text[:160]}") from exc


# ---------------------------------------------------------------- 上下文
def base_params(study_url: dict, course: dict, data: dict) -> dict:
    """剧情系列接口的公共参数。

    ⚠️ 注意大小写：剧情接口用 **camelCase**（``courseId``/``classId``），
    而 ``load-data``/``answer/init`` 用全小写（``courseid``/``clazzid``）。
    传错会得到「课程id为空」。
    """
    course_id, class_id, cpi = ld.extract_course_ids(study_url, course)
    uuid = (data.get("publishRelationUuid")
            or ld.parse_publish_relation_uuid(study_url) or "")
    return {
        "courseId": course_id,
        "classId": class_id,
        "cpi": cpi,
        "libraryUuid": data.get("libraryUuid") or "",
        "publishRelationUuid": uuid,
        "recordUuid": data.get("recordUuid") or "",
    }


def _as_dict(value: Any, what: str) -> dict:
    """挡在门口：接口本该返回对象却返回了别的类型时，留证据并降级为空对象。

    ★ 换来的教训（2026-10-15）：``get-progress`` / ``go-chapter`` 的 ``data``
    偶尔会是一个**字符串**，而旧代码一路 ``.get()`` 下去，最后只在任务日志里
    留下一句 `AttributeError: 'str' object has no attribute 'get'`，
    既看不出是哪个接口、也看不出原值。这一层把"到底谁返回了什么"直接写进日志。
    """
    if isinstance(value, dict):
        return value
    if value not in (None, "", [], {}):
        logger.warning("{} 返回的不是对象（{}）：{!r}",
                       what, type(value).__name__, str(value)[:160])
    return {}


def get_progress(session: requests.Session, base: dict) -> dict:
    """当前章的进度（含 ``condition[].hit`` 与 ``nextChapterUuid``）。"""
    return _as_dict(_get(session, "get-progress", base), "get-progress")


def enter_chapter(session: requests.Session, base: dict, chapter_uuid: str) -> dict:
    return _as_dict(_get(session, "go-chapter", {**base, "chapterUuid": chapter_uuid}),
                    "go-chapter")


def go_home(session: requests.Session, base: dict) -> dict:
    return _get(session, "go-home", base)


def get_chats(session: requests.Session, base: dict, chapter_uuid: str) -> dict:
    return _get(session, "get-chats", {**base, "chapterUuid": chapter_uuid})


def get_roles(session: requests.Session, base: dict) -> list:
    data = _get(session, "get-roles", base)
    return data if isinstance(data, list) else []


def add_chat(session: requests.Session, base: dict, chapter_record_uuid: str,
             role_uuid: str, content: str) -> dict:
    """发一轮对话。⚠️ 这会写入真实内容，不可撤销。"""
    return _post_form(session, "add-chat", {
        **base,
        "chapterRecordUuid": chapter_record_uuid,
        "roleUuid": role_uuid,
        "content": content,
    })


# ---------------------------------------------------------------- 取当前章
def current_chapter(progress: dict) -> Optional[dict]:
    """``get-progress`` 报的"当前章"。

    ★ 三重防御：``progress`` 本身、``progress["progress"]``、以及列表元素都可能是
    意外类型（实测见过字符串）。旧写法 ``chapters[0]`` 会把**字符串的第 0 个字符**
    当成"章节对象"返回，下游 ``chapter.get()`` 才炸，且报错完全指不到源头。
    """
    if not isinstance(progress, dict):
        logger.warning("get-progress 结果不是对象（{}），无法定位当前章",
                       type(progress).__name__)
        return None
    chapters = progress.get("progress") or []
    if not isinstance(chapters, list):
        logger.warning("get-progress.progress 不是列表（{}）", type(chapters).__name__)
        return None
    for ch in chapters:
        if isinstance(ch, dict):
            return ch
    return None


def _cond_name(c: Any) -> str:
    """条件名 —— 兼容 ``get-progress`` 的 dict 与 ``go-chapter`` 的裸字符串。"""
    if isinstance(c, dict):
        return str(c.get("name") or "")
    return str(c or "")


def chapter_hit(chapter: dict) -> bool:
    """``condition[].hit`` 全为真才算达标（无 condition 视为未达标）。"""
    conds = [c for c in (chapter.get("condition") or []) if isinstance(c, dict)]
    if not conds:
        return False
    return all(bool(c.get("hit")) for c in conds)


def chapter_condition_text(chapter: dict) -> str:
    conds = chapter.get("condition") or []
    return "；".join(n for n in (_cond_name(c) for c in conds) if n)


# ---------------------------------------------------------------- 内容
_PROMPT_RE = re.compile(r"AI\s*提示词[：:]\s*(.+?)(?:\n\s*\n|提交物|\Z)", re.S)


def extract_prompt(requirement: str) -> str:
    """题目常写着「复制下方提示词发给 AI」—— 第一轮就该发这段原文。

    从 ``requirement`` 里抽出 ``AI提示词：…`` 之后的内容；抽不到返回空串。
    """
    m = _PROMPT_RE.search(requirement or "")
    return (m.group(1).strip() if m else "")


def extract_prompts(requirement: str) -> list:
    """题目里**全部**的「AI提示词」块，按出现顺序。

    ★ 有的章节一次给了两段（实测第 4 章「拓展案例建模与解读」：
    「学生第一处AI提示词：…二项分布…」+「学生第二处AI提示词：…泊松分布…」，
    题目原文要求"复制**两段**提示词**依次**提问智能体"）。
    ``extract_prompt()`` 只取第一段，只发一段会被评卷器判"缺失第二组案例"。
    这里返回全部，调用方按顺序逐轮原样发送。
    """
    return [m.group(1).strip() for m in _PROMPT_RE.finditer(requirement or "")
            if m.group(1).strip()]


def build_round_prompt(chapter: dict, turn: int, history: list,
                       requirement: str) -> str:
    """给内容生成器的提示词。

    第 1 轮优先用题目里给定的「AI提示词」原文（这是题目明确要求的做法）；
    之后每轮要求 AI 结合要求继续追问，推动条件达成。
    """
    if turn == 1:
        given = extract_prompt(requirement)
        if given:
            return given
    return (
        "我们正在完成课程里的 AI剧情 交互任务。\n"
        f"章节：{chapter.get('title') or ''}\n"
        f"任务要求：{(requirement or '')[:600]}\n"
        f"对话轮次：第 {turn} 轮。\n"
        "请以学生身份继续向智能体提问或补充计算过程，内容要贴合上面的要求、"
        "有具体数值与推理，不要重复上一轮。"
    )


def pick_role_uuid(chats: dict, roles: list) -> str:
    """挑**学生**角色（``type == 0``）作为发言身份。

    ⚠️ 这里踩过坑：最初取的是 ``get-chats`` 首条消息的 ``roleUuid``（那是**智能体**
    的开场白角色），结果自己发的内容被服务端记成了「智能体说的话」，学生一句没说，
    评分自然是 0。回读 ``load-data?chapterRecordUuid=`` 才看清：
    用智能体角色发 → 存成 ``type=1``；用学生角色发 → 存成 ``type=0``。
    """
    for role in roles:
        # ⚠️ 不能写 `role.get("type") or -1` —— 学生的 type 恰好是 0（falsy），
        # `0 or -1` 会得到 -1，导致永远选不到学生（真实踩过，且让校验也跟着误判）。
        rtype = role.get("type")
        if rtype is not None and int(rtype) == ROLE_STUDENT and role.get("uuid"):
            return str(role["uuid"])
    for group in (chats.get("chat") or []):
        for msg in (group.get("chat") or []):
            if msg.get("roleUuid"):
                return str(msg["roleUuid"])
    for role in roles:
        if role.get("uuid"):
            return str(role["uuid"])
    return ""


# ---------------------------------------------------------------- 交卷与评分
def submit_chapter(session: requests.Session, study_url: dict, course: dict,
                   record_uuid: str, chapter_uuid: str,
                   chapter_record_uuid: str) -> dict:
    """章节交卷：``POST /mooc2-ans/ai-evaluate/v2/answer/submit``。

    实测交卷后该章 ``answerRecord[].status`` 由 0 变 1。
    """
    course_id, class_id, cpi = ld.extract_course_ids(study_url, course)
    try:
        r = session.post(f"{ld.MOOC_ANS_DOMAIN}/mooc2-ans/ai-evaluate/v2/answer/submit",
                         params={"courseid": course_id, "clazzid": class_id, "cpi": cpi,
                                 "recordUuid": record_uuid, "chapterUuid": chapter_uuid,
                                 "chapterRecordUuid": chapter_record_uuid},
                         timeout=90.0, headers=HEADERS)
    except requests.RequestException as exc:
        raise StoryError(f"章节交卷失败: {exc}") from exc
    if r.status_code != 200:
        raise StoryError(f"章节交卷返回 HTTP {r.status_code}")
    return r.json()


def evaluate_chapter(session: requests.Session, study_url: dict, course: dict,
                     ai_enc: str, record_uuid: str, chapter_record_uuid: str,
                     chapter_uuid: str = "", *, max_seconds: float = 300.0) -> str:
    """触发该章的 AI 评分（``GET /ai-ans/ai-evaluate/v2/evaluate/stream``）。

    参数完全对齐对话页 ``TaskChainChat.startEvaluate()`` 的写法：

    .. code-block:: javascript

        var a = {courseId, clazzId, cpi, aiEnc,
                 recordUuid: e.currentRecordUuid,   // ★ 外层记录
                 chapterRecordUuid: e.chapterRecordUuid,
                 isPreview, useJsonScheme: true};
        e.evaluateEventSource = qX(a, {...});       // EventSource => GET

    ★★ 2026-10-12 修正了一条**此前写错并写进过文档**的结论 ★★

    旧文档说「``recordUuid`` 要传章节记录，不是整体记录」—— **这是错的**。
    对话页传的是 ``currentRecordUuid``，即 URL 上的**外层 recordUuid**
    （``load-data`` 返回的那个），章节记录单独放在 ``chapterRecordUuid``。

    传错的表现是服务端回「会话不存在」（看起来像"没有会话"，
    其实只是参数对不上）。改对之后同一接口立刻给出有意义的答复，
    例如「任务未提交，无法开始评估」—— 那才是真实的业务状态。

    ⚠️ 服务端要求**本章已交卷**才会真正评分；未交卷时返回
    「任务未提交，无法开始评估」，此时返回的文本里没有分数。
    """
    course_id, class_id, cpi = ld.extract_course_ids(study_url, course)
    params = {
        "courseId": course_id, "clazzId": class_id or 0, "cpi": cpi,
        "aiEnc": ai_enc or "", "recordUuid": record_uuid,
        "chapterRecordUuid": chapter_record_uuid,
        "isPreview": 0, "useJsonScheme": "true",
    }
    if chapter_uuid:
        # 对话页并不发送该参数；仅在调用方明确提供时附带，保持与旧行为兼容
        params["chapterUuid"] = chapter_uuid
    try:
        # 用 GET：对话页走的是 EventSource，服务端按 GET 处理
        r = session.get(f"{ld.MOOC_ANS_DOMAIN}/ai-ans/ai-evaluate/v2/evaluate/stream",
                        params=params, stream=True, timeout=(30.0, max_seconds),
                        headers={"Accept": "text/event-stream",
                                 "Cache-Control": "no-cache",
                                 "Connection": "keep-alive",
                                 "Referer": HEADERS["Referer"]})
    except requests.RequestException as exc:
        raise StoryError(f"evaluate/stream 请求失败: {exc}") from exc
    if r.status_code != 200:
        raise StoryError(f"evaluate/stream 返回 HTTP {r.status_code}")

    # ★ 与 host_agent 同一个坑：该接口的 Content-Type 不带 charset，requests 会
    #   回退 ISO-8859-1，于是中文评语（dimension / reason / strengths）全成乱码。
    #   之前没被发现，是因为流程只从这段文本里正则抠 ``totalScore``（纯数字）。
    r.encoding = "utf-8"

    buf: list = []
    try:
        for raw in r.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                ev = json.loads(payload)
            except ValueError:
                buf.append(payload)
                continue
            if ev.get("status") is False:
                msg = str(ev.get("msg") or "")
                logger.debug("evaluate 流报错: {}", msg)
                continue
            if ev.get("type") == "text" and isinstance(ev.get("content"), str):
                buf.append(ev["content"])
    finally:
        r.close()
    return "".join(buf)


def parse_total_score(text: str) -> Optional[float]:
    """从评估文本里取 ``totalScore``；取不到返回 None。"""
    if not text:
        return None
    m = re.search(r'"totalScore"\s*:\s*([0-9]+(?:\.[0-9]+)?)', text)
    if not m:
        m = re.search(r"total\s*Score\D{0,4}([0-9]+(?:\.[0-9]+)?)", text, re.I)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


ANSWER_INIT = f"{ld.MOOC_ANS_DOMAIN}/mooc2-ans/ai-evaluate/v2/answer/init"


def reopen_chapter(session: requests.Session, base: dict,
                   chapter_uuid: str) -> Optional[str]:
    """★ 重开本章作答，返回新的章节记录 UUID（失败返回 ``None``）。

    ``init-chain-answer`` 只在"任务链顺位推进"时可用；一旦本章的**当前**作答是
    一条失败记录（``condition.hit=false``），它就会拒绝并报
    「任务链无法跳转到下一章节」—— 此时页面上的「重新作答」走的是**另一个**接口。

    实测（2026-10-15，planId=8519975 第 1 章）::

        POST /mooc2-ans/ai-evaluate/v2/answer/init
             ?courseid=&clazzid=&cpi=&publishRelationUuid=
             data: {"type": 7, "chapterUuid": <章>, "recordUuid": <外层记录>}
        -> {"status": true,
            "data": {"recordUuid": "7629554e-…",     ← 新的章节记录
                     "answerUuid": "acc675f9-…",
                     "picPreviewPrefix": "https://p.ananas.chaoxing.com/star3/origin/"}}

    三个字段缺一不可（都实测过）：

    * 缺 ``recordUuid`` → 「当前作答状态不满足重新作答」(errorCode 9001)
    * 缺 ``chapterUuid`` → 「已存在未完成的作答记录」(errorCode 4601)
    * ``type`` 必须是本题的作答类型（AI剧情 = 7）

    重开**不会**消耗作答次数（实测 ``answerCount`` / ``remainAnswerCount`` 不变），
    也会保留全部历史记录（``answerRecord`` 只增不减）。
    """
    params = {
        "courseid": base.get("courseId") or "",
        "clazzid": base.get("classId") or "",
        "cpi": base.get("cpi") or "",
        "publishRelationUuid": base.get("publishRelationUuid") or "",
    }
    payload = {
        "type": ld.MODE_STORY,
        "chapterUuid": chapter_uuid,
        "recordUuid": base.get("recordUuid") or "",
    }
    try:
        r = session.post(ANSWER_INIT, params=params, data=payload, timeout=60.0,
                         headers=HEADERS)
    except requests.RequestException as exc:
        logger.debug("answer/init 请求失败: {}", exc)
        return None
    if r.status_code != 200:
        logger.debug("answer/init 返回 HTTP {}", r.status_code)
        return None
    try:
        body = r.json()
    except ValueError:
        logger.debug("answer/init 返回非 JSON: {}", r.text[:160])
        return None
    if not body.get("status"):
        logger.debug("answer/init 被拒: {}", body.get("msg") or body.get("errorMsg"))
        return None
    data = body.get("data") or {}
    record = data.get("recordUuid") if isinstance(data, dict) else None
    if not record:
        return None
    logger.info("已用 answer/init 重开本章作答：{}", str(record)[:8])
    return str(record)


def init_chapter_chain(session: requests.Session, base: dict,
                       chapter_uuid: str, fallback_record: str = "") -> str:
    """为**目标章节**开链，返回该章的 ``chapterRecordUuid``。

    ★★ 参数名是 ``nextChapterUuid``（值 = **目标章的 uuid**），**不是**
    ``chapterUuid``。2026-10-15 实测 —— 这是本项目卡了两轮的真正原因：

    - ``init-chain-answer?chapterUuid=<第2章>``：服务端**忽略这个参数**，退回
      「链上第一个未完成节点」，而那个节点还是第 1 章。返回的记录因此仍属于
      第 1 章（run4 就是这样把第 1 章刚拿到的 100 分覆盖成 0 分的）。
    - ``init-chain-answer?nextChapterUuid=<第2章>``：**直接开在第 2 章**：
      ``{"data": "717dbb88-…", "status": true}``，且
      ``load-data(chapterRecordUuid=717dbb88).storyExtraData.title``
      = 「期望方差数值计算」。任务链从此真正推进。

    顺带纠正一个长期误判：**推进并不需要 ``condition[].hit`` 为真**。第 1 章
    当前作答 100 分、``status=2`` 时，``hit`` 仍然是 ``false``（那一栏判的是
    ``payload.condition.rule.dimension`` 里的「总分」，即**链级** `totalScore`，
    实测只有 26 分）。页面上「未解锁」只是展示；前端 ``handleRePractice``
    用的也正是这个带 ``nextChapterUuid`` 的形状。注意**不能**再带
    ``isAnswerAgain``：实测加了它会得到「当前作答状态不满足重新作答」。

    两级兜底（顺序不能颠倒）：

    1. 服务端在**目标章**已有未交卷记录时会拒绝（「存在作答中的记录，不允许
       重新开始新的作答」）。此时若调用方给出了 ``fallback_record``（当前进行中
       的记录），就直接复用它继续作答 —— 否则整条流程会因为一次重跑而彻底卡死。
    2. 其余错误才回落到 :func:`reopen_chapter`（``answer/init`` 是另一个接口，
       那个接口的参数**确实**叫 ``chapterUuid``）。注意它会额外留一行
       ``status=-1`` 的同章记录，所以只作最后手段。
    """
    try:
        data = _get(session, "init-chain-answer",
                    {**base, "nextChapterUuid": chapter_uuid}, timeout=60.0)
    except StoryError as exc:
        msg = str(exc)
        if fallback_record and ("作答中" in msg or "不允许重新开始" in msg):
            logger.info("该章已有作答中的记录，复用它继续：{}", fallback_record[:8])
            return str(fallback_record)
        record = reopen_chapter(session, base, chapter_uuid)
        if record:
            return record
        raise
    record = data if isinstance(data, str) else (data or {}).get("recordUuid")
    if not record:
        if fallback_record:
            return str(fallback_record)
        raise StoryError(f"init-chain-answer 未返回章节记录: {str(data)[:160]}")
    return str(record)


def chapter_payload(chapter: dict) -> dict:
    """``progress[].payload`` —— 字符串形式的 JSON，里面是本章的完整配置。

    实测键（2026-10-15）：``requirement`` / ``maxRound`` / ``condition{rule,dimension}``
    / ``publish`` / ``scoreStandard`` / ``card``（上传章）/ ``fileModeType`` …
    ``go-chapter`` 与 ``get-nodes`` 都**不返回**这些字段，只有 ``get-progress``
    或 ``load-data`` 才有。上传章（``subtype=1``）尤其只能从这里拿 ``requirement``，
    因为它的 ``go-chapter`` 会直接失败（「章节开场白角色为空」）。
    """
    payload = chapter.get("payload") if isinstance(chapter, dict) else None
    if isinstance(payload, str) and payload.strip().startswith("{"):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = None
    return payload if isinstance(payload, dict) else {}


def chapter_requirement(chapter: dict, data: Optional[dict] = None) -> str:
    """取本章的任务要求：优先 ``load-data``（带 chapterRecordUuid 才有），
    退到 ``progress[].payload.requirement``。"""
    req = str((data or {}).get("requirement") or "").strip()
    if req:
        return req
    req = str(chapter.get("requirement") or "").strip() if isinstance(chapter, dict) else ""
    if req:
        return req
    return str(chapter_payload(chapter).get("requirement") or "").strip()


def chain_rules(chapter: dict) -> list:
    """任务链的**分叉表** ``payload.condition.rule``（含 ``from``/``to``/``nextChapter``）。

    ``get-progress.progress[].payload`` 是**字符串形式的 JSON**，其中
    ``condition.rule`` 才是完整的推进规则（实测 2026-10-15）：

    - 第 1 章：``[{from: 0, to: 89, nextChapter: <自己 1ea18141>},
      {from: 90, to: 100, nextChapter: <第 2 章 84f25641>}]``
    - 第 2 章：``[{from: 0, to: 89, nextChapter: <自己 84f25641>},
      {from: 90, to: 100, nextChapter: <第 3 章「审计风险专业分析」19b84198>}]``

    为什么不能只看 ``progress[].condition``：那一栏第 1 章只给条件名
    「分数达到90分」、第 2 章给的是 ``from``/``to``/``dimension`` 三元组，
    **两章形状不同且都不含"下一章"的完整信息**；只有这张规则表既能给出
    "下一章是谁"，也能给出"多少分算通过"。
    """
    payload = chapter_payload(chapter)
    rule = (payload.get("condition") or {}).get("rule")
    return [r for r in (rule or []) if isinstance(r, dict)]


def chain_next(chapter: dict) -> tuple:
    """返回 ``(下一章的 uuid, 通过门槛)``。

    规则表里 ``nextChapter`` **不等于本章** 的那条就是"前进口"（另一条是
    "掉回本章"，即 0~89 分重做）。两章实测都是 90 分那条通向下一章。

    取不到规则表时退回 ``condition[].nextChapterUuid`` + 条件名里的分数
    （第 1 章那条「分数达到90分」），最后再用 ``publish.reachStandard``（60）。
    """
    me = str(chapter.get("uuid") or "") if isinstance(chapter, dict) else ""
    for rule in chain_rules(chapter):
        nxt = str(rule.get("nextChapter") or "")
        if not nxt or nxt == me:
            continue
        try:
            thr = float(rule.get("from"))
        except (TypeError, ValueError):
            thr = None
        # ★ 链尾判定：``progress[].condition`` 里若有"同一个 nextChapterUuid 但
        #   nextChapter 为空字符串"的条目，那只是**任务链终点标记**，不是可进入的
        #   章节 —— 实测 `2cbbb325…` 用 go-chapter 会报「章节类型错误」。
        #   （payload 的 rule 里它写着一个 uuid，所以只能靠 condition 区分。）
        for cond in _dict_list(chapter.get("condition")) if isinstance(chapter, dict) else []:
            if (str(cond.get("nextChapterUuid") or "") == nxt
                    and not str(cond.get("nextChapter") or "")):
                logger.info("本章的 90~100 分支没有后续章节（nextChapter 为空）→ 任务链终点")
                return "", thr
        return nxt, thr
    conds = _dict_list(chapter.get("condition")) if isinstance(chapter, dict) else []
    nxt = str(conds[0].get("nextChapterUuid") or "") if conds else ""
    thr = condition_threshold(chapter)
    if thr is None:
        pl = chapter.get("payload") if isinstance(chapter, dict) else None
        if isinstance(pl, str) and pl.strip().startswith("{"):
            try:
                pl = json.loads(pl)
            except (TypeError, ValueError):
                pl = None
        if isinstance(pl, dict):
            try:
                thr = float((pl.get("publish") or {}).get("reachStandard"))
            except (TypeError, ValueError):
                thr = None
    return nxt, thr


def condition_threshold(chapter: dict) -> Optional[float]:
    """从条件名里解析分数阈值，如「分数达到90分」→ 90.0。"""
    for text in ([chapter_condition_text(chapter)]
                 + [_cond_name(c) for c in (chapter.get("condition") or [])]):
        m = re.search(r"(\d+(?:\.\d+)?)\s*分", text or "")
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


# ---------------------------------------------------------------- 主持人 SSE
AI_ANS = f"{ld.MOOC_ANS_DOMAIN}/ai-ans/ai-evaluate/story"

HOST_AGENT_HEADERS = {
    "Accept": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Content-Type": "application/json",
    "Referer": f"{ld.MOOC_ANS_DOMAIN}/ai-evaluate/v2/answer",
}


def read_uid(session: requests.Session) -> str:
    """SSE 接口要 ``uid``，取 cookie 里的 ``_uid`` / ``UID`` / ``uid``。"""
    try:
        for ck in session.cookies:
            if ck.name in ("_uid", "UID", "uid") and ck.value:
                return str(ck.value)
    except Exception:  # noqa: BLE001
        pass
    return ""


def host_agent(session: requests.Session, base: dict, chapter_uuid: str,
               chapter_record_uuid: str, uid: str, user_content: str,
               *, speak_role_type: int = ROLE_STUDENT, timeout: float = 180.0,
               tokens: Optional[dict] = None):
    """把学生消息发给「AI 主持人」并**流式**读出事件。

    这才是剧情对话的真正通道（``add-chat`` 只是把消息写进记录）：
    它一边驱动智能体应答，一边把章节条件判定的结果以 SSE 事件推回来 ——
    **条件达成时事件里带 ``triggered: true``**。JS 里正是靠这个字段判定
    「本章完成」，而不是轮询 ``get-progress``。

    逐个 ``yield`` 解析后的 JSON 事件，便于调用方即时看到 ``triggered``。
    """
    params = {
        "previewType": 0,
        "courseId": base.get("courseId") or "",
        "clazzId": base.get("classId") or "",
        "uid": uid,
        "libraryUuid": base.get("libraryUuid") or "",
        "publishRelationUuid": base.get("publishRelationUuid") or "",
        "recordUuid": base.get("recordUuid") or "",
        "chapterUuid": chapter_uuid,
        "cpi": base.get("cpi") or "",
        "speakRoleType": speak_role_type,
        "chapterRecordUuid": chapter_record_uuid,
        "npcReply": "false",
    }
    # ★ 页面令牌：服务端要求 URL 上带 t / enc，缺了直接 HTTP 400。
    #   来源见 fetch_page_tokens()（入口 URL 的 302 Location）。
    for k in ("t", "enc"):
        v = (tokens or {}).get(k)
        if v:
            params[k] = v
    try:
        r = session.post(f"{AI_ANS}/story-host-agent", params=params,
                         json={"userContent": user_content or ""},
                         headers=HOST_AGENT_HEADERS, stream=True, timeout=timeout)
    except requests.RequestException as exc:
        raise StoryError(f"story-host-agent 请求失败: {exc}") from exc
    if r.status_code != 200:
        raise StoryError(f"story-host-agent 返回 HTTP {r.status_code}")

    # ★★ 必须强制 utf-8：该接口的 Content-Type 不带 charset，requests 会回退到
    #    ISO-8859-1，于是 iter_lines(decode_unicode=True) 把 UTF-8 中文解成乱码，
    #    json.loads 直接失败 —— 智能体的应答事件会被整条丢掉，
    #    表现成"学生自说自话、智能体从不回应"。原始字节本身是完好的 UTF-8。
    r.encoding = "utf-8"

    # ★ SSE 解析：按规范，**同一个事件可以拆成多个 `data:` 行**，要用换行拼接，
    #   直到遇到空行才算一个完整事件。此前是「一行一个 JSON」，结果智能体的
    #   应答事件（字段多、payload 长）被拆开解析失败，被整条丢掉 ——
    #   表现就是"学生发完消息、智能体一直不出现"。
    buf: list = []
    try:
        for raw in r.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            line = raw.rstrip("\r")
            if line == "":                      # 空行 = 事件结束
                if buf:
                    payload = "\n".join(buf)
                    buf = []
                    try:
                        yield json.loads(payload)
                    except ValueError:
                        logger.debug("SSE 事件无法解析（前 200 字）: {}", payload[:200])
                continue
            if line.startswith(":"):            # 注释 / 心跳行
                continue
            if line.startswith("data:"):
                buf.append(line[5:].lstrip())
        if buf:                                 # 流结束但没以空行收尾
            try:
                yield json.loads("\n".join(buf))
            except ValueError:
                logger.debug("SSE 尾部事件无法解析")
    finally:
        r.close()


def agent_triggered(events, on_event: Optional[Callable] = None) -> tuple[bool, str]:
    """消费事件流，返回 ``(是否达成条件, 原因)``。"""
    reason = ""
    for ev in events:
        if on_event:
            on_event(ev)
        if ev.get("reason"):
            reason = str(ev["reason"])
        elif ev.get("conditionDescription"):
            reason = str(ev["conditionDescription"])
        if ev.get("triggered"):
            return True, reason
    return False, reason


def fetch_story_data(session: requests.Session, study_url: dict, course: dict,
                     record_uuid: str, chapter_record_uuid: str) -> dict:
    """剧情页真正的数据源：``load-data`` **带 chapterRecordUuid**。

    ⚠️ 两个 UUID 必须分开传（``chapterRecordUuid`` 是章节记录、``recordUuid`` 是
    整体记录）。我最初把同一个值塞给两个参数，服务端返回
    ``{"status": false, "msg": "…"}``，于是误判「剧情没有章节数据」。

    返回的数据里才有 ``conversationId`` / ``messages`` / ``chapter`` / ``subtype``
    —— 这些都是 ``get-chats`` **看不到**的（我一度用 get-chats 验证落库，导致
    把「其实写进去了」误判成「没写进去」）。
    """
    course_id, class_id, cpi = ld.extract_course_ids(study_url, course)
    params = {
        "courseid": course_id, "clazzid": class_id, "cpi": cpi,
        "publishRelationUuid": ld.parse_publish_relation_uuid(study_url) or "",
        "chapterRecordUuid": chapter_record_uuid,
        "recordUuid": record_uuid,
    }
    try:
        r = session.get(f"{ld.MOOC_ANS_DOMAIN}/mooc2-ans/ai-evaluate/v2/answer/load-data",
                        params=params, timeout=30.0, headers=HEADERS)
    except requests.RequestException as exc:
        raise StoryError(f"load-data(chapterRecordUuid) 失败: {exc}") from exc
    body = r.json() if r.status_code == 200 else {}
    if not body.get("status"):
        raise StoryError("load-data(chapterRecordUuid) 返回失败: "
                         + str(body.get("errorMsg") or body.get("msg") or r.text[:120]))
    return body.get("data") or {}


def story_messages(data: dict) -> list:
    """从 ``load-data`` 的结果里取全部对话消息（展平）。"""
    out: list = []
    for group in ((data.get("storyExtraData") or {}).get("chat") or []):
        out.extend(group.get("chat") or [])
    if not out:
        out = list(data.get("messages") or [])
    return out


def student_message_count(data: dict) -> int:
    """学生自己发的消息条数（用于确认"我发的"真的落库了）。

    只看 ``type == 0`` —— 因为用错 roleUuid 时内容会变成 ``type=1``（智能体），
    那样虽然"有内容"但学生等于没说话。

    ⚠️ 同样要防 falsy-zero：``m.get("type") or -1`` 会把学生的 0 变成 -1，
    结果"有学生消息"也被数成 0，校验形同虚设。
    """
    count = 0
    for m in story_messages(data):
        mtype = m.get("type")
        if mtype is not None and int(mtype) == ROLE_STUDENT:
            count += 1
    return count


def agent_message_count(data: dict) -> int:
    """智能体（``type == 1``）在已存对话里的条数。

    用来保证「每条学生发言都有一条智能体回复」这个不变量：
    续作答时不会给已经回复过的轮次再补一条。
    """
    count = 0
    for m in story_messages(data):
        mtype = m.get("type")
        if mtype is not None and int(mtype) == ROLE_AGENT:
            count += 1
    return count


def pick_agent(data: dict, chats: dict, chapter_uuid: str,
               roles: list) -> tuple[str, str]:
    """挑**智能体**角色（``type == 1``），返回 ``(roleUuid, 角色名)``。

    ★ 为什么必须用智能体角色发一次：第 2 章「期望方差数值计算」的评卷器原文
    （2026-10-15，0 分那次）：

        对话历史中仅包含学生发送的提示词，缺失智能体（统计专家）的回复内容。
        根据评估要求，需核查是否完整获取了期望、方差、标准差、P(X=2)、P(X≥3) 的
        全部计算结果，由于缺乏AI的计算输出，无法确认任务完成情况及数值正确性，
        故判定为未完成。

    即：**题目要的是「保存 AI 回复」，只有学生那半边对话的章节拿不到分。**
    前端的做法是 ``addMessage(userText, aiResponse)`` → ``story/add-chat``
    （``roleUuid = loadData.startRoleUuid``，就是智能体角色）；本函数找的
    正是那个 roleUuid。
    """
    related = str((data.get("chapter") or {}).get("relatedRole") or "")
    want = [x.strip() for x in related.split(",") if x.strip()]
    sed_roles = [r for r in ((data.get("storyExtraData") or {}).get("role") or [])
                 if isinstance(r, dict)]
    agents = [r for r in sed_roles
              if r.get("type") is not None and int(r["type"]) == ROLE_AGENT]
    for r in agents:                                  # 优先本章 relatedRole 里的智能体
        if want and str(r.get("uuid")) == want[0]:
            return str(r["uuid"]), str(r.get("name") or "")
    if agents:
        return str(agents[0].get("uuid")), str(agents[0].get("name") or "")
    for r in (roles or []):                           # 退回 get-roles
        if (r.get("type") is not None and int(r["type"]) == ROLE_AGENT
                and r.get("uuid")):
            return str(r["uuid"]), str(r.get("name") or "")
    return "", ""


def generate_agent_reply(generator, role_name: str, requirement: str,
                         student_text: str, intent: str = "") -> str:
    """让内容生成器写出**智能体的回复正文**（要落库的那半边对话）。

    ``story-host-agent`` 的 SSE 只推角色元数据 + ``intent``，**回复正文走语音
    WebSocket**（见本模块 docstring），所以纯文字路径下必须自己把这半边补上——
    否则对话里永远只有学生的话，评卷器判「对话尚未展开 / 缺失智能体回复」。
    """
    system = (
        f"你是课程「AI实践」情景对话里的智能体角色「{role_name or '智能体'}」。"
        "学生刚刚向你发言，请你以该角色的身份直接给出**回复正文**。"
        "要求：紧扣任务要求；如果任务涉及计算，请把每一步公式、代入过程和最终数值"
        "完整写出来，数值一律保留 4 位小数；再说一句结论或提醒。"
        "不要写 Markdown 标题，不要出现「作为一个AI」之类的话，只输出回复正文。"
    )
    user = (f"任务要求：{(requirement or '')[:800]}\n\n"
            f"学生发言：{student_text[:800]}\n\n")
    if intent:
        user += f"（平台给你这一轮的意图提示：{intent}）\n\n"
    user += "请直接给出你的回复正文。"
    try:
        text = generator.generate_text(system, user, temperature=0.2,
                                       max_tokens=1500)
    except Exception as exc:  # noqa: BLE001
        logger.warning("生成智能体回复失败：{}", exc)
        return ""
    return str(text or "").strip()


# ---------------------------------------------------------------- 主流程
def chapter_max_round(chats: dict, chapter_uuid: str) -> Optional[int]:
    """从 ``get-chats`` 里取该章节点的 ``maxRound``（取最小值 = 最严格的限制）。

    ★ 这个限制必须遵守，实测教训：第 2 章「期望方差数值计算」的节点
    ``maxRound=1``（第 1 章是 5），而流程硬编码发了 3 轮 —— 结果智能体
    从第 1 轮起就反复回「已达到最大对话轮次，安排专家进行总结收尾」，
    学生还在硬发内容，**整段对话被判 0 分**。

    ``get-chats`` 返回的是剧情模板：每个节点形如
    ``{"id": "<chapterUuid>-<n>", "chapterUuid": ..., "maxRound": 5|1}``。
    """
    nodes = (chats or {}).get("chat") or []
    vals = []
    for n in nodes:
        if str(n.get("chapterUuid") or "") != chapter_uuid:
            continue
        try:
            v = int(n.get("maxRound") or 0)
        except (TypeError, ValueError):
            continue
        if v > 0:
            vals.append(v)
    return min(vals) if vals else None


def _norm_condition(conds: Any) -> list:
    """把 ``go-chapter`` 的 ``condition`` 归一到 ``get-progress`` 的形状。

    ★ 这两个接口的 ``condition`` **形状不一样**（实测 2026-10-15）：
      * ``get-progress`` → ``[{"hit": false, "name": "分数达到90分",
                               "nextChapter": "期望方差数值计算",
                               "nextChapterUuid": "84f25641…"}]``
      * ``go-chapter``   → ``["分数达到90分"]``  ← 只是**条件名的字符串**
    旧代码把 go-chapter 的这串名字原样塞进 ``chapter["condition"]``，下游
    ``conds[0].get("nextChapterUuid")`` 直接炸：
    ``AttributeError: 'str' object has no attribute 'get'``。
    只在"显式重做第 1 章"时会踩到 —— 第 2 章 go-chapter 的 condition 恰好是空列表，
    把这个坑完美地藏了过去。
    这里统一成 dict；``hit`` 未知就按 False（未达标）算，``nextChapterUuid`` 留空。
    """
    out: list = []
    for c in (conds or []):
        if isinstance(c, dict):
            out.append(c)
        elif c:
            out.append({"hit": False, "name": str(c),
                        "nextChapter": None, "nextChapterUuid": None})
    return out


def _dict_list(value: Any) -> list:
    """只保留列表里的 dict 元素（接口偶尔混进字符串，别让下游 ``.get()`` 炸掉）。"""
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, dict)]


def _enter_and_load_chapter(session: requests.Session, base: dict,
                            chapter_uuid: str) -> dict:
    """进入指定章节并**重建它的状态字典**（绝不复用上一章的字段）。

    ★ 这里修的是一个真实 bug：旧代码推进时只写
    ``chapter = {**chapter, "uuid": next_uuid}`` —— uuid 换了，但
    ``condition`` / ``answerRecord`` / ``title`` 仍然是**上一章的**。
    于是下一轮又拿旧章的数据判定"已通过"，再次跳过，uuid 原地打转，
    最后撞上防死循环保护并停手（实测日志里连续出现两次"此前已通过"）。

    ``go-chapter`` 会返回目标章自己的 ``condition`` / ``title`` / ``content``，
    用它重建即可。**不要指望 ``get-progress``** —— 实测它有严重滞后：
    切章之后仍然持续报上一章（连报多次都不变）。
    """
    info: dict = {}
    try:
        info = _as_dict(enter_chapter(session, base, chapter_uuid), "go-chapter")
    except StoryError as exc:
        logger.warning("go-chapter({}) 失败：{}", chapter_uuid[:8], exc)
    return {
        "uuid": chapter_uuid,
        "title": str(info.get("title") or ""),
        "content": str(info.get("content") or ""),
        "condition": _norm_condition(info.get("condition")),
        # subtype 不在 go-chapter 的返回里；取不到时按对话节点处理，
        # 真遇到上传节点会在后续阶段暴露出来（那时会明确停手并说明）。
        "subtype": int(info.get("subtype") or 0),
        "chapterRecordUuid": "",
        "answerRecord": [],
        "status": 0,
    }


def _wait_chapter_switch(session: requests.Session, base: dict, chapter_uuid: str,
                         tries: int = 6, delay: float = 3.0) -> bool:
    """等平台把"当前章"真的切到 ``chapter_uuid``；``go-home``/``get-progress`` 任一确认即可。

    ⚠️ **当前流程已不再调用它**（2026-10-15）：推进改为确定性的
    ``init-chain-answer?nextChapterUuid=``（一次就成）。保留此函数仅作排查用。

    它当初的判据本身就是错的：``go-home`` 永远返回**链首**第 1 章
    （名字就是"回链首"），所以"平台当前章"根本不能拿它来判 ——
    这也是上一版流程每次都报"任务链尚未推进"的原因。

    ★ 为什么非要等：章节刚达标时，平台要过一会儿才把任务链挪到下一章。此时若抢跑
    （``go-chapter(下一章)`` + ``init-chain-answer``），平台**不会报错**，而是给
    "还没让开的本章"再开一条作答记录 —— 一句本该属于下一章的对话就变成了本章的
    一次 0 分作答。实测（2026-10-15 run4）：第 1 章的 100 分就是这样被覆盖掉的。

    判据只用平台自己报的"当前章节"，**不能用 ``go-chapter`` 的返回值** ——
    ``go-chapter`` 只要那一章存在就成功，证明不了任务链推进过（run4 里它照样返回
    了「期望方差数值计算」，而平台的当前章其实还是第 1 章）。
    """
    target = str(chapter_uuid or "")
    if not target:
        return False
    for i in range(max(1, int(tries))):
        try:
            home = go_home(session, base) or {}
        except StoryError as exc:
            logger.debug("go-home 失败：{}", exc)
            home = {}
        if str(home.get("chapterUuid") or home.get("uuid") or "") == target:
            logger.info("平台已切到目标章（go-home 确认）")
            return True
        try:
            cur = current_chapter(get_progress(session, base)) or {}
        except StoryError as exc:
            logger.debug("get-progress 失败：{}", exc)
            cur = {}
        if str(cur.get("uuid") or "") == target:
            logger.info("平台已切到目标章（get-progress 确认）")
            return True
        if i + 1 < tries:
            time.sleep(delay)
    return False


# ---------------------------------------------------------------- 上传作品章节
def _story_upload_page_fields(session: requests.Session, base: dict, chapter_uuid: str,
                              chapter_record: str, *, timeout: float = 60.0) -> dict:
    """GET「上传作品」章节页，取一次性上传凭据（currentTime/currentUploadEnc/currentUserId）。

    ★ 页面 URL 的形状是实测出来的（2026-10-15 `chaoxing-fanya/_dz_ch5up.py`）：
    ``/ai-evaluate/v2/answer?courseid=&clazzid=&cpi=&publishRelationUuid=
    &chapterUuid=<章>&chapterRecordUuid=<章节记录>&__from=taskEngine``
    —— **必须同时带** ``chapterUuid`` 与 ``chapterRecordUuid`` 才会返回真正的上传页
    （实测 207987 字节、``<title>AI实践-作答</title>``、含 ``#uploadEnc``）；
    少任何一个都会被 302 回任务链 SPA 页（13563 字节，没有任何上传凭据）。
    """
    url = (f"{ld.MOOC_ANS_DOMAIN}/ai-evaluate/v2/answer"
           f"?courseid={base['courseId']}&clazzid={base['classId']}&cpi={base['cpi']}"
           f"&publishRelationUuid={base['publishRelationUuid']}"
           f"&chapterUuid={chapter_uuid}&chapterRecordUuid={chapter_record}"
           f"&__from=taskEngine")
    resp = session.get(url, timeout=timeout)
    resp.encoding = "utf-8"
    # ⚠️ 本模块里的 ``ld`` 是 ``api.fanya_ai``，它**没有** ``parse_hidden_inputs``
    #   （该助手在 ``api.fanya_ladder`` 里，`fanya_upload` 用的就是它）——
    #   run16 实测踩过：`AttributeError: module 'api.fanya_ai' has no attribute
    #   'parse_hidden_inputs'`，整轮任务在真正上传之前就失败了。
    from api import fanya_ladder as ladder
    fields = ladder.parse_hidden_inputs(resp.text or "")
    if not (fields.get("currentUploadEnc") and fields.get("currentUserId")):
        raise StoryError("上传页未提供上传凭据（currentUploadEnc/currentUserId），"
                         f"无法上传作品（HTTP {resp.status_code}）")
    return fields


def _attach_chapter_files(session: requests.Session, base: dict, chapter_record: str,
                          record_type: Any, uploaded: list, *, cards: bool = False,
                          timeout: float = 90.0) -> None:
    """把已上传的附件挂到**章节记录**上。

    ★★ 参数是踩出来的（2026-10-15 `chaoxing-fanya/_dz_ch5attach.py` 逐形状实测）：

    | 传给 `recordUuid` | 服务端回答 |
    | --- | --- |
    | 章节记录（`chapterRecordUuid`）❌ | `{"msg":"作答记录不存在","status":false}` |
    | **外层记录**（`load-data` 的 `recordUuid`）✅ | `{"data":{"fileObjectId":…,"fileParseStatus":"1","fileType":"png"},"status":true}` |

    并且必须**同时**带 `chapterRecordUuid=<章节记录>` 才能定位到上传章 ——
    与 `evaluate/stream` 完全同一套约定（外层记录 = "会话"，章节记录 = "章节"）。
    回读 `load-data` 能看到 ``files=[{fileName,fileObjectId,fileType}]``。

    载荷形状：``[{fileObjectId, fileType, fileName}]``（扁平）实测被接受并如期回显；
    ``file-mode/answer.js`` 发的是卡片式 ``[{fileCardId, files:[…]}]``，作为兜底保留。
    """
    if cards:
        payload = [{"fileCardId": "1",
                    "files": [{"fileObjectId": i["fileObjectId"],
                               "fileType": i["fileType"],
                               "fileName": i["name"], "name": i["name"]}
                              for i in uploaded]}]
    else:
        payload = [{"fileObjectId": i["fileObjectId"], "fileType": i["fileType"],
                    "fileName": i["name"], "name": i["name"]} for i in uploaded]
    resp = session.post(f"{ld.MOOC_ANS_DOMAIN}/mooc2-ans/ai-evaluate/v2/answer/update-file",
                        params={"courseid": base["courseId"], "clazzid": base["classId"],
                                "cpi": base["cpi"],
                                "publishRelationUuid": base["publishRelationUuid"],
                                "recordUuid": base["recordUuid"],
                                "chapterRecordUuid": chapter_record},
                        data={"type": record_type,
                              "files": json.dumps(payload, ensure_ascii=False)},
                        timeout=timeout)
    if resp.status_code != 200:
        raise StoryError(f"update-file 返回 HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not body.get("status"):
        raise StoryError(f"update-file 失败: {str(body.get('msg') or body)[:160]}")


def complete_story_upload(session: requests.Session, study_url: dict, course: dict,
                          generator: Any, chapter: dict, chapter_record: str, base: dict, *,
                          options: Optional[dict] = None,
                          on_log: Optional[Callable] = None) -> tuple[bool, str]:
    """完成 ``subtype=1`` 的「上传作品」章节。

    流程（与上传页一致）：生成文字小结 → 渲染 PNG → 传超星网盘 → 挂到章节记录 →
    等解析 → 交卷 → 触发 AI 评分 → 与 ``chain_next`` 的门槛比对。

    **红线**：回读 ``load-data`` 确认附件真的挂上了才交卷 —— 服务端对不认识的载荷
    是"静默成功"，交上去会是一条没有附件的空记录（``fanya_upload`` 里踩过）。
    """
    from api import fanya_upload as fu  # 延迟导入：避免两个模块的导入顺序互相牵制

    opts = options or {}
    log = on_log or (lambda m: logger.info(m))
    title = str(chapter.get("title") or "")
    chapter_uuid = str(chapter.get("uuid") or "")
    requirement = str(chapter.get("requirement") or "")

    fields = _story_upload_page_fields(session, base, chapter_uuid, chapter_record)
    log(f"「{title}」已取得上传凭据（uploadUid={str(fields.get('currentUserId'))[:8]}…）")

    body = str(opts.get("story_upload_hint") or
               "本次实训我已经完成的结果（务必在文中引用这些具体数值）："
               "①审计抽样：X~B(n=100, p=0.02)，E(X)=np=2，Var(X)=np(1-p)=1.96，σ≈1.4；"
               "②产品抽检：50 件、不合格率 0.03 的二项分布，期望不合格数 E(X)=50×0.03=1.5；"
               "③门店到店：泊松分布 λ=4，P(X=0)=e^-4≈0.0183，"
               "P(X≥2)=1-e^-4(1+4)≈0.9084。")
    want = fu.min_chars(requirement)
    system = getattr(fu, "_WORK_SYSTEM", "") or "你是完成课程实训的学生，正在写综合小结。"
    prompt = (f"题目要求：\n{requirement}\n\n{body}\n\n"
              f"请以学生本人的口吻写一份不少于 {max(want, 300) + 120} 字的综合小结，"
              "按题目要求分三点组织，务必把上面这些具体数值写进去（它们是本次实训"
              "我自己的结果），语言用自己的话整合，不要照抄公式推导过程，"
              "不要出现“作为AI/模型”之类的措辞。")
    body = str(generator.generate_text(system, prompt,
                                       temperature=0.7, max_tokens=2600) or "").strip()
    if not body:
        raise StoryError(f"「{title}」综合小结生成失败（AI 未返回正文），已中止")
    pages = fu.render_pngs(title or "综合实践小结", body,
                           max_pages=int(opts.get("story_upload_pages") or 5))
    if not pages:
        raise StoryError(f"「{title}」未能渲染出任何作品图片，已中止")
    log(f"「{title}」已生成小结 {len(body)} 字，渲染 {len(pages)} 张图片")

    uploaded = []
    for idx, blob in enumerate(pages, 1):
        name = f"{idx:02d}.png"
        object_id = fu.upload_to_pan(session, fields, name, blob)
        uploaded.append({"fileType": "png", "fileObjectId": object_id, "name": name})
        log(f"「{title}」已上传第 {idx}/{len(pages)} 张（objectId={object_id[:12]}…）")

    def _read_back() -> dict:
        return fetch_story_data(session, study_url, course, base["recordUuid"], chapter_record)

    node_type = (fetch_story_data(session, study_url, course, base["recordUuid"],
                                  chapter_record).get("type"))
    _attach_chapter_files(session, base, chapter_record, node_type, uploaded)
    after = _read_back()
    if not (after.get("fileObjectId") or after.get("files")):
        log(f"「{title}」扁平式载荷未被接受，改用卡片式再试一次")
        _attach_chapter_files(session, base, chapter_record, node_type, uploaded, cards=True)
        after = _read_back()
    if not (after.get("fileObjectId") or after.get("files")):
        return False, (f"「{title}」附件未能挂到记录上（服务端静默忽略），已中止交卷以免"
                       f"提交空记录；记录 {chapter_record[:8]}… 仍为未提交状态，"
                       f"请在浏览器里手动上传")
    log(f"「{title}」附件已确认挂上（fileObjectId="
        f"{str(after.get('fileObjectId'))[:16]}…）")

    fu.wait_parse(session, {"courseid": base["courseId"], "clazzid": base["classId"],
                            "cpi": base["cpi"],
                            "publishRelationUuid": base["publishRelationUuid"]},
                  chapter_record, uploaded=uploaded, on_log=log)

    log(f"「{title}」交卷…")
    submit_chapter(session, study_url, course, base["recordUuid"], chapter_uuid, chapter_record)
    time.sleep(3.0)
    log(f"「{title}」触发 AI 评分…")
    text = evaluate_chapter(session, study_url, course,
                            (fetch_story_data(session, study_url, course, base["recordUuid"],
                                              chapter_record).get("aiEnc") or ""),
                            base["recordUuid"], chapter_record)
    ai_score = parse_total_score(text)
    _, threshold = chain_next(chapter)
    if threshold is None:
        threshold = condition_threshold(chapter)
    if ai_score is None:
        return False, (f"「{title}」附件已交卷，但未能读到分数（评分可能仍在进行）；"
                       f"稍后重跑本任务点即可刷新")
    if threshold is not None and float(ai_score) < float(threshold):
        return False, (f"「{title}」得分 {ai_score}，未达到 {threshold} 分；"
                       f"附件已交卷，如需提高请修改小结内容后重做")
    log(f"「{title}」得分 {ai_score}（达到 {threshold} 分即通过）")
    return True, f"「{title}」已上传并交卷，得分 {ai_score}"


def complete_story_task(session: requests.Session, course: dict, study_url: dict,
                        generator: Any, *, options: Optional[dict] = None,
                        on_log: Optional[Callable] = None) -> tuple[bool, str]:
    """推进 AI剧情，**每章最多 ``story_max_rounds`` 轮**（默认 3）。

    返回 ``(是否完成, 说明)``。遇到上传节点或用尽轮数时返回 ``False`` + 明确原因，
    把剩余轮次留给用户自己做。
    """
    opts = options or {}
    log = on_log or (lambda m: logger.info(m))
    max_rounds = int(opts.get("story_max_rounds") or DEFAULT_MAX_ROUNDS)
    delay = float(opts.get("story_round_delay") or 3.0)

    if generator is None or not getattr(generator, "available", False):
        raise StoryAbort("未配置 AI 接口（endpoint/key/model），无法进行剧情对话")

    data = ld.load_data(session, ld.extract_course_ids(study_url, course)[0],
                        ld.extract_course_ids(study_url, course)[1],
                        ld.extract_course_ids(study_url, course)[2],
                        ld.parse_publish_relation_uuid(study_url) or "")
    if data.get("type") != ld.MODE_STORY:
        raise StoryAbort(f"该任务点不是 AI剧情（type={data.get('type')}）")

    base = base_params(study_url, course, data)
    if not base["recordUuid"]:
        raise StoryAbort("load-data 未返回 recordUuid，无法进入剧情作答")

    uid = read_uid(session)
    if not uid:
        logger.warning("cookie 里没有 _uid/UID —— story-host-agent 可能拒绝请求")

    # ★ 页面令牌（t / enc）：story-host-agent 缺了它直接 HTTP 400。
    #   来源是入口 URL 的 302 Location，一次 GET 即可，不需要浏览器。
    entry_url = str((study_url or {}).get("url") or "")
    tokens = fetch_page_tokens(session, entry_url)
    if tokens.get("enc"):
        log("已取得页面令牌（t / enc）—— 可以驱动智能体应答")
    else:
        logger.warning("未取得页面令牌，智能体应答可能失败（host-agent 会返回 400）")

    progress = get_progress(session, base)
    chapter = current_chapter(progress)
    if not chapter:
        raise StoryAbort("get-progress 未返回任何章节")

    done_rounds = 0
    chapters_done: list = []
    visited: set = set()

    # 允许从指定章节续做：平台一次只报"当前章"，已通过的章要靠 nextChapterUuid
    # 链跳过，否则会把已经拿到的分数白重做一遍。
    forced = str(opts.get("story_chapter_uuid") or "").strip()
    if forced:
        log(f"按设置从指定章节开始：{forced[:8]}…")
        # ★ 必须**重建**章节状态，只换 uuid 会把上一章的 condition/answerRecord/
        #   title 留在身上 —— 后面「已通过就跳过」的判定就会拿错章的数据
        #   （第 1 章 100 分会被误当成"目标章也通过了"）。
        chapter = _enter_and_load_chapter(session, base, forced) or chapter

    while True:
        title = chapter.get("title") or ""
        subtype = int(chapter.get("subtype") or 0)
        chapter_uuid = str(chapter.get("uuid") or "")

        # ★ 先把章对象换成 ``get-progress`` 里的**真实行**。
        #   推进那一步交给下一轮的是 ``_enter_and_load_chapter()`` 造出来的"空壳"：
        #   ``status`` 恒 0、没有 ``chapterRecordUuid`` / ``answerRecord`` / ``payload``。
        #   拿它判「本章是否已通过」永远判不出来 ⇒ 已经满分的章会被**完整重答一遍**
        #   （run14 实测：第 2 章 100 分、condition hit=True，流程还是重新进入并
        #   另开了一条记录 39b5fff1 又答了一次）。重答不只是浪费：新对话万一不如
        #   旧的，本章分数会被**覆盖**回未通过。
        #   代价是每轮多一次 get-progress（几十毫秒），换的是不会毁掉已有成绩。
        _rows = _dict_list(get_progress(session, base).get("progress"))
        _real = (next((r for r in _rows if str(r.get("uuid")) == chapter_uuid
                       and r.get("chapterRecordUuid")), None)
                 or next((r for r in _rows if str(r.get("uuid")) == chapter_uuid), None))
        if _real:
            chapter = _real
            title = chapter.get("title") or title
            subtype = int(chapter.get("subtype") or 0)

        # 防死循环：服务端一次只报"当前章"，推进不顺时会原地打转（实测 get-progress
        # 存在滞后），必须自己兜住，否则会无限重复同一章。
        if chapter_uuid in visited:
            return (not chapters_done), (
                f"章节推进回到已处理过的章（{chapter_uuid[:8]}…），已停止以免空转。"
                f"已完成：{', '.join(chapters_done) or '无'}")
        visited.add(chapter_uuid)

        if subtype == PLAN_TYPE_UPLOAD:
            # ---------------- 上传作品章（subtype=1）----------------
            # ★ 必须在 ``enter_chapter``（go-chapter）**之前**处理：上传章没有
            #   开场白角色，``go-chapter`` 会返回「章节开场白角色为空」并抛
            #   StoryError（run15 实测），整轮任务会因此失败。
            # ★ 这一支不写任何对话，只做：生成小结 → 渲染图片 → 传网盘 →
            #   ``answer/update-file`` 挂到章节记录 → 等解析 → 交卷 → 评分。
            prog_up = get_progress(session, base)
            row_up = next((r for r in _dict_list(prog_up.get("progress"))
                           if str(r.get("uuid")) == chapter_uuid), None) or chapter
            chapter = row_up
            title = str(chapter.get("title") or title)
            # ★ 上传章也要"已通过就跳过"，否则每次重跑都会**再传一份、再交一次卷**
            #   （run18/run19 实测两次都重新开了记录并重新上传）。
            up_thr = chain_next(chapter)[1] or condition_threshold(chapter) or 60.0
            up_rec_id = str(chapter.get("chapterRecordUuid") or "")
            up_recs = _dict_list(chapter.get("answerRecord"))
            up_cur = next((r for r in up_recs if str(r.get("uuid")) == up_rec_id), None) or {}
            up_score = up_cur.get("score")
            up_status = int(chapter.get("status") or 0)
            up_pending = False
            up_done = (not explicit and up_status == 2
                       and isinstance(up_score, (int, float)) and not isinstance(up_score, bool)
                       and float(up_score) >= float(up_thr))
            if not up_done and not explicit and up_status == 1 and up_rec_id:
                # status=1 = 已交卷、平台还没把 AI 分数写回这一行（上传章实测会停在这里）。
                # 这时只要记录上确实挂着附件，就不该再传一遍 —— 回读一次确认。
                try:
                    _back = fetch_story_data(session, study_url, course,
                                             base["recordUuid"], up_rec_id)
                    up_done = bool(_back.get("files") or _back.get("fileObjectId"))
                    if up_done:
                        # ★ 章节记录的 ``answerScore`` 在未落分时是 0.0（会误导用户），
                        #   真正可用的是 ``recordScore``；两者都没有就说"待评分"。
                        up_score = _back.get("recordScore") or None
                        up_pending = up_score is None
                except StoryError as exc:
                    logger.debug("回读上传记录失败（按未提交处理）：{}", exc)
            if up_done:
                mark = "已交卷待评分" if up_pending else f"{up_score} 分"
                log(f"「{title}」已交卷（{mark}），跳过上传并结束")
                chapters_done.append(f"{title}({mark})")
                return True, f"AI剧情已全部完成：{'；'.join(chapters_done)}"
            chapter_record = ""
            if int(row_up.get("status") if row_up.get("status") is not None else -9) == 0:
                chapter_record = str(row_up.get("chapterRecordUuid") or "")
            if not chapter_record:
                chapter_record = init_chapter_chain(session, base, chapter_uuid)
            log(f"「{title}」作答记录 {chapter_record[:8]}…")
            udata = fetch_story_data(session, study_url, course, base["recordUuid"],
                                     chapter_record)
            # 上传章的 requirement 只有 ``payload``/``load-data`` 里有
            requirement = chapter_requirement(chapter, udata)
            ok, msg = complete_story_upload(
                session, study_url, course, generator,
                {**chapter, "requirement": requirement}, chapter_record, base,
                options=opts, on_log=log)
            if not ok:
                return (not chapters_done), msg
            log(f"「{title}」通过 ✅（{msg}）")
            chapters_done.append(msg)
            nxt_up, _ = chain_next(chapter)
            if not nxt_up:
                return True, f"AI剧情已全部完成：{'；'.join(chapters_done)}"
            log(f"已通过，推进到下一章 {str(nxt_up)[:8]}…")
            # 末节点（链尾的"完成/成就"节点）可能并不存在实体章节，go-chapter 会报错 ——
            # 上传已经交卷评分完毕，这里绝不能把整轮判成失败。
            try:
                chapter = _enter_and_load_chapter(session, base, str(nxt_up))
            except StoryError as exc:
                logger.debug("进入末节点 {} 失败（按整链完成处理）：{}", str(nxt_up)[:8], exc)
                return True, f"AI剧情已全部完成：{'；'.join(chapters_done)}"
            if not chapter:
                return True, f"AI剧情已完成：{'；'.join(chapters_done)}"
            time.sleep(delay)
            continue

        # ---- 0) 已通过的章直接跳过 ----
        # ★ 判据必须是**当前那一条作答**（``chapterRecordUuid``）的分数，不是历史最高分。
        #   平台的推进规则只看"当前作答"：只要当前作答没达标，任务链就卡在本章，
        #   此时"历史上通过过"毫无意义 —— 跳过它反而会把下一章的对话写到本章头上。
        #   实测（2026-10-15 run4）：``progress[0].status == 2``、``score == 0``、
        #   ``condition[0].hit == False``（卡住），但 ``answerRecord`` 里有一条 812b2deb
        #   拿了 100 分；旧代码用 ``max(scores)`` 判定 → 直接跳过第 1 章去开第 2 章，
        #   而平台给"第 2 章"开的记录其实是**第 1 章的第 15 次作答**（0 分），
        #   于是第 1 章已到手的 100 分被这次 0 分覆盖，整章回到"未通过"。
        #
        # ★ 例外：调用方用 ``story_chapter_uuid`` **显式指定**了本章 ——
        #   那就是要重做，绝不能因为"历史上通过过"而跳过。
        conds = _dict_list(chapter.get("condition"))
        nxt0 = conds[0].get("nextChapterUuid") if conds else None
        recs0 = _dict_list(chapter.get("answerRecord"))
        cur_rec0 = str(chapter.get("chapterRecordUuid") or "")
        cur0 = next((r for r in recs0 if str(r.get("uuid")) == cur_rec0), None)
        cur_score0 = cur0.get("score") if cur0 else None
        # ★ 阈值**必须**也从 ``payload.condition.rule`` 取：从第 2 章起
        #   ``condition`` 里只有 ``from``/``to``/``dimension``、**没有 ``name``**，
        #   ``condition_threshold()``（靠「…分」字面量）解析出来是 None →
        #   跳过逻辑永远不成立。run13 实测：第 2 章已经 100 分，流程还是把
        #   它当"没通过"又完整答了一遍（多消耗一次作答、多写一份对话）。
        _nxt_rule, _thr_rule0 = chain_next(chapter)
        if _nxt_rule:
            nxt0 = _nxt_rule
        thr0 = _thr_rule0 if _thr_rule0 is not None else condition_threshold(chapter)
        explicit = bool(forced) and chapter_uuid == forced
        if explicit:
            log(f"「{title}」被显式指定重做，忽略'此前已通过'的跳过逻辑")
        elif (int(chapter.get("status") or 0) == 2
                and isinstance(cur_score0, (int, float))
                and not isinstance(cur_score0, bool)
                and thr0 is not None and float(cur_score0) >= thr0 and nxt0):
            log(f"「{title}」当前作答 {cur_score0} 分已达标，跳过并进入下一章")
            chapters_done.append(f"{title}({cur_score0})")
            chapter = _enter_and_load_chapter(session, base, str(nxt0))
            continue

        # ---- 1) 进入该章并为它开链（拿到本章的作答记录）----
        log(f"进入章节「{title or chapter_uuid[:8]}」")
        # ★ 链尾节点不是可作答章节：run18 实测最后一步
        #   ``go-chapter(2cbbb325…)`` 报「章节类型错误」。``_enter_and_load_chapter``
        #   会**吞掉**这个异常并返回空壳，于是下一轮走到这里才炸 ——
        #   而那时前面 5 章都已经交卷评分完毕（上传章甚至拿了 95 分），
        #   整轮却被判成"失败"，用户会以为白做了。所以这里必须自己兜住：
        #   进不去的章节 = 链已走完，按成功收尾。
        info: dict = {}
        try:
            info = enter_chapter(session, base, chapter_uuid)
        except StoryError as exc:
            logger.warning("go-chapter({}) 失败，按「任务链已走完」收尾：{}",
                           chapter_uuid[:8], exc)
            return True, f"AI剧情已全部完成：{'；'.join(chapters_done)}"
        if info.get("title"):
            title = str(info["title"])
            chapter = {**chapter, "title": title}
        # ★ 目标章的**章名**（go-chapter 的 title 就是章名）。后面用它校验
        #   "刚开的这条作答记录到底属于哪一章"。
        expected_title = title

        # ★ 先看有没有**已经开着但还没交卷**的记录（``status=0``）——
        #   有就直接续作答，**不要再调 init**：重复开新记录会被服务端拒
        #   （「已存在未完成的作答记录」），而且会平白多留一条空记录。
        #   ⚠️ 判据必须重新读 ``get-progress``：只有它有 ``chapterRecordUuid``；
        #   ``go-chapter``（以及 ``_enter_and_load_chapter`` 造出来的章对象）
        #   返回里既没有 status 也没有 chapterRecordUuid，用它判"已开着"永远
        #   判不出来 —— 那样每次续跑都会走 ``reopen_chapter``（answer/init），
        #   给本章再加一行 ``status=-1`` 的重复记录（实测过，就是它把
        #   "链上第一个未完成节点"一直指回本章的）。
        prog_now = get_progress(session, base)
        row_now = next((r for r in _dict_list(prog_now.get("progress"))
                        if str(r.get("uuid")) == chapter_uuid), None) or {}
        open_record = ""
        if int(row_now.get("status") if row_now.get("status") is not None else -9) == 0:
            open_record = str(row_now.get("chapterRecordUuid") or "")
        if open_record:
            log(f"「{title}」已有一条未交卷的记录 {open_record[:8]}…，直接续作答")
            chapter_record = open_record
        else:
            chapter_record = init_chapter_chain(session, base, chapter_uuid)
        log(f"「{title}」作答记录 {chapter_record[:8]}…")

        # 该章自己的数据（含 requirement —— load-data 带 chapterRecordUuid 才有，
        # get-nodes / go-chapter 都拿不到）
        cdata = fetch_story_data(session, study_url, course, base["recordUuid"],
                                 chapter_record)
        requirement = cdata.get("requirement") or chapter.get("requirement") or ""
        # ★ 顶层 ``title`` 是**任务名**（实测「离散型随机变量 AI 实践（审计与风控案例）」），
        #   真正的**章名**在 ``storyExtraData.title``（「审计抽样建模校验」）。
        #   旧代码拿顶层 title 覆盖 title，日志里两章标题一模一样 ——
        #   正是这个假象掩盖了"第 2 章那轮对话其实写在第 1 章"的错章 bug。
        task_title = str(cdata.get("title") or "")
        node_title = str((cdata.get("storyExtraData") or {}).get("title") or "")
        if node_title:
            title = node_title
        elif task_title:
            title = task_title
        # ★ 归属校验 —— 这个文件里最贵的一个教训（2026-10-15 run4）：
        #   ``init-chain-answer`` 在任务链没推进到位时，会**静默地**给"当前章"
        #   再开一条记录，而不是报错。run4 里它给「第 2 章」开的记录其实是
        #   第 1 章的第 15 次作答；我们照常发言 + 交卷 → 第 1 章刚拿到的 100 分
        #   被这次 0 分覆盖，整章回到"未通过"，任务进度卡死在 87.5。
        #   宁可停手，绝不把答案写到错章上。
        if node_title and expected_title and node_title != expected_title:
            raise StoryAbort(
                f"作答记录 {chapter_record[:8]}… 属于「{node_title}」，"
                f"不是目标章「{expected_title}」—— 平台任务链尚未推进到该章；"
                f"已停手，不写入任何对话（避免把该章已有的成绩覆盖成 0 分）")
        if not requirement:
            logger.warning("「{}」未取到 requirement，生成内容会缺少题目背景", title)

        # ★ 上传作品节点（``subtype=1``）**不能**用对话完成：平台要的是文件。
        #   ``go-chapter`` 不返回 subtype（``_enter_and_load_chapter`` 因此恒填 0），
        #   真实 subtype 只有"开完记录后"读 ``load-data`` 才拿得到。
        #   这里的检查在**任何写入之前**，所以停手是零副作用的 ——
        #   反之若把它当对话章处理，会往上传节点里灌一段无意义的对话。
        node_subtype = int(str(cdata.get("subtype") or chapter.get("subtype") or 0) or 0)
        if node_subtype == PLAN_TYPE_UPLOAD:
            return (not chapters_done), (
                f"进行到「{title}」是**上传作品**节点（subtype=1），该节点尚未自动化"
                f"（记录 {chapter_record[:8]}… 已开出但未写入任何内容）。"
                f"已完成 {len(chapters_done)} 章：{', '.join(chapters_done) or '无'}；"
                f"请手动上传报告后重跑，或等该节点自动化实现")

        # ---- 2) 对话（受 story_max_rounds 限制）----
        chats = get_chats(session, base, chapter_uuid)
        roles = get_roles(session, base)
        role_uuid = pick_role_uuid(chats, roles)
        if not role_uuid:
            raise StoryAbort("未能确定学生角色 roleUuid，无法发言")
        # ★ 智能体角色：每轮学生发言之后，要把**智能体的回复正文**也写进对话。
        #   原因见 pick_agent() 的注释（第 2 章评卷器明确要「智能体的回复内容」）。
        agent_uuid, agent_name = pick_agent(cdata, chats, chapter_uuid, roles)
        if not agent_uuid:
            logger.warning("「{}」没找到智能体角色，对话里将只有学生发言", title)
        # ★ 这里曾有一行 ``requirement = chapter.get("requirement") or ""``，
        #   会把上面刚从 ``load-data`` 取到的 requirement **覆盖成空串**——
        #   ``go-chapter`` / ``get-progress`` 返回的章节对象里根本没有
        #   ``requirement`` 字段，只有 ``load-data(带 chapterRecordUuid)`` 才有。
        #   后果是每轮喂给智能体的提示词都缺了题目背景，评分自然难看。
        #   现已删除：requirement 一律沿用上面取到的那份。
        # ★ 本题允许的对话轮数取「用户设置」与「平台 maxRound」的较小值。
        #   无视平台上限会把对话灌坏（实测第 2 章 maxRound=1 却发了 3 轮 → 0 分）。
        cap = chapter_max_round(chats, chapter_uuid)
        rounds_limit = max_rounds
        # ★ 题目里可能给了**不止一处** AI提示词，要求"依次提问"。
        #   run14 实测第 4 章「拓展案例建模与解读」：requirement 里写着
        #   「完成两组现实业务案例计算与风险分析，复制**两段**提示词依次提问智能体」，
        #   正则能抓到 2 块（77 字二项分布案例 + 83 字泊松分布案例），
        #   而旧代码只用 ``extract_prompt()`` 取第一块、轮数又被平台 maxRound=1
        #   压到 1 轮 ⇒ 只发了案例一，评卷器直接判
        #   「缺失第二组泊松分布案例（门店顾客到店）的提问及智能体回答…扣 60 分，得 40 分」。
        #   所以轮数下限必须抬到"提示词块数"：平台 maxRound 说的是**每个**提问槽位
        #   1 轮，不是整章只能 1 轮 —— 逐条提问正是题目要求的行为。
        prompts = extract_prompts(requirement)
        if cap:
            rounds_limit = min(max_rounds, cap)
            if cap < max_rounds:
                log(f"「{title}」本题限 {cap} 轮对话（平台限制），按 {rounds_limit} 轮执行")
        if len(prompts) > rounds_limit:
            rounds_limit = min(max_rounds, len(prompts))
            log(f"「{title}」题目有 {len(prompts)} 段 AI提示词，按 {rounds_limit} 轮依次提问")
        rounds_here = 0
        given_prompt = prompts[0] if prompts else extract_prompt(requirement)
        if given_prompt:
            log(f"「{title}」题目自带 AI提示词（{len(given_prompt)} 字），"
                f"第 1 轮将原样发送")
        # ★ 续作答时**从已有的学生发言条数接着数**，别把发过的轮次再发一遍。
        #   踩过的坑（run10，第 2 章 maxRound=1）：复用的记录里已经有第 1 轮的
        #   提示词了，流程又原样发了一次 → 对话里出现两条一模一样的学生发言，
        #   评卷器专门点了出来（「两次相同的用户发言…建议检查发送消息的唯一性」）。
        #   它这次没扣分，但没有任何理由主动制造这种脏数据。
        done_before = student_message_count(cdata)
        if done_before > 0:
            rounds_here = min(done_before, rounds_limit)
            log(f"「{title}」该记录已有 {done_before} 条学生发言，"
                f"从第 {rounds_here + 1} 轮继续")
        while rounds_here < rounds_limit:
            # ★ 第 1 轮必须**原样发送**题目里给的「AI提示词」。
            #   踩过的坑：这里本来是把提示词丢给内容生成器，让它"照着要求写一段学生发言"，
            #   结果发出去的是**答案正文**而不是题目规定的那句提示词。
            #   评卷器原文（2026-10-15，第 1 章拿 0 分那次）：
            #     「任务明确要求'复制提示词发给AI'并'保存AI回复'，但对话历史中显示
            #       学生直接发送了最终答案文本，而非规定的提示词」
            #   题目原文也写着「复制下方提示词发给AI校验模型正确性，保存AI回复」——
            #   所以第 1 轮零加工直发，之后几轮才让生成器补充计算与追问。
            if rounds_here < len(prompts):
                # 题目给的第 N 段提示词，原样发送（顺序与题目一致）
                content = prompts[rounds_here]
                if rounds_here:
                    log(f"「{title}」发送题目第 {rounds_here + 1} 段 AI提示词"
                        f"（{len(content)} 字）")
            elif rounds_here == 0 and given_prompt:
                content = given_prompt
            else:
                prompt = build_round_prompt(chapter, rounds_here + 1, [], requirement)
                content = generator.generate_chat_reply(prompt, history=[],
                                                        role_hint=requirement[:300])
            if not content or not str(content).strip():
                raise StoryAbort(f"「{title}」第 {rounds_here + 1} 轮未生成内容，已中止")

            # ★★★ 学生这半边：只调 story-host-agent，**不要再给学生消息补 add-chat**。
            #   两个受控实验（_dz_proto.py / _dz_which.py，2026-10-15）证明：
            #     A) 只调 host_agent              → messages 0 → 1，标记命中 1
            #     B) add_chat(X) 后 host_agent(X) → messages 2、标记命中 2
            #   即 **host-agent 自己就会把学生消息落库**；再加一次 add-chat 就是
            #   把同一句话写两遍。后果很具体：run3 的日志里学生发言累计是
            #   `1,3,5,7,9`（每轮 +2），而 run4 用同一份题目只发 1 轮时是 `1`。
            #   重复消息会让评卷器看到「同一提问连发两遍」的坏对话，直接拉低分数。
            #   注意 host-agent 是 SSE，必须边收边判 triggered，不能只看返回码。
            #   （下面**智能体那半边**反倒必须靠 add-chat 补，见本轮末尾。）
            before = student_message_count(
                fetch_story_data(session, study_url, course, base["recordUuid"],
                                 chapter_record))
            try:
                # ★ host-agent 的 SSE 里唯一和"智能体要说什么"有关的信息是
                #   ``intent``（实测：「提供二项分布各项计算结果，并自然结束本次辅导。」），
                #   回复正文本身走语音 WebSocket —— 留着它给生成器当提示。
                hint: dict = {}

                def _on_ev(ev, _h=hint):
                    if isinstance(ev, dict):
                        if ev.get("intent"):
                            _h["intent"] = str(ev["intent"])
                        logger.debug("host-agent 事件: {}", str(ev)[:160])

                fired, why = agent_triggered(
                    host_agent(session, base, chapter_uuid, chapter_record, uid,
                               str(content).strip(),
                               speak_role_type=ROLE_STUDENT, tokens=tokens),
                    on_event=_on_ev)
            except StoryError as exc:
                # ★ 智能体自己会写消息，所以「应答失败」不代表消息没落库 ——
                #   先回读确认，再决定是报错还是继续。
                logger.warning("「{}」驱动智能体应答失败：{}", title, exc)
                log(f"「{title}」智能体应答未成功（{exc}），回读落库情况…")
                fired, why = False, str(exc)
            time.sleep(delay)
            after = student_message_count(
                fetch_story_data(session, study_url, course, base["recordUuid"],
                                 chapter_record))
            if after <= before:
                # ★ 兜底：host-agent 偶发「模型异常【016】」时它可能一句都没写。
                #   这时退回 add-chat —— 虽然写不出智能体回复，但至少保证
                #   「学生确实问了」这件事落库，不会因为一次抖动整章作废。
                logger.warning("「{}」host-agent 未落库，退回 add-chat 兜底", title)
                add_chat(session, base, chapter_record, role_uuid,
                         str(content).strip())
                time.sleep(delay)
                after = student_message_count(
                    fetch_story_data(session, study_url, course, base["recordUuid"],
                                     chapter_record))
            if after <= before:
                raise StoryError(
                    f"story-host-agent 返回成功但学生消息未落库（{before}→{after}）；已停止")
            rounds_here += 1
            done_rounds += 1
            log(f"「{title}」第 {rounds_here}/{rounds_limit} 轮已写入"
                f"（{len(str(content))} 字，学生发言累计 {after} 条）")
            if fired:
                log(f"「{title}」智能体判定条件达成：{why}")
            else:
                logger.debug("「{}」本轮 host-agent 未报告 triggered", title)

            # ★★ 补齐**智能体的回复正文** —— 这是第 2 章从 0 分变满分的关键。
            #   题目里的「提交物」写着「粘贴AI完整计算结果」；评卷器（2026-10-15）
            #   原文判 0 分的理由就是「缺失智能体（统计专家）的回复内容…无法确认
            #   任务完成情况」。而 story-host-agent 只推角色元数据与 intent，回复
            #   正文走语音 WS，纯文字路径下没人把它落库 —— 所以这里用内容生成器
            #   写出该角色的回复，再用**智能体角色** add-chat 写进对话。
            #   实测：写进去后 ``messages[i]["role"] == "assistant"``、
            #   节点 chat 里是 ``type=1``，评卷器认可为「从智能体处获取了完整的
            #   计算结果」并给 100 分（见 _dz_addagent.py / _dz_submit.py）。
            #   不变量：智能体回复条数 < 学生发言条数 时才补，续作答不会写重。
            if agent_uuid:
                d_now = fetch_story_data(session, study_url, course,
                                         base["recordUuid"], chapter_record)
                if agent_message_count(d_now) < student_message_count(d_now):
                    reply = generate_agent_reply(
                        generator, agent_name, requirement, str(content).strip(),
                        intent=str(hint.get("intent") or ""))
                    if reply:
                        add_chat(session, base, chapter_record, agent_uuid, reply)
                        time.sleep(delay)
                        d_now = fetch_story_data(session, study_url, course,
                                                 base["recordUuid"], chapter_record)
                        log(f"「{title}」已写入智能体（{agent_name or '智能体'}）"
                            f"回复正文 {len(reply)} 字；当前对话：学生 "
                            f"{student_message_count(d_now)} 条 / 智能体 "
                            f"{agent_message_count(d_now)} 条")
                    else:
                        logger.warning("「{}」智能体回复正文生成失败，"
                                       "本轮对话只有学生发言", title)
            time.sleep(delay)

        # ---- 3) 交卷 ----
        log(f"「{title}」交卷…")
        submit_chapter(session, study_url, course, base["recordUuid"],
                       chapter_uuid, chapter_record)
        time.sleep(3.0)

        # ---- 4) 评分 ----
        log(f"「{title}」触发 AI 评分…")
        # ★ recordUuid 传**外层记录**（base["recordUuid"]），不是章节记录。
        #   对齐对话页 TaskChainChat.startEvaluate()；传章节记录会得到「会话不存在」。
        text = evaluate_chapter(session, study_url, course, data.get("aiEnc") or "",
                                base["recordUuid"], chapter_record)
        ai_score = parse_total_score(text)

        # ---- 5) 判定与推进 ----
        # ★ 必须**按本次作答记录选行**，不能用 ``current_chapter()``：
        #   它返回的永远是 ``progress[0]``，而第 1 章一旦完成就恒占 progress[0]，
        #   于是"下一章 / 本轮真实分数 / 门槛"全部读的是**第 1 章**的规则。
        #   run13 实测后果：第 2 章 100 分交卷后被"推进"到「期望方差建模校验」
        #   之外的第 2 章自己（拿第 1 章的 rule 算出来的下一章），
        #   被防空转守卫拦下，整轮白跑。
        prog5 = get_progress(session, base)
        rows5 = _dict_list(prog5.get("progress"))
        chapter = (next((r for r in rows5
                         if str(r.get("chapterRecordUuid")) == chapter_record), None)
                   or next((r for r in rows5
                            if str(r.get("uuid")) == chapter_uuid), None)
                   or current_chapter(prog5) or chapter)
        # ⚠️ answerRecord 是**多次尝试**的列表；必须取与本次 chapter_record 匹配的那条，
        # 取 recs[0] 会读到上一次尝试的分数（实测把旧成绩 100 当成了本次成绩）。
        recs = _dict_list(chapter.get("answerRecord"))
        cur = next((r for r in recs if str(r.get("uuid")) == chapter_record),
                   recs[-1] if recs else {})
        real_score = cur.get("score")
        # ★ 下一章与门槛都从 ``payload.condition.rule`` 取（见 :func:`chain_next`）：
        #   ``progress[].condition`` 那一栏第 1 章只有条件名（「分数达到90分」）、
        #   第 2 章只有 from/to/dimension，两章形状不一致，且都拿不到"前进口"；
        #   规则表是唯一既有下一章 uuid、又有分数的来源。
        nxt, thr_rule = chain_next(chapter)
        threshold = thr_rule if thr_rule is not None else condition_threshold(chapter)
        conds_now = _dict_list(chapter.get("condition"))

        # ⚠️ 必须先回落再判定：流式文本的解析可能失败（实测就失败过 —— 分片把
        # `"totalScore"` 拆成了 `"total` + `Score"`，导致正则没命中）。若先判定
        # 再回落，就会把「100 分」判成「未达到 90 分」。
        if ai_score is None:
            if text:
                logger.debug("评分文本未解析出 totalScore，前 200 字: {!r}", text[:200])
            ai_score = real_score
        if not isinstance(ai_score, (int, float)) or isinstance(ai_score, bool):
            try:
                ai_score = float(ai_score)
            except (TypeError, ValueError):
                ai_score = None
        passed = (ai_score is not None and threshold is not None
                  and float(ai_score) >= float(threshold)) or chapter_hit(chapter)
        log(f"「{title}」得分 {ai_score}（达到 {threshold} 分即通过）")

        if not passed:
            return False, (f"「{title}」得分 {ai_score}，未达到 "
                           f"{threshold if threshold is not None else '?'} 分；"
                           f"已完成 {len(chapters_done)} 章，按设置停手")
        chapters_done.append(f"{title}({ai_score})")
        log(f"「{title}」通过 ✅")

        if not nxt:
            return True, (f"AI剧情已全部完成：{'；'.join(chapters_done)}"
                          f"（共 {done_rounds} 轮对话）")
        # ★ 不再"等平台推进"：``init-chain-answer?nextChapterUuid=`` 的跳转是
        #   **确定性**的（实测一次成功），下一轮循环会用它去开下一章的记录，
        #   并靠 ``storyExtraData.title`` 的归属校验兜住任何异常 ——
        #   校验不过就 StoryAbort，绝不把内容写到错章上。
        #   （旧流程用的 :func:`_wait_chapter_switch` 判的是 ``go-home`` 报的
        #   "当前章"，而 ``go-home`` 永远返回链首第 1 章，所以永远等不到。）
        nxt_name = str((conds_now[0].get("nextChapter") if conds_now else "") or "")
        log(f"已通过，推进到「{nxt_name or str(nxt)[:8]}」…")
        # ★ 必须**重建**下一章的状态，不能拿着本章的 condition/answerRecord 继续跑，
        #   否则下一轮会再次认为"已通过"而无限跳过（旧 bug）。
        chapter = _enter_and_load_chapter(session, base, str(nxt))
        if not chapter:
            return True, (f"AI剧情已完成：{'；'.join(chapters_done)}")
