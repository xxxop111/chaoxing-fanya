"""学习窗口（「当前不在学习时间内」）的判定与「章节」旁路。

背景：任务引擎里的任务点各有一个有效期，过期后 ``getToStudyUrl`` 直接返回
``{"code":400,"result":false,"message":"任务点学习时间：…当前不在学习时间内"}``，
**不给任何 id**（没有 url / workId / publishRelationUuid / topicId，2026-10-16 实测），
所以依赖这个跳转的类型（作业、文档、视频、讨论、AI实践…）在窗口结束后**没有入口**，
现状下无法补完成 —— 这类任务点不该继续挂在「待完成」里骗人。

唯一的例外是「章节」：``handle_chapter`` 只用课程 id + ``plan.externalDataId``，
走 mooc1 的章节接口，不碰任务引擎的跳转。实测窗口 2026-09-20~09-30 已结束的
plan 8110517「5.1 人口增长-马尔萨斯模型视频教学」仍能正常播放并上报。

这里钉住三件事：
1. 窗口报错 -> (expired / not_open) 的判定；
2. 探测结果如何影响「待处理 / 可自动完成 / 窗口已过」的计数；
3. ``run_item`` 在窗口失败时：章节继续做，其它类型如实跳过。
"""

import pytest

from api import base as base_module
from api.base import SessionManager
from api.fanya_task import (
    HANDLERS, HANDLERS_WITHOUT_STUDY_URL, PLAN_TYPE_CHAPTER, PLAN_TYPE_WORK,
    FanyaPlanItem, FanyaTask, FanyaTaskError, FanyaTaskRunner,
    _probe_item_windows, _window_state_from_message,
)

PAST_WINDOW = ("任务点学习时间：2026-09-20 10:00至2026-09-30 23:59，"
               "当前不在学习时间内")
FUTURE_WINDOW = ("任务点学习时间：2027-01-01 00:00至2027-01-07 23:59，"
                 "当前不在学习时间内")


@pytest.fixture(autouse=True)
def _isolated_session(monkeypatch):
    monkeypatch.setattr(base_module, "use_cookies", lambda: {})
    base_module.SessionManager._instance = None
    yield
    base_module.SessionManager._instance = None


class _FakeClient:
    """只实现探测用得到的那一个方法。"""

    def __init__(self, behaviour: dict):
        self.behaviour = behaviour
        self.calls: list[int] = []

    def get_study_url(self, encry_task_user_id, encrypt_plan_id, study_jump_type=0):
        plan_id = int(str(encrypt_plan_id).lstrip("e") or 0)
        self.calls.append(plan_id)
        outcome = self.behaviour.get(plan_id, "open")
        if outcome == "open":
            return {"url": f"https://example.test/{plan_id}"}
        raise FanyaTaskError(outcome)


def _item(plan_id: int, plan_type: int = PLAN_TYPE_WORK, **kw) -> FanyaPlanItem:
    return FanyaPlanItem(plan_id=plan_id, encrypt_plan_id=f"e{plan_id}",
                         name=kw.pop("name", f"任务点{plan_id}"),
                         plan_type=plan_type, **kw)


def _task(items) -> FanyaTask:
    return FanyaTask(task_id=1, name="任务", encry_task_user_id="tu", items=items)


# ---------------------------------------------------------------- 报错解析

def test_window_state_from_real_messages():
    state, note = _window_state_from_message(PAST_WINDOW)
    assert state == "expired"
    assert "2026-09-30" in note

    state, note = _window_state_from_message(FUTURE_WINDOW)
    assert state == "not_open"
    assert "2027-01-07" in note

    # 不是窗口问题的报错不猜状态
    assert _window_state_from_message("任务点不存在")[0] == ""


# ---------------------------------------------------------------- 探测

def test_probe_marks_open_expired_and_unknown():
    items = [
        _item(1),
        _item(2),
        _item(3),
        _item(4),
        _item(5, is_finish=True),
    ]
    client = _FakeClient({2: PAST_WINDOW, 3: FUTURE_WINDOW, 4: "服务端异常"})
    # 2 个以上待探测任务点时走并发路径，用工厂注入假客户端（每个 worker 一份）
    _probe_item_windows(client, _task(items), probe_client_factory=lambda: client)

    assert [i.window_state for i in items[:4]] == ["open", "expired", "not_open", ""]
    assert items[1].window_blocked and items[2].window_blocked
    assert not items[0].window_blocked
    # 已完成的任务点不需要探测（省掉每个任务点一次请求）
    assert items[4].window_state == ""
    assert 5 not in client.calls


def test_probe_does_not_touch_finished_items_only():
    items = [_item(7, is_finish=True)]
    client = _FakeClient({})
    _probe_item_windows(client, _task(items))
    assert client.calls == []


# ---------------------------------------------------------------- 可达性

def test_unreachable_depends_on_handler_not_window_alone():
    work = _item(11, PLAN_TYPE_WORK, window_state="expired", handled_by="course_work")
    chapter = _item(12, PLAN_TYPE_CHAPTER, window_state="expired", handled_by="chapter")
    unknown = _item(13, PLAN_TYPE_WORK, window_state="expired", handled_by="")

    assert work.unreachable is True
    # 章节窗口关了也照样能学：不能算「做不了」
    assert chapter.unreachable is False
    # 还没分类的任务点不武断判死
    assert unknown.unreachable is True
    assert "chapter" in HANDLERS_WITHOUT_STUDY_URL


def test_completable_items_excludes_unreachable():
    task = _task([
        _item(21, PLAN_TYPE_WORK, supported=True, window_state="expired",
              handled_by="course_work"),
        _item(22, PLAN_TYPE_CHAPTER, supported=True, window_state="expired",
              handled_by="chapter"),
        _item(23, PLAN_TYPE_WORK, supported=True, window_state="open",
              handled_by="course_work"),
    ])
    assert [i.plan_id for i in task.completable_items] == [22, 23]


# ---------------------------------------------------------------- 执行器

def _stub_handlers(monkeypatch, seen: list):
    def fake_chapter(ctx):
        seen.append(("chapter", ctx.item.plan_id, ctx.study_url))
        return True, "章节已完成"

    monkeypatch.setitem(HANDLERS, "chapter", fake_chapter)
    monkeypatch.setitem(HANDLERS, "course_work",
                        lambda ctx: (seen.append(("work", ctx.item.plan_id, ctx.study_url))
                                     or (True, "作业已提交")))


def test_run_item_chapter_survives_closed_window(monkeypatch):
    seen: list = []
    _stub_handlers(monkeypatch, seen)

    class _Client:
        def get_study_url(self, *a, **k):
            raise FanyaTaskError(PAST_WINDOW)

        def verify_plan_finished(self, *a, **k):
            return True

    runner = FanyaTaskRunner(chaoxing=None, client=_Client(), options={})
    item = _item(31, PLAN_TYPE_CHAPTER, supported=True, handled_by="chapter")
    result = runner.run_item({"courseId": "1"}, _task([item]), item)

    assert seen == [("chapter", 31, {})], "章节类窗口关闭时仍应真正执行"
    assert result["windowBlocked"] is True
    assert result["skipped"] is False


def test_run_item_other_types_are_skipped_when_window_closed(monkeypatch):
    seen: list = []
    _stub_handlers(monkeypatch, seen)

    class _Client:
        def get_study_url(self, *a, **k):
            raise FanyaTaskError(PAST_WINDOW)

    runner = FanyaTaskRunner(chaoxing=None, client=_Client(), options={})
    item = _item(32, PLAN_TYPE_WORK, supported=True, handled_by="course_work")
    result = runner.run_item({"courseId": "1"}, _task([item]), item)

    assert seen == [], "窗口关闭的作业没有入口，不该硬闯"
    assert result["skipped"] is True
    assert "无法进入任务点" in result["message"]
