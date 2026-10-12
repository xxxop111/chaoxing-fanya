# -*- coding: utf-8 -*-
"""泛雅「自测(17) / 测验(5)」作答与提交（考试子系统）。

⚠️ **本模块未经真机验证**。原因：本账号唯一两个待完成自测的考试窗口已过期
（`齐次方程` 2026-06-13、`可分离变量的微分方程` 2026-06-10），而进入作答属于
变更性操作，无法在过期考试上验证。协议来自 shipped JS 静态分析 + 第三方参考实现
（详见 `docs/FANYA_TASK_API.md` 6.5 与 `_findings_exam.md`）。

因此实现上采用**默认绝不交卷**的策略：

* 默认只逐题 `tempSave=true`（暂存），**不提交**；
* 只有显式打开 ``options["exam_submit"]`` 才会在最后一题用 `tempSave=false` 交卷；
* 只要出现「题型未知」或「题库没给出答案」，立即中止且**绝不提交**；
* 交卷前把完整表单写入日志。

已确认的协议要点：

```
入口(自测)  POST {task}/userStudyPlan/getSelttestTemplateStatus?encryTaskUserId=&encryptPlanId=
            → data.data == 1 时 data.url 为 lookPaper；== 0 时轮询 getSelftestTaskStatus
入口(测验)  getToStudyUrl 直接给出 examnotes
须知        GET …/exam/test/examcode/examnotes?courseId=&classId=&cpi=&examId=<examRelationId>
作答页      GET …/exam/test/reVersionTestStartNew?…&tId=<examRelationId>&id=<examAnswerId>&…
保存/交卷   POST <页面隐藏域 #saveUrl> + ?classId&courseId&testPaperId&testUserRelationId&cpi&tempSave=
            &pos=<Web 端为空串>&version=1      body = #submitTest 表单序列化
题型        input[name=type<qid>]：0单选 1多选 2填空 3判断 4简答 6论述 11连线 13+结构不明
```

`pos` 不与鼠标事件绑定：Web 端为**空字符串**（官方 JS 里 `getEnc()` 被 `try/catch` 吞掉）。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Any, Optional

import requests
from loguru import logger

TASK_DOMAIN = "https://task.chaoxing.com"

# 已确认可处理的题型 → 提交字段构造方式
#   0 单选  → answer<qid>      值为选项字母
#   1 多选  → answers<qid>     （注意是复数）
#   2 填空  → answerEditor<qid><n> + blankNum<qid>
#   3 判断  → answer<qid>      值 "true"/"false"
#   4 简答 / 6 论述 → answer<qid> 文本
SUPPORTED_TYPES = {0, 1, 2, 3, 4, 6}
TYPE_NAMES = {0: "单选题", 1: "多选题", 2: "填空题", 3: "判断题", 4: "简答题",
              6: "论述题", 11: "连线题", 13: "完型填空", 14: "阅读理解",
              15: "综合题", 16: "其他", 19: "其他"}

# q_info.type（给题库用）→ 考试题型编号
QINFO_FOR_TYPE = {0: "single", 1: "multiple", 2: "completion", 3: "judgement",
                  4: "shortanswer", 6: "shortanswer"}


class ExamError(Exception):
    """考试子系统操作失败。"""


class ExamAbort(ExamError):
    """出于安全考虑主动中止（绝不应导致交卷）。"""


# ----------------------------------------------------------------------
# 入口解析
# ----------------------------------------------------------------------


def resolve_self_test_entry(client: Any, encry_task_user_id: str, encrypt_plan_id: str,
                            poll_rounds: int = 5, poll_delay: float = 3.0) -> str:
    """自测：先取模板状态，必要时轮询组卷，返回 lookPaper 入口 URL。"""
    data = client._post(  # noqa: SLF001 - 复用同一套请求封装
        "/userStudyPlan/getSelttestTemplateStatus",
        params={"encryTaskUserId": encry_task_user_id, "encryptPlanId": encrypt_plan_id},
    )
    payload = data.get("data") or {}
    status = payload.get("data")
    if status == 1:
        return payload.get("url") or ""
    if status != 0:
        raise ExamError(f"自测模板状态异常: {payload}")

    task_id = payload.get("taskId")
    logger.info("自测正在组卷，开始轮询（最多 {} 次）", poll_rounds)
    for i in range(poll_rounds):
        time.sleep(poll_delay)
        r = client._post(  # noqa: SLF001
            "/userStudyPlan/getSelftestTaskStatus",
            params={"encryTaskUserId": encry_task_user_id,
                    "encryptPlanId": encrypt_plan_id, "selftestTaskId": task_id},
        )
        d = r.get("data") or {}
        st = d.get("taskStatus")
        logger.info("组卷状态（{}/{}）：{}", i + 1, poll_rounds, st)
        if st == "ok":
            return d.get("url") or ""
        if st not in ("running", "busy", None, ""):
            raise ExamError(f"组卷失败: {st}")
    raise ExamError("组卷超时，请稍后在页面上重试")


# ----------------------------------------------------------------------
# 页面解析
# ----------------------------------------------------------------------

_HIDDEN_INPUT = re.compile(
    r'<input[^>]*?(?:id|name)\s*=\s*["\']([^"\']+)["\'][^>]*?value\s*=\s*["\']([^"\']*)["\']',
    re.I)
_HIDDEN_INPUT_REV = re.compile(
    r'<input[^>]*?value\s*=\s*["\']([^"\']*)["\'][^>]*?(?:id|name)\s*=\s*["\']([^"\']+)["\']',
    re.I)


def parse_hidden_inputs(html: str) -> dict:
    """把页面里的隐藏域抓成字典（id 或 name 作键）。"""
    fields: dict[str, str] = {}
    for key, value in _HIDDEN_INPUT.findall(html):
        fields.setdefault(key, value)
    for value, key in _HIDDEN_INPUT_REV.findall(html):
        fields.setdefault(key, value)
    return fields


def looks_like_answering_page(html: str) -> bool:
    """作答页特征：有 #saveUrl 或 reVersion 提交相关隐藏域 / 题目容器。"""
    return ("saveUrl" in html and "submitTest" in html) or "questionLi" in html


def parse_answering_page(html: str) -> dict:
    """解析作答页：提交地址、时效令牌、当前题号与题型、选项。"""
    fields = parse_hidden_inputs(html)
    for required in ("saveUrl", "testPaperId", "testUserRelationId", "courseId", "classId"):
        if not fields.get(required):
            raise ExamAbort(f"作答页缺少必需隐藏域 {required}，拒绝继续（避免误提交）")

    qid = fields.get("questionId") or ""
    if not qid:
        m = re.search(r"questionId\s*[=:]\s*['\"]?(\d+)", html)
        qid = m.group(1) if m else ""

    qtype = None
    if qid:
        raw = fields.get(f"type{qid}")
        if raw is None:
            m = re.search(rf'name=["\']type{qid}["\'][^>]*value=["\'](\d+)["\']', html)
            raw = m.group(1) if m else None
        if raw is not None:
            try:
                qtype = int(raw)
            except (TypeError, ValueError):
                qtype = None

    # 选项：形如 <li>A. xxx</li> 或 <label>...</label>，尽量宽松
    options = re.findall(r'<li[^>]*>\s*([A-Z])[\.、]\s*(.*?)</li>', html, re.S)
    option_lines = []
    for letter, text in options:
        clean = re.sub(r"<[^>]+>", "", text)
        clean = re.sub(r"\s+", " ", clean).strip()
        if clean:
            option_lines.append(f"{letter}. {clean}")

    title = ""
    m = re.search(r'<h3[^>]*class="[^"]*mark_name[^"]*"[^>]*>(.*?)</h3>', html, re.S)
    if m:
        title = re.sub(r"<[^>]+>", "", m.group(1))
        title = re.sub(r"\s+", " ", title).strip()
    # 公式：LaTeX 藏在 img[data]（URL 编码），必须解码后拼回题干
    latex = re.findall(r'class="[^"]*ans-latex-moudle[^"]*"[^>]*data="([^"]+)"', html)
    if not latex:
        latex = re.findall(r'data="(%22[^"]+)"', html)
    decoded = []
    for item in latex[:4]:
        try:
            decoded.append(urllib.parse.unquote(item).strip('"'))
        except Exception:  # noqa: BLE001
            continue
    if decoded:
        title = f"{title} [公式: {' ; '.join(decoded)}]".strip()

    return {
        "fields": fields,
        "saveUrl": fields["saveUrl"],
        "testPaperId": fields["testPaperId"],
        "testUserRelationId": fields["testUserRelationId"],
        "courseId": fields["courseId"],
        "classId": fields["classId"],
        "cpi": fields.get("cpi") or "",
        "openc": fields.get("openc") or "",
        "questionId": qid,
        "questionType": qtype,
        "title": title,
        "options": option_lines,
    }


# ----------------------------------------------------------------------
# 保存 / 提交
# ----------------------------------------------------------------------


def build_q_info(page: dict) -> Optional[dict]:
    """把作答页解析结果转成题库需要的 q_info。题型未知则返回 None。"""
    qtype = page.get("questionType")
    if qtype not in QINFO_FOR_TYPE or not page.get("title"):
        return None
    return {
        "title": page["title"],
        "type": QINFO_FOR_TYPE[qtype],
        "options": page["options"],
    }


def build_answer_form(page: dict, answer: str) -> dict:
    """按题型构造要提交的表单字段。"""
    qtype = page.get("questionType")
    qid = page.get("questionId")
    form = dict(page.get("fields") or {})
    for noisy in ("questionId", "tempSave"):
        form.pop(noisy, None)

    if qtype in (0, 3, 4, 6):            # 单选 / 判断 / 简答 / 论述
        form[f"answer{qid}"] = answer
    elif qtype == 1:                     # 多选：注意字段名是复数 answers
        form[f"answers{qid}"] = answer
    elif qtype == 2:                     # 填空：按空拆分
        blanks = [b.strip() for b in re.split(r"[;；\n]+", answer) if b.strip()]
        if not blanks:
            blanks = [answer.strip()]
        for idx, text in enumerate(blanks, start=1):
            form[f"answerEditor{qid}{idx}"] = text
        form[f"blankNum{qid}"] = ",".join(str(i) for i in range(1, len(blanks) + 1)) + ","
    else:
        raise ExamAbort(f"题型 {qtype}（{TYPE_NAMES.get(qtype, '未知')}）不支持，已中止")
    return form


def save_or_submit(session: requests.Session, page: dict, form: dict,
                   is_submit: bool, timeout: float = 30.0) -> dict:
    """保存或交卷。``is_submit=False`` 时 ``tempSave=true``（仅暂存）。"""
    params = {
        "classId": page.get("classId"),
        "courseId": page.get("courseId"),
        "testPaperId": page.get("testPaperId"),
        "testUserRelationId": page.get("testUserRelationId"),
        "cpi": page.get("cpi"),
        "tempSave": "false" if is_submit else "true",
        # Web 端 pos 为空串（官方 JS 里 getEnc() 未定义，被 try/catch 吞掉）
        "pos": "",
        "version": "1",
    }
    url = page["saveUrl"]
    if not url.startswith("http"):
        url = "https://mooc1.chaoxing.com" + ("" if url.startswith("/") else "/") + url
    try:
        resp = session.post(url, params=params, data=form,
                            headers={"Referer": page.get("entryUrl", url),
                                     "X-Requested-With": "XMLHttpRequest"},
                            timeout=timeout)
    except requests.RequestException as exc:
        raise ExamError(f"提交请求失败: {exc}") from exc
    if resp.status_code != 200:
        raise ExamError(f"提交返回 HTTP {resp.status_code}: {resp.text[:160]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ExamError(f"提交返回非 JSON: {resp.text[:200]}") from exc
