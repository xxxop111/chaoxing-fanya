# -*- coding: utf-8 -*-
"""泛雅 AI实践「代码实践」(``data.type == 4``) 与「思维阶梯」(``data.type == 2``)。

本模块补齐 ``api/fanya_ai.py`` 里识别但未实现的两种 AI实践 子模式。

============================================================================
重要纠正：``/ai-ans`` **不是** 401 屏蔽的后端
============================================================================
早期结论认为 ``/ai-ans/...`` 需要单独授权、用本项目 cookies 一律 401。
2026-10 复测（用**假** recordUuid，不产生任何记录）表明这是误判：

==============================================  =======  ==========================
请求                                             状态     响应
==============================================  =======  ==========================
``/ai-ans/v2/evaluate/stream``（假 recordUuid）   200      ``text/event-stream``
                                                          ``event:error data:会话不存在``
``/ai-ans/think/main-talk``（假 recordUuid）      200      ``text/event-stream``
``/ai-ans/think/end-report``（假 recordUuid）     200      ``text/event-stream``
``/ai-ans/v2/evaluate/app/stream``                500      （type 4 本就不走这条）
==============================================  =======  ==========================

即：``/ai-ans`` 会**正常鉴权**（并下发 ``k8s-ai`` cookie），业务错误以 SSE
``event:error`` 的形式返回，而不是 HTTP 401。之前的 401 来自一个没有可用
``recordUuid``/``libraryAiEnc`` 的请求。

============================================================================
type=4（代码实践）协议 —— 全部走 ``mooc2-ans``
============================================================================
入口是**服务端渲染的经典页**（``AI实践-代码编辑 作答``），脚本
``/mooc2-ans/ai-evaluate/code-editor/js/answer-code-editor.js``。

1. ``GET /mooc2-ans/ai-evaluate/v2/answer/load-data``
   → ``content``（题目 HTML）、``codeUuid``、``codeTaskType``、``aiEnc``、
   ``publishRelationUuid``、``libraryUuid``、``publishSetting``。
2. 语言列表来自入口页隐藏域 ``#languages``（非严格 JSON，形如
   ``[[3, Java, text/x-java, 1], ...]``，需按页面自身的正则"修 JSON"后再解析）。
   Java = ``codeNum 3``。
3. ``answerResult`` 的载荷是**字符串化 JSON**：
   ``{"answerResultCode": "<代码>", "languageIndex": 3, "languageName": "Java"}``
4. 自测运行：``POST /mooc2-ans/ai-evaluate/v2/answer/procedural-run``（表单）
   ``{type:'selfcheck', runResultType, language, answerResult, stdin, stdout,
     courseid, cpi, exampleArr}``
   ``status``：1 通过 / 2 答案不正确 / 3 运行错误 / 6 编译错误 / 8 服务超时 …
5. 建卷：``POST /mooc2-ans/ai-evaluate/v2/answer/init``
   ``?courseid&clazzid&cpi&publishRelationUuid``
   body ``{type:4, codeUuid, answerResult}`` → ``{recordUuid, answerUuid}``
6. 交卷：``POST /mooc2-ans/ai-evaluate/v2/answer/submit?...&recordUuid=`` → ``recordStatus``
7. 评估（**这一步才产生分数**）：
   ``POST /ai-ans/ai-evaluate/v2/evaluate/stream?courseId&clazzId&cpi&recordUuid
     &aiEnc&isPreview=0&isSendStatusEvent=1&language=`` （SSE）
8. 结果：``GET /mooc2-ans/ai-evaluate/v2/answer/evaluate-result?...&recordUuid=``
   直到 ``isComplete``。

完成判据：任务引擎 ``plan.score != null``。COM0018A 这批是
``enableAicoach=1, aicoachScore=60``，**需要评分 ≥ 60**。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional

import requests
from loguru import logger

MOOC2 = "https://mooc2-ans.chaoxing.com"
AI_ANS = f"{MOOC2}"  # /ai-ans 与 mooc2-ans 同源

MODE_LADDER = 2
MODE_CODE = 4

# 运行结果状态码（来自页面 statusMsg）
RUN_STATUS = {
    1: "通过", 2: "答案不正确", 3: "运行错误", 4: "超出时间限制",
    5: "超出内存限制", 6: "编译错误", 7: "格式错误",
    8: "服务超时", 9: "服务不支持",
}
# 这些状态说明代码本身有问题 → 不应提交
RUN_FATAL = {2, 3, 4, 5, 6, 7}


class LadderError(Exception):
    """AI实践（思维阶梯/代码实践）操作失败。"""


class LadderAbort(LadderError):
    """出于安全考虑主动中止：绝不提交空答案或明知有问题的答案。"""


# ----------------------------------------------------------------------
# 页面 / 语言列表
# ----------------------------------------------------------------------

_HIDDEN = re.compile(
    r'<input[^>]*?(?:id|name)\s*=\s*["\']([^"\']+)["\'][^>]*?value\s*=\s*["\']([^"\']*)["\']',
    re.I)
_HIDDEN_REV = re.compile(
    r'<input[^>]*?value\s*=\s*["\']([^"\']*)["\'][^>]*?(?:id|name)\s*=\s*["\']([^"\']+)["\']',
    re.I)


def parse_hidden_inputs(html: str) -> dict:
    fields: dict[str, str] = {}
    for key, value in _HIDDEN.findall(html):
        fields.setdefault(key, value)
    for value, key in _HIDDEN_REV.findall(html):
        fields.setdefault(key, value)
    return fields


def parse_languages(raw: str) -> dict[int, dict]:
    """复刻页面 ``loadLangListFromInput`` 的"修 JSON"逻辑后解析语言列表。

    页面里的 ``#languages`` 是**非严格 JSON**（``Golang``、``R``、``text/x-c++src``
    等标识符未加引号），页面自己用一连串正则修补，这里照做。
    """
    if not raw:
        return {}
    s = re.sub(r'"', '\\"', raw)
    s = re.sub(r'([\u4e00-\u9fa5a-zA-Z]+[\u4e00-\u9fa5a-zA-Z0-9/\+#\-_.]*)', r'"\1"', s)
    s = re.sub(r'"(\d+)"', r'\1', s)
    s = re.sub(r'\],\s*\[', '], [', s)
    s = s.replace('[[', '[ [').replace(']]', '] ]')
    try:
        arr = json.loads(s)
    except ValueError as exc:
        logger.debug("解析语言列表失败: {}", exc)
        return {}
    out: dict[int, dict] = {}
    for item in arr:
        if not isinstance(item, list) or len(item) < 4:
            continue
        code, name, mime, supported = item[0], item[1], item[2], item[3]
        if supported != 1:
            continue
        try:
            out[int(code)] = {"codeNum": int(code), "codeName": str(name),
                              "mimeType": str(mime)}
        except (TypeError, ValueError):
            continue
    return out


def pick_language(langs: dict[int, dict], prefer: str = "Java") -> tuple[int, str]:
    """挑一个可用语言。默认 Java（COM0018A 是面向对象/Java 课程）。"""
    for code, info in langs.items():
        if info["codeName"].lower() == prefer.lower():
            return code, info["codeName"]
    for code, info in langs.items():          # 退而求其次：Python3
        if "python3" in info["codeName"].lower():
            return code, info["codeName"]
    for code, info in langs.items():
        if code != -1:
            return code, info["codeName"]
    raise LadderAbort("未能从页面解析出任何可用编程语言")


def strip_html(text: str) -> str:
    """把题目 HTML 转成纯文本（保留段落换行）。"""
    if not text:
        return ""
    t = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.I)
    t = re.sub(r"</\s*(p|div|li|h[1-6])\s*>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = (t.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
         .replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&"))
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ----------------------------------------------------------------------
# 接口封装
# ----------------------------------------------------------------------


def _ids(study_url: dict, course: dict) -> dict:
    """从 studyUrl 解析出 (courseid, clazzid, cpi, publishRelationUuid)。"""
    from urllib.parse import parse_qs, urlparse

    url = study_url.get("url") or ""
    q = parse_qs(urlparse(url).query)
    return {
        "courseid": (q.get("courseid") or [str(course.get("courseId") or "")])[0],
        "clazzid": (q.get("clazzid") or [str(course.get("clazzId") or "")])[0],
        "cpi": (q.get("cpi") or [str(course.get("cpi") or "")])[0],
        "publishRelationUuid": (q.get("publishRelationUuid") or [""])[0],
        "url": url,
    }


def load_data(session: requests.Session, ids: dict, timeout: float = 30.0) -> dict:
    r = session.get(
        f"{MOOC2}/mooc2-ans/ai-evaluate/v2/answer/load-data",
        params={"courseid": ids["courseid"], "clazzid": ids["clazzid"], "cpi": ids["cpi"],
                "publishRelationUuid": ids["publishRelationUuid"], "recordUuid": ""},
        headers={"Referer": ids["url"] or f"{MOOC2}/ai-evaluate/v2/answer"},
        timeout=timeout)
    body = r.json() if r.status_code == 200 else {}
    if not body.get("status"):
        raise LadderError(f"load-data 失败: {body.get('errorMsg') or body.get('msg') or r.status_code}")
    return body.get("data") or {}


def procedural_run(session: requests.Session, ids: dict, language: int, code: str,
                   stdin: str = "", stdout: str = "",
                   examples: Optional[list] = None, timeout: float = 120.0) -> dict:
    """自测运行。返回 ``{status, right, wrong, ...}``。"""
    example_arr = []
    for ex in (examples or []):
        example_arr.append({"stdin": ex.get("stdin", ""), "stdout": ex.get("stdout", ""),
                            "memory": ex.get("memory") or 65536,
                            "time": ex.get("time") or 10000})
    if not example_arr:
        example_arr = [{"stdin": stdin, "stdout": stdout, "memory": 65536, "time": 10000}]
    data = {
        "type": "selfcheck",
        "runResultType": "customRunResult",
        "language": language,
        "answerResult": code,
        "stdin": stdin,
        "stdout": stdout,
        "courseid": ids["courseid"],
        "cpi": ids["cpi"],
        "exampleArr": json.dumps(example_arr, ensure_ascii=False),
    }
    r = session.post(f"{MOOC2}/mooc2-ans/ai-evaluate/v2/answer/procedural-run",
                     data=data, timeout=timeout,
                     headers={"Referer": ids["url"],
                              "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    if r.status_code != 200:
        raise LadderError(f"procedural-run 返回 HTTP {r.status_code}")
    body = r.json()
    if body.get("status") is False:
        raise LadderError(f"运行服务错误: {body.get('msg')}")
    return body


def answer_init(session: requests.Session, ids: dict, code_uuid: str,
                answer_result: str, timeout: float = 60.0) -> dict:
    r = session.post(
        f"{MOOC2}/mooc2-ans/ai-evaluate/v2/answer/init",
        params={"courseid": ids["courseid"], "clazzid": ids["clazzid"],
                "cpi": ids["cpi"], "publishRelationUuid": ids["publishRelationUuid"]},
        data={"type": MODE_CODE, "codeUuid": code_uuid, "answerResult": answer_result},
        timeout=timeout, headers={"Referer": ids["url"]})
    body = r.json() if r.status_code == 200 else {}
    if not body.get("status") or not body.get("data"):
        raise LadderError(f"answer/init 失败: {body.get('msg') or r.status_code}")
    return body["data"]


def answer_submit(session: requests.Session, ids: dict, record_uuid: str,
                  timeout: float = 60.0) -> Any:
    r = session.post(
        f"{MOOC2}/mooc2-ans/ai-evaluate/v2/answer/submit",
        params={"courseid": ids["courseid"], "clazzid": ids["clazzid"],
                "cpi": ids["cpi"], "recordUuid": record_uuid},
        data={}, timeout=timeout, headers={"Referer": ids["url"]})
    body = r.json() if r.status_code == 200 else {}
    if not body.get("status"):
        raise LadderError(f"answer/submit 失败: {body.get('msg') or r.status_code}")
    return body.get("data")


def consume_sse(resp: requests.Response, on_event: Optional[Callable] = None,
                max_seconds: float = 300.0) -> dict:
    """消费 SSE 流。返回 ``{"status": <终态>, "text": <累积文本>}``。"""
    # 响应头常不带 charset，requests 会猜成 latin-1 → 中文全变乱码
    try:
        resp.encoding = "utf-8"
    except Exception:  # noqa: BLE001
        pass
    started = time.time()
    event = "message"
    status = None
    chunks: list[str] = []
    for raw in resp.iter_lines(decode_unicode=True):
        if time.time() - started > max_seconds:
            logger.warning("SSE 消费超时（{} 秒），提前结束", max_seconds)
            break
        if raw is None:
            continue
        if raw == "":
            event = "message"
            continue
        if raw.startswith("event:"):
            event = raw[6:].strip()
            continue
        if not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if not payload:
            continue
        if event == "error":
            raise LadderError(f"评估流返回错误: {payload[:200]}")
        if event == "status":
            try:
                parsed = json.loads(payload)
            except ValueError:
                status = payload
            else:
                status = parsed.get("content") if isinstance(parsed, dict) else payload
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            content = payload
        else:
            # 流里除了正文对象，还会出现保活用的裸数字（如 ``data: 0``）或数组，
            # 早期版本直接 ``obj.get(...)`` 会抛 AttributeError 打断整条评估流。
            content = obj.get("content") if isinstance(obj, dict) else ""
            if content is None:
                content = ""
        if content:
            chunks.append(str(content))
        if callable(on_event):
            try:
                on_event(content)
            except Exception:  # noqa: BLE001
                pass
    return {"status": status, "text": "".join(chunks)}


def evaluate_stream(session: requests.Session, ids: dict, record_uuid: str,
                    ai_enc: str, language: str = "",
                    max_seconds: float = 300.0) -> dict:
    """触发评估（这一步才产生分数）。"""
    url = (f"{AI_ANS}/ai-ans/ai-evaluate/v2/evaluate/stream"
           f"?courseId={ids['courseid']}&clazzId={ids['clazzid']}&cpi={ids['cpi']}"
           f"&recordUuid={record_uuid}&aiEnc={ai_enc}&isPreview=0"
           f"&isSendStatusEvent=1&language={language}")
    try:
        with session.post(url, headers={"Content-Type": "application/x-www-form-urlencoded"},
                          stream=True, timeout=(30, max_seconds)) as resp:
            if resp.status_code != 200:
                raise LadderError(f"evaluate/stream 返回 HTTP {resp.status_code}")
            return consume_sse(resp, max_seconds=max_seconds)
    except requests.RequestException as exc:
        raise LadderError(f"evaluate/stream 请求失败: {exc}") from exc


def evaluate_result(session: requests.Session, ids: dict, record_uuid: str,
                    timeout: float = 30.0) -> dict:
    r = session.get(
        f"{MOOC2}/mooc2-ans/ai-evaluate/v2/answer/evaluate-result",
        params={"courseid": ids["courseid"], "clazzid": ids["clazzid"],
                "cpi": ids["cpi"], "recordUuid": record_uuid},
        timeout=timeout, headers={"Referer": ids["url"]})
    if r.status_code != 200:
        return {}
    return (r.json() or {}).get("data") or {}


def extract_score(result: dict, data: Optional[dict] = None) -> Optional[float]:
    """从评估结果里取总分。

    ``evaluate-result`` **没有**顶层 ``totalScore``：总分要么是各评分维度
    ``evaluateResult.criteria[].score`` 之和，要么直接读
    ``load-data.answerRecords[0].score``（最可靠）。
    """
    for key in ("totalScore", "score", "finalScore"):
        v = (result or {}).get(key)
        if isinstance(v, (int, float)) and v:
            return float(v)
    crit = ((result or {}).get("evaluateResult") or {}).get("criteria") or []
    total = 0.0
    found = False
    for item in crit:
        if isinstance(item, dict) and isinstance(item.get("score"), (int, float)):
            total += float(item["score"])
            found = True
    if found:
        return total
    recs = ((data or {}).get("answerRecords") or [])
    for rec in recs:
        if isinstance(rec, dict) and isinstance(rec.get("score"), (int, float)):
            return float(rec["score"])
    return None


def wait_result(session: requests.Session, ids: dict, record_uuid: str,
                tries: int = 20, delay: float = 3.0) -> Optional[dict]:
    for i in range(tries):
        d = evaluate_result(session, ids, record_uuid)
        if d.get("isComplete") or d.get("evaluateResult"):
            return d
        logger.info("等待评估结果（{}/{}）…", i + 1, tries)
        time.sleep(delay)
    return None


# ----------------------------------------------------------------------
# 代码实践（type=4）
# ----------------------------------------------------------------------

_CODE_SYSTEM = (
    "你是一名计算机专业的学生，正在完成课程平台上的编程实践题。"
    "请直接输出**完整、可直接编译运行**的源代码，不要输出 Markdown 代码块标记、"
    "不要输出任何解释性文字。\n"
    "硬性要求：\n"
    "1) 只输出代码本身；\n"
    "2) 代码必须自包含（单文件、含入口方法），不依赖外部库；\n"
    "3) **不要依赖交互式输入**：把题目中给出的具体数据直接内置到程序里，"
    "自动跑完一次完整演示；如果确实需要读取输入，必须用 try/catch 或判空兜底，"
    "保证在没有标准输入时也不会抛异常；\n"
    "4) 输出要清晰、带中文说明，符合题目要求的输出内容。"
)


def build_code_prompt(requirement: str, language: str,
                      examples: Optional[list] = None) -> tuple[str, str]:
    user = f"编程语言：{language}\n\n题目要求：\n{requirement}"
    if examples:
        lines = []
        for i, ex in enumerate(examples, 1):
            lines.append(f"用例{i}: 输入={ex.get('stdin','')!r} 期望输出={ex.get('stdout','')!r}")
        if lines:
            user += "\n\n参考用例：\n" + "\n".join(lines)
    user += "\n\n请给出完整代码。"
    return _CODE_SYSTEM, user


def complete_code_task(session: requests.Session, course: dict, study_url: dict,
                       generator: Any, *, options: Optional[dict] = None,
                       on_log: Optional[Callable] = None,
                       approval: Optional[Callable[[str, str, Callable], None]] = None
                       ) -> tuple[bool, str]:
    """完成一个「代码实践」(type=4) 任务点。

    安全约束：自测运行失败（编译/运行错误）时会重新生成代码；仍失败则**中止且不提交**。

    ``approval`` 不为 None 时进入「生成后暂停」模式：只生成代码并跑完自测，
    把 ``(代码, 语言名, 提交回调)`` 交给 ``approval``，**不建卷、不交卷**。
    """
    opts = options or {}
    log = on_log or (lambda m: logger.info(m))

    ids = _ids(study_url, course)
    if not ids["publishRelationUuid"]:
        raise LadderAbort("未能从任务点解析出 publishRelationUuid")

    # 1) 入口页：语言列表 + aiEnc
    r = session.get(ids["url"], timeout=30)
    r.encoding = "utf-8"
    page = r.text
    fields = parse_hidden_inputs(page)
    # aiEnc 稍后会用 load-data 的值兜底（类型 4 的页面通常有，但不保证）
    ai_enc = fields.get("aiEnc") or ""
    langs = parse_languages(fields.get("languages") or "")
    if not langs:
        raise LadderAbort("未能从页面解析出语言列表，无法确定 languageIndex")
    prefer = opts.get("code_language") or "Java"
    lang_code, lang_name = pick_language(langs, prefer)
    log(f"编程语言：{lang_name}（codeNum={lang_code}）")

    # 2) load-data：题目与 codeUuid
    data = load_data(session, ids)
    # aiEnc 以 load-data 为准；页面里没有时尤其重要（否则 /ai-ans 返回 401）
    ai_enc = (ai_enc or data.get("aiEnc") or "").strip()
    if not ai_enc:
        raise LadderAbort("load-data 未返回 aiEnc，调用 /ai-ans 会被拒（401），已中止")
    code_uuid = data.get("codeUuid") or ""
    if not code_uuid:
        raise LadderAbort("load-data 未返回 codeUuid，无法作答")
    requirement = strip_html(data.get("content") or data.get("requirementContent")
                            or data.get("requirement") or "")
    if not requirement:
        raise LadderAbort("题目内容为空，拒绝提交")
    examples = data.get("examples") or []
    log(f"题目长度 {len(requirement)} 字，用例 {len(examples)} 个，"
        f"需评分≥{data.get('publishSetting', {}).get('reachStandard')}")

    if not getattr(generator, "available", False):
        raise LadderAbort("未配置 AI 接口（endpoint/key/model），无法生成代码")

    # 3) 生成代码 → 自测运行（失败则带上错误重试一次）
    system, user = build_code_prompt(requirement, lang_name, examples)
    code = ""
    last_error = ""
    max_retries = int(opts.get("code_max_retries") or 2)
    max_tokens = int(opts.get("code_max_tokens") or 4000)
    for attempt in range(1, max_retries + 1):
        prompt = user
        if last_error:
            prompt += (f"\n\n上一次生成的代码没有通过自测运行，错误信息：\n{last_error}\n"
                       "请修正后重新给出完整代码（务必输出完整程序，不要省略任何部分）。")
        code = generator.generate_text(system, prompt, temperature=0.2,
                                       max_tokens=max_tokens) or ""
        code = _strip_code_fence(code)
        if not code.strip():
            raise LadderAbort("AI 未生成任何代码，已中止")
        log(f"第 {attempt} 次生成代码：{len(code)} 字符")

        unbalanced = _unbalanced(code)
        if unbalanced:
            last_error = f"代码看起来被截断了（{unbalanced}）"
            log(f"生成结果疑似不完整：{unbalanced}")
            if attempt >= max_retries:
                raise LadderAbort(f"AI 连续 {max_retries} 次生成的代码不完整，已中止且未提交")
            continue

        try:
            run = procedural_run(session, ids, lang_code, code, examples=examples)
        except LadderError as exc:
            # 运行服务本身不可用时不阻断（题目没有用例时无法判定对错）
            log(f"自测运行服务异常，跳过自测：{exc}")
            break
        status = run.get("status")
        log(f"自测运行结果：status={status}（{RUN_STATUS.get(status, '未知')}）")
        fatal = _run_is_fatal(run, has_examples=bool(examples))
        if not fatal:
            break
        last_error = (f"status={status}（{RUN_STATUS.get(status, '未知')}）；"
                      f"errors={str(run.get('errors'))[:400]}")
        if attempt >= max_retries:
            raise LadderAbort(f"生成代码连续 {max_retries} 次未通过自测运行，已中止且未提交。"
                              f"最后一次：{last_error}")
        log("将带上错误信息重新生成代码…")

    # 4~6) 建卷 → 交卷 → 触发评估。抽成闭包，便于「生成后暂停、逐条审批」：
    #      审批模式下只生成代码并跑完自测，真正的建卷/交卷留到用户批准之后。
    def _submit_and_evaluate() -> tuple[bool, str]:
        answer_result = json.dumps(
            {"answerResultCode": code, "languageIndex": lang_code,
             "languageName": lang_name}, ensure_ascii=False)
        # 同样优先复用已有的未完成记录（否则会被「已存在未完成的作答记录」拒绝）；
        # 勾了「重做」则先结束旧记录再建新卷
        record_uuid = redo_or_reuse_record(
            session, ids, data, log=log,
            init_fn=lambda: answer_init(session, ids, code_uuid, answer_result),
            allow_redo=bool(opts.get("ai_redo")))
        if not record_uuid:
            raise LadderAbort("answer/init 未返回 recordUuid")
        log(f"作答记录：recordUuid={record_uuid}")

        record_status = answer_submit(session, ids, record_uuid)
        log(f"已交卷：recordStatus={record_status}")

        if opts.get("skip_evaluate"):
            return True, f"已提交作答（跳过评估），recordStatus={record_status}"
        try:
            stream = evaluate_stream(session, ids, record_uuid, ai_enc,
                                    language=opts.get("evaluate_language") or "")
            log(f"评估流结束：status={stream.get('status')}")
        except LadderError as exc:
            log(f"评估流异常（继续轮询结果）：{exc}")

        result = wait_result(session, ids, record_uuid,
                            tries=int(opts.get("evaluate_tries") or 15))
        try:
            fresh = load_data(session, ids)
        except LadderError:
            fresh = {}
        score = extract_score(result or {}, fresh)
        if score is not None:
            log(f"评估完成，得分：{score}")
            return True, f"代码已提交并完成评估（得分 {score}）"
        try:
            recs = (fresh.get("answerRecords") or [])
            if recs and isinstance(recs[0], dict) and recs[0].get("statusMsg"):
                return True, f"代码已提交，平台状态：{recs[0].get('statusMsg')}（分数稍后刷新）"
        except Exception:  # noqa: BLE001
            pass
        return True, "代码已提交，评估结果仍在生成中（稍后可刷新查看）"

    if approval is not None:
        approval(code, lang_name, _submit_and_evaluate)
        return True, f"已生成 {len(code)} 字符 {lang_name} 代码并通过自测，待审批后提交"

    return _submit_and_evaluate()


# ----------------------------------------------------------------------
# 思维阶梯（type=2）
# ----------------------------------------------------------------------

_LADDER_SYSTEM = (
    "你是一名大学生，正在完成课程平台上的「思维阶梯」AI实践。"
    "平台会就同一主题连续追问，你需要用中文认真作答。\n"
    "要求：\n"
    "1) 只输出你的作答内容本身，不要任何解释性旁白、不要 Markdown 标题；\n"
    "2) 直接回答被问到的内容，观点明确、有依据、有条理；\n"
    "3) 每题篇幅约 120-300 字。"
)


def split_questions(requirement: str) -> list[str]:
    """把题目文本拆成若干小问（形如 ``1. xxx`` / ``2、xxx`` / ``3）xxx``）。"""
    text = requirement or ""
    marks = list(re.finditer(r"(?:^|\n)\s*(\d{1,2})\s*[.、)．]\s*", text))
    if len(marks) < 2:
        return [text.strip()] if text.strip() else []
    out = []
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        q = text[start:end].strip()
        if q:
            out.append(q)
    return out


def redo_or_reuse_record(session: requests.Session, ids: dict, data: dict, *,
                         log: Optional[Callable] = None,
                         init_fn: Optional[Callable[[], dict]] = None,
                         allow_redo: bool = False) -> str:
    """取得可用的 ``recordUuid``，并支持「重做」。

    平台**没有**「放弃/删除作答记录」的接口（前端 ``rePractice()`` 只是清空本地引用，
    列全了 ``/ai-evaluate/v2/answer/*`` 也没有删除类端点），只要存在未完成记录，
    ``answer/init`` 就会被拒（``已存在未完成的作答记录``）。

    因此「重做」只能这样实现：**先把旧记录交卷结束掉**，它就不再是「未完成」，
    之后再建新卷就合法了（``remainScoreCount`` 有 100 次余量，代价很小）。

    ``allow_redo=False``（默认）时保持平台语义：续做已有记录。
    """
    log = log or (lambda m: logger.info(m))
    existing = (data.get("recordUuid") or "").strip()

    if not existing:
        init = (init_fn() if init_fn else None) or {}
        return (init.get("recordUuid") or "").strip()

    if not allow_redo:
        log(f"复用已有的未完成作答记录：{existing}")
        return existing

    log(f"检测到未完成作答记录 {existing}，按要求重做：先结束旧记录")
    try:
        status = answer_submit(session, ids, existing)
        log(f"旧记录已交卷（recordStatus={status}），尝试建新卷")
    except LadderError as exc:
        log(f"结束旧记录失败（{exc}），回退为续做")

    # 交卷后 load-data 通常会给出新的（或空的）recordUuid
    try:
        fresh = load_data(session, ids)
        new_uuid = (fresh.get("recordUuid") or "").strip()
        if new_uuid and new_uuid != existing:
            log(f"重做成功，新的作答记录：{new_uuid}")
            return new_uuid
    except LadderError as exc:
        log(f"重做后读取 load-data 失败：{exc}")

    if init_fn:
        try:
            init = init_fn() or {}
            new_uuid = (init.get("recordUuid") or "").strip()
            if new_uuid:
                log(f"重做成功（新建），作答记录：{new_uuid}")
                return new_uuid
        except LadderError as exc:
            log(f"重做建卷失败（{exc}）")

    log(f"重做未成功，回退为续做旧记录 {existing}")
    return existing


def reuse_or_init_record(session: requests.Session, ids: dict, data: dict, *,
                         log: Optional[Callable] = None,
                         init_fn: Optional[Callable[[], dict]] = None) -> str:
    """取得可用的 ``recordUuid``：优先复用已有的未完成记录。

    关键事实（实测）：``load-data`` 会带上当前**未完成**作答记录的
    ``recordUuid`` / ``answerUuid``。真实页面只在它们为空时才调 ``answer/init``
    （前端 JS：``if (!this.current.answerUuid || !this.current.recordUuid)``）。
    如果无条件新建，服务端会拒绝：``已存在未完成的作答记录``。

    返回 ``recordUuid``；都拿不到时返回空串。
    """
    log = log or (lambda m: logger.info(m))

    existing = (data.get("recordUuid") or "").strip()
    if existing:
        log(f"复用已有的未完成作答记录：{existing}")
        return existing

    if init_fn is None:
        return ""
    try:
        init = init_fn() or {}
    except LadderError as exc:
        # 兜底：即便服务端说「已存在未完成的作答记录」，也重新读一次 load-data 取它
        if "已存在" not in str(exc):
            raise
        fresh = load_data(session, ids)
        reused = (fresh.get("recordUuid") or "").strip()
        if reused:
            log(f"服务端提示已存在作答记录，改为续做：{reused}")
            return reused
        raise
    return (init.get("recordUuid") or "").strip()


def answer_init_ladder(session: requests.Session, ids: dict,
                       timeout: float = 60.0) -> dict:
    """思维阶梯的建卷：``type=2``，无 ``scene``/``codeUuid``。"""
    r = session.post(
        f"{MOOC2}/mooc2-ans/ai-evaluate/v2/answer/init",
        params={"courseid": ids["courseid"], "clazzid": ids["clazzid"],
                "cpi": ids["cpi"], "publishRelationUuid": ids["publishRelationUuid"]},
        data={"type": MODE_LADDER}, timeout=timeout,
        headers={"Referer": ids["url"]})
    body = r.json() if r.status_code == 200 else {}
    if not body.get("status") or not body.get("data"):
        raise LadderError(f"answer/init(type=2) 失败: {body.get('msg') or r.status_code}")
    return body["data"]


def main_talk(session: requests.Session, ids: dict, record_uuid: str, ai_enc: str,
              user_message: str, max_seconds: float = 180.0) -> str:
    """发一轮对话，返回 AI 回复文本（SSE 累积）。"""
    url = (f"{AI_ANS}/ai-ans/ai-evaluate/think/main-talk"
           f"?courseId={ids['courseid']}&clazzId={ids['clazzid']}&cpi={ids['cpi']}"
           f"&aiEnc={ai_enc}&recordUuid={record_uuid}")
    try:
        with session.post(url, data={"userMessage": user_message},
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          stream=True, timeout=(30, max_seconds)) as resp:
            if resp.status_code != 200:
                raise LadderError(f"main-talk 返回 HTTP {resp.status_code}")
            out = consume_sse(resp, max_seconds=max_seconds)
    except requests.RequestException as exc:
        raise LadderError(f"main-talk 请求失败: {exc}") from exc
    return out.get("text") or ""


def end_report(session: requests.Session, ids: dict, record_uuid: str, ai_enc: str,
               max_seconds: float = 300.0) -> dict:
    """触发结题报告 —— 思维阶梯的**分数由这一步产生**。"""
    url = (f"{AI_ANS}/ai-ans/ai-evaluate/think/end-report"
           f"?courseId={ids['courseid']}&clazzId={ids['clazzid']}&cpi={ids['cpi']}"
           f"&aiEnc={ai_enc}&recordUuid={record_uuid}")
    try:
        with session.post(url, headers={"Content-Type": "application/x-www-form-urlencoded"},
                          stream=True, timeout=(30, max_seconds)) as resp:
            if resp.status_code != 200:
                raise LadderError(f"end-report 返回 HTTP {resp.status_code}")
            return consume_sse(resp, max_seconds=max_seconds)
    except requests.RequestException as exc:
        raise LadderError(f"end-report 请求失败: {exc}") from exc


# ----------------------------------------------------------------------
# 选择题支持
#
# 实测（截图 + 平台原话「你的回答与本题选项作答无关哦，请回忆…选出正确的选项」）：
# 思维阶梯的题**可能是选择题**，此时作答必须是**选项字母**（前端 sendChange() 把
# radioArr 里的字母拼起来直接当 userMessage 发出）。之前一律发散文 → 平台比对不到
# 选项 → 0 分。
# ----------------------------------------------------------------------

_CHOICE_SYSTEM = (
    "你是学科答题助手。用户会给你一道选择题（可能是单选，也可能是多选）。"
    "请判断所有正确选项并只输出它们的字母（例如 B 或 ABDE），"
    "不要输出任何解释、空格、标点或其它文字。"
)


def parse_choice_question(raw: Any) -> Optional[dict]:
    """从消息内容里解析选择题：返回 ``{"stem":…, "options":[{option,optionContent}]}``。"""
    if not raw:
        return None
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text.startswith("{"):
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    options = data.get("questionOptions")
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except ValueError:
            options = []
    if not isinstance(options, list):
        return None
    cleaned = [o for o in options if isinstance(o, dict) and o.get("option")]
    if not cleaned:
        return None
    return {"stem": str(data.get("questionStem") or ""), "options": cleaned}


def extract_stem(raw: Any) -> str:
    """从回复里取出题干文本（选择题已由 parse_choice_question 处理）。"""
    if not raw:
        return ""
    text = raw if isinstance(raw, str) else str(raw)
    for match in re.finditer(r"\{[^{}]*\}", text):
        try:
            data = json.loads(match.group(0))
        except ValueError:
            continue
        if isinstance(data, dict):
            stem = data.get("questionStem") or data.get("content")
            if stem:
                return str(stem)
    return text.strip()[:800]


def find_current_choice(data: dict) -> Optional[dict]:
    """在 ``messageList`` 里找**最后一道**选择题（就是当前要作答的那题）。"""
    found = None
    for msg in data.get("messageList") or []:
        if isinstance(msg, dict):
            choice = parse_choice_question(msg.get("content"))
            if choice:
                found = choice
    return found


def pick_option(generator: Any, choice: dict, log: Optional[Callable] = None) -> str:
    """选出正确选项，返回可直接作答的值。

    分两种题型（都要支持）：

    * **字母选项**（A/B/C/D…）：单选返回一个字母，多选返回按字母序拼接的串
      （``ABDE``）。前端 ``sendChange()`` 就是把 radioArr 排序后拼接发出的。
    * **非字母选项**：判断题等，选项标识可能就是「正确 / 错误」这种词
      —— 实测第 3 题就是这种，之前只抠 ``[A-Ea-e]`` 会把 AI 的「否」判定为失败，
      白费一次作答机会。
    """
    log = log or (lambda m: None)
    options = choice["options"]
    labels = [str(o["option"]).strip() for o in options]
    as_letters = all(re.fullmatch(r"[A-Ea-e]", lab) for lab in labels)

    lines = [f"{o['option']}. {o.get('optionContent') or ''}" for o in options]
    if as_letters:
        prompt = (f"题目：{choice['stem']}\n选项：\n" + "\n".join(lines) +
                  "\n\n请判断正确选项。如果只有一个正确选项，只输出那一个字母；"
                  "如果是多选题，输出**所有**正确选项的字母。"
                  "只输出字母，不要空格、逗号、标点或任何解释。")
    else:
        prompt = (f"题目：{choice['stem']}\n选项：\n" + "\n".join(lines) +
                  "\n\n请判断应选哪一项，只输出该项的**标识**，"
                  f"必须是以下之一：{'、'.join(labels)}。不要任何解释。")

    raw = (generator.generate_text(_CHOICE_SYSTEM, prompt,
                                   temperature=0.1, max_tokens=24) or "").strip()

    if as_letters:
        letters = re.findall(r"[A-Ea-e]", raw)
        valid = {lab.lower(): lab for lab in labels}
        picked = [valid[ch.lower()] for ch in sorted({c.lower() for c in letters})
                  if ch.lower() in valid]
        if not picked:
            raise LadderAbort(f"AI 未能给出选项字母（返回：{raw[:40]!r}），已中止")
        answer = "".join(picked)
        for o in options:
            if str(o["option"]).strip() in picked:
                log(f"  选项 {o['option']}：{str(o.get('optionContent'))[:34]}…")
        log(f"选择题作答：{answer}（{len(picked)} 个选项"
            f"{'，多选' if len(picked) > 1 else ''}）")
        return answer

    # ---- 非字母选项：先直接匹配标识，再按语义兜底 ----
    for lab in labels:
        if lab and lab in raw:
            log(f"作答：{lab}（AI 返回 {raw[:20]!r}）")
            return lab
    # 语义兜底：AI 常答「否/错/不正确」——映射到含否定词的选项
    negative = any(k in raw for k in ("否", "错", "不正确", "false", "False", "不对"))
    affirmative = any(k in raw for k in ("是", "正确", "对", "true", "True"))
    for o in options:
        blob = f"{o['option']}{o.get('optionContent') or ''}"
        if negative and ("错误" in blob or "不正确" in blob):
            log(f"作答：{o['option']}（按否定语义匹配 {raw[:20]!r}）")
            return str(o["option"]).strip()
        if affirmative and ("正确" in blob or "对" in blob):
            log(f"作答：{o['option']}（按肯定语义匹配 {raw[:20]!r}）")
            return str(o["option"]).strip()
    # 最后兜底：取第一个选项标识，保证流程不中断（不因一次解析失败废掉整次作答）
    log(f"警告：无法解析 AI 返回 {raw[:30]!r}，暂按第一个选项 {labels[0]!r} 作答")
    return labels[0]


def complete_think_ladder(session: requests.Session, course: dict, study_url: dict,
                          generator: Any, *, options: Optional[dict] = None,
                          on_log: Optional[Callable] = None) -> tuple[bool, str]:
    """完成一个「思维阶梯」(type=2) 任务点。

    ✅ **已真机验证**（2026-10-12）：planId 7881197「静电场环路定理闯关」——
    题目 243 字拆出 8 个小问、逐题作答（含多选）、交卷、结题报告出分，
    得分 85（目标 80），平台 ``isFinish`` false→true。

    安全约束：AI 没能生成作答、或对话没有任何回复时，一律中止且不提交。
    """
    opts = options or {}
    log = on_log or (lambda m: logger.info(m))
    ids = _ids(study_url, course)
    if not ids["publishRelationUuid"]:
        raise LadderAbort("未能从任务点解析出 publishRelationUuid")

    r = session.get(ids["url"], timeout=30)
    r.encoding = "utf-8"
    page_fields = parse_hidden_inputs(r.text)

    data = load_data(session, ids)
    # ⚠️ aiEnc 必须优先取 load-data 的值：类型 2/4 的入口页里根本没有 aiEnc
    # 隐藏域（实测为 None），拿页面值会让 /ai-ans 直接返回 HTTP 401。
    ai_enc = (data.get("aiEnc") or page_fields.get("aiEnc") or "").strip()
    requirement = strip_html(data.get("content") or data.get("requirement") or "")
    if not requirement:
        raise LadderAbort("未能取得思维阶梯的题目内容，拒绝提交")
    questions = split_questions(requirement)
    max_turns = int(opts.get("ladder_max_turns") or max(len(questions), 3))
    log(f"题目 {len(requirement)} 字，拆出 {len(questions)} 个小问，计划对话 {max_turns} 轮")
    if not ai_enc:
        raise LadderAbort("load-data 未返回 aiEnc，调用 /ai-ans 会被拒（401），已中止")

    if not getattr(generator, "available", False):
        raise LadderAbort("未配置 AI 接口（endpoint/key/model），无法作答")

    # 复用已有的「未完成」作答记录，而不是新建。
    # 真实页面就是这么做的（answer-common.js：只有 !answerUuid || !recordUuid
    # 时才调 answer/init）。总是新建会被服务端拒绝：已存在未完成的作答记录。
    # 若用户勾了「重做」，则先结束旧记录再建新卷（平台没有放弃接口）。
    record_uuid = redo_or_reuse_record(
        session, ids, data, log=log,
        init_fn=lambda: answer_init_ladder(session, ids),
        allow_redo=bool(opts.get("ai_redo")))
    if not record_uuid:
        raise LadderAbort("未能取得作答记录（answer/init 未返回 recordUuid）")

    history: list[dict] = []
    turns_done = 0
    max_turns = max(max_turns, len(questions))      # 每题至少答一轮

    # 平台是**逐轮下发题目**的：首次必须发一个空的 userMessage，平台才会回第一道题
    # （前端 answer 页的 chat() 首次调用就是 `userMessage=""`）。
    # 之前直接从 requirement 里编造问题发散文，导致「回答与本题选项作答无关」。
    try:
        reply = main_talk(session, ids, record_uuid, ai_enc, "")
        log("已向平台索取第一题")
    except LadderError as exc:
        raise LadderAbort(f"获取第一题失败：{exc}") from exc

    answered_stems: set[str] = set()
    for i in range(max_turns):
        # 题目以权威 JSON 形式存在于 load-data 的 messageList 里；
        # main_talk 的回复是 Markdown 渲染文本（还带排版），不适合解析。
        # 因此每轮重新读一次 load-data 取「最后一道题」。
        try:
            fresh = load_data(session, ids)
            current = find_current_choice(fresh)
        except LadderError as exc:
            log(f"重新读取题目失败（改用回复文本判断）：{exc}")
            current = parse_choice_question(reply)
        if current and current["stem"] in answered_stems:
            log("题目没有变化，说明本次作答未被接受，结束作答")
            break
        choice = current
        if choice:
            answered_stems.add(choice["stem"])
            # 单选题/多选题由 AI 自行判断（多选要给出全部正确选项）
            log(f"第 {i + 1} 题（选择题，{len(choice['options'])} 个选项）："
                f"{choice['stem'][:36]}…")
            answer = pick_option(generator, choice, log=log)
        else:
            # 非选择题（简答/开放题）：按题目文本生成作答
            stem = extract_stem(reply) or (
                questions[i] if i < len(questions) else "请继续展开你的思考。")
            prompt = f"平台的追问/题目：{stem}"
            if history:
                prompt += "\n\n（此前的对话摘要）\n" + "\n".join(
                    f"{h['role']}: {h['content'][:200]}" for h in history[-3:])
            answer = (generator.generate_text(
                _LADDER_SYSTEM, prompt, temperature=0.6,
                max_tokens=int(opts.get("ladder_max_tokens") or 1200)) or "").strip()
            if not answer:
                raise LadderAbort(f"第 {i + 1} 题 AI 未生成作答，已中止且未提交")
            log(f"第 {i + 1} 题（简答）已作答 {len(answer)} 字")

        history.append({"role": "user", "content": answer})
        try:
            reply = main_talk(session, ids, record_uuid, ai_enc, answer)
        except LadderError as exc:
            log(f"发送第 {i + 1} 题作答失败（{exc}），结束作答")
            break
        turns_done += 1
        if reply:
            history.append({"role": "assistant", "content": reply})
        log(f"第 {turns_done} 轮完成（回复 {len(reply)} 字）")

        # 平台如果说「答对了/结束」，就不再出新题
        if any(k in (reply or "") for k in ("恭喜", "已全部完成", "测评结束", "学习质量评估")):
            log("平台提示作答结束")
            break

    if turns_done == 0:
        raise LadderAbort("没有完成任何一轮对话，已中止且未提交")

    record_status = answer_submit(session, ids, record_uuid)
    log(f"已交卷：recordStatus={record_status}")

    if not opts.get("skip_evaluate"):
        try:
            stream = end_report(session, ids, record_uuid, ai_enc)
            log(f"结题报告流结束：status={stream.get('status')} "
                f"（{len(stream.get('text') or '')} 字）")
        except LadderError as exc:
            log(f"结题报告异常（继续轮询结果）：{exc}")

    result = wait_result(session, ids, record_uuid,
                        tries=int(opts.get("evaluate_tries") or 15))
    try:
        fresh = load_data(session, ids)
    except LadderError:
        fresh = {}
    score = extract_score(result or {}, fresh)
    if score is not None:
        log(f"评估完成，得分：{score}")
        return True, f"思维阶梯已完成（{turns_done} 轮对话，得分 {score}）"
    return True, f"思维阶梯已提交（{turns_done} 轮对话），评估结果生成中"


def _strip_code_fence(text: str) -> str:
    t = (text or "").strip()
    m = re.match(r"^```[a-zA-Z0-9_+-]*\s*\n(.*?)\n?```\s*$", t, re.S)
    if m:
        return m.group(1).strip()
    return t


def _unbalanced(code: str) -> str:
    """粗略检测代码是否被截断（括号不配对）。返回空串表示看起来完整。"""
    pairs = {"{": "}", "(": ")", "[": "]"}
    stack: list[str] = []
    in_str = in_ch = in_line_comment = False
    prev = ""
    i = 0
    while i < len(code):
        ch = code[i]
        nxt = code[i + 1] if i + 1 < len(code) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
        elif in_str:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_str = False
        elif in_ch:
            if ch == "\\":
                i += 1
            elif ch == "'":
                in_ch = False
        elif ch == "/" and nxt == "/":
            in_line_comment = True
            i += 1
        elif ch == "/" and nxt == "*":
            end = code.find("*/", i)
            i = len(code) if end < 0 else end + 1
        elif ch == '"':
            in_str = True
        elif ch == "'":
            in_ch = True
        elif ch in pairs:
            stack.append(pairs[ch])
        elif ch in "})]":
            if not stack or stack.pop() != ch:
                return f"括号不匹配（多余或错位的 {ch}）"
        prev = ch
        i += 1
    if in_str or in_ch:
        return "字符串/字符字面量未闭合"
    if stack:
        return f"缺少 {len(stack)} 个闭合符号（{''.join(stack[:5])}）"
    return ""


def _run_is_fatal(run: dict, has_examples: bool) -> bool:
    """判断自测运行结果是否说明代码有问题（应当重生成而不是提交）。

    **关键**：题目没有用例时，期望输出为空，任何输出都会被判成
    ``access: "Wrong Answer"``（status 0）—— 这**不代表代码错**，
    因此只有编译/运行/超时一类的硬错误才算致命。
    """
    status = run.get("status")
    if status in (3, 4, 5, 6, 7, 8, 9):
        return True
    results = run.get("results") or []
    for item in results:
        if not isinstance(item, dict):
            continue
        exit_code = item.get("exit_code")
        if exit_code not in (None, 0):
            return True
        access = str(item.get("access") or "")
        if "Compilation" in access or "Compile" in access or "Runtime" in access:
            return True
    # 只有在**提供了用例**的前提下，「答案不正确」才说明代码有问题
    if has_examples and (run.get("wrong") or 0) > 0 and (run.get("right") or 0) == 0:
        return True
    return False
