# -*- coding: utf-8 -*-
"""泛雅「作业」(planType 4) 任务点自动完成 —— mooc2 课程作业。

**与章节检测作业不是同一套入口**（实测）：

```
任务点跳转  https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?courseId=&classId=&cpi=&workId=&enc=
   ↓ 302
作答页      https://mooc1.chaoxing.com/mooc-ans/mooc2/work/dowork?…&answerId=&standardEnc=&enc=
   ↓ <form action="/mooc-ans/work/addStudentWorkNewWeb?_classId=&courseid=&token=&totalQuestionNum=&wMicroNodeId=">
提交        该 form 的全部字段 + 每题的 answer 字段
```

章节检测作业走的是 ``/mooc-ans/api/work`` + ``work/addStudentWorkNew`` + ``jobid=work-<uuid>``；
课程作业的 ``jobid`` 为空、``knowledgeid=0``，因此**不能**复用 ``Chaoxing.study_work``。

题目 DOM 也与章节作业不同（``decode_questions_info`` 在本页直接抛
``AttributeError``）：题干在 ``div.singleQuesId`` 上，题型由
``input[name=answertype<qid>]`` 给出（0单选/1多选/2填空/3判断/4简答），
答案字段是 ``answer<qid>``（选择题）或 ``textarea#answer<qid>``（简答题）。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

import requests
from bs4 import BeautifulSoup
from loguru import logger

from api.decode import _extract_title, _get_question_type

MOOC1_DOMAIN = "https://mooc1.chaoxing.com"


class WorkError(Exception):
    """课程作业处理失败。"""


def parse_work_page(html: str) -> dict:
    """解析课程作业作答页。

    返回 ``{"action", "formData", "questions": [{id, typeCode, type, title,
    options, answerFields}]}``。纯函数，便于离线自测。
    """
    soup = BeautifulSoup(html, "lxml")
    form = soup.find("form")
    if form is None:
        raise WorkError("课程作业页面里没有找到提交表单")

    action = form.get("action") or ""
    # 收集表单级字段。注意：不能简单地「名字里含 answer 就跳过」——
    # ``answerwqbid``（题目 id 列表）名字里就含 answer，但它恰恰是必填项，
    # 漏掉它会直接返回「无效的参数：code-1」。
    form_data: dict[str, str] = {}
    per_question_answer = re.compile(r"^answer\d+(_\d+)?$")
    for tag in form.find_all("input"):
        name = tag.get("name")
        if not name or name.startswith("answertype") or per_question_answer.match(name):
            continue
        form_data[name] = tag.get("value", "")

    questions = []
    for block in form.find_all("div", class_="singleQuesId"):
        qid = block.get("data") or re.sub(r"\D", "", block.get("id") or "")
        if not qid:
            continue

        type_input = block.find("input", attrs={"name": f"answertype{qid}"})
        type_code = str(type_input.get("value")) if type_input else ""

        # 选项：章节作业用 ul.mark_letter > li
        ul = block.find("ul")
        options = []
        if ul is not None:
            options = [re.sub(r"\s+", " ", li.get_text(" ", strip=True)).strip()
                       for li in ul.find_all("li")]
            options = [o for o in options if o]

        # 题干：去掉选项与作答区后的剩余内容（保留图片以支持 OCR）
        stem = BeautifulSoup(str(block), "lxml")
        for node in stem.find_all("ul"):
            node.decompose()
        for node in stem.find_all("div", class_="stem_answer"):
            node.decompose()
        for node in stem.find_all("script"):
            node.decompose()
        # 题干：整块内容（含 h3 题号、正文段落与图片），已剔除选项与作答区。
        # 不能只取 h3 —— 简答题/公式题的真正题干常常是紧随其后的 <p><img></p>。
        title = _extract_title(stem)
        h3 = stem.find("h3")
        if h3 is not None and not title.strip():
            title = _extract_title(h3)

        # 答案字段：input[name^=answer] 与 textarea[name^=answer]
        answer_fields = {}
        for tag in block.find_all(["input", "textarea"]):
            name = tag.get("name") or ""
            if name.startswith("answer") and not name.startswith("answertype"):
                answer_fields[name] = tag.get("value", "") or ""
            elif name.startswith("answertype"):
                answer_fields[name] = tag.get("value", "")

        questions.append({
            "id": qid,
            "typeCode": type_code,
            "type": _get_question_type(type_code),
            "title": title,
            "options": options,
            "answerFields": answer_fields,
        })

    return {"action": action, "formData": form_data, "questions": questions,
            "answerwqbid": ",".join(q["id"] for q in questions) + ","}


def _normalise_letters(answer: str) -> str:
    """把题库答案规整成选项字母串（如 "A"、"AC"）。"""
    letters = re.findall(r"[A-Za-z]", answer or "")
    return "".join(sorted({x.upper() for x in letters}))


def build_submission(parsed: dict, tiku: Any) -> tuple[dict, int, int, list]:
    """按题目查询题库并拼出提交字段。

    返回 ``(提交字段, 命中题数, 总题数, 逐题明细)``。未命中的题目保持空答案。
    """
    payload = dict(parsed["formData"])
    # 必填：题目 id 列表（缺失会被服务端判为「无效的参数」）
    if parsed.get("answerwqbid"):
        payload["answerwqbid"] = parsed["answerwqbid"]
    questions = parsed["questions"]
    found = 0
    details: list[dict] = []

    for q in questions:
        qid = q["id"]
        answer: Optional[str] = None
        if tiku is not None:
            try:
                answer = tiku.query({
                    "title": q["title"],
                    "type": q["type"],
                    "options": "\n".join(q["options"]),
                })
            except Exception as exc:  # noqa: BLE001 - 单题失败不应中断
                logger.debug("题库查询失败（{}）: {}", qid, exc)

        # 题库有时会把「我看不到题目」这类**拒答/元回复**当成答案返回。
        # 这种文本绝不能写进作业（曾经真的把「请提供题目的文字描述」交上去过）。
        if answer and _is_refusal(str(answer)):
            logger.warning("题库对题目 {} 返回了拒答文本，按未命中处理: {}",
                           qid, str(answer)[:60])
            details.append({"qid": qid, "type": q["type"], "title": q["title"],
                            "answer": None, "answered": False, "reason": "题库拒答"})
            answer = None

        if answer:
            found += 1
        # 无论是否命中都要带上字段，否则表单校验可能不通过；
        # 注意这里要保留原始值（例如 answertype<qid>=4），不能用空串覆盖。
        for name, original in q["answerFields"].items():
            payload.setdefault(name, original)

        if not answer:
            details.append({"qid": qid, "type": q["type"], "title": q["title"],
                            "answer": None, "answered": False, "reason": "未命中"})
            continue

        if q["type"] in ("single", "judgement", "multiple"):
            if q["type"] == "judgement":
                # ⚠️ 课程作业的判断题取值是字符串 "true"/"false"（页面 JS：
                # itemAnswer == "true" ? true : false）。这里曾经写成「对/错」，
                # 服务端表单校验会直接拒绝整份提交 —— 实测表现为提交请求 ~50ms
                # 内失败，且该作业恰好全是判断题。
                value = "true" if str(answer).strip() in (
                    "正确", "对", "T", "t", "true", "TRUE", "√", "是", "1") else "false"
            else:
                value = _normalise_letters(answer) or str(answer).strip()
            for name in q["answerFields"]:
                if name.startswith("answer") and not name.startswith("answertype"):
                    payload[name] = value
        elif q["type"] == "completion":
            parts = re.split(r"[\s,，、;；]+", str(answer).strip())
            keys = [n for n in q["answerFields"]
                    if n.startswith("answer") and not n.startswith("answertype")]
            for idx, key in enumerate(sorted(keys)):
                if idx < len(parts):
                    payload[key] = parts[idx]
        else:  # shortanswer 及其它：整段文本写入第一个 answer 字段
            for name in q["answerFields"]:
                if name.startswith("answer") and not name.startswith("answertype"):
                    payload[name] = str(answer).strip()
                    break
        details.append({"qid": qid, "type": q["type"], "title": q["title"],
                        "answer": str(answer).strip(), "answered": True})

    rate = (found / len(questions)) if questions else 0.0
    logger.info("课程作业共 {} 题，题库命中 {} 题（覆盖率 {:.0%}）",
                len(questions), found, rate)
    return payload, found, len(questions), details


# 题库把「我看不到题目」这类回复当成答案时的识别词
_REFUSAL_MARKERS = (
    "无法看到", "无法查看", "看不到", "无法识别", "看不清", "无法读取",
    "请提供题目", "请提供文字", "请重新上传", "重新上传清晰", "题目图片",
    "图片链接", "无法解答", "无法回答", "我无法", "抱歉我",
    "作为一个AI", "作为AI", "没有具体的题目", "没有题目文字",
)


def _is_refusal(text: str) -> bool:
    """判断题库返回的是不是「拒答 / 元回复」而不是真正的答案。"""
    if not text:
        return False
    stripped = text.strip()
    # 真正的答案很少会这么长又全是这种口吻；命中任意标记即视为拒答
    return any(marker in stripped for marker in _REFUSAL_MARKERS)


def submit_work(session: requests.Session, action: str, payload: dict,
                referer: str = "", timeout: float = 30.0) -> tuple[bool, str]:
    """提交（或保存）课程作业。

    页面自己的 ``saveAnswer`` / ``confirmSubmitWork`` 都会把这几项拼到 action 后面
    （``ua=pc&formType=post&saveStatus=1&version=1``），缺了会被服务端判为
    「无效的提交，参数异常」。提交与保存共用同一套参数，区别只在表单里的
    ``pyFlag``（``1``=保存草稿，空=正式提交）。

    注意：正式提交会把答案真实写入作业记录，不可撤销。
    """
    url = action if action.startswith("http") else f"{MOOC1_DOMAIN}{action}"
    separator = "&" if "?" in url else "?"
    url = f"{url}{separator}ua=pc&formType=post&saveStatus=1&version=1"

    headers = {
        "Referer": referer or url,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": MOOC1_DOMAIN,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    try:
        resp = session.post(url, data=payload, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"提交作业请求失败: {exc}"
    if resp.status_code != 200:
        return False, f"提交作业返回 HTTP {resp.status_code}"
    try:
        body = resp.json()
    except ValueError:
        return False, f"提交作业返回非 JSON: {resp.text[:200]}"
    if body.get("status"):
        return True, str(body.get("msg") or "提交成功")
    return False, str(body.get("msg") or body)


def check_work_gate(session: requests.Session, html: str, referer: str,
                    timeout: float = 30.0) -> tuple[bool, str]:
    """提交前的 ``/mooc-ans/work/validate`` 闸门。

    返回 ``(是否可继续, 说明)``。页面自己提交前也会调这个接口，因此必须复现，
    否则行为与浏览器不一致：

    * ``1`` = 账号异常 → 放弃
    * ``2`` = 需要验证码 → 无法全自动完成（会把验证码图片地址提示出来）
    * ``3`` = 可直接提交
    """
    fields = parse_hidden_inputs(html)
    course_id = fields.get("courseId") or fields.get("courseid")
    class_id = fields.get("classId") or fields.get("clazzid") or fields.get("clazzId")
    cpi = fields.get("cpi")
    if not (course_id and class_id and cpi):
        # 拿不到参数就不阻断（老版本页面可能没有这个闸门）
        logger.debug("作业页缺少 courseId/classId/cpi，跳过 validate 闸门")
        return True, ""

    url = "https://mooc1.chaoxing.com/mooc-ans/work/validate"
    try:
        resp = session.get(url, params={"courseId": course_id, "classId": class_id,
                                        "cpi": cpi},
                           headers={"Referer": referer,
                                    "X-Requested-With": "XMLHttpRequest"},
                           timeout=timeout)
        status = (resp.json() or {}).get("status")
    except Exception as exc:  # noqa: BLE001 - 闸门不可用时不应阻断主流程
        logger.debug("调用作业 validate 闸门失败（忽略）: {}", exc)
        return True, ""

    if status == 3:
        return True, ""
    if status == 2:
        return False, ("该作业提交需要输入验证码，无法自动完成"
                       "（验证码图片：/mooc-ans/verifyCode/stuWork），请手动提交")
    if status == 1:
        return False, "作业提交被拒绝：账号状态异常，请手动在网页上提交"
    return True, ""


def parse_hidden_inputs(html: str) -> dict:
    """抓取页面隐藏域（id 或 name 作键）。"""
    fields: dict[str, str] = {}
    for key, value in re.findall(
            r'<input[^>]*?(?:id|name)\s*=\s*["\']([^"\']+)["\'][^>]*?value\s*=\s*["\']([^"\']*)["\']',
            html, re.I):
        fields.setdefault(key, value)
    for value, key in re.findall(
            r'<input[^>]*?value\s*=\s*["\']([^"\']*)["\'][^>]*?(?:id|name)\s*=\s*["\']([^"\']+)["\']',
            html, re.I):
        fields.setdefault(key, value)
    return fields


def complete_course_work(session: requests.Session, entry_url: str, tiku: Any, *,
                         save_only: bool = False, cover_rate: float = 0.0,
                         on_log: Optional[Callable] = None, timeout: float = 30.0
                         ) -> tuple[bool, str]:
    """完成一个课程作业任务点。

    ``save_only=True``（对应题库设置 ``submit=1``）只保存草稿不提交；
    ``cover_rate`` 是最低命中率（0~1 的小数），未达到时同样只保存不提交，
    避免把大片空白答案交上去。
    """
    log = on_log or (lambda msg: logger.info(msg))

    try:
        resp = session.get(entry_url, headers={"Referer": entry_url}, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"打开作业页失败: {exc}"
    if resp.status_code != 200:
        return False, f"打开作业页返回 HTTP {resp.status_code}"

    html = resp.content.decode("utf-8", errors="replace")
    final_url = resp.url
    if "已过期" in html or "已结束" in html:
        return False, "该作业已过期或已结束"

    # 已提交的作业会 302 到 mooc2/work/view（「作业详情」，没有作答表单）
    if "work/view" in final_url or "作业详情" in html:
        return False, "该作业已经提交过了，无需重复作答"

    try:
        parsed = parse_work_page(html)
    except WorkError as exc:
        # 没有作答表单通常意味着已提交 / 已批阅
        if "作业详情" in html or "查看" in html and "重做" in html:
            return False, "该作业已经提交过了，无需重复作答"
        return False, str(exc)

    # 提交前闸门：页面自身在提交前会调用 /mooc-ans/work/validate。
    # 1=账号异常（放弃）  2=需要验证码（无法全自动）  3=可直接提交
    gate_ok, gate_msg = check_work_gate(session, html, final_url, timeout)
    if not gate_ok:
        return False, gate_msg

    questions = parsed["questions"]
    if not questions:
        return False, "未在作业页解析到任何题目"

    prepared = build_prepared(parsed, final_url, tiku, cover_rate=cover_rate, log=log)
    if prepared["total"] == 0:
        return False, "作业页没有题目"

    if save_only:
        prepared["do_submit"] = False
        log("题库设置为「只保存不提交」，本次不提交")

    ok, msg = submit_prepared(session, prepared, timeout=timeout)
    if not ok:
        return False, msg
    return True, msg


def build_prepared(parsed: dict, referer: str, tiku: Any, *, cover_rate: float = 0.0,
                   log: Optional[Callable] = None) -> dict:
    """把「读到的题目 + 题库答案」整理成一个可提交/可送审的载荷（不做任何写操作）。"""
    log = log or (lambda m: logger.info(m))
    payload, found, total, details = build_submission(parsed, tiku)
    rate = (found / total) if total else 0.0
    if cover_rate and rate < cover_rate:
        log(f"题库覆盖率 {rate:.0%} 低于阈值 {cover_rate:.0%}，将只保存不提交")
    return {
        "action": parsed["action"],
        "referer": referer,
        "payload": payload,
        "found": found,
        "total": total,
        "rate": rate,
        "details": details,
        # 覆盖率不足时默认不提交；是否提交最终由调用方（含审批）决定
        "do_submit": not (cover_rate and rate < cover_rate),
        "answerwqbid": parsed.get("answerwqbid", ""),
    }


def summarize_prepared(prepared: dict, limit: int = 6) -> str:
    """把送审载荷整理成人看的预览文本。"""
    lines = [f"共 {prepared['total']} 题，题库命中 {prepared['found']} 题"
             f"（覆盖率 {prepared['rate']:.0%}）"]
    for row in prepared.get("details", [])[:limit]:
        mark = "✓" if row.get("answered") else "✗"
        answer = (row.get("answer") or "(未命中)").replace("\n", " ")
        lines.append(f"{mark} [{row.get('type')}] {row.get('title', '')[:60]} → {answer[:60]}")
    if len(prepared.get("details", [])) > limit:
        lines.append(f"… 其余 {len(prepared['details']) - limit} 题见日志")
    return "\n".join(lines)


def submit_prepared(session: requests.Session, prepared: dict, *,
                    force_submit: Optional[bool] = None,
                    timeout: float = 30.0) -> tuple[bool, str]:
    """提交（或保存）已经准备好的作业载荷。

    ``force_submit`` 为 ``None`` 时沿用 ``prepared["do_submit"]``；
    为 ``True`` 表示用户已审批通过，正式提交（``pyFlag`` 置空）。
    """
    do_submit = prepared["do_submit"] if force_submit is None else bool(force_submit)
    payload = dict(prepared["payload"])
    # pyFlag=1 表示「保存」，空串表示「提交」（与项目既有 study_work 一致）
    payload["pyFlag"] = "" if do_submit else "1"
    ok, msg = submit_work(session, prepared["action"], payload,
                          referer=prepared.get("referer", ""), timeout=timeout)
    if not ok:
        return False, f"{'提交' if do_submit else '保存'}作业失败: {msg}"
    return True, (f"作业已提交（{prepared['total']} 题，命中 {prepared['found']} 题）"
                  if do_submit else
                  f"作业已保存为草稿（{prepared['total']} 题，命中 {prepared['found']} 题）")


def prepare_course_work(session: requests.Session, entry_url: str, tiku: Any, *,
                        cover_rate: float = 0.0, on_log: Optional[Callable] = None,
                        timeout: float = 30.0) -> tuple[bool, str, dict]:
    """**只准备、不写任何东西**：读题 + 查题库，返回送审载荷。

    用于「生成后暂停、逐条审批」流程 —— 在用户批准之前，既不保存草稿也不提交。
    """
    log = on_log or (lambda m: logger.info(m))
    try:
        resp = session.get(entry_url, headers={"Referer": entry_url}, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"打开作业页失败: {exc}", {}
    if resp.status_code != 200:
        return False, f"打开作业页返回 HTTP {resp.status_code}", {}

    html = resp.content.decode("utf-8", errors="replace")
    final_url = resp.url
    if "已过期" in html or "已结束" in html:
        return False, "该作业已过期或已结束", {}
    if "work/view" in final_url or "作业详情" in html:
        return False, "该作业已经提交过了，无需重复作答", {}
    try:
        parsed = parse_work_page(html)
    except WorkError as exc:
        return False, str(exc), {}

    if not parsed["questions"]:
        return False, "未在作业页解析到任何题目", {}

    gate_ok, gate_msg = check_work_gate(session, html, final_url, timeout)
    if not gate_ok:
        return False, gate_msg, {}

    prepared = build_prepared(parsed, final_url, tiku, cover_rate=cover_rate, log=log)
    prepared["entry_url"] = entry_url
    return True, summarize_prepared(prepared), prepared
