# -*- coding: utf-8 -*-
"""泛雅 AI实践「情景对话」(``data.type == 0``) 自动完成。

⚠️ **本模块未经真机验证**：本账号 22 个 AI实践 里没有任何**未过期**的
``type=0`` 任务点（唯一名字带「情景对话」的那个处于关闭的学习窗口），因此
无法真机跑通。协议来自 `_findings_ai.md` §4 的前端 JS 静态分析。

流程（前两步为 SSE 流）：

```
1) GET  /mooc2-ans/ai-evaluate/v2/answer/load-data
        → type=0, scene, messages(开场白), libraryUuid, libraryAiEnc, aiEnc
2) 若 scene 为空：
   POST /ai-ans/ai-evaluate/v2/scene-chat/generate/scene-by-library
        ?courseId&cpi&libraryUuid&libraryAiEnc&language=      body: isUpdateScene=true   (SSE)
3) POST /mooc2-ans/ai-evaluate/v2/answer/init?courseid&clazzid&cpi&publishRelationUuid
        body: scene=<场景文本>                                  → recordUuid / answerUuid
4) 每轮：POST /ai-ans/ai-evaluate/v2/scene-chat
        ?courseId&clazzId&cpi&aiEnc&recordUuid&isPreview=0&isUseJson=true&language=
        body: userMessage=<学生发言>                            (SSE，回复=拼接 content)
5) POST /mooc2-ans/ai-evaluate/v2/answer/submit?…&recordUuid=
6) POST /ai-ans/ai-evaluate/v2/evaluate/stream?…&recordUuid&aiEnc&isPreview=0  (SSE)
7) GET  /mooc2-ans/ai-evaluate/v2/answer/evaluate-result?…&recordUuid= 直到 isComplete
```

**关于 `/ai-ans`**：它**不是**独立鉴权后端。带真实 `aiEnc` 时返回
``200 + text/event-stream``，业务错误以 SSE ``event:error`` 返回；之前的
HTTP 401 来自缺少 `aiEnc` 的请求（见 `_findings_ladder.md` §1）。

安全约束：未配置 AI 接口、拿不到 `recordUuid`、AI 未产出回复、
SSE 报错 —— 任一情况都**中止且不提交**。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

import requests
from loguru import logger

from api import fanya_ladder as ld

MOOC2 = ld.MOOC2
AI_ANS = ld.AI_ANS
from api.fanya_ai import MODE_STORY


class ChatError(Exception):
    """情景对话流程失败。"""


class ChatAbort(ChatError):
    """出于安全考虑主动中止（绝不提交空对话）。"""


def _ai_ans_post(session: requests.Session, path: str, params: dict, body: dict,
                 timeout: float = 120.0) -> dict:
    """向 /ai-ans 发一个 SSE 流式 POST 并消费它。"""
    url = f"{AI_ANS}{path}"
    try:
        resp = session.post(url, params=params, data=body, stream=True, timeout=timeout,
                            headers={"Referer": f"{MOOC2}/ai-evaluate/v2/answer",
                                     "Content-Type": "application/x-www-form-urlencoded"})
    except requests.RequestException as exc:
        raise ChatError(f"请求 {path} 失败: {exc}") from exc
    if resp.status_code != 200:
        raise ChatError(f"请求 {path} 返回 HTTP {resp.status_code}（/ai-ans 需有效 aiEnc）")
    return ld.consume_sse(resp, max_seconds=timeout)


def generate_scene(session: requests.Session, ids: dict, data: dict,
                   language: str = "Chinese") -> str:
    """第 2 步：让服务端生成场景文本（scene 为空时）。"""
    result = _ai_ans_post(
        session,
        "/ai-ans/ai-evaluate/v2/scene-chat/generate/scene-by-library",
        params={"courseId": ids["courseid"], "cpi": ids["cpi"],
                "libraryUuid": data.get("libraryUuid") or "",
                "libraryAiEnc": data.get("libraryAiEnc") or "",
                "language": language},
        body={"isUpdateScene": "true"},
    )
    text = (result.get("text") or "").strip()
    if not text:
        raise ChatAbort("服务端未生成场景文本，已中止")
    return text


def send_turn(session: requests.Session, ids: dict, data: dict, record_uuid: str,
              message: str, language: str = "Chinese") -> str:
    """第 4 步：发一句学生发言，返回 AI 回复文本。"""
    result = _ai_ans_post(
        session,
        "/ai-ans/ai-evaluate/v2/scene-chat",
        params={"courseId": ids["courseid"], "clazzId": ids["clazzid"],
                "cpi": ids["cpi"], "aiEnc": data.get("aiEnc") or "",
                "recordUuid": record_uuid, "isPreview": 0, "isUseJson": "true",
                "language": language},
        body={"userMessage": message},
    )
    if result.get("status") and str(result["status"]).lower() not in ("success", "ok", ""):
        raise ChatError(f"对话轮次返回异常状态: {result['status']}")
    reply = (result.get("text") or "").strip()
    if not reply:
        raise ChatAbort("AI 未返回回复，已中止（不提交空对话）")
    return reply


def init_scene_record(session: requests.Session, ids: dict, scene: str,
                      data: Optional[dict] = None) -> dict:
    """第 3 步：用场景文本建卷。优先复用已有的未完成记录。

    ``load-data`` 会返回未完成记录的 ``recordUuid``；无条件新建会被服务端拒绝
    （``已存在未完成的作答记录``）。
    """
    existing = ((data or {}).get("recordUuid") or "").strip()
    if existing:
        logger.info("复用已有的未完成作答记录：{}", existing)
        return {"recordUuid": existing, "answerUuid": (data or {}).get("answerUuid") or ""}

    r = session.post(
        f"{MOOC2}/mooc2-ans/ai-evaluate/v2/answer/init",
        params={"courseid": ids["courseid"], "clazzid": ids["clazzid"],
                "cpi": ids["cpi"], "publishRelationUuid": ids["publishRelationUuid"]},
        data={"scene": scene}, timeout=60.0)
    if r.status_code != 200:
        raise ChatError(f"answer/init 返回 HTTP {r.status_code}")
    body = r.json()
    payload = body.get("data") or {}
    if not payload.get("recordUuid"):
        msg = str(body.get("msg") or "")
        if "已存在" in msg:
            raise ChatAbort(f"服务端报告已存在未完成的作答记录，但 load-data 未给出其 "
                            f"recordUuid，请先手动进入一次该任务点：{msg}")
        raise ChatAbort(f"answer/init 未返回 recordUuid: {str(body)[:160]}")
    return payload


def complete_chat_task(session: requests.Session, course: dict, study_url: dict,
                       generator: Any, *, options: Optional[dict] = None,
                       on_log: Optional[Callable] = None,
                       story: bool = False) -> tuple[bool, str]:
    """完成一个「情景对话」AI实践任务点。

    默认进行 ``chat_turns`` 轮（默认 5 轮，对应「至少 5 轮问答」的常见要求）。

    ``story=True`` 时同时接受 **AI剧情（type=7）**：它的对话部分复用同一套
    ``scene-chat`` 接口，但没有 ``scene`` 字段（背景取自 ``requirement`` /
    ``storyExtraData``），且应当在最后上传一份报告 —— **上传节点本函数不处理**。
    """
    opts = options or {}
    log = on_log or (lambda m: logger.info(m))
    turns_wanted = int(opts.get("chat_turns") or 5)

    if generator is None or not getattr(generator, "available", False):
        raise ChatAbort("未配置 AI 接口（endpoint/key/model），无法进行情景对话")

    ids = ld._ids(study_url, course)
    if not ids.get("publishRelationUuid"):
        raise ChatAbort("未能从任务点解析出 publishRelationUuid")

    data = ld.load_data(session, ids)
    mode = data.get("type")
    allowed = (0, MODE_STORY) if story else (0,)
    if mode not in allowed:
        raise ChatAbort(f"该任务点不是情景对话（type={mode}）")
    is_story = mode == MODE_STORY

    library_uuid = data.get("libraryUuid") or ""
    library_ai_enc = data.get("libraryAiEnc") or ""
    ai_enc = data.get("aiEnc") or ""
    if not ai_enc:
        raise ChatAbort("load-data 未返回 aiEnc，无法调用 /ai-ans（会得到 401）")

    scene = (data.get("scene") or "").strip()
    if not scene:
        if is_story:
            # AI剧情没有 scene 字段；背景取自 requirement / storyExtraData。
            # 不能调 generate_scene —— 那是模式 0 的接口。
            story_data = data.get("storyExtraData") or {}
            scene = (data.get("requirement") or story_data.get("content") or "").strip()
            log(f"AI剧情背景取自 requirement（{len(scene)} 字）")
        else:
            log("场景为空，请求服务端生成场景…")
            scene = generate_scene(session, ids, data)
            log(f"场景已生成（{len(scene)} 字）")

    record = init_scene_record(session, ids, scene, data=data)
    record_uuid = record["recordUuid"]
    log(f"作答记录 recordUuid={record_uuid}")

    topic = ld.strip_html(data.get("content") or data.get("title") or "")[:400]
    history: list[dict] = []
    done = 0
    for turn in range(1, turns_wanted + 1):
        kind = "AI剧情" if is_story else "情景对话"
        prompt = (f"我们正在完成课程里的{kind}任务。任务背景：{topic}\n"
                  f"这是第 {turn} 轮（共 {turns_wanted} 轮）。"
                  "请继续推进对话：结合背景提出你的看法或追问，"
                  "要有自己的判断，不要重复上一轮内容。")
        message = generator.generate_chat_reply(prompt, history=history,
                                                role_hint=scene[:300])
        if not message:
            raise ChatAbort(f"第 {turn} 轮 AI 未生成学生发言，已中止（不提交）")
        reply = send_turn(session, ids, data, record_uuid, message)
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": reply})
        done += 1
        log(f"第 {turn} 轮完成（提问 {len(message)} 字 / 回复 {len(reply)} 字）")
        time.sleep(float(opts.get("chat_turn_delay") or 1.0))

    if done == 0:
        raise ChatAbort("没有完成任何一轮对话，已中止（不提交）")

    submit = ld.answer_submit(session, ids, record_uuid)
    log(f"已交卷：{str(submit)[:80]}")

    try:
        evaluated = ld.evaluate_stream(session, ids, record_uuid, ai_enc,
                                       max_seconds=float(opts.get("eval_seconds") or 300))
        log(f"评估流状态：{evaluated.get('status')}")
    except Exception as exc:  # noqa: BLE001 - 评估失败不回滚交卷
        logger.warning("评估流异常（交卷已发生）: {}", exc)

    result = ld.wait_result(session, ids, record_uuid, tries=15, delay=3.0)
    score = None
    if result:
        score = result.get("score") or result.get("totalScore")
    label = "AI剧情" if is_story else "情景对话"
    extra = "（⚠️ AI剧情的最终「上传报告」节点未自动化）" if is_story else ""
    return True, (f"已完成 {done} 轮{label}并交卷"
                  + (f"，得分 {score}" if score is not None else "，等待评分")
                  + extra)
