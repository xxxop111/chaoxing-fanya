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

⚠️ 纯文字路径的**已知不足**：学生的话不会进入 WS（WS 是语音驱动的），
智能体只会回应开场白，于是没有"AI 回复正文"可落库；而题目要求
「保存AI回复 / 提交物含 AI 对话文本」，因此这样的作答实测得 0 分。
要补全需在 WS 侧把学生文本合成音频帧（``{"type":"audio", audio:<base64>}``）送进去。

其它实测要点：``realtimeDialogEnc`` 必须用 JS 里硬编码的 ``test_enc_2026``
（``load-data`` 返回的真实值会被判「enc 鉴权错误」）；``config`` 消息的
``speaker``/``botName``/``systemRole``/``speakingStyle`` 四项必填。

⚠️ 交卷不可逆：本模块会发言、交卷、评分，但调用方必须自行确认
（部分课程的作答记录 ``enableReset=false``，提交后无法重置）。

结构（实测）
------------
剧情是一串**章节**，每章就是一个节点，靠 ``condition[].hit`` 判完成、
``nextChapterUuid`` 串到下一章。每章要么是「情景对话」(``subtype=0``)，
要么是「上传作品」(``subtype=1``)。

```
get-progress   → 当前章：chapterRecordUuid / subtype / condition[].hit / nextChapterUuid
go-chapter     → 进入指定章
get-chats      → 该章的对话结构（condition 文案 / maxRound / 历史）
add-chat       → 发一轮（chapterRecordUuid + roleUuid + content）
```

安全边界
--------
* ``add-chat`` 会**写入真实对话内容且不可撤销**，因此每章的轮数由
  ``story_max_rounds`` 硬性封顶（默认 3 —— 用户明确要求：先由 AI 试 3 轮，
  达不到就停手交给用户，把剩下的轮次留给他自己做）。
* 任何异常、AI 生成失败、条件不满足，都**只停止并报告**，不会提交或伪造数据。
* 上传节点（``subtype=1``）尚未实现，遇到时明确返回"需人工"。
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


def get_progress(session: requests.Session, base: dict) -> dict:
    """当前章的进度（含 ``condition[].hit`` 与 ``nextChapterUuid``）。"""
    return _get(session, "get-progress", base)


def enter_chapter(session: requests.Session, base: dict, chapter_uuid: str) -> dict:
    return _get(session, "go-chapter", {**base, "chapterUuid": chapter_uuid})


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
    chapters = progress.get("progress") or []
    return chapters[0] if chapters else None


def chapter_hit(chapter: dict) -> bool:
    """``condition[].hit`` 全为真才算达标（无 condition 视为未达标）。"""
    conds = chapter.get("condition") or []
    if not conds:
        return False
    return all(bool(c.get("hit")) for c in conds)


def chapter_condition_text(chapter: dict) -> str:
    conds = chapter.get("condition") or []
    return "；".join(str(c.get("name") or "") for c in conds if c.get("name"))


# ---------------------------------------------------------------- 内容
_PROMPT_RE = re.compile(r"AI\s*提示词[：:]\s*(.+?)(?:\n\s*\n|提交物|\Z)", re.S)


def extract_prompt(requirement: str) -> str:
    """题目常写着「复制下方提示词发给 AI」—— 第一轮就该发这段原文。

    从 ``requirement`` 里抽出 ``AI提示词：…`` 之后的内容；抽不到返回空串。
    """
    m = _PROMPT_RE.search(requirement or "")
    return (m.group(1).strip() if m else "")


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


def init_chapter_chain(session: requests.Session, base: dict,
                       chapter_uuid: str, fallback_record: str = "") -> str:
    """为新章节开链，返回该章的 ``chapterRecordUuid``。

    ★ 这是推进到下一章的关键一步：`go-chapter` 只是"进入"，**不会**给出该章的
    作答记录；必须再调 ``init-chain-answer``（**必须带 chapterUuid**）才会创建
    一条新的章节记录并返回它的 UUID。

    实测：``{"data": "98224ca7-…", "status": true}``；不带 ``chapterUuid`` 会得到
    「作答信息为空」。

    幂等处理：服务端在已有未交卷记录时会拒绝（「存在作答中的记录，不允许重新开始
    新的作答」）。此时若调用方给出了 ``fallback_record``（当前进行中的记录），
    就直接复用它继续作答 —— 否则整条流程会因为一次重跑而彻底卡死。
    """
    try:
        data = _get(session, "init-chain-answer", {**base, "chapterUuid": chapter_uuid},
                    timeout=60.0)
    except StoryError as exc:
        msg = str(exc)
        if fallback_record and ("作答中" in msg or "不允许重新开始" in msg):
            logger.info("该章已有作答中的记录，复用它继续：{}", fallback_record[:8])
            return str(fallback_record)
        raise
    record = data if isinstance(data, str) else (data or {}).get("recordUuid")
    if not record:
        if fallback_record:
            return str(fallback_record)
        raise StoryError(f"init-chain-answer 未返回章节记录: {str(data)[:160]}")
    return str(record)


def condition_threshold(chapter: dict) -> Optional[float]:
    """从条件名里解析分数阈值，如「分数达到90分」→ 90.0。"""
    for text in ([chapter_condition_text(chapter)]
                 + [str(c.get("name") or "") for c in (chapter.get("condition") or [])]):
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
        info = enter_chapter(session, base, chapter_uuid) or {}
    except StoryError as exc:
        logger.warning("go-chapter({}) 失败：{}", chapter_uuid[:8], exc)
    return {
        "uuid": chapter_uuid,
        "title": str(info.get("title") or ""),
        "content": str(info.get("content") or ""),
        "condition": info.get("condition") or [],
        # subtype 不在 go-chapter 的返回里；取不到时按对话节点处理，
        # 真遇到上传节点会在后续阶段暴露出来（那时会明确停手并说明）。
        "subtype": int(info.get("subtype") or 0),
        "chapterRecordUuid": "",
        "answerRecord": [],
        "status": 0,
    }


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
        chapter = {**chapter, "uuid": forced}

    while True:
        title = chapter.get("title") or ""
        subtype = int(chapter.get("subtype") or 0)
        chapter_uuid = str(chapter.get("uuid") or "")

        # 防死循环：服务端一次只报"当前章"，推进不顺时会原地打转（实测 get-progress
        # 存在滞后），必须自己兜住，否则会无限重复同一章。
        if chapter_uuid in visited:
            return (not chapters_done), (
                f"章节推进回到已处理过的章（{chapter_uuid[:8]}…），已停止以免空转。"
                f"已完成：{', '.join(chapters_done) or '无'}")
        visited.add(chapter_uuid)

        if subtype == PLAN_TYPE_UPLOAD:
            return (not chapters_done), (
                f"进行到「{title}」是**上传作品**节点，该节点尚未自动化。"
                f"前面已完成 {len(chapters_done)} 章：{', '.join(chapters_done) or '无'}；"
                f"请手动上传报告")

        # ---- 0) 已通过的章直接跳过 ----
        # ⚠️ 这里曾用 ``recs0[0]``（= **最早**一次尝试）判分，会读错记录。
        #    改用全部尝试里的**最高分**：只要有过一次达标就算通过，
        #    避免把已经拿到的分数白重做一遍。
        #
        # ★ 例外：调用方用 ``story_chapter_uuid`` **显式指定**了本章 ——
        #   那就是要重做，绝不能因为"历史上通过过"而跳过。
        #   实测踩过的坑：平台的"当前作答"若是一条失败记录，本章
        #   ``condition.hit`` 会是 false，任务链就卡住不让进下一章；
        #   此时唯一的解法是重做本章拿到通过分。
        conds = chapter.get("condition") or []
        nxt0 = conds[0].get("nextChapterUuid") if conds else None
        recs0 = chapter.get("answerRecord") or []
        scores0 = [r.get("score") for r in recs0
                   if isinstance(r.get("score"), (int, float))
                   and not isinstance(r.get("score"), bool)]
        best0 = max(scores0) if scores0 else None
        thr0 = condition_threshold(chapter)
        explicit = bool(forced) and chapter_uuid == forced
        if explicit:
            log(f"「{title}」被显式指定重做，忽略'此前已通过'的跳过逻辑")
        elif (int(chapter.get("status") or 0) == 2 and best0 is not None
                and thr0 is not None and float(best0) >= thr0 and nxt0):
            log(f"「{title}」此前已通过（最高 {best0} 分），跳过并进入下一章")
            chapters_done.append(f"{title}({best0})")
            chapter = _enter_and_load_chapter(session, base, str(nxt0))
            continue

        # ---- 1) 进入该章并为它开链（拿到本章的作答记录）----
        log(f"进入章节「{title or chapter_uuid[:8]}」")
        info = enter_chapter(session, base, chapter_uuid)
        if info.get("title"):
            title = str(info["title"])
            chapter = {**chapter, "title": title}
        chapter_record = init_chapter_chain(session, base, chapter_uuid,
                                            fallback_record=str(chapter.get("chapterRecordUuid") or ""))
        log(f"「{title}」作答记录 {chapter_record[:8]}…")

        # 该章自己的数据（含 requirement —— load-data 带 chapterRecordUuid 才有，
        # get-nodes / go-chapter 都拿不到）
        cdata = fetch_story_data(session, study_url, course, base["recordUuid"],
                                 chapter_record)
        requirement = cdata.get("requirement") or chapter.get("requirement") or ""
        if cdata.get("title"):
            title = str(cdata["title"])
        if not requirement:
            logger.warning("「{}」未取到 requirement，生成内容会缺少题目背景", title)

        # ---- 2) 对话（受 story_max_rounds 限制）----
        chats = get_chats(session, base, chapter_uuid)
        roles = get_roles(session, base)
        role_uuid = pick_role_uuid(chats, roles)
        if not role_uuid:
            raise StoryAbort("未能确定学生角色 roleUuid，无法发言")
        requirement = chapter.get("requirement") or ""
        # ★ 本题允许的对话轮数取「用户设置」与「平台 maxRound」的较小值。
        #   无视平台上限会把对话灌坏（实测第 2 章 maxRound=1 却发了 3 轮 → 0 分）。
        cap = chapter_max_round(chats, chapter_uuid)
        rounds_limit = max_rounds
        if cap:
            rounds_limit = min(max_rounds, cap)
            if cap < max_rounds:
                log(f"「{title}」本题限 {cap} 轮对话（平台限制），按 {rounds_limit} 轮执行")
        rounds_here = 0
        while rounds_here < rounds_limit:
            prompt = build_round_prompt(chapter, rounds_here + 1, [], requirement)
            content = generator.generate_chat_reply(prompt, history=[],
                                                    role_hint=requirement[:300])
            if not content or not str(content).strip():
                raise StoryAbort(f"「{title}」第 {rounds_here + 1} 轮未生成内容，已中止")
            before = student_message_count(
                fetch_story_data(session, study_url, course, base["recordUuid"],
                                 chapter_record))
            add_chat(session, base, chapter_record, role_uuid, str(content).strip())
            time.sleep(delay)
            after = student_message_count(
                fetch_story_data(session, study_url, course, base["recordUuid"],
                                 chapter_record))
            if after <= before:
                raise StoryError(
                    f"add-chat 返回成功但学生消息未落库（{before}→{after}）；已停止")
            rounds_here += 1
            done_rounds += 1
            log(f"「{title}」第 {rounds_here}/{rounds_limit} 轮已写入"
                f"（{len(str(content))} 字，学生发言累计 {after} 条）")

            # ★★ 关键：add-chat 只是把消息写进记录，**不会让智能体应答**。
            #    真正的对话通道是 story-host-agent（SSE）：它驱动智能体回复，
            #    并在条件达成时推出 {"triggered": true}。
            #    实测漏掉这一步的后果：章节里只有学生自说自话，
            #    而题目要求「保存AI回复 / 提交物含 AI 对话文本」→ 得分 0。
            try:
                fired, why = agent_triggered(
                    host_agent(session, base, chapter_uuid, chapter_record, uid,
                               str(content).strip(),
                               speak_role_type=ROLE_STUDENT, tokens=tokens),
                    on_event=lambda ev: logger.debug("host-agent 事件: {}",
                                                     str(ev)[:160]))
                if fired:
                    log(f"「{title}」智能体判定条件达成：{why}")
                else:
                    logger.debug("「{}」本轮 host-agent 未报告 triggered", title)
            except StoryError as exc:
                # 智能体没应答不该让整章失败 —— 记录警告，交卷评分照走
                logger.warning("「{}」驱动智能体应答失败：{}", title, exc)
                log(f"「{title}」智能体应答未成功（{exc}），继续后续步骤")
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
        chapter = current_chapter(get_progress(session, base)) or chapter
        # ⚠️ answerRecord 是**多次尝试**的列表；必须取与本次 chapter_record 匹配的那条，
        # 取 recs[0] 会读到上一次尝试的分数（实测把旧成绩 100 当成了本次成绩）。
        recs = chapter.get("answerRecord") or []
        cur = next((r for r in recs if str(r.get("uuid")) == chapter_record),
                   recs[-1] if recs else {})
        real_score = cur.get("score")
        threshold = condition_threshold(chapter)
        nxt = ((chapter.get("condition") or [{}])[0]).get("nextChapterUuid")

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
        # ★ 必须**重建**下一章的状态，不能拿着本章的 condition/answerRecord 继续跑，
        #   否则下一轮会再次认为"已通过"而无限跳过（旧 bug）。
        chapter = _enter_and_load_chapter(session, base, str(nxt))
        if not chapter:
            return True, (f"AI剧情已完成：{'；'.join(chapters_done)}")
