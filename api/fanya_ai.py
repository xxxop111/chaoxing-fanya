# -*- coding: utf-8 -*-
"""泛雅「AI实践」(planType 15) 读取与模式识别。

AI实践在服务端有 **三种子模式**，完成方式完全不同，因此预览阶段必须先识别出来：

===== ============== ==================================================
type  模式            说明
===== ============== ==================================================
0     情景对话        与 AI 角色对话，完成对话即算完成
1     上传作品        上传图片/文档作为作品，随后提交评估
2     思维阶梯        多步骤引导式作答（Vue SPA）
===== ============== ==================================================

入口页与接口：

* 入口 ``https://mooc2-ans.chaoxing.com/ai-evaluate/v2/answer?courseid=..&clazzid=..&cpi=..&publishRelationUuid=..&__from=taskEngine``
* 读取 ``GET /mooc2-ans/ai-evaluate/v2/answer/load-data``
  ``?courseid=&clazzid=&cpi=&publishRelationUuid=&recordUuid=``
  返回 ``data.type`` / ``data.rules`` / ``data.remainScoreCount`` / ``data.title`` /
  ``data.requirement`` 等。

注意：任务点里的 ``externalDataId`` 是 AI 评估的 ``libraryUuid``，**不是**
``publishRelationUuid``；后者必须从 ``getToStudyUrl`` 返回的 ``url`` 里解析。
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Optional

import requests
from loguru import logger

MOOC_ANS_DOMAIN = "https://mooc2-ans.chaoxing.com"

MODE_CHAT = 0        # 情景对话
MODE_UPLOAD = 1      # 上传作品
MODE_LADDER = 2      # 思维阶梯
MODE_CODE = 4        # 代码实践（服务端渲染的代码编辑器作答页）
# AI剧情：章节 + 节点结构，每个节点是「情景对话」或「上传作品」。
# 作答页路由 /taskChain/evaluate；提交走 JSON（前端 axios 的 aiStory 标志），
# 与模式 0 的表单编码不同。节点级接口尚未探明，故暂未实现自动完成。
MODE_STORY = 7

MODE_LABELS = {
    MODE_CHAT: "情景对话",
    MODE_UPLOAD: "上传作品",
    MODE_LADDER: "思维阶梯",
    MODE_CODE: "代码实践",
    MODE_STORY: "AI剧情",
}


class AiPracticeError(Exception):
    """AI实践接口异常。"""


def mode_label(mode: Any) -> str:
    try:
        return MODE_LABELS.get(int(mode), f"未知模式({mode})")
    except (TypeError, ValueError):
        return "未知模式"


def parse_publish_relation_uuid(study_url: dict) -> str:
    """从 ``getToStudyUrl`` 结果中解析 ``publishRelationUuid``。"""
    url = study_url.get("url") or ""
    if not url:
        return ""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return (query.get("publishRelationUuid") or [""])[0]


def extract_course_ids(study_url: dict, course: dict) -> tuple[str, str, str]:
    """解析 (courseid, clazzid, cpi)。"""
    url = study_url.get("url") or ""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    course_id = (query.get("courseid") or [str(course.get("courseId") or "")])[0]
    class_id = (query.get("clazzid") or [str(course.get("clazzId") or course.get("classId") or "")])[0]
    cpi = (query.get("cpi") or [str(course.get("cpi") or "")])[0]
    return course_id, class_id, cpi


def load_data(session: requests.Session, course_id: str, class_id: str, cpi: str,
              publish_relation_uuid: str, timeout: float = 30.0) -> dict:
    """读取 AI实践任务详情（只读）。"""
    url = f"{MOOC_ANS_DOMAIN}/mooc2-ans/ai-evaluate/v2/answer/load-data"
    params = {
        "courseid": course_id,
        "clazzid": class_id,
        "cpi": cpi,
        "publishRelationUuid": publish_relation_uuid,
        "recordUuid": "",
    }
    try:
        resp = session.get(url, params=params, timeout=timeout,
                           headers={"Referer": f"{MOOC_ANS_DOMAIN}/ai-evaluate/v2/answer"})
    except requests.RequestException as exc:
        raise AiPracticeError(f"读取 AI实践详情失败: {exc}") from exc
    if resp.status_code != 200:
        raise AiPracticeError(f"读取 AI实践详情返回 HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise AiPracticeError(f"读取 AI实践详情返回非 JSON: {resp.text[:200]}") from exc
    if not body.get("status"):
        raise AiPracticeError(
            f"读取 AI实践详情失败: {body.get('errorMsg') or body.get('msg') or body}")
    return body.get("data") or {}


def describe(session: requests.Session, course: dict, study_url: dict,
             timeout: float = 30.0) -> Optional[dict]:
    """识别某个 AI实践任务点的子模式。失败返回 ``None``（不影响整体盘点）。"""
    publish_relation_uuid = parse_publish_relation_uuid(study_url)
    if not publish_relation_uuid:
        return None
    course_id, class_id, cpi = extract_course_ids(study_url, course)
    try:
        data = load_data(session, course_id, class_id, cpi, publish_relation_uuid, timeout)
    except AiPracticeError as exc:
        logger.debug("识别 AI实践模式失败: {}", exc)
        return None

    rules = data.get("rules")
    if isinstance(rules, str):
        try:
            import json
            rules = json.loads(rules)
        except (ValueError, TypeError):
            rules = None

    return {
        "mode": data.get("type"),
        "modeLabel": mode_label(data.get("type")),
        "publishRelationUuid": publish_relation_uuid,
        "courseId": course_id,
        "classId": class_id,
        "cpi": cpi,
        "title": data.get("title") or "",
        "requirement": data.get("requirement") or "",
        "rules": rules or [],
        "remainScoreCount": data.get("remainScoreCount"),
        "answerCountLimit": data.get("answerCountLimit"),
        "entryUrl": study_url.get("url") or "",
    }


def format_rules(rules: list) -> str:
    """把上传规则整理成一句人话，便于在预览界面展示。"""
    if not rules:
        return ""
    parts = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        exts = "/".join(rule.get("extensions") or [])
        max_mb = round((rule.get("maxSizeBytes") or 0) / 1024 / 1024, 1)
        category = rule.get("category") or ""
        parts.append(f"{category} {exts} 最多 {rule.get('maxCount')} 个 / {max_mb}MB")
    return "；".join(parts)
