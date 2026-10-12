# -*- coding: utf-8 -*-
"""泛雅「自测(17) / 测验(5)」读取与状态识别。

这两个类型走的是**考试子系统**（`mooc1.chaoxing.com/mooc-ans/exam/*` 与
`exam-ans`），与章节检测（`mooc-ans/api/work`）完全不同，无法复用 `study_work`。

自测需要两段式取入口：

```
POST {task}/userStudyPlan/getSelttestTemplateStatus?encryTaskUserId=&encryptPlanId=   # 注意拼写 Selttest
  → data.data == 1 试卷已生成，data.url 给出 lookPaper 页
  → data.data == 0 组卷中，轮询 /userStudyPlan/getSelftestTaskStatus?...&selftestTaskId=
```

而 ``lookPaper`` 是**只读的「查看详情」页，包含整份试卷**，因此可以安全地用它来判断
考试窗口是否已经过期 —— 这对用户很重要：**过期的自测无法通过任何接口补完成**
（`scoreRefresh` 只是把 mooc 侧已合格的成绩同步回来，不是「补完成」按钮）。
"""

from __future__ import annotations

import re
from typing import Any, Optional

import requests
from loguru import logger

TASK_DOMAIN = "https://task.chaoxing.com"

_EXPIRED_MARKERS = ("考试已过期", "已过期", "考试已结束", "该考试已结束")


class QuizInfoError(Exception):
    """自测/测验信息读取失败。"""


def _post(client: Any, path: str, params: dict) -> dict:
    return client._post(path, params=params)  # noqa: SLF001 - 复用同一套请求封装


def get_self_test_url(client: Any, encry_task_user_id: str, encrypt_plan_id: str,
                      timeout: float = 25.0) -> str:
    """取自测真正的入口 URL（lookPaper）。返回空串表示拿不到。"""
    data = _post(client, "/userStudyPlan/getSelttestTemplateStatus", {
        "encryTaskUserId": encry_task_user_id,
        "encryptPlanId": encrypt_plan_id,
    })
    payload = data.get("data") or {}
    status = payload.get("data")
    if status == 1:
        return payload.get("url") or ""
    if status == 0:
        logger.info("自测正在组卷中（planId={}），需稍后再试", encrypt_plan_id)
        return ""
    return ""


def parse_self_test_page(text: str) -> dict:
    """从 ``lookPaper`` 页 HTML 中解析考试窗口/题量/是否过期（纯函数，便于自测）。"""
    expired = any(marker in text for marker in _EXPIRED_MARKERS)

    window = ""
    m = re.search(r"考试时间[：:]\s*([0-9\-: ]+?)\s*至\s*([0-9\-: ]+)", text)
    if m:
        window = f"{m.group(1).strip()} ~ {m.group(2).strip()}"

    count = ""
    m = re.search(r"题量[：:]\s*(\d+)", text)
    if m:
        count = m.group(1)

    parts = []
    if expired:
        parts.append("考试已过期，无法补完成（需联系老师）")
    if window:
        parts.append(f"考试时间 {window}")
    if count:
        parts.append(f"题量 {count}")
    return {"expired": expired, "window": window, "questionCount": count,
            "summary": "；".join(parts)}


def describe_self_test(client: Any, encry_task_user_id: str, encrypt_plan_id: str,
                       timeout: float = 25.0) -> Optional[dict]:
    """读取自测的考试窗口与过期状态（只读）。

    返回 ``{"expired", "window", "entryUrl", "summary"}``；读取失败返回 ``None``。
    """
    try:
        entry_url = get_self_test_url(client, encry_task_user_id, encrypt_plan_id, timeout)
    except Exception as exc:  # noqa: BLE001 - 识别失败不应影响盘点
        logger.debug("读取自测模板状态失败: {}", exc)
        return None
    if not entry_url:
        return None

    try:
        resp = client.session.get(
            entry_url,
            headers={"Referer": f"{TASK_DOMAIN}/userStudyPlan/myPlanList"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.debug("打开自测详情页失败: {}", exc)
        return None
    if resp.status_code != 200:
        return None

    text = resp.content.decode("utf-8", errors="replace")
    parsed = parse_self_test_page(text)
    parsed["entryUrl"] = entry_url
    return parsed


def describe_exam(client: Any, study_url: dict) -> Optional[dict]:
    """测验(5)：``getToStudyUrl`` 直接给出 examnotes 入口。"""
    url = study_url.get("url") or ""
    if not url:
        return None
    return {
        "expired": False,
        "window": "",
        "questionCount": "",
        "entryUrl": url,
        "summary": "走考试子系统（exam-ans）；已有作答适配器（默认只逐题暂存、不交卷），但该链路未真机验证",
    }
