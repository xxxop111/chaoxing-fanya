# -*- coding: utf-8 -*-
"""泛雅「主题讨论」回复接口封装（groupweb.chaoxing.com）。

协议由浏览器实测得出，关键点：

1. 回复接口
   ``POST https://groupweb.chaoxing.com/pc/invitation/{topicUuid}/addReplys``
   ``Content-Type: application/x-www-form-urlencoded; charset=UTF-8``
   表单字段：courseId/classId/replyId/uuid/topic_content/files_url/files_attr/
   anonymous/urlToken/bbsid，其中 ``replyId=-1`` 表示顶层回复。

2. **topic_content 需要二次 URL 编码**
   页面 JS 先做一次 ``encodeURIComponent(text)``，jQuery 的表单序列化再做一次。
   因此 Python 端要先把文本编码一次，再让表单编码器编码第二次。
   直接把明文交给 ``requests`` 会把字面的 ``%E4%BD%A0...`` 存进帖子。

3. ``urlToken``（32 位 hex）是服务端下发到 ``window.obj.urlToken`` 的，
   并非任何 JS 计算出来的签名，但每次打开详情页都会变，
   所以必须在 POST 之前重新抓取一次。

4. 是否已回复：``GET /pc/invitation/getReplyList`` 翻页查找
   ``createrPuid == 当前用户 puid``（注意是 puid，不是 uid）。
"""

from __future__ import annotations

import re
import time
import urllib.parse
import uuid as _uuid
from typing import Optional

import requests
from loguru import logger

GROUPWEB_DOMAIN = "https://groupweb.chaoxing.com"

# encodeURIComponent 的安全字符集（与 Python quote 默认 safe='/' 不同）
_JS_SAFE = "-_.!~*'()"


def js_encode(value) -> str:
    """等价于 JS 的 encodeURIComponent()。"""
    if value is None:
        return ""
    return urllib.parse.quote(str(value), safe=_JS_SAFE, encoding="utf-8")


class DiscussError(Exception):
    """主题讨论操作失败。"""


def topic_detail_url(bbs_id: str, topic_uuid: str, class_id: str) -> str:
    return (
        f"{GROUPWEB_DOMAIN}/pc/topic/jumpToTopicDetail"
        f"?bbsid={bbs_id}&uuid={topic_uuid}&classId={class_id}"
    )


def _decode_page(resp: requests.Response) -> str:
    """详情页是 GBK 编码；失败时退回 UTF-8。"""
    raw = resp.content
    for enc in ("gbk", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def scrape_url_token(session: requests.Session, bbs_id: str, topic_uuid: str,
                     class_id: str, timeout: float = 30.0) -> str:
    """从话题详情页抓 ``window.obj.urlToken``（每次加载都会变）。"""
    url = topic_detail_url(bbs_id, topic_uuid, class_id)
    resp = session.get(url, headers={"Referer": url}, timeout=timeout)
    resp.raise_for_status()
    text = _decode_page(resp)
    match = re.search(r"urlToken\s*:\s*'([0-9a-fA-F]{16,64})'", text)
    if not match:
        raise DiscussError("未能从话题详情页解析到 urlToken（登录失效或页面结构变更）")
    return match.group(1)


def scrape_topic_context(session: requests.Session, bbs_id: str, topic_uuid: str,
                         class_id: str, timeout: float = 30.0) -> dict:
    """抓取话题详情页里的标题/正文/发帖人等，用于生成更贴题的回复。"""
    url = topic_detail_url(bbs_id, topic_uuid, class_id)
    resp = session.get(url, headers={"Referer": url}, timeout=timeout)
    resp.raise_for_status()
    text = _decode_page(resp)

    def pick(pattern: str) -> str:
        m = re.search(pattern, text, re.S)
        return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""

    return {
        "title": pick(r"topicTitle\s*:\s*'([^']*)'") or pick(r"var\s+topicTitle\s*=\s*'([^']*)'"),
        "content": pick(r"topicContent\s*:\s*'([^']*)'"),
        "urlToken": scrape_url_token(session, bbs_id, topic_uuid, class_id, timeout),
        "url": url,
    }


def get_replies_page(session: requests.Session, bbs_id: str, topic_uuid: str, class_id: str,
                     order: int = 2, last_value: str = "", last_aux_value: str = "",
                     timeout: float = 30.0) -> dict:
    """只读：一页回复。``order=2`` 为时间倒序（最新在前）。"""
    referer = topic_detail_url(bbs_id, topic_uuid, class_id)
    resp = session.get(
        f"{GROUPWEB_DOMAIN}/pc/invitation/getReplyList",
        params={
            "bbsid": bbs_id,
            "uuid": topic_uuid,
            "tag": f"classId{class_id}",
            "order": order,
            "lastValue": last_value,
            "lastAuxValue": last_aux_value,
        },
        headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def has_replied(session: requests.Session, bbs_id: str, topic_uuid: str, class_id: str,
                my_puid: str, max_pages: int = 60) -> tuple[bool, Optional[str], int]:
    """当前用户是否已在本话题下回复过。返回 ``(是否已回复, 楼层, 翻页数)``。"""
    my_puid = str(my_puid)
    last_value = last_aux = ""
    page = 0
    while page < max_pages:
        page += 1
        data = get_replies_page(session, bbs_id, topic_uuid, class_id,
                                order=2, last_value=last_value, last_aux_value=last_aux)
        if not data.get("status"):
            raise DiscussError(f"getReplyList 返回失败: {data.get('msg')!r}")
        items = data.get("datas") or []
        for item in items:
            if str(item.get("createrPuid")) == my_puid:
                return True, item.get("floor"), page
        poff = data.get("poff") or {}
        if not items or poff.get("lastPage") == 1 or poff.get("lastValue") is None:
            break
        last_value = poff.get("lastValue")
        last_aux = poff.get("lastAuxValue")
    return False, None, page


def reply_to_topic(session: requests.Session, bbs_id: str, topic_uuid: str, class_id: str,
                   course_id: str, content: str, reply_id: int = -1,
                   url_token: Optional[str] = None, timeout: float = 30.0) -> dict:
    """向话题发布一条回复。

    ``reply_id=-1`` 为顶层回复（已验证足以让任务点变为已完成）。
    ``content`` 传**明文**，函数内部负责二次编码。
    """
    referer = topic_detail_url(bbs_id, topic_uuid, class_id)
    if not url_token:
        url_token = scrape_url_token(session, bbs_id, topic_uuid, class_id, timeout)

    fields = [
        ("courseId", course_id),
        ("classId", class_id),
        ("replyId", reply_id),
        # 幂等键：每次提交都要新的，服务端用它避免重复提交
        ("uuid", str(_uuid.uuid4())),
        ("topic_content", js_encode(content)),  # 第一层编码
        ("files_url", ""),
        ("files_attr", ""),
        ("anonymous", ""),
        ("urlToken", url_token),
        ("bbsid", bbs_id),
    ]
    # 第二层编码（等价 jQuery.param）
    body = "&".join(f"{js_encode(k)}={js_encode(v)}" for k, v in fields)

    resp = session.post(
        f"{GROUPWEB_DOMAIN}/pc/invitation/{topic_uuid}/addReplys",
        data=body.encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError as exc:
        raise DiscussError(f"回复接口返回非 JSON: {resp.text[:200]}") from exc


def parse_topic_from_study_url(study_url: dict, class_id: str = "") -> dict:
    """从任务点的 ``getToStudyUrl`` 结果中解析出 bbsid / topicUuid / classId。"""
    bbs_id = str(study_url.get("bbsId") or study_url.get("bbsid") or "")
    topic_uuid = str(study_url.get("topicUuid") or study_url.get("uuid") or "")
    cls = str(study_url.get("moocClassId") or class_id or "")
    url = study_url.get("url") or ""
    if url:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        bbs_id = bbs_id or (query.get("bbsid") or [""])[0]
        topic_uuid = topic_uuid or (query.get("uuid") or [""])[0]
        cls = cls or (query.get("classId") or [""])[0]
    return {"bbsId": bbs_id, "topicUuid": topic_uuid, "classId": cls, "url": url}


def fallback_reply_text(question: str, course_title: str = "") -> str:
    """AI 不可用时使用的模板回复，保证仍然切题且语句通顺。"""
    question = (question or "本次讨论题").strip().rstrip("？?")
    return (
        f"关于“{question}”这个问题，我的理解可以分三个层面来说。\n"
        "第一，从概念本身出发，任何结论都要先明确讨论的对象和前提条件，"
        "否则很容易把不同层次的问题混在一起，得出似是而非的结论。\n"
        "第二，结合实际去看，理论只有放到具体情境里才有解释力，"
        "我尝试用课程中学到的分析方法去对照现实中的例子，发现关键变量往往不止一个，"
        "需要区分主要矛盾和次要矛盾。\n"
        "第三，这个问题的价值还在于它提醒我们要保持批判性思维："
        "既要尊重已有的理论框架，也要敢于对前提提出疑问，并在论证中给出自己的依据。\n"
        "综上，我认为不能简单地下一个非此即彼的判断，而应当具体问题具体分析，"
        "在把握基本原理的基础上，结合实际情况灵活运用。"
    )
