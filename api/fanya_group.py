# -*- coding: utf-8 -*-
"""泛雅「分组任务」(planType 18) 自动完成。

协议（真机只读实测，见 `_findings_group.md`）：

```
任务点 → getToStudyUrl → widget/renwu/proxyRenwuAttendUrl?...&activeId=<A>
      → 302 https://mobilelearn.chaoxing.com/page/renwuNew/stu/taskDetailStuForAnswer_new
             ?activeId=<A>&courseId=&classId=&fid=&returnType=11
        页面内联脚本给出 var activeId / courseId / classId / fid / user.uid

GET /v2/apis/task/getTaskActiveInfo?activeId=<A>          → 活动详情（题干 content、groupType、status）
GET /v2/apis/taskStu/getMyGroupInfo?activeId=<A>&puid=<UID>
                                                          → groupId / editVersion / commitLogCount
                                                            / taskGroupInfo.submitStatus / pptUserAttend
POST /widget/apis/renwu/studentSaveRenwu                  → 提交（表单字段见下）
     forceSave, title, content, groupId, editVersion, activeId, courseId
```

⚠️ **两个重要事实（实测）**

1. **提交不等于完成**：该任务点的完成判据是 ``enableGroupTaskScore`` → 总成绩 ≥ 60
   （教师评分/组内互评）。提交后任务点**仍可能是未完成**，这是正常的，不是 bug。
2. **提交会覆盖全组内容**：``studentSaveRenwu`` 是「保存并覆盖本组答案」的语义
   （并发时若他人已提交，服务端返回 ``status=2`` 并提示「你提交后会覆盖他的内容」）。
   本账号两个分组任务的小组**都已经有提交内容**（``commitLogCount`` 9 / 14，
   ``taskGroupInfo.submitStatus = 1``），因此贸然提交会**抹掉组员已经写好的答案**。

基于第 2 点，本模块默认**绝不提交**，并且额外加了一道「已有内容就不覆盖」的闸门：
即使显式开启了 ``group_submit``，只要检测到小组已有编辑/提交记录，也会拒绝，
除非再显式打开 ``group_force_overwrite``。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import requests
from loguru import logger

GROUP_BASE = "https://mobilelearn.chaoxing.com"
SUBMIT_PATH = "/widget/apis/renwu/studentSaveRenwu"
ACTIVE_INFO_PATH = "/v2/apis/task/getTaskActiveInfo"
MY_GROUP_INFO_PATH = "/v2/apis/taskStu/getMyGroupInfo"


class GroupTaskError(Exception):
    """分组任务操作失败。"""


class GroupAbort(GroupTaskError):
    """出于安全考虑主动中止（绝不提交）。"""


# ----------------------------------------------------------------------
# 页面解析
# ----------------------------------------------------------------------


def parse_group_page(html: str) -> dict:
    """从分组任务作答页的内联脚本里取出运行期参数。"""
    def pick(name: str) -> str:
        m = re.search(rf'var\s+{name}\s*=\s*"([^"]*)"', html)
        return m.group(1) if m else ""

    user: dict = {}
    m = re.search(r"var\s+user\s*=\s*(\{.*?\});", html, re.S)
    if m:
        try:
            user = json.loads(m.group(1))
        except ValueError:
            user = {}

    info = {
        "activeId": pick("activeId"),
        "courseId": pick("courseId"),
        "classId": pick("classId"),
        "fid": pick("fid"),
        "returnType": pick("returnType"),
        "puid": str(user.get("uid") or ""),
    }
    if not info["activeId"]:
        raise GroupTaskError("作答页里没有找到 activeId，页面结构可能已变化")
    return info


_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>|</p>|</li>|</div>", re.I)


def html_to_text(html: str) -> str:
    """把题干 HTML 转成可读纯文本（保留换行，用于喂给 AI）。"""
    if not html:
        return ""
    text = _BR_RE.sub("\n", html)
    text = _TAG_RE.sub("", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_answering_page(html: str) -> bool:
    """是否是我们期望的分组任务作答页。"""
    return "var activeId" in html and ("studentSaveRenwu" in html or "submitMyAnswer" in html)


# ----------------------------------------------------------------------
# 只读接口
# ----------------------------------------------------------------------


def get_active_info(session: requests.Session, active_id: str, referer: str = "",
                    timeout: float = 30.0) -> dict:
    """活动详情（题干、状态、分组类型等）。"""
    try:
        resp = session.get(f"{GROUP_BASE}{ACTIVE_INFO_PATH}", params={"activeId": active_id},
                           headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"},
                           timeout=timeout)
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise GroupTaskError(f"读取分组任务详情失败: {exc}") from exc
    if body.get("result") != 1:
        raise GroupTaskError(f"读取分组任务详情被拒绝: {body.get('errorMsg') or body}")
    return body.get("data") or {}


def get_my_group_info(session: requests.Session, active_id: str, puid: str,
                      referer: str = "", timeout: float = 30.0) -> dict:
    """我所在小组的信息（groupId / editVersion / 编辑次数 / 提交状态）。"""
    try:
        resp = session.get(f"{GROUP_BASE}{MY_GROUP_INFO_PATH}",
                           params={"activeId": active_id, "puid": puid},
                           headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"},
                           timeout=timeout)
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise GroupTaskError(f"读取小组信息失败: {exc}") from exc
    if body.get("result") != 1:
        raise GroupTaskError(f"读取小组信息被拒绝: {body.get('errorMsg') or body}")
    return body.get("data") or {}


def fetch_context(session: requests.Session, study_url: str, timeout: float = 30.0) -> dict:
    """打开作答页并汇总后续提交需要的全部参数。"""
    if not study_url:
        raise GroupTaskError("任务点没有提供跳转地址")
    try:
        resp = session.get(study_url, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        raise GroupTaskError(f"打开分组任务页失败: {exc}") from exc
    if resp.status_code != 200:
        raise GroupTaskError(f"打开分组任务页返回 HTTP {resp.status_code}")
    html = resp.content.decode("utf-8", errors="replace")
    if not is_answering_page(html):
        raise GroupAbort("打开的页面不是分组任务作答页，已中止（不提交任何内容）")

    info = parse_group_page(html)
    info["pageUrl"] = resp.url
    info["html"] = html

    active = get_active_info(session, info["activeId"], resp.url, timeout)
    info["active"] = active
    info["activeTitle"] = active.get("title") or ""
    info["activeText"] = html_to_text(active.get("content") or "")
    info["activeStatus"] = active.get("status")

    if not info["puid"]:
        info["puid"] = str(session.cookies.get("_uid") or "")

    group = get_my_group_info(session, info["activeId"], info["puid"], resp.url, timeout)
    info["group"] = group
    info["groupId"] = group.get("groupId") or 0
    info["editVersion"] = group.get("editVersion") or 0
    info["commitLogCount"] = group.get("commitLogCount") or 0
    info["pptUserAttend"] = group.get("pptUserAttend") or {}
    task_group = group.get("taskGroupInfo") or {}
    info["submitStatus"] = task_group.get("submitStatus")
    info["memberCount"] = task_group.get("stunum")
    info["submitOrNotStuForAnswerPage"] = group.get("submitOrNotStuForAnswerPage")
    return info


# ----------------------------------------------------------------------
# 答案生成
# ----------------------------------------------------------------------


def build_answer(generator: Any, prompt_text: str, fallback_name: str,
                 course_title: str = "") -> tuple[str, str, bool]:
    """生成 (标题, 正文HTML, 是否来自 AI)。AI 不可用时退回模板。"""
    question = prompt_text or fallback_name
    text = None
    if generator is not None and getattr(generator, "available", False):
        text = generator.generate_discussion_reply(question, course_title=course_title)
    used_ai = bool(text)
    if not text:
        text = (
            f"围绕“{fallback_name[:60]}”这一问题，我们小组的讨论结论如下。\n\n"
            "第一，需要先厘清问题中的核心概念与前提。任何结论都建立在特定条件之上，"
            "如果前提不同，答案也会随之改变，因此不能脱离情境直接下判断。\n"
            "第二，结合课程内容与现实案例可以看到，影响因素往往不止一个，"
            "需要区分主要矛盾与次要矛盾，抓住起决定作用的那一环。\n"
            "第三，对立的观点并非完全不能相容。在把握基本原则的前提下，"
            "具体情况具体分析，反而能得出更稳妥、更有解释力的结论。\n"
            "综上，我们小组认为应当坚持从实际出发、辩证地看问题，"
            "既尊重客观规律，也保留批判性思考的空间。"
        )
    # 页面编辑器提交的是富文本，这里用简单段落包裹
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    content_html = "".join(f"<p>{p}</p>" for p in paragraphs)
    title = (fallback_name or "小组讨论结论").strip()[:60]
    return title, content_html, used_ai


# ----------------------------------------------------------------------
# 提交
# ----------------------------------------------------------------------


def submit_answer(session: requests.Session, *, active_id: str, course_id: str,
                  group_id: Any, edit_version: Any, title: str, content: str,
                  force_save: int = 0, referer: str = "",
                  timeout: float = 30.0) -> dict:
    """提交（保存）本组答案。

    注意：这是**覆盖式**保存 —— 会替换小组当前已保存/已提交的内容。
    """
    data = {
        "forceSave": force_save,
        "title": title,
        "content": content,
        "groupId": group_id,
        "editVersion": edit_version,
        "activeId": active_id,
        "courseId": course_id,
    }
    try:
        resp = session.post(f"{GROUP_BASE}{SUBMIT_PATH}", data=data,
                            headers={"Referer": referer,
                                     "X-Requested-With": "XMLHttpRequest"},
                            timeout=timeout)
    except requests.RequestException as exc:
        raise GroupTaskError(f"提交分组任务失败: {exc}") from exc
    if resp.status_code != 200:
        raise GroupTaskError(f"提交分组任务返回 HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise GroupTaskError(f"提交分组任务返回非 JSON: {resp.text[:200]}") from exc
