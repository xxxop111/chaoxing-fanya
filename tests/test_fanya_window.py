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
3. ``run_item`` 在窗口失败时：章节继续做，其它类型如实跳过；
4. ``run_task`` 执行前会先探窗口，并把「已完成 / 窗口已过 / 待完成」逐条报出来
   —— 不能再出现「待完成 0 个」+「成功 0 / 失败 0 / 跳过 0」这种静默结果。
"""

import pytest

from api import base as base_module
from api import fanya_task as ft
from api.base import SessionManager
from api.fanya_task import (
    HANDLERS, HANDLERS_WITHOUT_STUDY_URL, PLAN_TYPE_CHAPTER, PLAN_TYPE_KNOWLEDGE,
    PLAN_TYPE_WORK, STATUS_AUTO, STATUS_MANUAL,
    FanyaPlanItem, FanyaTask, FanyaTaskError, FanyaTaskRunner, TaskSkipped,
    _probe_item_windows, _window_state_from_message, resolve_item,
)

MICRO_TOPIC_URL = ("https://mooc2-ans.chaoxing.com/mooc2-ans/coursetopic/"
                   "topicdetailsmiddle?taskEngine=true&courseid=266162038"
                   "&clazzid=152604525&cpi=493917483&topicid=239094628&ut=s")

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
        if isinstance(outcome, dict):  # 直接指定跳转报文（用来喂不同的落地页）
            return outcome
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


# ------------------------------------------------- 执行时的逐条交代（不再静默）

def _runner_offline(monkeypatch, events: list) -> FanyaTaskRunner:
    """把 run_task 的外围依赖全部短路，只留下「计数 + 留痕」这段逻辑。"""
    runner = FanyaTaskRunner(chaoxing=None, client=_FakeClient({}),
                             options={}, on_event=events.append)
    client_type = type(runner.client)
    monkeypatch.setattr(client_type, "load_task_items", lambda self, t: None, raising=False)
    monkeypatch.setattr(client_type, "check_allow_study", lambda self, t: (True, ""), raising=False)
    monkeypatch.setattr(client_type, "refresh_task", lambda self, t: None, raising=False)
    monkeypatch.setattr("api.fanya_task._describe_ai_practice_items", lambda *a, **k: None)
    monkeypatch.setattr("api.fanya_task._describe_quiz_items", lambda *a, **k: None)
    monkeypatch.setattr("api.fanya_task.classify_items", lambda t: None)
    monkeypatch.setattr(FanyaTaskRunner, "run_item",
                        lambda self, course, task, item, client=None: {"planId": item.plan_id})
    return runner


def test_run_task_accounts_for_finished_and_expired_items(monkeypatch):
    """执行时必须逐条交代，不能出现「待完成 0 / 跳过 0」这种什么都没说的结果。

    真实反馈（2026-10-16）：数学建模里勾了 9 个已完成项点执行，日志只有
    「共 16 个任务点，待完成 0 个」+「成功 0 / 失败 0 / 跳过 0」，
    另外 17 个窗口已过的任务点一个字都没提。
    """
    done = _item(41, name="已完成文档", is_finish=True, supported=True,
                 handled_by="document")
    expired = _item(42, name="过期视频", supported=True, handled_by="video",
                    window_state="expired", window_note="2026-09-30 23:59 已结束")
    todo = _item(43, name="待做作业", supported=True, handled_by="course_work")
    task = _task([done, expired, todo])

    events: list[dict] = []
    runner = _runner_offline(monkeypatch, events)
    runner.run_task({"courseId": "1"}, task, only_plan_ids=[41])

    started = [e for e in events if e["type"] == "task_start"]
    assert started, "任务开始事件不能因为「没得做」就省掉"
    assert started[0]["pending"] == 0, "勾到的都是已完成项，可执行数应为 0"
    assert started[0]["finishedCount"] == 1, "已完成数量必须报出来"
    assert started[0]["expiredCount"] == 1, "窗口已过数量必须报出来（哪怕没勾它）"

    rows = [e for e in events if e["type"] == "item_done"]
    assert [r["planId"] for r in rows] == [41], "勾到的已完成项要留一条跳过记录"
    assert rows[0]["skipped"] is True
    assert "已完成" in rows[0]["message"]


def test_run_task_marks_expired_items_as_skipped_rows(monkeypatch):
    """没勾选的任务点里，窗口已过的也要逐条记成「跳过」（不能再静默丢弃）。"""
    expired = _item(51, name="过期讨论", supported=True, handled_by="topic",
                    window_state="expired", window_note="2026-09-13 23:59 已结束")
    todo = _item(52, name="待做作业", supported=True, handled_by="course_work")
    task = _task([expired, todo])

    events: list[dict] = []
    runner = _runner_offline(monkeypatch, events)
    runner.run_task({"courseId": "1"}, task)

    rows = [e for e in events if e["type"] == "item_done"]
    assert [r["planId"] for r in rows] == [51]
    assert "窗口已过" in rows[0]["message"]
    assert "2026-09-13" in rows[0]["message"], "跳过原因要带上窗口终点"


def test_run_task_is_silent_for_tasks_outside_the_selection(monkeypatch):
    """勾选执行会遍历所有任务；无关任务不该再刷「待完成 0 个」占满日志。"""
    task = _task([_item(61, supported=True, handled_by="course_work")])
    events: list[dict] = []
    runner = _runner_offline(monkeypatch, events)

    runner.run_task({"courseId": "1"}, task, only_plan_ids=[999])

    assert events == [], "没勾到这个任务里的任何任务点时，应静默返回"
    assert runner.results == []


# ------------------------------------------ 知识点微课页：如实判「需人工」
#
# 真实反馈（2026-10-17）：概率论与数理统计里两个「知识点」任务点失败，报
# 「章节列表中未找到 id=239094628 的章节」——那个 id 是**微课 topicId**，
# 不是章节 id。这类页的完成判据是微课里的阅读计时上报（wk-view → 计时 → 上报），
# 章节流程没有可走的任务点，所以只能在盘点阶段就判「需人工」。


def test_probe_recognises_micro_topic_knowledge_page():
    item = _item(71, PLAN_TYPE_KNOWLEDGE)
    other = _item(72, PLAN_TYPE_KNOWLEDGE)
    client = _FakeClient({71: {"url": MICRO_TOPIC_URL},
                          72: {"url": "https://mooc1.chaoxing.com/mooc-ans/knowledge/cards?clazzid=1"}})
    _probe_item_windows(client, _task([item, other]), probe_client_factory=lambda: client)

    assert item.window_state == "open", "能拿到跳转就算可进入"
    assert item.page_kind == "micro_topic"
    assert other.page_kind == "", "普通章节型知识点不能被误判"


def test_micro_topic_item_is_manual_not_auto():
    status, handler, note = resolve_item(
        _item(73, PLAN_TYPE_KNOWLEDGE, page_kind="micro_topic"))

    assert status == STATUS_MANUAL
    assert handler == "chapter"  # 清掉 handler 会连带影响 unreachable 判定
    assert "微课" in note and "手动" in note


def test_chapter_handler_skips_micro_topic_plan_instead_of_failing():
    """万一没在盘点阶段识别出来：执行到 chapter 处理器时也要记「跳过」而不是「失败」。"""
    from api.fanya_task import handle_chapter, CompletionContext

    ctx = CompletionContext(
        chaoxing=object(), client=None,
        course={"courseId": "1", "clazzId": "2", "cpi": "3"},
        task=_task([]),
        item=_item(74, PLAN_TYPE_KNOWLEDGE, extra={"externalDataId": 239094628}),
        generator=None, study_url={}, options={},
        require_approval=False, approvals={}, stop_requested=lambda: False,
    )
    ctx.chaoxing = type("X", (), {"get_course_point": lambda *a, **k: {"points": []}})()
    with pytest.raises(TaskSkipped) as info:
        handle_chapter(ctx)
    assert "知识点" in str(info.value) or "微课" in str(info.value)


def test_run_item_records_task_skipped_as_skipped_row(monkeypatch):
    """处理器抛 TaskSkipped（=这事得人来做）时，结果要算「跳过」，不算失败。"""
    def fake_work(ctx):
        raise TaskSkipped("需要人工：本工具没有这条链路")

    monkeypatch.setitem(HANDLERS, "course_work", fake_work)

    class _Client:
        def get_study_url(self, *a, **k):
            return {"url": "https://example.test/work"}

        def verify_plan_finished(self, *a, **k):
            return None

    runner = FanyaTaskRunner(chaoxing=None, client=_Client(), options={})
    item = _item(81, supported=True, handled_by="course_work")
    result = runner.run_item({"courseId": "1"}, _task([item]), item)


    assert result["skipped"] is True
    assert result["ok"] is False
    assert "需要人工" in result["message"]
    assert not result["message"].startswith("异常"), "跳过不是异常"


def test_every_registered_handler_is_callable():
    """注册表里每个名字都必须绑定到真正的函数。

    真实 bug（2026-10-17）：AI实践 那行写成了 ``register_handler("ai_practice")(None)``，
    于是 ``HANDLERS["ai_practice"] = None``；``effective_status`` 只检查
    ``handler not in HANDLERS``（名字在表里就放过），执行时 ``None(ctx)`` 直接
    TypeError —— 界面上还写着「可自动完成」。
    """
    import api.fanya_task as ft

    assert ft.HANDLERS, "处理器注册表不能是空的"
    broken = {name: value for name, value in ft.HANDLERS.items() if not callable(value)}
    assert broken == {}, f"这些处理器不是可调用对象：{sorted(broken)}"


def test_knowledge_adapter_note_does_not_promise_micro_topics():
    from api.fanya_task import adapter_for

    spec = adapter_for(PLAN_TYPE_KNOWLEDGE)
    assert spec.status == STATUS_AUTO, "章节型知识点仍是可自动的"
    assert "微课" in spec.note and "不支持" in spec.note, "但说明里要讲清微课型做不了"




# ------------------------------------ 处理器返回怪东西 / 「待审批」不当成「没提交成功」
#
# 真实反馈（2026-10-17）：「第二章章节测验」只得到一句
# ``TypeError: cannot unpack non-iterable NoneType object``，堆栈里只有
# ``run_item`` 一帧 —— 处理器跑完了却返回 None，日志里没有半句线索。
# 另外审批模式的行被补上「注意：任务点状态仍为未完成」，让人误以为交卷失败。


def test_handler_outcome_names_the_culprit_instead_of_a_bare_unpack_error():
    assert ft._handler_outcome("video", (True, "ok")) == (True, "ok")
    assert ft._handler_outcome("video", (0, "")) == (False, "")

    with pytest.raises(TypeError) as caught:
        ft._handler_outcome("course_work", None)

    text = str(caught.value)
    assert "course_work" in text, "报错必须点名是哪个处理器"
    assert "NoneType" in text


def test_awaits_approval_only_matches_pending_rows():
    assert ft._awaits_approval(True, "已生成 3 题答案，待审批后提交 [id=ap-4]") is True
    # 真提交了、只是状态没刷新：不是待审批，要照常回读并提示
    assert ft._awaits_approval(True, "作业已提交（2 题）") is False
    assert ft._awaits_approval(False, "待审批后提交 [id=ap-4]") is False


def test_thin_hit_warning_only_fires_below_half():
    assert ft._thin_hit_warning(29, 4).startswith("只答上 4/29 题")
    assert "25 题" in ft._thin_hit_warning(29, 4)
    assert ft._thin_hit_warning(10, 5) == "", "一半命中不算「交白卷」"
    assert ft._thin_hit_warning(0, 0) == ""
