# -*- coding: utf-8 -*-
"""泛雅「任务」模块（任务引擎 / task.chaoxing.com）自动化支持。

本模块补齐了原始项目只处理**章节任务点**的空白：泛雅课程页左侧的「任务」
标签实际由独立的任务引擎提供服务（task.chaoxing.com），其中包含
`AI实践`（AI 对话挑战）、`主题讨论` 等需要交互才能完成的任务点。

接口来源（浏览器实测，见 `docs/FANYA_TASK_API.md`）：

课程 → 任务列表
    POST {task}/userStudyTask/getData                     {page,searchKey,pageSize,courseId,moocClassId}
    POST {task}/userStudyTask/getTaskSpeedOfProgress      ?encryTaskUserId=
    POST {task}/userStudyTask/isAllowStudyTask            ?encryTaskUserId=
任务 → 分组 → 任务点
    POST {task}/userStudyPlan/getGroupData                ?encryTaskUserId=
    POST {task}/userStudyPlan/getPlanDataByGroupId        ?encryTaskUserId=&encryGroupId=
    POST {task}/userStudyPlan/getToStudyUrl               ?encryptPlanId=&encryTaskUserId=&studyJumpType=0&isInterface=false
    POST {task}/userStudyPlan/scoreRefresh                ?encryTaskUserId=&encryptPlanId=

`planType` 取值（`planTypeName` 由接口直接返回，这里只用于分发）：
    14 = 主题讨论, 15 = AI实践, 10 = 视频, 11 = 文档, 22 = 音频,
    4 = 作业, 5 = 考试, 8 = 章节, 9 = 签到, 17 = 自测
"""

from __future__ import annotations

import datetime
import json
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests
from loguru import logger

from api.base import SessionManager
from api.config import GlobalConst as gc

TASK_DOMAIN = "https://task.chaoxing.com"
MOOC_ANS_DOMAIN = "https://mooc2-ans.chaoxing.com"
GROUPWEB_DOMAIN = "https://groupweb.chaoxing.com"
MOBILELEARN_DOMAIN = "https://mobilelearn.chaoxing.com"

# 任务点类型 → 适配器名。未列出的类型只做展示，不做自动完成。
PLAN_TYPE_AI_PRACTICE = 15
PLAN_TYPE_DISCUSSION = 14
PLAN_TYPE_VIDEO = 10
PLAN_TYPE_DOCUMENT = 11
PLAN_TYPE_AUDIO = 22
PLAN_TYPE_WORK = 4
PLAN_TYPE_EXAM = 5
PLAN_TYPE_CHAPTER = 8
PLAN_TYPE_SIGN = 9
PLAN_TYPE_QUIZ = 17

# 只需由「章节学习」既有流程负责的类型，任务模块内不重复处理
CHAPTER_MANAGED_TYPES = {0, 1, 8, 10, 11, 22}

PLAN_TYPE_LABELS = {
    0: "课程",
    1: "知识点",
    4: "作业",
    5: "考试",
    8: "章节",
    9: "签到",
    10: "视频",
    11: "文档",
    13: "自测",
    14: "主题讨论",
    15: "AI实践",
    17: "自测",
    22: "音频",
}


class FanyaTaskError(Exception):
    """任务引擎接口返回异常。"""


def _plan_type_label(plan_type: Any, fallback: str = "") -> str:
    if fallback:
        return fallback
    try:
        return PLAN_TYPE_LABELS.get(int(plan_type), f"未知类型({plan_type})")
    except (TypeError, ValueError):
        return fallback or "未知类型"


def _resolve_is_finish(raw: dict) -> bool:
    """判断任务点是否已完成。

    ``isFinish`` 是权威字段；``score`` 会在部分类型（分组任务等）上提前出现，
    因此只在接口没有返回 ``isFinish`` 时才退化用 ``score`` 判断。
    """
    if "isFinish" in raw and raw.get("isFinish") is not None:
        return bool(raw.get("isFinish"))
    return raw.get("score") is not None


@dataclass
class FanyaPlanItem:
    """任务引擎中的一个任务点。"""

    plan_id: int
    encrypt_plan_id: str
    name: str
    plan_type: int
    plan_type_name: str = ""
    is_finish: bool = False
    is_lock: bool = False
    plan_allow_study: bool = True
    choose_study: bool = False
    score: Any = None
    extra: dict = field(default_factory=dict)

    # 预览阶段填充
    supported: bool = False
    handled_by: str = ""
    skip_reason: str = ""
    # 学习窗口状态（预览时逐个探测 getToStudyUrl 得到）：
    #   "" 未探测 / "open" 可进入 / "expired" 已结束 / "not_open" 尚未开放
    # 窗口已结束且处理器**依赖** study_url 的任务点，在当前账号上无法补完成，
    # 因此不算进「待处理」——否则界面上会永远挂着一批做不了的作业。
    window_state: str = ""
    window_note: str = ""

    @property
    def window_blocked(self) -> bool:
        """窗口已结束 / 尚未开放 —— 现在进不去。"""
        return self.window_state in ("expired", "not_open")

    @property
    def unreachable(self) -> bool:
        """进不去、且这类任务点没有别的入口 —— 现存条件下无法完成。

        「章节」是例外：它不经过任务引擎的跳转（``HANDLERS_WITHOUT_STUDY_URL``），
        窗口关掉也照样能学，所以不算 unreachable。
        """
        return self.window_blocked and self.handled_by not in HANDLERS_WITHOUT_STUDY_URL

    @property
    def type_label(self) -> str:
        return _plan_type_label(self.plan_type, self.plan_type_name)


@dataclass
class FanyaTask:
    """任务引擎中的一个任务（课程页「任务」里的一张卡片，例如「第4周课堂任务」）。"""

    task_id: int
    name: str
    encry_task_user_id: str
    progress: float = 0.0
    allow_study: bool = True
    allow_study_message: str = ""
    items: list[FanyaPlanItem] = field(default_factory=list)
    group_encry_id: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def pending_items(self) -> list[FanyaPlanItem]:
        return [i for i in self.items if not i.is_finish]

    @property
    def completable_items(self) -> list[FanyaPlanItem]:
        # unreachable（窗口已结束/未开放）不算「可完成」：执行器不必为它们
        # 白跑一趟 getToStudyUrl，界面上也不该把它们显示成能做的事。
        return [i for i in self.items if not i.is_finish and i.supported and not i.unreachable]


class FanyaTaskClient:
    """任务引擎客户端。复用项目全局登录会话（cookies 已在其中）。"""

    def __init__(self, session: Optional[requests.Session] = None, timeout: float = 20.0):
        self.session = session or SessionManager.get_session()
        self.timeout = timeout
        self._referer = f"{TASK_DOMAIN}/userStudyTask/index"

    # ------------------------------------------------------------------ 基础请求

    def _post(self, path: str, params: Optional[dict] = None, payload: Optional[dict] = None,
              timeout: Optional[float] = None, allow_form: bool = False) -> Any:
        """任务引擎的 POST 接口。默认发 JSON（该站点对表单类型返回 415）。"""
        url = path if path.startswith("http") else f"{TASK_DOMAIN}{path}"
        headers = {
            "Referer": self._referer,
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            if allow_form:
                resp = self.session.post(url, params=params, data=payload or {}, headers=headers,
                                         timeout=timeout or self.timeout)
            else:
                resp = self.session.post(url, params=params, json=payload if payload is not None else {},
                                         headers=headers, timeout=timeout or self.timeout)
        except requests.RequestException as exc:
            raise FanyaTaskError(f"请求 {url} 失败: {exc}") from exc

        if resp.status_code != 200:
            raise FanyaTaskError(f"请求 {url} 返回 HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise FanyaTaskError(f"请求 {url} 返回非 JSON 内容: {resp.text[:200]}") from exc

        if isinstance(data, dict) and data.get("result") is False:
            raise FanyaTaskError(f"{url} 返回失败: {data.get('message') or data.get('msg')}")
        return data

    def _get(self, path: str, params: Optional[dict] = None, referer: Optional[str] = None,
             timeout: Optional[float] = None) -> requests.Response:
        url = path if path.startswith("http") else f"{TASK_DOMAIN}{path}"
        headers = {"Referer": referer or self._referer}
        try:
            return self.session.get(url, params=params, headers=headers,
                                    timeout=timeout or self.timeout)
        except requests.RequestException as exc:
            raise FanyaTaskError(f"请求 {url} 失败: {exc}") from exc

    # ------------------------------------------------------------------ 任务/任务点

    def get_course_tasks(self, course_id: Any, class_id: Any, page_size: int = 30) -> list[FanyaTask]:
        """拉取某门课程在任务引擎里的全部任务。"""
        tasks: list[FanyaTask] = []
        page = 1
        while True:
            data = self._post(
                "/userStudyTask/getData",
                payload={
                    "page": page,
                    "searchKey": "",
                    "pageSize": page_size,
                    "courseId": int(course_id),
                    "moocClassId": int(class_id),
                },
            )
            batch = data.get("data") or []
            for raw in batch:
                tasks.append(
                    FanyaTask(
                        task_id=raw.get("taskId"),
                        name=raw.get("name") or "",
                        encry_task_user_id=raw.get("encryTaskUserId") or "",
                        progress=float(raw.get("taskStudyProgress") or 0.0),
                        extra=raw,
                    )
                )
            if len(batch) < page_size:
                break
            page += 1
            if page > 20:  # 安全上限
                break
        return tasks

    def get_task_progress(self, encry_task_user_id: str) -> Optional[float]:
        try:
            data = self._post(
                "/userStudyTask/getTaskSpeedOfProgress",
                params={"encryTaskUserId": encry_task_user_id},
            )
        except FanyaTaskError as exc:
            logger.debug("读取任务进度失败: {}", exc)
            return None
        payload = data.get("data") or {}
        speed = payload.get("speedOfprogress")
        try:
            return float(speed)
        except (TypeError, ValueError):
            return None

    def check_allow_study(self, encry_task_user_id: str) -> tuple[bool, str]:
        """任务是否在可学习时间窗口内。"""
        try:
            data = self._post(
                "/userStudyTask/isAllowStudyTask",
                params={"encryTaskUserId": encry_task_user_id},
            )
        except FanyaTaskError as exc:
            return False, str(exc)
        return bool(data.get("result")), data.get("message") or ""

    def load_task_items(self, task: FanyaTask) -> FanyaTask:
        """填充任务的默认分组与任务点列表。"""
        groups = self._post(
            "/userStudyPlan/getGroupData",
            params={"encryTaskUserId": task.encry_task_user_id},
        )
        items: list[FanyaPlanItem] = []
        for group in groups.get("data") or []:
            group_info = group.get("taskGroup") or {}
            encry_group_id = group.get("encryptGroupId") or ""
            if not encry_group_id:
                continue
            if not task.group_encry_id:
                task.group_encry_id = encry_group_id
            plans = self._post(
                "/userStudyPlan/getPlanDataByGroupId",
                params={
                    "encryTaskUserId": task.encry_task_user_id,
                    "encryGroupId": encry_group_id,
                },
            )
            for raw in plans.get("data") or []:
                plan_type = raw.get("planType")
                try:
                    plan_type_int = int(plan_type)
                except (TypeError, ValueError):
                    plan_type_int = -1
                items.append(
                    FanyaPlanItem(
                        plan_id=raw.get("planId"),
                        encrypt_plan_id=raw.get("encryptPlanId") or "",
                        name=raw.get("name") or "",
                        plan_type=plan_type_int,
                        plan_type_name=raw.get("planTypeName") or "",
                        # 完成判定：isFinish 是权威字段。注意 score 会在「分组任务」
                        # 等类型上提前出现（组内已有部分得分），因此只有当接口
                        # 完全没有返回 isFinish 时才退化为「score 非空」。
                        is_finish=_resolve_is_finish(raw),
                        is_lock=bool(raw.get("isLock")),
                        plan_allow_study=bool(raw.get("planAllowStudy", True)),
                        choose_study=bool(raw.get("chooseStudy")),
                        score=raw.get("score"),
                        extra=dict(raw, group_encry_id=encry_group_id),
                    )
                )
            task.extra.setdefault("groups", []).append(
                {
                    "group_id": group_info.get("id"),
                    "group_name": group_info.get("name"),
                    "encrypt_group_id": encry_group_id,
                    "group_allow_study": group.get("groupAllowStudy"),
                    "group_allow_study_condition": group.get("groupAllowStudyCondition"),
                    "plan_count": len(plans.get("data") or []),
                }
            )
        task.items = items
        return task

    def get_plan_item(self, encry_task_user_id: str, encry_group_id: str,
                      plan_id: Any) -> Optional[dict]:
        """重新读取单个任务点的原始状态（用于完成后校验）。

        ``scoreRefresh`` 在本账号上是空操作（返回 ``PARAMETER_INVALID``），
        因此校验完成状态只能靠重新拉取分组下的任务点。
        """
        if not encry_group_id:
            return None
        try:
            data = self._post(
                "/userStudyPlan/getPlanDataByGroupId",
                params={
                    "encryTaskUserId": encry_task_user_id,
                    "encryGroupId": encry_group_id,
                },
            )
        except FanyaTaskError as exc:
            logger.debug("重新读取任务点状态失败: {}", exc)
            return None
        for raw in data.get("data") or []:
            if str(raw.get("planId")) == str(plan_id):
                return raw
        return None

    def verify_plan_finished(self, encry_task_user_id: str, encry_group_id: str,
                             plan_id: Any) -> Optional[bool]:
        """完成任务点后校验其是否真的变为已完成。返回 ``None`` 表示读取失败。"""
        raw = self.get_plan_item(encry_task_user_id, encry_group_id, plan_id)
        if raw is None:
            return None
        return _resolve_is_finish(raw)

    def get_study_url(self, encry_task_user_id: str, encrypt_plan_id: str,
                      study_jump_type: int = 0) -> dict:
        """取得任务点的学习跳转信息（url / topicId / bbsId / topicUuid ...）。"""
        data = self._post(
            "/userStudyPlan/getToStudyUrl",
            params={
                "encryptPlanId": encrypt_plan_id,
                "encryTaskUserId": encry_task_user_id,
                "studyJumpType": study_jump_type,
                "isInterface": "false",
            },
        )
        return data.get("data") or {}

    def refresh_plan(self, encry_task_user_id: str, encrypt_plan_id: str) -> dict:
        """完成任务点后刷新成绩/状态，使任务进度统计同步。"""
        try:
            data = self._post(
                "/userStudyPlan/scoreRefresh",
                params={
                    "encryTaskUserId": encry_task_user_id,
                    "encryptPlanId": encrypt_plan_id,
                },
            )
        except FanyaTaskError as exc:
            logger.debug("刷新任务点成绩失败: {}", exc)
            return {}
        return data.get("data") or {}

    def refresh_task(self, encry_task_user_id: str, timeout: float = 30.0) -> Optional[float]:
        """重新读取任务进度（完成后调用，用于前端展示）。"""
        try:
            data = self._post(
                "/userStudyTask/getTaskSpeedOfProgress",
                params={"encryTaskUserId": encry_task_user_id},
                timeout=timeout,
            )
        except FanyaTaskError as exc:
            logger.debug("刷新任务进度失败: {}", exc)
            return None
        payload = data.get("data") or {}
        try:
            return float(payload.get("speedOfprogress"))
        except (TypeError, ValueError):
            return None


# ======================================================================
# 作答内容生成（复用前端已配置的 OpenAI 兼容接口，默认 DeepSeek）
# ======================================================================


class ContentGenerator:
    """用配置里已有的 AI 接口生成讨论回复 / 对话内容。

    配置沿用 Web 端「AI 题库」的三项：``endpoint`` / ``key`` / ``model``。
    未配置时所有生成方法都返回 ``None``，调用方需回退到模板文本。
    """

    def __init__(self, endpoint: str = "", key: str = "", model: str = "",
                 timeout: float = 90.0, max_retries: int = 2):
        self.endpoint = (endpoint or "").strip()
        self.key = (key or "").strip()
        self.model = (model or "").strip()
        self.timeout = timeout
        self.max_retries = max_retries
        #: 最近一次 ``_chat`` 失败的原文（形如 ``HTTP 401: {...}``）。
        #: 生成类调用只看得到 ``None``，而「测试连接」需要把模型侧原话显示给用户，
        #: 所以在这里留一份，避免为了报错再发一次请求。
        self.last_error: str = ""

    @classmethod
    def from_tiku_config(cls, tiku_config: Optional[dict]) -> "ContentGenerator":
        cfg = tiku_config or {}
        return cls(
            endpoint=cfg.get("endpoint") or "",
            key=cfg.get("key") or "",
            model=cfg.get("model") or "",
        )

    @property
    def available(self) -> bool:
        return bool(self.endpoint and self.key and self.model)

    def _chat(self, messages: list[dict], temperature: float = 0.7, max_tokens: int = 900) -> Optional[str]:
        self.last_error = ""
        if not self.available:
            self.last_error = "未配置 AI 接口（endpoint / key / model 都不能为空）"
            logger.warning("未配置 AI 接口（endpoint/key/model），无法生成作答内容")
            return None

        import httpx  # 与 api/answer.py 使用同一依赖

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # DeepSeek V4 默认开启 thinking，会显著拉长响应时间并可能超出超时
        if "deepseek" in self.endpoint.lower():
            payload["thinking"] = {"type": "disabled"}

        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(self.endpoint, headers=headers, json=payload)
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                body = resp.json()
                choices = body.get("choices") or []
                if not choices:
                    raise RuntimeError(f"响应中没有 choices: {str(body)[:200]}")
                content = (choices[0].get("message") or {}).get("content") or ""
                content = content.strip()
                if not content:
                    raise RuntimeError("模型返回空内容")
                return content
            except Exception as exc:  # noqa: BLE001 - 生成失败需回退到模板
                last_error = exc
                self.last_error = str(exc)
                logger.warning("AI 生成失败（第 {}/{} 次）: {}", attempt, self.max_retries, exc)
                if attempt < self.max_retries:
                    time.sleep(1.5 * attempt)
        logger.error("AI 生成最终失败: {}", last_error)
        self.last_error = str(last_error) if last_error else "模型没有返回内容"
        return None

    def ping(self, prompt: str = "请只回复两个字符：OK", *, max_tokens: int = 16) -> dict:
        """连通性自检，供「AI 接口配置」页的「测试连接」按钮使用。

        真发一条极短请求（温度 0、16 token），因此能验证 endpoint / key / model
        三项是否真的可用，而不是只看字段填没填。

        返回 ``{"ok": bool, "model": str, "latency": int, "sample": str, "error": str}``：
        失败时 ``error`` 是模型侧原话（例如 ``HTTP 401: {"error":...}``），
        ``latency`` 单位毫秒（含重试耗时）。
        """
        started = time.perf_counter()
        if not self.available:
            return {
                "ok": False, "model": self.model, "latency": 0, "sample": "",
                "error": "未配置 AI 接口（endpoint / key / model 都不能为空）",
            }
        text = self._chat(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=max_tokens,
        )
        latency = int((time.perf_counter() - started) * 1000)
        if not text:
            return {
                "ok": False, "model": self.model, "latency": latency, "sample": "",
                "error": self.last_error or "模型没有返回内容",
            }
        return {
            "ok": True, "model": self.model, "latency": latency,
            "sample": text.strip()[:200], "error": "",
        }

    def generate_discussion_reply(self, topic: str, course_title: str = "",
                                  min_chars: int = 120) -> Optional[str]:
        """为主题讨论生成一段可直接发布的回复。"""
        system = (
            "你是一名大学课程的学习者，正在参与课程论坛的主题讨论。"
            "请用中文写一段观点明确、有条理、口语自然的回复。"
            "要求：紧扣题目，先给出核心观点，再分点展开论述，最后简要总结；"
            f"长度约 {min_chars}-{max(min_chars * 3, 300)} 字；"
            "不要出现「作为一个AI」「根据我的训练数据」之类的表述，不要使用 Markdown 标题。"
        )
        user = f"课程：{course_title}\n讨论题目：{topic}\n\n请直接给出你的回复正文。"
        return self._chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )

    def generate_text(self, system: str, user: str, temperature: float = 0.2,
                      max_tokens: int = 4000) -> Optional[str]:
        """通用文本生成，供需要较长输出（例如整段代码）的适配器使用。

        注意 ``_chat`` 默认 ``max_tokens=900``，对代码来说远远不够 ——
        实测 900 token 会把一个 115 行的 Java 程序**截断在半句**，
        服务端判题直接返回「编译错误: reached end of file while parsing」。
        """
        return self._chat([{"role": "system", "content": system},
                           {"role": "user", "content": user}],
                          temperature=temperature, max_tokens=max_tokens)

    def generate_chat_reply(self, prompt: str, history: Optional[list[dict]] = None,
                            role_hint: str = "") -> Optional[str]:
        """为情景对话类 AI 实践生成一句学生发言。"""
        system = (
            "你是一名正在完成大学课程「AI实践」任务的学生。"
            "请用中文自然地回复对方，内容要切题、有思考深度、有自己的判断，"
            "长度约 100-250 字，不要输出任何解释性旁白或 Markdown 格式。"
        )
        if role_hint:
            system += f"\n任务背景：{role_hint}"
        messages = [{"role": "system", "content": system}]
        for turn in history or []:
            role = turn.get("role")
            content = turn.get("content") or ""
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": prompt})
        return self._chat(messages)


# ======================================================================
# 适配器注册表：planType → 支持状态与处理函数
# ======================================================================

# status 取值：
#   auto      完全自动，无需用户额外提供材料
#   partial   可自动但需要用户先提供材料（例如上传型 AI实践的作品文件）
#   manual    接口层面无法自动完成，需人工/教师操作
#   pending   尚未实现（协议未知或开发中）
STATUS_AUTO = "auto"
STATUS_PARTIAL = "partial"
STATUS_MANUAL = "manual"
STATUS_PENDING = "pending"


@dataclass
class AdapterSpec:
    plan_type: int
    handler: str
    status: str
    label: str
    note: str = ""


ADAPTERS: dict[int, AdapterSpec] = {}


def register_adapter(plan_type: int, handler: str, status: str, label: str, note: str = "") -> None:
    ADAPTERS[plan_type] = AdapterSpec(plan_type, handler, status, label, note)


# 由既有「章节学习」流程负责的类型：任务引擎里的同一个任务点，
# 实际就是章节下的视频/文档/作业，复用 main.process_job 即可。
register_adapter(PLAN_TYPE_CHAPTER, "chapter", STATUS_AUTO, "章节",
                 "复用章节任务点流程（externalDataId == mooc1 章节 point id）")
# 视频：任务引擎自有播放页（videoStudy/learnPage），进度由 videoDataLog 上报。
# ★ 2026-10-15 更正：此前这里写「实测服务端接受一次到位的进度上报，isFinish 立即翻转」
#   —— 该结论**只在任务点没设观看时长要求时成立**。设了要求的任务点判据是
#   score.totalTime >= watchDuration(分钟) × 60000，而 totalTime 只按**真实经过时间**
#   累计（跳到片尾 / 几秒内连发心跳 / 快进全部计 0）。现已实现 _satisfy_watch_duration()
#   按真实节奏补足；因此仍标 AUTO，但逐项说明见 resolve_item()。
register_adapter(PLAN_TYPE_VIDEO, "video", STATUS_AUTO, "视频",
                 "上报 videoDataLog 观看进度（未设时长要求时一次到位即可；"
                 "设了要求则按真实时间补足累计观看时长）")
# 文档：documentStudy/readEnd 只能把 completeWeightRate 置 100（「读完」）。
# 若该任务点还要求累计阅读时长，readEnd 一分一秒都不计入 totalTime，且阅读器的
# ac_mark 埋点无法用 HTTP 重放（实测原样重发/篡改签名/伪造 h 都是 Δ=0）。
# ★ 2026-10-15 起走**浏览器通道**（api/fanya_browser.py）：自动开真实浏览器滚到底，
# 实测把 totalTime 从 0 补到 300000ms 并让 isFinish 翻转为 True（planId=8360000）。
register_adapter(PLAN_TYPE_DOCUMENT, "document", STATUS_AUTO, "文档",
                 "上报文档阅读完成（task.chaoxing.com/documentStudy/readEnd）；"
                 "若叠加累计阅读时长门槛，则自动改用真实浏览器滚动阅读补足")
# 作业：mooc2 课程作业（mooc2/work/task?workId=&enc=），knowledgeid=0 且 jobid 为空，
# 不在章节任务点里，无法复用 study_work → 需单独的课程作业适配器。
register_adapter(PLAN_TYPE_WORK, "course_work", STATUS_AUTO, "作业",
                 "mooc2 课程作业：题库作答后提交（复用题库 submit/cover_rate 策略）")
register_adapter(1, "chapter", STATUS_AUTO, "知识点", "复用章节任务点流程")
# 音频：与视频同构（audioStudy/learnPage + audioDataLog）。本账号没有音频任务点，
# 因此无法真机验证；处理器会明确报错（而不是静默失败），所以仍然标记为可自动完成，
# 但备注如实说明「未验证」。
register_adapter(PLAN_TYPE_AUDIO, "audio", STATUS_AUTO, "音频",
                 "与视频同构实现（audioStudy/audioDataLog），本账号无样本，未验证")

register_adapter(PLAN_TYPE_DISCUSSION, "discussion", STATUS_AUTO, "主题讨论", "AI 生成回复并提交")
# AI实践 按子模式分别处理（代码实践 / 思维阶梯 / 情景对话 / 上传作品），
# 这里的类型级注册只是兜底：真正生效的是 _ai_practice_handler() + resolve_item()。
register_adapter(PLAN_TYPE_AI_PRACTICE, "ai_practice", STATUS_PARTIAL, "AI实践",
                 "未能识别子模式（多为学习窗口未开放）；已实现 代码实践 与 思维阶梯")
register_adapter(PLAN_TYPE_QUIZ, "quiz", STATUS_PARTIAL, "自测",
                 "可用题库作答；默认只暂存不交卷（需开启「考试交卷」）；"
                 "过期的无法补完成，本账号缺未过期样本故未验证")
register_adapter(PLAN_TYPE_EXAM, "quiz", STATUS_PARTIAL, "测验",
                 "可用题库作答；默认只暂存不交卷（需开启「考试交卷」）；未验证")
# 问卷实际走超星 OA 表单（智慧办公），提交端点需要管理员侧登录，无法自动化
register_adapter(6, "manual", STATUS_MANUAL, "问卷", "需人工在超星 OA 表单中填写")
# 分组任务：本账号已在组内，任意成员即可提交（onlyLeaderSubmit=0）。
# 但提交有两个坑：(1) 提交会**覆盖全组已有内容**（实测两个小组都已提交过，
# 编辑次数 9 / 14）；(2) 完成判据是总成绩 ≥60，提交后任务点可能仍为未完成。
# 因此默认只生成内容不提交，且默认拒绝覆盖已有内容。
register_adapter(18, "group_task", STATUS_PARTIAL, "分组任务",
                 "可生成小组答案；默认不提交，且默认拒绝覆盖小组已有内容；"
                 "完成取决于评分 ≥60，提交后可能仍未完成")
register_adapter(20, "manual", STATUS_MANUAL, "线下课堂", "由教师在课堂上记录，无法自动完成")
# AI课堂为第三方对接（棱镜），客户端不上报泛雅，无确定性完成接口
register_adapter(10042, "manual", STATUS_MANUAL, "AI课堂", "第三方课堂，需人工进入完成")


def adapter_for(plan_type: int) -> AdapterSpec:
    return ADAPTERS.get(
        plan_type,
        AdapterSpec(plan_type, "unknown", STATUS_PENDING, f"未知类型({plan_type})", "尚未支持"),
    )


def effective_status(spec: AdapterSpec) -> tuple[str, str]:
    """结合「处理器是否真的实现了」给出最终状态。

    注册表里声明的状态是**目标**状态；如果对应处理器还没写，
    就不能对外宣称「可自动完成」，否则执行时只会得到一堆报错。
    这里统一降级为 ``pending``，保证界面与命令行输出始终诚实。
    """
    if spec.status in (STATUS_AUTO, STATUS_PARTIAL) and spec.handler not in HANDLERS:
        return STATUS_PENDING, f"{spec.label}：自动完成处理器尚未实现"
    return spec.status, spec.note


def _ai_practice_handler(ai_mode: dict) -> str:
    """AI实践每个子模式对应不同的处理器。"""
    from api.fanya_ai import (MODE_CHAT, MODE_CODE, MODE_LADDER,
                          MODE_STORY, MODE_UPLOAD)

    mode = ai_mode.get("mode")
    if mode == MODE_CODE:
        return "ai_code"
    if mode == MODE_LADDER:
        return "ai_ladder"
    if mode == MODE_CHAT:
        return "ai_chat"
    if mode == MODE_STORY:
        # AI剧情：对话部分复用 chat 链路；最后的「上传报告」节点尚未自动化
        return "ai_story"
    if mode == MODE_UPLOAD:
        return "ai_upload"
    return "ai_practice"


def resolve_item(item: FanyaPlanItem) -> tuple[str, str, str]:
    """给出任务点最终生效的 ``(status, handler, note)``。

    两类需要**逐项**细分，注册表里的类型级条目太笼统：

    1. **AI实践**：upload / chat / code / ladder / story 各自能力不同；
    2. **视频/音频/文档**：设了「累计观看时长」要求的任务点，判据是
       ``score.totalTime >= watchDuration × 60000``，而 totalTime 只认真实经过时间。
       视频/音频能靠真实播放补足（慢但可行）；文档的时长走云盘阅读器跨域埋点，
       纯 HTTP 补不了 —— 必须如实降级，否则界面上显示「可自动」就等于骗用户。
    """
    if item.plan_type == PLAN_TYPE_AI_PRACTICE:
        ai_mode = item.extra.get("ai_mode")
        if not ai_mode:
            return (STATUS_PENDING, "ai_practice",
                    "AI实践：未能识别子模式（学习窗口可能已关闭）")
        status, note = _ai_practice_status(ai_mode)
        handler = _ai_practice_handler(ai_mode)
        if status in (STATUS_AUTO, STATUS_PARTIAL) and handler not in HANDLERS:
            return STATUS_PENDING, handler, f"{note}（处理器尚未实现）"
        return status, handler, note

    spec = adapter_for(item.plan_type)
    status, note = effective_status(spec)
    watch_note = _watch_duration_note(item, status)
    if watch_note:
        status, note = watch_note
    return status, spec.handler, note


#: 观看时长门槛逐项的说明与状态修正。
#: 文档侧 HTTP 补不了，但可以走**浏览器通道**（api/fanya_browser.py）真实滚动，
#: 所以是 ``partial``；视频/音频同样要真实等待，也是 ``partial``。
_WATCH_KIND_LABELS = {"video": "视频", "audio": "音频", "document": "文档"}


def _watch_duration_note(item: FanyaPlanItem, status: str) -> Optional[tuple[str, str]]:
    """该任务点若设了累计观看时长要求，返回修正后的 ``(status, note)``；否则 ``None``。"""
    if status not in (STATUS_AUTO, STATUS_PARTIAL):
        return None
    for kind, label in _WATCH_KIND_LABELS.items():
        required = watch_duration_ms(item.extra, kind)
        if not required:
            continue
        minutes = required / 60000.0
        if kind == "document":
            return (STATUS_PARTIAL,
                    f"文档还要求累计阅读 {minutes:g} 分钟：documentStudy/readEnd 只把"
                    f"「读完」置 100%，一分一秒都不计入 totalTime（实测等 40 秒再调、"
                    f"连调 3 次都仍是 0）；云盘阅读器的 ac_mark 埋点也无法用 HTTP 重放"
                    f"（原样重发 / 篡改签名 / 伪造停留时长，三种都是 Δ=0）。"
                    f"执行时会**自动打开真实浏览器并滚动阅读**来补足，需要真等到"
                    f"{minutes:g} 分钟；这依赖可选的 playwright（未安装时如实报失败）")
        return (STATUS_PARTIAL,
                f"{label}要求累计观看 {minutes:g} 分钟，只能按**真实经过时间**累计"
                f"（跳到片尾 / 快进 / 连发心跳全部计 0，实测倍速约 0.63 倍被采信）。"
                f"执行时会真实等待，可能很久；中途可停止，已累计的时长会保留")
    return None


def _ai_practice_status(ai_mode: dict) -> tuple[str, str]:
    """根据 AI实践子模式给出支持状态与说明。"""
    from api.fanya_ai import (
        MODE_CHAT, MODE_CODE, MODE_LADDER, MODE_STORY, MODE_UPLOAD, format_rules,
    )

    mode = ai_mode.get("mode")
    rules_text = format_rules(ai_mode.get("rules") or [])
    if mode == MODE_UPLOAD:
        # 上传作品型：已实现「生成作品 → 上传网盘 → 挂附件 → 交卷 → AI评分」。
        # 但有些题目原文写着「禁止直接大段复制 AI 输出，用自己的话整理」，
        # 这类默认只生成草稿（不提交），需显式开启 upload_ai_text 覆盖。
        note = ("上传作品型：已实现（生成作品 → 上传网盘 → 挂附件 → 交卷 → AI评分）；"
                "交卷前会回读校验附件确实挂上，挂不上则中止（不会交空记录）；"
                "题目若声明「禁止复制 AI 输出」则默认只出草稿，需勾选覆盖")
        if rules_text:
            note += f"｜要求：{rules_text}"
        return STATUS_PARTIAL, note
    if mode == MODE_CHAT:
        return STATUS_PARTIAL, ("情景对话型：已实现（生成场景 → 多轮对话 → 交卷 → 评估出分）；"
                                "本账号无未过期的该模式样本，未真机验证")
    if mode == MODE_LADDER:
        return STATUS_AUTO, ("思维阶梯型：已真机验证（2026-10-12 于「静电场环路定理闯关」"
                             "完整跑通：拆题 → 8 轮逐题作答（含多选）→ 交卷 → 结题报告出分，"
                             "得分 85，平台判定任务点完成）")
    # 代码实践型（type=4）：已真机验证 —— 一次到位提交并评估，得分 ≥60 即完成
    if mode == MODE_CODE:
        return STATUS_AUTO, ("代码实践型：AI 生成代码 → 自测运行校验 → 建卷交卷 → 评估出分"
                             "（已实测完成，得分 76）")
    if mode == MODE_STORY:
        # ★ 2026-10-12 更正：此前这里写「评分拿不到，因为评分绑定在 WebSocket
        #   实时会话上」——**该结论是错的**。真实原因只是 evaluate/stream 的
        #   recordUuid 传错了记录：
        #       传章节记录 → 「会话不存在」   传外层记录 → 正常应答
        #   对照实测见 api/fanya_story.py 模块说明与 docs/FANYA_TASK_API.md。
        #
        #   链路本身已通：add-chat 发言 → story-host-agent 驱动应答 →
        #   answer/submit 交卷 → evaluate/stream 评分。
        #   2026-10-14 实测：完整自动化在「审计抽样建模校验」拿到 **100 分**
        #   （平台原文 totalScore=100，「完全符合满分标准」），任务点判定通过。
        #
        #   标 PARTIAL 而不是 AUTO 的原因（两条，都是真实缺口）：
        #   1) 交卷**不可逆**（部分课程 enableReset=false），因此处理器默认
        #      要 ``options["allow_ai_submit"]`` 才真的动手，审批模式下只出草稿；
        #   2) 剧情的最后一章往往是**上传作品**节点（subtype=1），尚未自动化。
        return STATUS_PARTIAL, ("AI剧情型：已真机验证（发言 → 智能体应答 → 交卷 → "
                                "评分，2026-10-14 实测满分 100）。两条边界：交卷不可逆"
                                "（需显式开启「允许 AI实践直接提交」才执行）；剧情末尾的"
                                "「上传作品」节点尚未自动化，遇到会停下并请你手动上传")
    return STATUS_PENDING, f"{ai_mode.get('modeLabel') or '未知模式'}：暂未支持自动完成"


def _describe_quiz_items(client: FanyaTaskClient, task: FanyaTask, redo: bool = False) -> None:
    """为未完成的自测/测验任务点补充考试窗口与过期状态（只读）。

    过期的自测**无法通过任何接口补完成**（`scoreRefresh` 只是同步已有成绩），
    因此必须在界面上直接说清楚，避免用户反复尝试。
    """
    from api import fanya_quiz

    for item in task.items:
        # 注意：自测/测验**始终**跳过已完成项 —— 它们不支持自动重做
        # （作业/测验的重做是平台 UI 流程），而且在重做模式下逐个去识别已完成的
        # 试卷非常慢（每项多次请求、每次 30s 超时），会拖到用户以为卡死。
        if item.plan_type not in (PLAN_TYPE_QUIZ, PLAN_TYPE_EXAM) or item.is_finish:
            continue
        if item.extra.get("quiz_info"):
            continue
        if item.plan_type == PLAN_TYPE_QUIZ:
            info = fanya_quiz.describe_self_test(
                client, task.encry_task_user_id, item.encrypt_plan_id)
        else:
            try:
                study_url = client.get_study_url(task.encry_task_user_id, item.encrypt_plan_id)
            except FanyaTaskError as exc:
                logger.debug("测验任务点暂不可进入（{}）：{}", item.name, exc)
                continue
            info = fanya_quiz.describe_exam(client, study_url)
        if info:
            item.extra["quiz_info"] = info
            logger.info("自测/测验「{}」：{}", item.name[:30], info.get("summary") or "已识别")


def _window_state_from_message(message: str) -> tuple[str, str]:
    """把服务端的窗口报错判成 ``(state, note)``。"""
    note = _window_reason(message)
    if "已结束" in note:
        return "expired", note
    if "尚未开放" in note:
        return "not_open", note
    return "", note


def _probe_item_windows(client: FanyaTaskClient, task: FanyaTask,
                        max_workers: int = 8,
                        probe_client_factory: Optional[Callable[[], Any]] = None) -> None:
    """给未完成的任务点逐个探测学习窗口（**只读**）。

    服务端没有「窗口列表」这种接口：唯一能判断某个任务点现在还能不能做的方法，
    就是对它调一次 ``getToStudyUrl`` —— 成功=可进入，失败=按报错里的窗口终点
    判「已结束 / 尚未开放」。没有这一步，界面上就会把一批永远做不了的作业
    一直显示成「待处理」。

    实测（2026-10-16）：窗口已结束的任务点，服务端只回
    ``{"code":400,"result":false,"message":"任务点学习时间：…当前不在学习时间内"}``，
    **不给任何 id**（workId / publishRelationUuid / topicId 都没有），
    所以这类任务点在当前账号上无法通过别的入口补完成。

    探测只影响内存里的标记，不改任何学习记录。
    """
    pending = [i for i in task.items if not i.is_finish and not i.window_state]
    if not pending:
        return

    workers = max(1, min(int(max_workers or 1), 8, len(pending)))

    def probe(item: FanyaPlanItem) -> None:
        try:
            client.get_study_url(task.encry_task_user_id, item.encrypt_plan_id)
        except FanyaTaskError as exc:
            message = str(exc)
            if "不在学习时间内" not in message:
                return
            state, note = _window_state_from_message(message)
            item.window_state = state or "expired"
            item.window_note = note
        except Exception as exc:  # noqa: BLE001 - 探测失败不该影响盘点
            logger.debug("窗口探测失败（{}）：{}", item.name[:24], exc)
        else:
            item.window_state = "open"

    if workers == 1:
        for item in pending:
            probe(item)
        return

    # 探测是纯 GET，用独立会话并发跑（共享 session 不是线程安全的）。
    # ``probe_client_factory`` 让测试注入假客户端；生产路径复制全局会话。
    factory = probe_client_factory

    def probe_with(item: FanyaPlanItem) -> None:
        probe_client = factory() if factory else None
        if probe_client is None:
            probe(item)
            return
        try:
            probe_client.get_study_url(task.encry_task_user_id, item.encrypt_plan_id)
        except FanyaTaskError as exc:
            message = str(exc)
            if "不在学习时间内" not in message:
                return
            state, note = _window_state_from_message(message)
            item.window_state = state or "expired"
            item.window_note = note
        except Exception as exc:  # noqa: BLE001
            logger.debug("窗口探测失败（{}）：{}", item.name[:24], exc)
        else:
            item.window_state = "open"

    def default_factory() -> Any:
        try:
            from api.base import SessionManager
            return FanyaTaskClient(session=SessionManager.clone_session())
        except Exception as exc:  # noqa: BLE001 - 复制失败就退回共享客户端
            logger.debug("窗口探测会话复制失败，改用共享会话：{}", exc)
            return None

    if factory is None:
        factory = default_factory

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="window-probe") as pool:
        futures = [pool.submit(probe_with, item) for item in pending]
        for fut in futures:
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.debug("窗口探测任务异常: {}", exc)


_REDO_TIMES_RE = re.compile(r"redoTimes\s*=\s*([0-9+\-\s]+);")


def _parse_redo_times(html: str) -> Optional[int]:
    """从作业判定页里解出 ``redoTimes``。

    页面里它是一个 JS 算式（实测 ``var redoTimes = 0 + 0 - 0;``），所以不能直接
    ``int()``；这里只接受「数字 + 加减号」的表达式，避免把变量名当表达式求值。
    """
    match = _REDO_TIMES_RE.search(html or "")
    if not match:
        return None
    expression = match.group(1)
    if not re.fullmatch(r"[0-9+\-\s]+", expression):
        return None
    total = 0
    for part in re.finditer(r"([+-]?)\s*(\d+)", expression):
        total += (-1 if part.group(1) == "-" else 1) * int(part.group(2))
    return max(total, 0)


def _probe_work_redo_times(client: FanyaTaskClient, task: FanyaTask) -> None:
    """为「已出分但低于达标线」的作业查一次还能重做几次（**只读**）。

    作业的重做次数只出现在判定页的 JS 里，而判定页要先 ``getToStudyUrl`` 拿跳转
    （已提交的作业这时拿到的是「查看」页而不是答题页），所以每个低分作业要花两次
    请求。只对低分作业做 —— 正常任务点一次都不会多发。

    结果写进 ``item.extra["redo_times"]``：``0`` = 平台根本不给重做次数（实测
    2026-10-16 的「Logistic模型作业」就是这种，页面里没有任何修改入口），
    ``None`` = 没读到（页面打不开 / 结构变了，不要猜）。
    """
    for item in task.items:
        if item.plan_type != PLAN_TYPE_WORK or not is_below_threshold(item):
            continue
        if item.extra.get("redo_times") is not None:
            continue
        try:
            study_url = client.get_study_url(task.encry_task_user_id, item.encrypt_plan_id)
            url = str(study_url.get("url") or "")
            if not url:
                continue
            resp = client.session.get(url, timeout=30)
            resp.encoding = "utf-8"
            html = resp.text
        except Exception as exc:  # noqa: BLE001 - 读不到就不下结论
            logger.debug("作业重做次数读取失败（{}）：{}", item.name[:24], exc)
            continue
        times = _parse_redo_times(html)
        if times is not None:
            item.extra["redo_times"] = times


def _describe_ai_practice_items(client: FanyaTaskClient, course: dict, task: FanyaTask, redo: bool = False) -> None:
    """为未完成的 AI实践任务点补充子模式信息（只读）。"""
    from api import fanya_ai

    for item in task.items:
        if item.plan_type != PLAN_TYPE_AI_PRACTICE or (item.is_finish and not redo):
            continue
        if item.extra.get("ai_mode"):
            continue
        try:
            study_url = client.get_study_url(task.encry_task_user_id, item.encrypt_plan_id)
        except FanyaTaskError as exc:
            # 最常见的原因是该任务点自己的学习窗口还没开 / 已关。
            # 记下来，让界面说明「读不到模式」的原因，而不是含糊地说「未支持」。
            item.extra["ai_mode_error"] = _window_reason(str(exc))
            logger.debug("AI实践任务点暂不可进入（{}）：{}", item.name, exc)
            continue
        described = fanya_ai.describe(client.session, course, study_url)
        if described:
            item.extra["ai_mode"] = described
            logger.info("AI实践「{}」子模式：{}", item.name[:30], described.get("modeLabel"))
        else:
            item.extra["ai_mode_error"] = "未能读取作答模式（load-data 未返回或结构变化）"


def _window_reason(message: str) -> str:
    """把服务端的窗口报错整理成给用户看的一句话。

    ★ 服务端对「还没开」和「已经关了」用的是**同一句**报错
    （`任务学习时间：A至B，当前不在学习时间内`），必须自己比较 B 与当前时间，
    否则会把「窗口已经结束、再也进不去」误说成「等窗口开启再来」——
    那是在让用户白等。

    实测（2026-10-15）：全账号 19 个被拒任务点的窗口终点全部落在
    2026-09-07 ~ 2026-10-01，即**全部已过期**。
    """
    if "不在学习时间内" not in message:
        return f"暂不可进入该任务点：{message[:60]}"

    # 抽出窗口终点（「…至YYYY-MM-DD HH:MM」），判断到底是没开还是已关
    m = re.search(r"至\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", message)
    if not m:
        return "学习窗口未开放（服务端拒绝进入）；窗口已关闭的任务点无法补完成，需联系老师处理"

    try:
        end = datetime.datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return "学习窗口未开放（服务端拒绝进入）；窗口已关闭的任务点无法补完成，需联系老师处理"

    span = m.group(0).lstrip("至 ")
    if datetime.datetime.now() > end:
        return f"学习窗口已结束（{span}），平台不再允许进入，无法补完成（需联系老师）"
    return f"学习窗口尚未开放（{span}），等窗口开启后再盘点"


def _format_progress(value: Any) -> str:
    """把任务进度格式化成不会误导人的字符串。

    多数任务返回 0~1 的比例，但实测个别任务（COM0018A 的项目1/项目2）返回的是
    8.33 这样的**百分数**。不能一律按比例渲染成「833%」，所以超出 [0,1] 时
    直接显示原始值并标注。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if 0.0 <= num <= 1.0:
        return f"{num:.1%}"
    return f"{num:g}（服务端原始值，疑似百分数）"


def classify_items(task: FanyaTask) -> None:
    """为任务点标注支持状态，供执行器与预览界面共用。"""
    for item in task.items:
        status, handler, note = resolve_item(item)
        item.supported = status in (STATUS_AUTO, STATUS_PARTIAL)
        item.handled_by = handler
        if item.is_finish:
            item.skip_reason = "已完成"
        elif item.is_lock:
            item.skip_reason = "未解锁（前置任务点未完成）"
        elif item.choose_study:
            item.skip_reason = "选学任务点"
        elif not item.plan_allow_study:
            item.skip_reason = "当前不允许学习"
        elif status == STATUS_MANUAL:
            item.skip_reason = note or "需人工完成"
        elif status == STATUS_PENDING:
            item.skip_reason = note or "暂未支持自动完成"
        else:
            item.skip_reason = note


def item_score(item: FanyaPlanItem) -> Optional[float]:
    """任务点的平台得分。

    作业类任务点的 ``score`` 是一整条评分记录（dict，分数在 ``totalScore`` 里），
    其它类型直接是数值或 None；两者都归一化成 float / None。
    """
    raw = item.score
    if isinstance(raw, dict):
        for key in ("totalScore", "studentEvaluationScore", "score"):
            value = raw.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def item_threshold(item: FanyaPlanItem) -> Optional[float]:
    """任务点的达标线：AI实践 看 ``aicoachScore``，作业看 ``workScore``。

    其余类型（视频/文档/章节/讨论…）平台不给达标线，返回 None。
    """
    breakthrough = (item.extra or {}).get("planBreakthroughSet") or {}
    if breakthrough.get("enableAicoach"):
        value = breakthrough.get("aicoachScore")
    elif breakthrough.get("enableWorkScore"):
        value = breakthrough.get("workScore")
    else:
        return None
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def is_below_threshold(item: FanyaPlanItem) -> bool:
    """已出分但低于平台达标线（AI实践 60 / 作业 workScore）。

    这类任务点即使交过卷也会一直挂着（AI实践 低于 ``aicoachScore`` 时
    ``isFinish`` 不会翻成 True），需要重做或改进答案，值得在界面上单独提示。
    """
    score = item_score(item)
    threshold = item_threshold(item)
    return score is not None and threshold is not None and score < threshold


# ======================================================================
# 预览：列出所有课程 / 任务 / 任务点及其可自动化判定
# ======================================================================


def build_task_preview(chaoxing: Any, courses: list[dict],
                       client: Optional[FanyaTaskClient] = None,
                       on_progress: Optional[Callable[[dict], None]] = None,
                       redo: bool = False, probe_windows: bool = True) -> dict:
    """遍历给定课程，返回「任务」模块的完整清单与可自动化判定。

    该函数只做 GET/POST 读取，不会修改任何学习记录，可安全用于前端预览。

    ``probe_windows``：是否为每个未完成任务点探测学习窗口（默认开）。关掉可以
    省掉每个任务点一次 ``getToStudyUrl``，代价是界面分不清「现在能做」和
    「窗口已经关了、永远做不了」。
    """
    client = client or FanyaTaskClient()
    result: dict = {"courses": [], "summary": {
        "courseCount": 0, "taskCount": 0, "pendingCount": 0,
        "completableCount": 0, "manualCount": 0, "unsupportedCount": 0,
        "expiredCount": 0, "belowThresholdCount": 0,
    }}

    def emit(**payload) -> None:
        if callable(on_progress):
            try:
                on_progress(payload)
            except Exception as exc:  # noqa: BLE001
                logger.debug("预览进度回调失败: {}", exc)

    for index, course in enumerate(courses, start=1):
        emit(type="course_start", index=index, total=len(courses),
             courseId=str(course.get("courseId")), title=course.get("title") or "")
        course_entry = {
            "courseId": str(course.get("courseId")),
            "classId": str(course.get("clazzId")),
            "cpi": str(course.get("cpi") or ""),
            "title": course.get("title") or "",
            "hasTaskEngine": False,
            "tasks": [],
            "error": "",
        }
        try:
            tasks = client.get_course_tasks(course.get("courseId"), course.get("clazzId"))
        except FanyaTaskError as exc:
            course_entry["error"] = str(exc)
            logger.warning("课程「{}」任务列表读取失败: {}", course_entry["title"], exc)
            result["courses"].append(course_entry)
            emit(type="course_done", index=index, total=len(courses),
                 courseId=course_entry["courseId"], title=course_entry["title"],
                 taskCount=0)
            continue

        if not tasks:
            logger.info("课程「{}」没有任务引擎任务", course_entry["title"])
            result["courses"].append(course_entry)
            emit(type="course_done", index=index, total=len(courses),
                 courseId=course_entry["courseId"], title=course_entry["title"],
                 taskCount=0)
            continue

        course_entry["hasTaskEngine"] = True
        result["summary"]["courseCount"] += 1

        for task in tasks:
            try:
                client.load_task_items(task)
            except FanyaTaskError as exc:
                logger.warning("任务「{}」任务点读取失败: {}", task.name, exc)
                continue
            # AI实践 有四种子模式（情景对话 / 上传作品 / 思维阶梯 / 代码实践），
            # 完成方式不同；自测/测验要识别考试窗口。**必须在 classify_items 之前**
            # 识别：resolve_item() 依赖 item.extra["ai_mode"] 才能选出模式专属处理器，
            # 顺序反了会让 AI实践 被判成「不支持」，任务级 completableCount 会少算。
            _describe_ai_practice_items(client, course, task, redo=redo)
            _describe_quiz_items(client, task, redo=redo)
            classify_items(task)
            # 窗口探测必须在 classify_items 之后：unreachable 依赖 handled_by。
            if probe_windows:
                _probe_item_windows(client, task)
                # 低分作业：再查一次「还能重做几次」（只对低分作业，见该函数注释）
                _probe_work_redo_times(client, task)
            allow, allow_msg = client.check_allow_study(task.encry_task_user_id)
            task.allow_study = allow
            task.allow_study_message = allow_msg

            items = []
            for item in task.items:
                status, handler, note = resolve_item(item)
                ai_mode = item.extra.get("ai_mode")
                if not ai_mode and item.extra.get("ai_mode_error"):
                    # 读不到模式时说明真实原因（多为学习窗口未开放）
                    note = item.extra["ai_mode_error"]
                if item.window_note and (item.unreachable or not note):
                    # 窗口探测给出的说法比「取不到跳转」更具体
                    note = item.window_note
                if item.unreachable:
                    # 进不去、也没有别的入口：现状下无法补完成。不放进「待处理」，
                    # 否则界面上会一直挂着一批永远做不了的作业。
                    status = STATUS_MANUAL
                quiz_info = item.extra.get("quiz_info")
                if quiz_info and quiz_info.get("summary"):
                    note = quiz_info["summary"]
                    if quiz_info.get("expired"):
                        status = STATUS_MANUAL
                score = item_score(item)
                threshold = item_threshold(item)
                below_threshold = is_below_threshold(item)
                redo_times = item.extra.get("redo_times")
                if below_threshold and item.plan_type == PLAN_TYPE_WORK:
                    # 低分作业：平台的重做是纯 UI 流程（本工具不支持），一跑就只会
                    # 得到「已经提交过了，无需重复作答」。所以要如实说「得了几分、
                    # 达标线多少、还能不能重做」，而不是让它继续显示成「可自动完成」。
                    status = STATUS_MANUAL
                    if redo_times is None:
                        tail = "本工具不支持作业重做，请到课程页面手动操作"
                    elif redo_times > 0:
                        tail = f"平台还剩 {redo_times} 次重做机会，请到课程页面手动重做"
                    else:
                        tail = "平台未开放重做，请老师开启重做"
                    note = f"得分 {score:g}，低于达标线 {threshold:g}；{tail}"
                items.append({
                    "planId": item.plan_id,
                    "name": item.name,
                    "planType": item.plan_type,
                    "typeLabel": item.type_label,
                    "isFinish": item.is_finish,
                    "isLock": item.is_lock,
                    "chooseStudy": item.choose_study,
                    "status": status,
                    "handler": handler,
                    "supported": status in (STATUS_AUTO, STATUS_PARTIAL),
                    "note": note,
                    "skipReason": item.skip_reason,
                    "windowState": item.window_state,
                    "windowNote": item.window_note,
                    "expired": item.unreachable,
                    "aiMode": ai_mode.get("mode") if ai_mode else None,
                    "aiModeLabel": ai_mode.get("modeLabel") if ai_mode else "",
                    "aiRules": ai_mode.get("rules") if ai_mode else [],
                    "entryUrl": ai_mode.get("entryUrl") if ai_mode else "",
                    "quizExpired": bool(quiz_info.get("expired")) if quiz_info else False,
                    "quizWindow": (quiz_info.get("window") or "") if quiz_info else "",
                    # 成绩：作业/测验/AI实践 才有分数；未达标（分数 < 平台达标线）
                    # 的任务点即使 isFinish 也可能仍挂着，界面据此提示「需重做」。
                    "score": score,
                    "threshold": threshold,
                    "belowThreshold": below_threshold,
                    "redoTimes": redo_times if isinstance(redo_times, int) else None,
                })
                if below_threshold:
                    result["summary"]["belowThresholdCount"] += 1
                if not item.is_finish:
                    if item.unreachable:
                        # 窗口已结束 / 尚未开放：单列，不混进「待处理」
                        result["summary"]["expiredCount"] += 1
                    else:
                        result["summary"]["pendingCount"] += 1
                        if status in (STATUS_AUTO, STATUS_PARTIAL):
                            result["summary"]["completableCount"] += 1
                        elif status == STATUS_MANUAL:
                            result["summary"]["manualCount"] += 1
                        else:
                            result["summary"]["unsupportedCount"] += 1

            result["summary"]["taskCount"] += 1
            course_entry["tasks"].append({
                "taskId": task.task_id,
                "name": task.name,
                "progress": task.progress,
                "allowStudy": task.allow_study,
                "allowStudyMessage": task.allow_study_message,
                "encryTaskUserId": task.encry_task_user_id,
                "pendingCount": len([i for i in task.pending_items if not i.unreachable]),
                "completableCount": len([i for i in task.pending_items
                                         if not i.unreachable and i.supported]),
                "expiredCount": len([i for i in task.pending_items if i.unreachable]),
                "belowThresholdCount": len([i for i in task.items
                                            if is_below_threshold(i)]),
                "groups": task.extra.get("groups") or [],
                "items": items,
            })

        result["courses"].append(course_entry)
        emit(type="course_done", index=index, total=len(courses),
             courseId=course_entry["courseId"], title=course_entry["title"],
             taskCount=len(course_entry["tasks"]))

    return result


def collect_plan_items(tasks: list[FanyaTask]) -> list[FanyaPlanItem]:
    """把多个任务的任务点摊平，便于执行器按顺序处理。"""
    items: list[FanyaPlanItem] = []
    for task in tasks:
        items.extend(task.items)
    return items


# ======================================================================
# 执行器
# ======================================================================


@dataclass
class CompletionContext:
    """单个任务点的执行上下文，传给各 handler。"""

    chaoxing: Any
    client: FanyaTaskClient
    course: dict
    task: FanyaTask
    item: FanyaPlanItem
    generator: ContentGenerator
    study_url: dict
    options: dict
    # 是否要求「生成后暂停、逐条审批」：为 True 时，处理器只准备内容，
    # 把「预览 + 提交回调」登记到 approvals，绝不真实提交。
    require_approval: bool = True
    approvals: Any = None
    # 返回 True 表示已收到停止请求（由 runner 注入）。长耗时的处理器
    # （尤其是文档的真实浏览器阅读）要靠它及时收手。
    stop_requested: Any = None

    def log(self, message: str) -> None:
        logger.info(message)

    @property
    def label(self) -> str:
        return (f"{self.course.get('title') or ''} / {self.task.name} / {self.item.name}")

    def register_approval(self, kind: str, preview: str,
                          submit_fn: Optional[Callable[[], tuple[bool, str]]] = None,
                          extra: Optional[dict] = None) -> Optional[str]:
        """登记一个待审批项，返回其 id（未启用审批时返回 ``None``）。"""
        if not self.require_approval or self.approvals is None:
            return None
        entry = self.approvals.add(kind=kind, label=self.label, preview=preview,
                                   submit_fn=submit_fn, extra=extra or {})
        return entry.id


class FanyaTaskRunner:
    """按任务点逐个执行自动完成，并把进度通过回调上报给前端。"""

    def __init__(self, chaoxing: Any, client: Optional[FanyaTaskClient] = None,
                 generator: Optional[ContentGenerator] = None,
                 options: Optional[dict] = None,
                 on_event: Optional[Callable[[dict], None]] = None):
        self.chaoxing = chaoxing
        self.client = client or FanyaTaskClient()
        self.generator = generator or ContentGenerator()
        self.options = options or {}
        self.on_event = on_event
        self.results: list[dict] = []
        self.stop_event = self.options.get("stop_event")
        # 「并发任务点数」：每个任务点的累计时长门槛是**各算各的**，所以并行 N 个
        # 任务点 = N 倍吞吐（这是把几十分钟的等待压缩掉的主要手段）。
        # 默认 1（串行），前端可调到 2~8；并发会成倍占用连接与浏览器实例，
        # 所以上限压在 8。
        try:
            self.concurrency = max(1, min(int(self.options.get("concurrency") or 1), 8))
        except (TypeError, ValueError):
            self.concurrency = 1
        # 并行时事件回调会在多个线程里被调用；加锁让前端状态更新永远串行，
        # 否则 app.py 里的 stats 计数会出现读改写竞争。
        self._emit_lock = threading.Lock()
        # 「重做模式」：允许选中**已完成**的任务点再答一次
        # （平台按 remainScoreCount 允许重练；作业/测验的重做是平台自己的 UI 流程，
        #   未探明请求格式，因此这些类型在重做模式下会明确拒绝而不是硬写）
        self.redo = bool(self.options.get("redo"))
        if self.redo:
            # AI实践 的重做＝先结束旧作答记录再建新卷（平台没有放弃接口）
            self.options = {**self.options, "ai_redo": True}
        # 「生成后暂停、逐条审批」：默认开启（options.require_approval=False 才关闭）
        self.require_approval = bool(self.options.get("require_approval", True))
        self.approvals = self.options.get("approvals")
        if self.approvals is None:
            from api.fanya_approval import STORE
            self.approvals = STORE

    # ---------------------------------------------------------------- 事件

    def _emit(self, **payload) -> None:
        if callable(self.on_event):
            try:
                with self._emit_lock:
                    self.on_event(payload)
            except Exception as exc:  # noqa: BLE001 - 回调失败不影响主流程
                logger.debug("上报任务完成事件失败: {}", exc)

    def _stop_requested(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    # ---------------------------------------------------------------- 主流程

    def run_courses(self, courses: list[dict], tasks_by_course: Optional[dict] = None) -> list[dict]:
        """对每门课程下的每个任务执行自动完成。

        ``tasks_by_course`` 可选，形如 ``{courseId: [taskId, ...]}``，
        用于只处理用户勾选的任务。未提供时处理所有任务。
        """
        course_index = 0
        for course in courses:
            if self._stop_requested():
                break
            course_index += 1
            course_id = str(course.get("courseId"))
            title = course.get("title") or course_id
            self._emit(type="course_start", courseId=course_id, title=title,
                       index=course_index, total=len(courses))

            try:
                tasks = self.client.get_course_tasks(course.get("courseId"), course.get("clazzId"))
            except FanyaTaskError as exc:
                logger.error("课程「{}」任务读取失败: {}", title, exc)
                self._emit(type="course_error", courseId=course_id, title=title, error=str(exc))
                continue

            wanted = (tasks_by_course or {}).get(course_id)
            if wanted:
                wanted_set = {int(x) for x in wanted}
                tasks = [t for t in tasks if int(t.task_id) in wanted_set]

            for task in tasks:
                if self._stop_requested():
                    break
                self.run_task(course, task)

            self._emit(type="course_done", courseId=course_id, title=title)
        return self.results

    def run_task(self, course: dict, task: FanyaTask, only_plan_ids: Optional[list[int]] = None) -> list[dict]:
        """执行单个任务下的所有可自动化任务点。"""
        try:
            self.client.load_task_items(task)
        except FanyaTaskError as exc:
            logger.error("任务「{}」任务点读取失败: {}", task.name, exc)
            return self.results

        # 必须先识别 AI实践 子模式 / 自测状态，再 classify_items：
        # resolve_item() 依赖 item.extra["ai_mode"] 才能选出模式专属处理器
        # （代码实践 / 思维阶梯）。顺序反了的话 AI实践 会被判成「不支持」而被跳过
        # —— 预览说可勾选、执行却跳过。
        _describe_ai_practice_items(self.client, course, task, redo=self.redo)
        _describe_quiz_items(self.client, task, redo=self.redo)
        classify_items(task)
        allow, allow_msg = self.client.check_allow_study(task.encry_task_user_id)
        task.allow_study = allow
        task.allow_study_message = allow_msg

        wanted = {int(x) for x in only_plan_ids} if only_plan_ids else None
        candidates = []
        for item in task.items:
            if wanted is not None and int(item.plan_id) not in wanted:
                continue
            if item.is_finish and not self.redo:
                continue
            candidates.append(item)

        logger.info(
            "任务「{}」：共 {} 个任务点，待完成 {} 个，其中可自动化 {} 个",
            task.name, len(task.items), len(candidates),
            len([i for i in candidates if i.supported]),
        )

        if not task.allow_study:
            # 服务端会直接拒绝未到学习时间的任务点（getToStudyUrl 返回
            # “任务学习时间：… 当前不在学习时间内”），因此这里只做记录，
            # 不去逐条尝试，避免制造一堆必然失败的请求。
            reason = task.allow_study_message or "当前不在任务学习时间内"
            logger.warning("任务「{}」暂不可学习：{}", task.name, reason)
            for item in candidates:
                result = {
                    "courseId": str(course.get("courseId")),
                    "courseTitle": course.get("title") or "",
                    "taskId": task.task_id,
                    "taskName": task.name,
                    "planId": item.plan_id,
                    "name": item.name,
                    "typeLabel": item.type_label,
                    "planType": item.plan_type,
                    "handler": adapter_for(item.plan_type).handler,
                    "ok": False,
                    "skipped": True,
                    "message": f"未到学习时间：{reason}",
                }
                self.results.append(result)
                self._emit(type="item_done", **result)
            self._emit(type="task_done", courseId=str(course.get("courseId")),
                       taskId=task.task_id, name=task.name, progress=task.progress)
            return self.results

        self._emit(type="task_start", courseId=str(course.get("courseId")),
                   taskId=task.task_id, name=task.name, pending=len(candidates))

        if self.concurrency > 1 and len(candidates) > 1:
            self._run_items_parallel(course, task, candidates)
        else:
            for item in candidates:
                if self._stop_requested():
                    logger.warning("已请求停止，剩余任务点跳过")
                    break
                self.run_item(course, task, item)

        progress = self.client.refresh_task(task.encry_task_user_id)
        self._emit(type="task_done", courseId=str(course.get("courseId")),
                   taskId=task.task_id, name=task.name, progress=progress)
        if progress is not None:
            logger.info("任务「{}」进度已更新为 {}", task.name, _format_progress(progress))
        return self.results

    def _worker_client(self) -> FanyaTaskClient:
        """并行 worker 的私有客户端（独立 session）。

        ``requests.Session`` 的 cookie jar 与连接池都不支持并发写，多个 worker
        共用一个 session 会出现串号/丢 cookie；这里给每个 worker 复制一份。
        复制失败就退回共享 session（功能仍然可用，只是并发安全性下降）。
        """
        try:
            return FanyaTaskClient(session=SessionManager.clone_session())
        except Exception as exc:  # noqa: BLE001 - 复制失败不应中断运行
            logger.warning("并行会话复制失败，退回共享会话：{}", exc)
            return self.client

    def _run_items_parallel(self, course: dict, task: FanyaTask,
                            candidates: list[FanyaPlanItem]) -> None:
        """并发执行同一任务下的多个任务点。

        每个任务点的「累计观看/阅读时长」是**各任务点各算各的**，所以并发跑的
        总等待时间 ≈ 最慢的那个任务点，而不是所有任务点之和 —— 这是把「一轮跑
        几十分钟」压缩掉的主要手段。并发数上限 8（每个视频任务点都会起一个真实
        浏览器，太多会拖慢整机）。
        """
        logger.info("并发执行任务「{}」的 {} 个任务点（并发数 {}）",
                    task.name, len(candidates), self.concurrency)
        self._emit(type="task_parallel", taskId=task.task_id, name=task.name,
                   concurrency=self.concurrency, pending=len(candidates))

        with ThreadPoolExecutor(max_workers=self.concurrency,
                                thread_name_prefix="fanya-item") as pool:
            futures = {}
            for item in candidates:
                if self._stop_requested():
                    logger.warning("已请求停止，剩余任务点不再提交")
                    break
                client = self._worker_client()
                futures[pool.submit(self.run_item, course, task, item, client)] = item
            for fut in as_completed(futures):
                item = futures[fut]
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001 - 单点失败不影响其他任务点
                    logger.opt(exception=True).error(
                        "任务点「{}」并发执行异常: {}", item.name, exc)

    def run_item(self, course: dict, task: FanyaTask, item: FanyaPlanItem,
                 client: Optional[FanyaTaskClient] = None) -> dict:
        """执行一个任务点，返回结果字典。

        ``client`` 由并发调度器注入（每个 worker 一份独立 session）；串行调用
        时省略即可，默认用 runner 自己的 client。
        """
        worker = client or self.client
        _, handler_name, _ = resolve_item(item)
        # 重做闸门：只有 AI实践 有安全的自动重做路径（平台按 remainScoreCount
        # 允许重练）。作业/测验的「重做」是平台自己的 UI 流程，请求格式未探明，
        # 其余类型（视频/文档/章节/音频/讨论/分组）重做没有意义，一律拒绝。
        label = f"[{item.type_label}] {item.name}"
        logger.info("开始处理任务点: {}", label)

        result = {
            "courseId": str(course.get("courseId")),
            "courseTitle": course.get("title") or "",
            "taskId": task.task_id,
            "taskName": task.name,
            "planId": item.plan_id,
            "name": item.name,
            "typeLabel": item.type_label,
            "planType": item.plan_type,
            "handler": handler_name,
            "ok": False,
            "skipped": False,
            "verified": None,
            "message": "",
        }
        self._emit(type="item_start", **{k: result[k] for k in
                                        ("courseId", "courseTitle", "taskId", "taskName",
                                         "planId", "name", "typeLabel")})

        # 重做闸门：只有 AI实践 有安全的自动重做路径（平台按 remainScoreCount 允许
        # 重练，做法是结束旧作答记录再建新卷）。作业/测验的「重做」是平台自己的 UI
        # 流程，请求格式未探明；其余类型（视频/文档/章节/音频/讨论/分组）重做没有
        # 意义 —— 这些一律拒绝，绝不硬写。
        if self.redo and item.is_finish and item.plan_type != PLAN_TYPE_AI_PRACTICE:
            result["skipped"] = True
            result["message"] = ("该类型不支持自动重做：请在课程页面用「重做 / 重新练习」"
                                 "手动操作（自动重做目前仅支持 AI实践）")
            logger.warning("跳过重做 {}：{}", label, result["message"])
            self.results.append(result)
            self._emit(type="item_done", **result)
            return result

        if not item.supported:
            result["skipped"] = True
            result["message"] = item.skip_reason or "该类型暂不支持自动完成"
            logger.warning("跳过任务点 {}：{}", label, result["message"])
            self.results.append(result)
            self._emit(type="item_done", **result)
            return result

        handler = HANDLERS.get(handler_name)
        if handler is None:
            result["skipped"] = True
            result["message"] = f"处理器 {handler_name} 尚未实现"
            logger.warning("跳过任务点 {}：{}", label, result["message"])
            self.results.append(result)
            self._emit(type="item_done", **result)
            return result

        study_url: dict = {}
        try:
            study_url = worker.get_study_url(task.encry_task_user_id, item.encrypt_plan_id)
        except FanyaTaskError as exc:
            # 常见原因：该任务点有自己的有效期，服务端会返回
            # “任务点学习时间：… 当前不在学习时间内”。这时服务端**什么都不给**
            # （实测 code=400，没有 url / workId / publishRelationUuid），
            # 依赖它的类型就只能记为跳过。
            #
            # 但「章节」类不依赖它（见 HANDLERS_WITHOUT_STUDY_URL）：窗口关闭只是
            # 任务引擎那条腿被掐，章节内容本身仍可学习 —— 实测 2026-10-16 在窗口
            # 已结束的「5.1 人口增长-马尔萨斯模型视频教学」上仍能正常播放并上报。
            # 这里如果一并跳过，就是把还能做的事误判成不能做。
            if handler_name not in HANDLERS_WITHOUT_STUDY_URL:
                result["skipped"] = True
                result["message"] = f"无法进入任务点：{exc}"
                logger.warning("跳过任务点 {}：{}", label, result["message"])
                self.results.append(result)
                self._emit(type="item_done", **result)
                return result
            result["windowBlocked"] = True
            logger.warning("任务点 {} 取不到学习跳转（{}），但「{}」不依赖它，继续执行",
                           label, exc, handler_name)

        ctx = CompletionContext(
            chaoxing=self.chaoxing,
            client=worker,
            course=course,
            task=task,
            item=item,
            generator=self.generator,
            study_url=study_url or {},
            options=self.options,
            require_approval=self.require_approval,
            approvals=self.approvals,
            stop_requested=self._stop_requested,
        )

        try:
            ok, message = handler(ctx)
            result["ok"] = bool(ok)
            result["message"] = message or ("完成" if ok else "未成功")
        except Exception as exc:  # noqa: BLE001 - 单个任务点失败不应中断整门课程
            logger.error("任务点 {} 处理异常: {}", label, exc)
            # ★ loguru 不认 ``exc_info=True``（那会被当成 sink 的格式参数，堆栈根本
            #   打不出来）。要堆栈必须用 ``opt(exception=True)``。
            logger.opt(exception=True).debug("异常详情")
            result["message"] = f"异常: {type(exc).__name__}: {exc}"

        if result["ok"]:
            logger.info("任务点完成: {} — {}", label, result["message"])
            # scoreRefresh 在本账号上是空操作，因此改为重新拉取分组下的任务点来校验
            group_id = item.extra.get("group_encry_id") or task.group_encry_id
            verified = worker.verify_plan_finished(
                task.encry_task_user_id, group_id, item.plan_id)
            result["verified"] = verified
            if verified is True:
                result["message"] = f"{result['message']}（任务点状态已确认完成）"
            elif verified is False:
                result["message"] = (
                    f"{result['message']}（注意：任务点状态仍为未完成，"
                    "可能需要等待老师评分或平台同步）"
                )
                logger.warning("任务点 {} 提交成功但状态仍为未完成", label)
            else:
                logger.debug("未能校验任务点 {} 的完成状态", label)
        else:
            logger.warning("任务点未完成: {} — {}", label, result["message"])

        self.results.append(result)
        self._emit(type="item_done", **result)
        return result


# handler 注册表在文件末尾由各适配器模块填充
#: 这些处理器**不需要** ``getToStudyUrl`` 的结果（只用课程 id + ``plan.externalDataId``）。
#: 章节类任务点经常「任务点窗口已结束、章节内容本身仍可学习」，因此窗口失败时
#: 不能像别的类型那样直接跳过 —— 那是把还能做的事误判成不能做。
#: 实测（2026-10-16）：窗口 2026-09-20~09-30 已结束的 plan 8110517
#: 「5.1 人口增长-马尔萨斯模型视频教学」仍可 get_course_point → get_job_list →
#: process_job 正常播放/上报。
HANDLERS_WITHOUT_STUDY_URL = {"chapter"}

HANDLERS: dict[str, Callable[[CompletionContext], tuple[bool, str]]] = {}


def register_handler(name: str):
    def decorator(func):
        HANDLERS[name] = func
        return func
    return decorator


def _session_puid(session: requests.Session) -> str:
    """当前登录用户的 puid（讨论区回复比对的正是这个值，而非 uid）。"""
    for key in ("_uid", "UID"):
        value = session.cookies.get(key)
        if value:
            return str(value)
    return ""


# ----------------------------------------------------------------------
# 适配器：主题讨论（planType 14）
# ----------------------------------------------------------------------


@register_handler("discussion")
def handle_discussion(ctx: CompletionContext) -> tuple[bool, str]:
    """为主题讨论任务点生成一段回复并提交。"""
    from api import fanya_discuss as fd

    session = ctx.client.session
    class_id = str(ctx.course.get("clazzId") or ctx.course.get("classId") or "")
    course_id = str(ctx.course.get("courseId") or "")

    topic = fd.parse_topic_from_study_url(ctx.study_url, class_id=class_id)
    if not topic["bbsId"] or not topic["topicUuid"]:
        return False, "未能从任务点解析出 bbsid / topicUuid，无法定位讨论帖"

    puid = _session_puid(session)
    if not puid:
        return False, "未能取得当前用户 puid，无法判断是否已回复"

    try:
        replied, floor, _ = fd.has_replied(session, topic["bbsId"], topic["topicUuid"],
                                           topic["classId"] or class_id, puid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("查询讨论回复列表失败: {}", exc)
        replied, floor = False, None

    if replied:
        return True, f"该话题下已有本账号回复（{floor}），无需重复提交"

    question = ctx.item.name or topic.get("title") or ""
    text = None
    if ctx.generator.available:
        text = ctx.generator.generate_discussion_reply(
            question, course_title=ctx.course.get("title") or "")
    if not text:
        logger.warning("使用模板文本作为讨论回复")
        text = fd.fallback_reply_text(question, ctx.course.get("title") or "")
    logger.info("拟发布回复（{} 字）：{}", len(text), text[:80].replace("\n", " "))

    topic_class = topic["classId"] or class_id

    def _publish() -> tuple[bool, str]:
        """真正发布回复（仅在审批通过或未启用审批时执行）。"""
        try:
            token = fd.scrape_url_token(session, topic["bbsId"], topic["topicUuid"],
                                        topic_class)
            result = fd.reply_to_topic(
                session, topic["bbsId"], topic["topicUuid"], topic_class,
                course_id, text, url_token=token,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"发布回复失败: {type(exc).__name__}: {exc}"

        if not result.get("status"):
            return False, f"发布回复被拒绝: {result.get('msg') or result}"

        # 复核：确认回复确实出现在列表里
        try:
            time.sleep(1.5)
            replied2, floor2, _ = fd.has_replied(session, topic["bbsId"], topic["topicUuid"],
                                                 topic_class, puid)
        except Exception as exc:  # noqa: BLE001
            logger.debug("复核回复结果失败: {}", exc)
            replied2, floor2 = False, None

        if replied2:
            return True, f"已发布回复（{floor2}）"
        return True, "回复已提交（列表复核未命中，可能是分页延迟）"

    # 审批模式：只生成不发布
    if ctx.require_approval:
        entry_id = ctx.register_approval(
            kind="discussion",
            preview=f"{ctx.label}\n话题：{question[:120]}\n\n拟发布回复：\n{text}",
            submit_fn=_publish,
            extra={"topicUuid": topic["topicUuid"]},
        )
        return True, f"已生成回复（{len(text)} 字），待审批后发布 [id={entry_id}]"

    return _publish()


# ----------------------------------------------------------------------
# 适配器：文档（planType 11）
# ----------------------------------------------------------------------


@register_handler("document")
def handle_document(ctx: CompletionContext) -> tuple[bool, str]:
    """完成「文档」任务点。

    任务引擎自己的文档学习页（``task.chaoxing.com/documentStudy/learnPage``）
    在云盘阅读器上报 ``isFinished=1`` 时会调用 ``GET /documentStudy/readEnd``。
    这里直接复现该请求，它能让 ``completeWeightRate`` 变成 100（=「读完」）。

    ⚠️ **但读完了不等于完成**：若任务点设了 ``enableDocumentWatchDuration``，
    服务端还要求 ``score.totalTime >= documentWatchDuration × 60000`` 毫秒。
    实测（2026-10-15，受控实验）：

    * ``readEnd`` 返回 ``{"result": true}`` 却**一分一秒都不计入** ``totalTime``；
    * 真等 40 秒再调 ``readEnd``、连调多次，``totalTime`` 仍是 ``None``；
    * ``documentStudy`` 下**没有** ``dataLog`` 之类的时长端点（4 个候选全 404）。

    文档的时长由云盘阅读器 iframe（``pan-yz.chaoxing.com``）按停留时间累计：
    学生真实滚动、阅读器判到底后才 ``postMessage({isFinished:1})``。
    ★ 该埋点（``data-xxt.aichaoxing.com/analysis/ac_mark``）**纯 HTTP 重放无效** ——
    抓到的 URL 原样重发（带正确 ``enc`` 签名）、篡改签名、伪造停留毫秒 ``h``，
    三种都让 ``totalTime`` 保持 Δ=0。

    因此叠加了时长门槛的文档**必须走浏览器通道**：见 ``api/fanya_browser.py``。
    这里先做 HTTP 的 ``readEnd``（把「读完」置 100%），再用真实浏览器滚动补足时长。
    浏览器不可用时**如实返回失败**并说明原因，不假装成功。
    """
    url = ctx.study_url.get("url") or ""
    match = re.search(r"encryPlanUserId=([^&\s]+)", url)
    if not match:
        return False, f"未能从任务点跳转地址解析出 encryPlanUserId：{url[:120]}"

    encry_plan_user_id = urllib.parse.unquote(match.group(1))
    read_end_url = f"{TASK_DOMAIN}/documentStudy/readEnd"
    try:
        resp = ctx.client.session.get(
            read_end_url,
            params={"encryPlanUserId": encry_plan_user_id},
            headers={
                "Referer": url,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=ctx.client.timeout,
        )
    except requests.RequestException as exc:
        return False, f"上报文档阅读完成失败: {exc}"

    if resp.status_code != 200:
        return False, f"上报文档阅读完成返回 HTTP {resp.status_code}"

    try:
        body = resp.json()
    except ValueError:
        return False, f"上报文档阅读完成返回非 JSON: {resp.text[:160]}"

    if not body.get("result"):
        return False, f"上报文档阅读完成被拒绝: {body.get('message') or body.get('msg') or body}"

    # 没设时长门槛：readEnd 就够了。
    required_ms = watch_duration_ms(ctx.item.extra, "document")
    if required_ms <= 0:
        return True, "已上报文档阅读完成"

    # 叠加了累计阅读时长门槛 —— 纯 HTTP 到不了终点，改用浏览器通道。
    got = _optional_watched_ms(ctx)
    if got is not None and got >= required_ms:
        return True, (f"已上报文档阅读完成；累计阅读已达标（服务端记 "
                      f"{got / 1000.0:.0f} 秒 ≥ {required_ms / 60000.0:g} 分钟）")

    logger.info(
        "文档「{}」要求累计阅读 {:.0f} 分钟（服务端已记 {}），改用真实浏览器滚动补足",
        ctx.item.name, required_ms / 60000.0,
        f"{got / 1000.0:.0f} 秒" if got is not None else "未知",
    )

    try:
        from api import fanya_browser
    except ImportError as exc:  # pragma: no cover - 理论上不会发生
        return False, f"浏览器通道模块不可用：{exc}"

    try:
        budget = float(ctx.options.get("doc_read_max_seconds") or 1800.0)
    except (TypeError, ValueError):
        budget = 1800.0

    def _read_state() -> tuple:
        """回读 (isFinish, totalTime_ms, completeWeightRate)。"""
        group_id = (ctx.item.extra or {}).get("group_encry_id") or ctx.task.group_encry_id
        raw = ctx.client.get_plan_item(ctx.task.encry_task_user_id, group_id,
                                       ctx.item.plan_id) or {}
        score = raw.get("score") or {}
        return (bool(raw.get("isFinish")), watched_ms(score),
                score.get("completeWeightRate"))

    try:
        ok, detail = fanya_browser.read_document_in_browser(
            ctx.client.session,
            fanya_browser.document_learn_url(encry_plan_user_id),
            required_ms,
            _read_state,
            budget_seconds=budget,
            headless=bool(ctx.options.get("browser_headless", True)),
            # 「停止」要能打断文档阅读：否则一个 30 分钟的文档会把停止请求
            # 一起卡到预算耗尽（用户看到的「正在停止…」迟迟不动）。
            should_stop=ctx.stop_requested,
            on_log=logger.info,
        )
    except fanya_browser.BrowserUnavailable as exc:
        return False, (
            f"已上报「文档阅读完成」（阅读进度 100%），但该任务点还要求累计阅读 "
            f"{required_ms / 60000.0:g} 分钟。文档时长只能由真实浏览器滚动阅读产生"
            f"（HTTP 的 readEnd 不计时长，埋点也不可重放），而浏览器通道当前不可用："
            f"{exc}。请安装 playwright 后重试，或用浏览器手动打开该文档停留阅读。"
        )

    return ok, detail


# ----------------------------------------------------------------------
# 适配器：视频（planType 10）/ 音频（planType 22）
# ----------------------------------------------------------------------

# 任务引擎自有的播放页 → 进度上报前缀
# 「累计观看时长」门槛的字段名（都在 planBreakthroughSet 里）。
# 实测（2026-10-15，27 门课 220 个任务点）：planBreakthroughSet 里唯一有判别力
# 的完成条件就是它 —— 设了 enableXxxWatchDuration=1 的 15 个任务点里 14 个未完成，
# 且未完成的全部满足 score.totalTime < watchDuration × 60000。
_WATCH_DURATION_KEYS = {
    "video": ("enableVideoWatchDuration", "videoWatchDuration"),
    "audio": ("enableAudioWatchDuration", "audioWatchDuration"),
    "document": ("enableDocumentWatchDuration", "documentWatchDuration"),
}

# 观看时长补足时的心跳间隔（秒）与回读服务端的间隔（秒）。
# 心跳间隔取 6 秒，与页面 videojs 插件的 setInterval 一致。
_WATCH_HEARTBEAT_SECONDS = 6.0
_WATCH_POLL_SECONDS = 30.0
# 达标后等待 isFinish 翻转的最长时间（秒）——服务端结算是异步的。
_WATCH_SETTLE_SECONDS = 45.0


def watch_duration_ms(extra: dict, kind: str) -> int:
    """返回任务点要求的累计观看时长（毫秒）；没设要求时返回 ``0``。"""
    keys = _WATCH_DURATION_KEYS.get(kind)
    if not keys:
        return 0
    enabled_key, value_key = keys
    bts = (extra or {}).get("planBreakthroughSet") or {}
    if not bts.get(enabled_key):
        return 0
    try:
        minutes = float(bts.get(value_key) or 0)
    except (TypeError, ValueError):
        return 0
    if minutes <= 0:
        return 0
    return int(minutes * 60 * 1000)


def watched_ms(score: Any) -> int:
    """从 ``score`` 里取出服务端记录的累计观看毫秒数（``totalTime``）。"""
    if not isinstance(score, dict):
        return 0
    try:
        return int(score.get("totalTime") or 0)
    except (TypeError, ValueError):
        return 0


def _optional_watched_ms(ctx: CompletionContext) -> Optional[int]:
    """回读任务点当前的累计观看毫秒数；读取失败返回 ``None``。"""
    group_id = (ctx.item.extra or {}).get("group_encry_id") or ctx.task.group_encry_id
    raw = ctx.client.get_plan_item(ctx.task.encry_task_user_id, group_id, ctx.item.plan_id)
    if raw is None:
        return None
    return watched_ms(raw.get("score"))


def _satisfy_watch_duration(ctx: CompletionContext, kind: str, url: str, encry_id: str,
                            duration: int, required_ms: int,
                            report: Callable[[int, int], tuple[bool, str]],
                            headers: dict) -> tuple[bool, str]:
    """按真实时间补足累计观看时长，直到服务端记录达标。

    平台把观看时长记在 ``score.totalTime``（毫秒），达标线是
    ``planBreakthroughSet.{video,audio,document}WatchDuration``（**分钟**）。
    2026-10-15 受控实验确认的三条规则：

    1. **一次性跳到片尾计 0** —— 上报返回 ``result:true`` 但 ``totalTime`` 不变；
    2. **几秒内连发一串心跳计 0** —— 大跳 ``currentTime`` 同样不给分；
    3. **只认真实经过的时间** —— 真实 6 秒心跳 ×7（墙上 42.6 秒）换来 ``totalTime``
       增长；倍速节奏有折扣（3 倍速实测约 0.63 倍）。

    因此这里只能按真实节奏发心跳，并定期回读服务端确认，不能靠跳播取巧。
    """
    heartbeat = max(2.0, float(ctx.options.get("video_heartbeat")
                               or _WATCH_HEARTBEAT_SECONDS))
    try:
        speed = float(ctx.options.get("video_speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(1.0, min(speed, 8.0))
    try:
        budget = float(ctx.options.get("video_max_seconds") or 7200.0)
    except (TypeError, ValueError):
        budget = 7200.0
    stop_event = ctx.options.get("stop_event")

    got = _optional_watched_ms(ctx)
    if got is not None and got >= required_ms:
        return True, (f"已满足累计观看时长（服务端已记 {got / 1000.0:.0f} 秒 ≥ "
                      f"{required_ms / 60000.0:g} 分钟）")

    logger.info(
        "{}任务点要求累计观看 {:.0f} 分钟（服务端已记 {}），按真实时间补足",
        kind, required_ms / 60000.0,
        f"{got / 1000.0:.0f} 秒" if got is not None else "未知",
    )

    ok, msg = report(0, 0)
    if not ok:
        return False, f"上报「开始播放」被拒绝：{msg}"

    started = time.time()
    last_poll = started
    current = 0
    best = got or 0

    while True:
        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
            report(current, 2)
            return False, (f"收到停止请求，已累计观看约 {best / 1000.0:.0f} 秒，"
                           f"未达 {required_ms / 60000.0:g} 分钟")
        if time.time() - started > budget:
            report(current, 2)
            return False, (f"超过单任务点时间上限（{budget:.0f} 秒），已累计观看约 "
                           f"{best / 1000.0:.0f} 秒，未达 {required_ms / 60000.0:g} 分钟")

        # 兜底：即使一直读不到服务端计数，也不能无限播下去。
        # 实测 3 倍速只按约 0.63 倍墙上时间计入，留 2.2 倍余量足够。
        elapsed = time.time() - started
        if best <= 0 and elapsed > required_ms / 1000.0 * 2.2:
            report(current, 2)
            return False, (f"已按真实时间播放 {elapsed:.0f} 秒，但始终读不到服务端的"
                           f"累计观看计数（要求 {required_ms / 60000.0:g} 分钟），"
                           f"无法确认是否达标")

        time.sleep(heartbeat)
        current = min(duration, current + int(round(heartbeat * speed)))
        ok, msg = report(current, 1)
        if not ok:
            return False, f"播放心跳被拒绝：{msg}"

        now = time.time()
        if now - last_poll < _WATCH_POLL_SECONDS:
            continue
        last_poll = now
        got = _optional_watched_ms(ctx)
        if got is not None:
            best = max(best, got)
            logger.info("累计观看进度：服务端 {:.0f} 秒 / 要求 {:.0f} 秒",
                        got / 1000.0, required_ms / 1000.0)
            if got >= required_ms:
                report(current, 2)
                flipped = _await_finish(ctx, _WATCH_SETTLE_SECONDS)
                from api import fanya_video

                fanya_video.save_position(ctx.client.session, encry_id, current,
                                          timeout=ctx.client.timeout)
                if flipped:
                    return True, (f"已补足累计观看时长（服务端记 {got / 1000.0:.0f} 秒 ≥ "
                                  f"{required_ms / 60000.0:g} 分钟）")
                # 计数达标但完成位还没翻 —— 不说"完成"，把实情交给调用方
                return False, (f"累计观看已达 {got / 1000.0:.0f} 秒（要求 "
                               f"{required_ms / 60000.0:g} 分钟），但服务端尚未把任务点标为完成；"
                               f"稍后重试或等待平台结算")


def _await_finish(ctx: CompletionContext, seconds: float) -> bool:
    """等待服务端把 ``isFinish`` 翻成 true（结算有延迟）。返回是否已翻转。"""
    deadline = time.time() + seconds
    group_id = (ctx.item.extra or {}).get("group_encry_id") or ctx.task.group_encry_id
    while True:
        if ctx.client.verify_plan_finished(ctx.task.encry_task_user_id, group_id,
                                           ctx.item.plan_id) is True:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(5.0)


_PLAY_KINDS = {
    "video": {"page": "videoStudy/learnPage", "data_log": "videoDataLog/dataLog",
              "entity": "video"},
    "audio": {"page": "audioStudy/learnPage", "data_log": "audioDataLog/dataLog",
              "entity": "audio"},
}


def _parse_play_vo(html: str, entity: str) -> Optional[dict]:
    """从播放页 HTML 中解析 ``videoLearnVo`` / ``audioLearnVo``（页面内嵌 JSON）。"""
    pattern = rf"{entity}LearnVo\s*=\s*(\{{.*?\}})\s*(?:const|</script>|\n)"
    match = re.search(pattern, html, re.S)
    if not match:
        return None
    raw = match.group(1).replace("\\/", "/")
    try:
        return json.loads(raw)
    except ValueError as exc:
        logger.debug("解析 {}LearnVo 失败: {}", entity, exc)
        return None


def _handle_play(ctx: CompletionContext, kind: str) -> tuple[bool, str]:
    """视频/音频共用的完成逻辑。

    分两条路，取决于任务点是否设了**累计观看时长**要求：

    * **设了要求**（``enableVideoWatchDuration=1`` 等）：必须先按真实时间把
      ``score.totalTime`` 补到 ``watchDuration × 60000`` 毫秒。一次性上报返回
      ``result:true`` 是假象 —— 实测 ``totalTime`` 一点都不涨，这也是「上报成功
      但状态仍为未完成」的唯一原因。见 :func:`_satisfy_watch_duration`。
    * **没设要求**：服务端接受一次到位的进度上报（2026-10，planId 4097446，
      时长 1362 秒，``isFinish`` 立即翻转），直接报「开始 + 到片尾」即可。
    """
    spec = _PLAY_KINDS[kind]
    url = ctx.study_url.get("url") or ""
    if spec["page"] not in url:
        return False, f"任务点跳转地址不是任务引擎的{kind}播放页：{url[:120]}"

    session = ctx.client.session
    try:
        resp = session.get(url, timeout=ctx.client.timeout)
    except requests.RequestException as exc:
        return False, f"打开播放页失败: {exc}"
    if resp.status_code != 200:
        return False, f"打开播放页返回 HTTP {resp.status_code}"

    html = resp.content.decode("utf-8", errors="replace")
    vo = _parse_play_vo(html, kind)
    if not vo:
        return False, f"未能从播放页解析出 {kind}LearnVo"
    encry_id = vo.get("encryId")
    info = vo.get("videoInfo") or vo.get("audioInfo") or {}
    try:
        duration = int(info.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    if not encry_id or duration <= 0:
        return False, f"播放参数不完整（encryId={bool(encry_id)}, duration={duration}）"
    logger.info("{}任务点：时长 {} 秒，开始上报进度", kind, duration)

    headers = {"Referer": url, "X-Requested-With": "XMLHttpRequest"}

    def report(current_time: int, status: int) -> tuple[bool, str]:
        try:
            r = session.get(
                f"{TASK_DOMAIN}/{spec['data_log']}/{duration}/{int(current_time)}/{status}",
                params={"encryId": encry_id}, headers=headers, timeout=ctx.client.timeout,
            )
        except requests.RequestException as exc:
            return False, f"进度上报失败: {exc}"
        if r.status_code != 200:
            return False, f"进度上报返回 HTTP {r.status_code}"
        try:
            body = r.json()
        except ValueError:
            return False, f"进度上报返回非 JSON: {r.text[:120]}"
        if not body.get("result"):
            return False, str(body.get("message") or body)
        return True, ""

    # 0) 有累计观看时长要求时，必须真实把时间刷够，跳播取巧无效
    required_ms = watch_duration_ms(ctx.item.extra, kind)
    if required_ms > 0:
        return _satisfy_watch_duration(ctx, kind, url, encry_id, duration,
                                       required_ms, report, headers)

    # 1) 开始播放
    ok, msg = report(0, 0)
    if not ok:
        return False, f"上报「开始播放」被拒绝：{msg}"

    # 2) 一次跳到片尾；被拒则回退为「按真实时间模拟播放」
    ok, msg = report(duration, 2)
    if not ok:
        logger.warning("一次到位上报被拒（{}），回退为按真实时间模拟播放", msg)
        from api import fanya_video

        try:
            ok2, msg2 = fanya_video.simulate_playback(
                session,
                {"encryId": encry_id,
                 "currentTime": 0,
                 "farthestTimeValue": 0,
                 "videoInfo": {"duration": duration}},
                heartbeat_seconds=float(ctx.options.get("video_heartbeat")
                                        or fanya_video.DEFAULT_HEARTBEAT_SECONDS),
                speed=float(ctx.options.get("video_speed") or 1.0),
                max_wall_seconds=float(ctx.options.get("video_max_seconds") or 7200.0),
                progress_callback=lambda cur, total: logger.info(
                    "播放进度 {:.1f}%（{}/{} 秒）", cur * 100.0 / max(total, 1), cur, total),
                stop_event=ctx.options.get("stop_event"),
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"真实时长播放失败: {exc}"
        if not ok2:
            return False, msg2
        return True, msg2

    # 3) 记录播放位置
    try:
        session.get(
            f"{TASK_DOMAIN}/planUserSchedule/lastCurrentTime/{duration}",
            params={"encryId": encry_id}, headers=headers, timeout=ctx.client.timeout,
        )
    except requests.RequestException as exc:
        logger.debug("记录播放位置失败: {}", exc)

    return True, f"已上报{duration}秒观看进度"


@register_handler("video")
def handle_video(ctx: CompletionContext) -> tuple[bool, str]:
    return _handle_play(ctx, "video")


@register_handler("audio")
def handle_audio(ctx: CompletionContext) -> tuple[bool, str]:
    return _handle_play(ctx, "audio")


# ----------------------------------------------------------------------
# 适配器：AI实践 —— 代码实践(4) / 思维阶梯(2)
# ----------------------------------------------------------------------


@register_handler("ai_code")
def handle_ai_code(ctx: CompletionContext) -> tuple[bool, str]:
    """完成「AI实践 · 代码实践」(``data.type == 4``) 任务点。

    已真机验证：planId 4107919（COM0018A 项目1）由未完成变为已完成，得分 76。

    **审批模式（默认）**：只生成代码并跑完自测，把代码预览登记为待审批项，
    用户批准后才建卷交卷。
    """
    from api import fanya_ladder as ladder

    def _approval(code: str, lang_name: str, submit_fn) -> None:
        ctx.register_approval(
            kind="ai_code",
            preview=(f"{ctx.label}\n语言：{lang_name}｜已通过自测运行\n\n"
                     f"{code[:2000]}" + ("\n…（代码更长，提交时用完整版本）"
                                        if len(code) > 2000 else "")),
            submit_fn=submit_fn,
            extra={"language": lang_name, "codeLength": len(code)},
        )

    try:
        ok, msg = ladder.complete_code_task(
            ctx.client.session, ctx.course, ctx.study_url, ctx.generator,
            options=ctx.options, on_log=logger.info,
            approval=_approval if ctx.require_approval else None,
        )
    except ladder.LadderAbort as exc:
        return False, f"代码实践已中止（未提交）：{exc}"
    except ladder.LadderError as exc:
        return False, f"代码实践失败: {exc}"
    return ok, msg


@register_handler("ai_upload")
def handle_ai_upload(ctx: CompletionContext) -> tuple[bool, str]:
    """完成「AI实践 · 上传作品」(``data.type == 1``) 任务点。

    按题目要求生成作品（对话实录型会**真的**和 AI 聊够轮次，
    文本作品型生成达标字数），渲染成 PNG 并**保存到磁盘**。

    ⚠️ **不自动上传**：``fileModeType=0`` 的挂附件载荷尚未解析正确，
    贸然提交只会得到一条没有附件的空记录（实测评分服务返回 500）。
    作品图片会保存到项目下的 ``作品/`` 目录，由用户在浏览器手动上传。

    ⚠️ 题目若写明「禁止直接大段复制 AI 输出」，也不会自动提交。
    """
    from api import fanya_upload as up

    def _approval(preview: str, submit_fn) -> None:
        ctx.register_approval(
            kind="ai_upload",
            preview=f"{ctx.label}\n{preview}",
            submit_fn=submit_fn,
            extra={"kind": "upload"},
        )

    try:
        ok, msg = up.complete_upload_task(
            ctx.client.session, ctx.course, ctx.study_url, ctx.generator,
            options=ctx.options, on_log=logger.info,
            approval=_approval if ctx.require_approval else None,
            # 挂附件载荷已抓真实请求确认（扁平数组），交卷前会回读二次校验
            submit_enabled=True,
        )
    except up.UploadAbort as exc:
        return False, f"上传作品已中止（未提交）：{exc}"
    except up.UploadError as exc:
        return False, f"上传作品失败: {exc}"
    return ok, msg


@register_handler("ai_ladder")
def handle_ai_ladder(ctx: CompletionContext) -> tuple[bool, str]:
    """完成「AI实践 · 思维阶梯」(``data.type == 2``) 任务点。

    ✅ **已真机验证**（2026-10-12）：在「静电场环路定理闯关」上完整跑通 ——
    题目拆出 8 个小问、逐题作答（含单选与多选）、交卷、结题报告出分，
    得分 85；平台将任务点判定为完成（``isFinish=True``）。
    也验证了容错分支：建新卷失败时回退为「续做旧记录」。

    **审批模式（默认）下不执行**：该模式暂未实现「生成后暂停」，而它又未经真机
    验证，因此默认拒绝直接交卷，需显式开启 ``options.allow_ai_submit``。
    """
    if ctx.require_approval and not ctx.options.get("allow_ai_submit"):
        return False, ("思维阶梯尚未支持「生成后暂停」审批流程（链路本身已真机验证，"
                       "得分 85）；如确认要直接交卷，请开启「允许 AI实践直接提交」选项")

    from api import fanya_ladder as ladder

    # 「以目标分为标准，越高越好」：一次答不达标就再练一次。
    # 平台的达标标准是「多次练习平均分」，所以多刷几次把平均分抬上去是有效手段；
    # 每次重练都会先结束上一条记录再建新卷（平台没有放弃接口）。
    threshold = float(ctx.options.get("ladder_threshold") or 80)
    max_attempts = max(1, int(ctx.options.get("ladder_attempts") or 3))
    ids = ladder._ids(ctx.study_url, ctx.course)

    def current_score() -> float | None:
        try:
            data = ladder.load_data(ctx.client.session, ids)
        except ladder.LadderError:
            return None
        raw = data.get("answerScore")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    best: float | None = None
    last_msg = ""
    for attempt in range(1, max_attempts + 1):
        opts = dict(ctx.options)
        if attempt > 1:
            opts["ai_redo"] = True          # 结束旧记录后再建新卷
        logger.info("思维阶梯第 {}/{} 次作答（目标 {} 分）", attempt, max_attempts, threshold)
        try:
            ok, msg = ladder.complete_think_ladder(
                ctx.client.session, ctx.course, ctx.study_url, ctx.generator,
                options=opts, on_log=logger.info,
            )
        except ladder.LadderAbort as exc:
            return False, f"思维阶梯已中止（未提交）：{exc}"
        except ladder.LadderError as exc:
            return False, f"思维阶梯失败: {exc}"
        last_msg = msg
        if not ok:
            return ok, msg
        score = current_score()
        if score is not None:
            best = score if best is None else max(best, score)
            logger.info("第 {} 次得分：{}（目标 {}）", attempt, score, threshold)
            if score >= threshold:
                return True, f"思维阶梯已达标：第 {attempt} 次得 {score} 分（目标 {threshold}）"
        else:
            logger.info("第 {} 次已交卷，但暂时读不到分数", attempt)

    if best is None:
        return True, f"{last_msg}（试了 {max_attempts} 次，分数待平台刷新）"
    if best >= threshold:
        return True, f"思维阶梯已达标：最高 {best} 分（目标 {threshold}）"
    return True, (f"思维阶梯已提交 {max_attempts} 次，最高 {best} 分，"
                  f"仍未达 {threshold} 分（可在重做模式再试）")


@register_handler("ai_chat")
def handle_ai_chat(ctx: CompletionContext) -> tuple[bool, str]:
    """完成「AI实践 · 情景对话」(``data.type == 0``) 任务点。

    ⚠️ 尚未真机验证（账号内无未过期的情景对话样本）。默认进行 5 轮问答，
    可用选项 ``chat_turns`` 调整。

    **审批模式（默认）下不执行**：该链路**未真机验证**（与已实测的思维阶梯不同），
    且暂未实现「生成后暂停」，需显式开启 ``options.allow_ai_submit``。
    """
    if ctx.require_approval and not ctx.options.get("allow_ai_submit"):
        return False, ("情景对话尚未支持「生成后暂停」审批流程，且该链路未真机验证；"
                       "如确认要直接交卷，请开启「允许 AI实践直接提交」选项")

    from api import fanya_chat as chat

    try:
        ok, msg = chat.complete_chat_task(
            ctx.client.session, ctx.course, ctx.study_url, ctx.generator,
            options=ctx.options, on_log=logger.info,
        )
    except chat.ChatAbort as exc:
        return False, f"情景对话已中止（未提交）：{exc}"
    except chat.ChatError as exc:
        return False, f"情景对话失败: {exc}"
    return ok, msg


@register_handler("ai_story")
def handle_ai_story(ctx: CompletionContext) -> tuple[bool, str]:
    """推进「AI实践 · AI剧情」(``data.type == 7``) 的**章节对话**。

    协议来自客户端 JS 逆向 + 真机只读探测（见 ``docs/FANYA_TASK_API.md``）：
    剧情是一串「章节」，每章由 ``condition[].hit`` 判完成、``nextChapterUuid``
    串到下一章；每章要么是对话（``subtype=0``）要么是上传（``subtype=1``）。

    ⚠️ **两条边界**（都是刻意的，不是缺陷）：

    1. **每章最多发 ``options["story_max_rounds"]`` 轮**（默认 3）。平台每章
       上限 5 轮，用户明确要求「AI 先试 3 轮，达不到就把剩下的留给我自己做」，
       所以轮数用尽即停手并报告，绝不硬刷。
    2. **上传节点（``subtype=1``）尚未自动化**，遇到就停并说明。

    ``add-chat`` 会写入**不可撤销**的真实对话内容，故本处理器在审批模式下
    默认不执行，需显式开启 ``options["allow_ai_submit"]``。
    """
    if ctx.require_approval and not ctx.options.get("allow_ai_submit"):
        return False, ("AI剧情会写入不可撤销的真实对话内容，且最后一章「上传报告」"
                       "尚未自动化；如确认要执行，请开启「允许 AI实践直接提交」选项")

    from api import fanya_story as story

    try:
        ok, msg = story.complete_story_task(
            ctx.client.session, ctx.course, ctx.study_url, ctx.generator,
            options=ctx.options, on_log=logger.info,
        )
    except story.StoryAbort as exc:
        return False, f"AI剧情已停手：{exc}"
    except story.StoryError as exc:
        return False, f"AI剧情失败: {exc}"
    return ok, msg


# ----------------------------------------------------------------------
# 适配器：自测（17）/ 测验（5）
# ----------------------------------------------------------------------


@register_handler("quiz")
def handle_quiz(ctx: CompletionContext) -> tuple[bool, str]:
    """完成自测/测验任务点。

    ⚠️ 该链路**未经真机验证**（本账号没有未过期的考试样本）。因此默认行为是
    **只逐题暂存（tempSave=true），绝不交卷**；只有显式打开
    ``options["exam_submit"]`` 才会交卷。任何异常、未知题型、题库缺答案都会
    立刻中止 —— 保证不会误交白卷。
    """
    from api import fanya_exam as ex
    from api import fanya_quiz

    session = ctx.client.session
    course = ctx.course
    class_id = str(course.get("clazzId") or course.get("classId") or "")

    # 1) 先判过期：过期试卷无法补完成，直接给出明确结论
    if ctx.item.plan_type == PLAN_TYPE_QUIZ:
        info = fanya_quiz.describe_self_test(
            ctx.client, ctx.task.encry_task_user_id, ctx.item.encrypt_plan_id)
        if info and info.get("expired"):
            return False, "考试已过期，无法补完成（需联系老师处理）"

    # 2) 取入口 URL
    entry_url = (ctx.study_url or {}).get("url") or ""
    if ctx.item.plan_type == PLAN_TYPE_QUIZ:
        try:
            entry_url = ex.resolve_self_test_entry(
                ctx.client, ctx.task.encry_task_user_id, ctx.item.encrypt_plan_id) or entry_url
        except ex.ExamError as exc:
            return False, f"获取自测入口失败: {exc}"
    if not entry_url:
        return False, "未能取得考试入口地址"

    try:
        resp = session.get(entry_url, timeout=ctx.client.timeout, allow_redirects=True)
    except requests.RequestException as exc:
        return False, f"打开考试入口失败: {exc}"
    html = resp.content.decode("utf-8", errors="replace")

    # 3) 尝试进入作答页（入口可能是 lookPaper「查看详情」或 examnotes「须知」）
    if not ex.looks_like_answering_page(html):
        # 先识别「状态页」：过期 / 已交卷 / 从未参加。旧实现只会含糊地报
        # 「缺少必需隐藏域 saveUrl」，会把「考试窗口已结束」误报成页面结构问题。
        state = ex.describe_exam_page(html)
        if state:
            return False, f"{state}，无法补完成（需联系老师处理）"
        next_url = _find_answering_url(html, course, class_id)
        if not next_url:
            return False, ("已定位到考试入口，但当前无法取得作答页"
                           "（该链路尚未真机验证；请手动进入一次以确认页面结构）")
        try:
            resp = session.get(next_url, timeout=ctx.client.timeout, allow_redirects=True)
        except requests.RequestException as exc:
            return False, f"打开作答页失败: {exc}"
        html = resp.content.decode("utf-8", errors="replace")
        if not ex.looks_like_answering_page(html):
            state = ex.describe_exam_page(html)
            if state:
                return False, f"{state}，无法补完成（需联系老师处理）"
            return False, "打开的页面不是预期的作答页，已中止（不提交任何内容）"

    # 4) 逐题作答：默认只暂存
    submit_enabled = bool(ctx.options.get("exam_submit"))
    if not submit_enabled:
        logger.warning("「考试交卷」开关未开启：本任务点只会逐题暂存，不会交卷")

    total = 0
    max_questions = int(ctx.options.get("exam_max_questions") or 50)
    while total < max_questions:
        try:
            page = ex.parse_answering_page(html)
        except ex.ExamAbort as exc:
            return False, str(exc)
        page["entryUrl"] = entry_url
        total += 1

        q_info = ex.build_q_info(page)
        if q_info is None:
            return False, (f"第 {total} 题题型无法识别（type={page.get('questionType')}），"
                           "已中止且未提交任何内容")

        answer = None
        if ctx.chaoxing is not None and getattr(ctx.chaoxing, "tiku", None) is not None:
            try:
                answer = ctx.chaoxing.tiku.query(q_info)
            except Exception as exc:  # noqa: BLE001
                logger.warning("题库查询异常: {}", exc)
        if not answer:
            return False, f"第 {total} 题题库未给出答案，已中止（按约定绝不提交白卷）"
        logger.info("第 {} 题 [{}] 答案: {}", total,
                    ex.TYPE_NAMES.get(page.get("questionType"), "?"), str(answer)[:60])

        try:
            form = ex.build_answer_form(page, answer)
        except ex.ExamAbort as exc:
            return False, str(exc)

        has_next = bool(re.search(r"(下一题|nextQuestion|start=\d+)", html))
        is_last = not has_next
        do_submit = bool(submit_enabled and is_last)
        if do_submit:
            logger.warning("即将交卷，完整表单字段：{}", sorted(form.keys()))

        try:
            result = ex.save_or_submit(session, page, form, is_submit=do_submit,
                                       timeout=ctx.client.timeout)
        except ex.ExamError as exc:
            return False, f"第 {total} 题保存失败: {exc}"

        if not (result.get("status") or result.get("result")):
            return False, f"第 {total} 题保存被拒绝: {result.get('msg') or result.get('message')}"

        if is_last:
            if do_submit:
                return True, f"已作答 {total} 题并交卷"
            return False, (f"已暂存 {total} 题作答，但**未交卷**"
                           "（安全默认；如需交卷请开启「考试交卷」选项）")

        nxt = _find_next_question_url(html)
        if not nxt:
            return False, f"已暂存 {total} 题，但未能定位下一题，已中止（未交卷）"
        try:
            resp = session.get(nxt, timeout=ctx.client.timeout, allow_redirects=True)
        except requests.RequestException as exc:
            return False, f"打开下一题失败: {exc}"
        html = resp.content.decode("utf-8", errors="replace")

    return False, f"答题轮次超过上限（{max_questions}），已中止（未交卷）"


def _find_answering_url(html: str, course: dict, class_id: str) -> str:
    """从须知/详情页里找出真正的作答页地址。"""
    m = re.search(r"(reVersionTestStartNew[^\"'\s<>]*)", html)
    if m:
        frag = m.group(1)
        if frag.startswith("http"):
            return frag
        return f"https://mooc1.chaoxing.com/{frag.lstrip('/')}"
    m = re.search(r'(https?://[^"\'\s<>]*examnotes[^"\'\s<>]*)', html)
    if m:
        return m.group(1)
    m = re.search(r'(/exam[^"\'\s<>]*(?:examnotes|test)[^"\'\s<>]*)', html)
    if m:
        return f"https://mooc1.chaoxing.com{m.group(1)}"
    return ""


def _find_next_question_url(html: str) -> str:
    """找「下一题」链接。"""
    m = re.search(r'href=["\']([^"\']*start=\d+[^"\']*)["\']', html)
    if m:
        href = m.group(1).replace("&amp;", "&")
        if href.startswith("http"):
            return href
        return f"https://mooc1.chaoxing.com/{href.lstrip('/')}"
    return ""


# ----------------------------------------------------------------------
# 适配器：分组任务（planType 18）
# ----------------------------------------------------------------------


@register_handler("group_task")
def handle_group_task(ctx: CompletionContext) -> tuple[bool, str]:
    """完成分组任务任务点。

    ⚠️ 两条硬约束（均在真机上实测确认）：

    1. **提交不等于完成**：完成判据是 ``enableGroupTaskScore`` → 总成绩 ≥ 60
       （教师评分/组内互评）。提交后任务点仍可能是未完成，这属正常。
    2. **提交是覆盖式的**：``studentSaveRenwu`` 会替换本组已保存/已提交的内容。
       本账号两个分组任务的小组**都已有提交内容**，贸然提交会抹掉组员成果。

    因此默认**只生成内容、绝不提交**：需要显式开启 ``options["group_submit"]``；
    即使开启了，只要检测到小组已有编辑/提交记录也会拒绝，除非再显式打开
    ``options["group_force_overwrite"]``。
    """
    from api import fanya_group as gr

    session = ctx.client.session
    study_url = (ctx.study_url or {}).get("url") or ""
    if not study_url:
        return False, "任务点没有提供跳转地址"

    # 1) 解析页面与小组上下文（全部只读）
    try:
        info = gr.fetch_context(session, study_url, timeout=ctx.client.timeout)
    except gr.GroupAbort as exc:
        return False, str(exc)
    except gr.GroupTaskError as exc:
        return False, f"读取分组任务信息失败: {exc}"

    logger.info(
        "分组任务「{}」：activeId={} groupId={} 编辑次数={} 提交状态={} 组内人数={}",
        ctx.item.name[:30], info["activeId"], info["groupId"],
        info["commitLogCount"], info["submitStatus"], info.get("memberCount"),
    )

    # 2) 基本可行性检查
    if info["activeStatus"] == 2:
        return False, ("该分组任务的活动已结束（active.status=2），无法再提交。"
                       "任务点仍未完成通常是因为需要评分 ≥60（教师评分/组内互评），"
                       "需联系老师处理")
    if not info["groupId"]:
        return False, "当前账号不在任何小组中，无法提交分组任务（需联系老师分配小组）"
    if not info["activeId"] or not info["courseId"]:
        return False, "未能取得 activeId / courseId，已中止（不提交任何内容）"

    # 3) 生成内容
    title, content, used_ai = gr.build_answer(
        ctx.generator, info.get("activeText") or "", info.get("activeTitle") or ctx.item.name,
        course_title=ctx.course.get("title") or "",
    )
    if not title and not content:
        return False, "生成的内容为空，已中止（不提交空答案）"
    plain_preview = gr.html_to_text(content)[:200].replace("\n", " ")
    logger.info("拟提交标题：{}", title)
    logger.info("拟提交正文（{}，共 {} 字）：{}",
                "AI 生成" if used_ai else "模板兜底", len(gr.html_to_text(content)),
                plain_preview)

    form_preview = {
        "forceSave": 0, "title": title, "groupId": info["groupId"],
        "editVersion": info["editVersion"], "activeId": info["activeId"],
        "courseId": info["courseId"], "content": f"<{len(content)} 字节 HTML>",
    }
    logger.info("完整提交字段：{}", form_preview)

    # 4) 覆盖保护：小组已有内容就不动它
    has_existing = bool(info["commitLogCount"]) or str(info["submitStatus"]) == "1"
    if has_existing:
        detail = (f"该小组已有内容（编辑 {info['commitLogCount']} 次，"
                  f"提交状态 {info['submitStatus']}）")
        if not ctx.options.get("group_force_overwrite"):
            if not used_ai:
                logger.warning("AI 不可用，本次为模板兜底文本")
            return False, (f"{detail}。提交会**覆盖组员已写好的答案**，因此已拒绝提交"
                           "（如确实要覆盖，请显式开启「强制覆盖小组内容」）")

    # 5) 默认不提交
    if not ctx.options.get("group_submit"):
        return False, ("已生成小组答案但**未提交**（安全默认）。"
                       "该任务点的完成取决于评分 ≥60，提交本身也不保证变为已完成；"
                       "如需提交请开启「分组任务提交」选项")

    # 6) 真正提交
    try:
        result = gr.submit_answer(
            session, active_id=info["activeId"], course_id=info["courseId"],
            group_id=info["groupId"], edit_version=info["editVersion"],
            title=title, content=content,
            force_save=1 if ctx.options.get("group_force_overwrite") else 0,
            referer=info.get("pageUrl") or study_url, timeout=ctx.client.timeout,
        )
    except gr.GroupTaskError as exc:
        return False, f"提交分组任务失败: {exc}"

    if result.get("result") != 1:
        return False, f"提交分组任务被拒绝: {result.get('errorMsg') or result}"

    data = result.get("data") or {}
    # status==2：与他人并发编辑冲突，服务端会提示「提交后会覆盖他的内容」
    if str(data.get("status")) == "2":
        return False, (f"提交被服务端要求确认（并发编辑冲突）：{data.get('message')}"
                       "；未做覆盖，请手动在页面上确认")
    return True, (f"小组答案已提交（标题：{title}）。注意：该任务点需评分 ≥60 才会"
                  "变为已完成，提交后可能仍显示未完成")


# ----------------------------------------------------------------------
# 适配器：作业（planType 4，mooc2 课程作业）
# ----------------------------------------------------------------------


@register_handler("course_work")
def _build_ai_verifier(tiku: Any) -> Any:
    """当主题库**不是**大模型时，额外造一个 AI 实例用于复核选择题答案。

    背景（实测 2026-10-16）：网络题库按题干模糊匹配，串题时会返回语义完全不相干
    的字母答案，而这类答案能通过 ``check_answer`` 的宽松校验，于是整份作业被交成
    4/9。大模型在同一份卷子上稳定 9/9。所以非大模型主题库 + 已配置 AI 时，选择题
    会复核一次，冲突以模型为准。

    返回 ``None`` 表示无法复核（主题库已经是大模型 / 没配 endpoint+key+model），
    此时行为与以前完全一致 —— 复核是**可选增强**，绝不能因为它坏掉而影响答题。
    """
    conf = getattr(tiku, "_conf", None)
    if not conf:
        return None
    try:
        provider = str(conf.get("provider") or "").strip()
    except Exception:  # noqa: BLE001 - 配置对象形态不定
        return None
    if provider in ("AI", "SiliconFlow"):
        return None  # 主答案已经来自大模型，没必要再问一次
    for key in ("endpoint", "key", "model"):
        try:
            if not conf.get(key):
                return None
        except Exception:  # noqa: BLE001
            return None
    try:
        from api.answer import AI

        verifier = AI()
        verifier.config_set(conf)
        verifier.init_tiku()
        if verifier.DISABLE:
            return None
        logger.info("已启用大模型复核：主题库「{}」的答案会与 AI 交叉验证", provider)
        return verifier
    except Exception as exc:  # noqa: BLE001 - 复核失败不影响主流程
        logger.warning("启用大模型复核失败，本次不做复核: {}", exc)
        return None


def handle_course_work(ctx: CompletionContext) -> tuple[bool, str]:
    """完成「作业」任务点（mooc2 课程作业）。

    与章节检测作业不是同一套入口，详见 ``api/fanya_work.py``。

    **审批优先**：默认 ``require_approval=True``，此时只读题 + 查题库，
    **不保存草稿、不提交**，把答案预览登记为待审批项；用户批准后才真正提交。
    只有在明确关闭审批（``options.require_approval=False``）时，才沿用题库的
    ``submit`` / ``cover_rate`` 策略直接提交。
    """
    from api.fanya_work import (
        _opt_in, complete_course_work, prepare_course_work, submit_prepared,
    )

    entry_url = ctx.study_url.get("url") or ""
    if not entry_url:
        return False, "任务点没有提供作业页地址"

    tiku = getattr(ctx.chaoxing, "tiku", None) if ctx.chaoxing is not None else None
    if tiku is None:
        return False, "缺少题库实例，无法作答作业"

    # 项目既有语义：get_submit_params() == "1" 表示「保存但不提交」
    try:
        save_only = str(tiku.get_submit_params()) == "1"
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取题库提交设置失败，默认提交: {}", exc)
        save_only = False
    try:
        cover_rate = float(getattr(tiku, "COVER_RATE", 0) or 0)
    except (TypeError, ValueError):
        cover_rate = 0.0

    def on_log(message: str) -> None:
        logger.info("[作业] {}", message)

    # 大模型复核：主题库不是大模型（言溪/LIKE/TikuAdapter 之类）时，答案会与 AI
    # 交叉验证 —— 网络题库串题是「分数偏低」的主要成因（见 docs §6.2.1）。
    # 主题库本身就是 AI 时返回 None（同一来源复核没有意义，只会白花调用）。
    verifier = _build_ai_verifier(tiku)

    # 一次性作业保护（2026-10-16 自查）：作业页只有在**已提交**的判分页里才写
    # redoTimes；未提交时平台不告诉我们还剩几次机会，所以「查不到」一律按
    # 「只有一次」处理 —— 交了错答案就没有第二次机会。这类作业默认只保存草稿，
    # 要真正交卷得让用户在审批栏里核对后批准（或显式打开
    # tiku_config.allow_one_shot_submit，且整卷都答上）。
    item = getattr(ctx, "item", None)
    redo_raw = (getattr(item, "extra", None) or {}).get("redo_times") if item is not None else None
    try:
        redo_times = int(redo_raw) if redo_raw is not None else None
    except (TypeError, ValueError):
        redo_times = None
    single_shot = redo_times is None or redo_times <= 0
    try:
        allow_single_shot = _opt_in((getattr(tiku, "_conf", None) or {}).get("allow_one_shot_submit"))
    except Exception as exc:  # noqa: BLE001 - 配置形态不定
        logger.debug("读取 allow_one_shot_submit 失败，按未放行处理: {}", exc)
        allow_single_shot = False
    if single_shot:
        logger.warning(
            "[作业] 「{}」的平台重做次数{}，本次不会自动交卷；"
            "需要交卷时请在待审批里核对答案后批准",
            getattr(ctx, "label", ""), "为 0" if redo_times == 0 else "查不到")

    if not ctx.require_approval:
        return complete_course_work(
            ctx.client.session, entry_url, tiku,
            save_only=save_only, cover_rate=cover_rate, on_log=on_log,
            verifier=verifier, single_shot=single_shot,
            allow_single_shot=allow_single_shot,
        )

    # ---- 审批模式：只准备，不写任何东西 ----
    ok, msg, prepared = prepare_course_work(
        ctx.client.session, entry_url, tiku,
        cover_rate=cover_rate, on_log=on_log,
        verifier=verifier, single_shot=single_shot,
        allow_single_shot=allow_single_shot,
    )
    if not ok:
        return False, msg

    session = ctx.client.session
    total = prepared["total"]
    found = prepared["found"]

    def _submit() -> tuple[bool, str]:
        # 用户已批准 → 正式提交（pyFlag 置空）
        logger.info("[作业] 审批通过，提交 {} 题答案", total)
        return submit_prepared(session, prepared, force_submit=True)

    if found == 0:
        return False, (f"题库对这 {total} 题全部未命中（可能是纯图片题），"
                       "已按未命中处理，未提交任何内容")

    warning = ""
    if prepared.get("single_shot"):
        warning = ("\n⚠️ 平台没给这个作业重做次数（或查不到）：批准后会直接交卷、"
                   "不可逆（交上去就不能改了）—— 请先核对上面的答案。")
    entry_id = ctx.register_approval(
        kind="course_work",
        preview=f"{ctx.label}\n{msg}{warning}",
        submit_fn=_submit,
        extra={"total": total, "found": found, "rate": prepared["rate"],
               "singleShot": bool(prepared.get("single_shot"))},
    )
    return True, (f"已生成 {total} 题答案（命中 {found} 题），"
                  f"待审批后提交 [id={entry_id}]"
                  + ("（该作业不可重做，批准即不可逆）" if prepared.get("single_shot") else ""))


@register_handler("chapter")
def handle_chapter(ctx: CompletionContext) -> tuple[bool, str]:
    """完成「章节」任务点：复用既有的章节学习流程。

    定位方式（实测）：``plan.externalDataId`` 是一个小整数，**精确等于 mooc1
    章节列表里 ``point["id"]``**（注意 JSON 里是字符串）。而 11/10/4 的
    ``externalDataId`` 是 32 位 UUID，与 mooc1 的任何 id 都不相等，因此只有
    planType=8 能走这条路。
    """
    raw_ext = ctx.item.extra.get("externalDataId")
    ext = "" if raw_ext is None else str(raw_ext).strip()
    if not ext or not ext.isdigit():
        return False, f"任务点未提供可用的章节标识（externalDataId={raw_ext!r}）"

    course = ctx.course
    course_id = course.get("courseId")
    class_id = course.get("clazzId") or course.get("classId")
    cpi = course.get("cpi")
    if not (course_id and class_id and cpi):
        return False, "课程信息缺少 courseId / clazzId / cpi，无法定位章节"

    chaoxing = ctx.chaoxing
    if chaoxing is None:
        return False, "缺少 Chaoxing 实例（章节学习需要它来播放/上报）"

    from main import process_job  # 延迟导入，避免与 main 形成循环依赖

    try:
        point_list = chaoxing.get_course_point(course_id, class_id, cpi)
    except Exception as exc:  # noqa: BLE001
        return False, f"读取章节列表失败: {exc}"

    point = None
    for candidate in point_list.get("points") or []:
        if str(candidate.get("id")) == ext:
            point = candidate
            break
    if point is None:
        return False, f"章节列表中未找到 id={ext} 的章节（可能已调整或删除）"

    try:
        jobs, job_info = chaoxing.get_job_list(course, point)
    except Exception as exc:  # noqa: BLE001
        return False, f"读取章节任务点失败: {exc}"

    if not jobs:
        if job_info.get("notOpen"):
            return False, "该章节尚未开放"
        # 没有任务点即视为无内容可做，直接认为完成
        return True, f"章节「{point.get('title')}」没有需要完成的任务点"

    speed = float(ctx.options.get("speed") or 1.5)
    logger.info("章节「{}」共 {} 个任务点，开始复用既有学习流程", point.get("title"), len(jobs))
    failed = []
    for job in jobs:
        try:
            result = process_job(chaoxing, course, job, job_info, speed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("任务点处理异常: {}", exc)
            failed.append(f"{job.get('type')}:{job.get('jobid')}(异常)")
            continue
        if result is not None and getattr(result, "is_failure", None) and result.is_failure():
            failed.append(f"{job.get('type')}:{job.get('jobid')}")

    if failed:
        return False, f"章节「{point.get('title')}」有 {len(failed)}/{len(jobs)} 个任务点未完成：" \
                      f"{', '.join(failed[:3])}"
    return True, f"章节「{point.get('title')}」的 {len(jobs)} 个任务点已全部完成"

