"""成绩与「未达标」（低分）判定。

背景：作业类任务点的得分低于老师设的达标线（``workScore``）时会一直挂着；
AI实践 低于 ``aicoachScore`` 时 ``isFinish`` 根本不会翻成 True —— 例如
2026-10-16 实测「线性与非线性回归AI实践作业」得分 35 < 60，重做后 100 分
才被判定完成。所以盘点里必须能看出「哪些任务点已经出分但没达标」，
否则界面只会说「已完成/未完成」，看不出低分还需要重做。

低分**作业**还多一层：平台的重做是纯 UI 流程（本工具只支持 AI实践 自动重做），
而它的重做次数只藏在判定页的 JS 里（实测「Logistic模型作业」是
``var redoTimes = 0 + 0 - 0;``，页面里没有任何修改入口）。这种任务点必须如实
标成「需人工处理」，否则界面会一直把它算成「可自动完成」。

这里钉住四件事：
1. ``item_score`` 对「作业的评分记录 dict」与数值两种形态的归一化；
2. ``item_threshold`` 只认 AI实践 的 ``aicoachScore`` 与作业的 ``workScore``；
3. ``build_task_preview`` 把 score / threshold / belowThreshold 暴露出来并计数；
4. 低分作业的重做次数解析与「需人工处理」标注。
"""

import pytest

from api import base as base_module
from api.fanya_task import (
    FanyaPlanItem, FanyaTask, FanyaTaskError, _parse_redo_times,
    _probe_work_redo_times, build_task_preview, is_below_threshold,
    item_score, item_threshold,
)


@pytest.fixture(autouse=True)
def _isolated_session(monkeypatch):
    monkeypatch.setattr(base_module, "use_cookies", lambda: {})
    base_module.SessionManager._instance = None
    yield
    base_module.SessionManager._instance = None


def _item(plan_id: int, plan_type: int = 0, **kw) -> FanyaPlanItem:
    return FanyaPlanItem(plan_id=plan_id, encrypt_plan_id=f"e{plan_id}",
                         name=kw.pop("name", f"任务点{plan_id}"),
                         plan_type=plan_type, **kw)


def _breakthrough(**kw) -> dict:
    return {"planBreakthroughSet": kw}


def _task(items) -> FanyaTask:
    return FanyaTask(task_id=1, name="任务", encry_task_user_id="tu", items=items)


# ---------------------------------------------------------------- 得分归一化

def test_item_score_normalises_work_score_record():
    """作业的 score 是整条评分记录，分数在 totalScore 里。"""
    item = _item(1, score={"id": 137030652, "totalScore": 44.5, "workStatus": 4})
    assert item_score(item) == 44.5

    # 某些记录只有 studentEvaluationScore
    item = _item(2, score={"studentEvaluationScore": 12})
    assert item_score(item) == 12.0

    # 数值形态（AI实践 等）
    assert item_score(_item(3, score=100)) == 100.0
    # 没有分数
    assert item_score(_item(4, score=None)) is None
    # 有记录但里面没有数值分：不猜
    assert item_score(_item(5, score={"totalScore": None})) is None


# ---------------------------------------------------------------- 达标线

def test_item_threshold_reads_aicoach_and_work_lines():
    ai = _item(11, extra=_breakthrough(enableAicoach=1, aicoachScore=60))
    work = _item(12, extra=_breakthrough(enableWorkScore=1, workScore=50.0))
    assert item_threshold(ai) == 60.0
    assert item_threshold(work) == 50.0

    # AI实践 优先（两个开关同时开时按 AI实践 的线）
    both = _item(13, extra=_breakthrough(enableAicoach=1, aicoachScore=60,
                                         enableWorkScore=1, workScore=50))
    assert item_threshold(both) == 60.0

    # 没开达标开关 / 没有 planBreakthroughSet / 值为空：都没有达标线
    assert item_threshold(_item(14, extra=_breakthrough(enableAicoach=0,
                                                        aicoachScore=60))) is None
    assert item_threshold(_item(15, extra={})) is None
    assert item_threshold(_item(16, extra=_breakthrough(enableWorkScore=1,
                                                        workScore=None))) is None
    # 脏数据不炸
    assert item_threshold(_item(17, extra=_breakthrough(enableWorkScore=1,
                                                        workScore="abc"))) is None


def test_is_below_threshold():
    # 35 分 < 60：AI实践 的真实场景（重做后 100 分才完成）
    assert is_below_threshold(_item(21, score=35,
                                    extra=_breakthrough(enableAicoach=1,
                                                        aicoachScore=60))) is True
    # 44.5 < 50：作业
    assert is_below_threshold(_item(22, score={"totalScore": 44.5, "workStatus": 4},
                                    extra=_breakthrough(enableWorkScore=1,
                                                        workScore=50))) is True
    # 达标 / 没有达标线 / 没出分：都不算未达标
    assert is_below_threshold(_item(23, score=100,
                                    extra=_breakthrough(enableAicoach=1,
                                                        aicoachScore=60))) is False
    assert is_below_threshold(_item(24, score=10, extra={})) is False
    assert is_below_threshold(_item(25,
                                    extra=_breakthrough(enableAicoach=1,
                                                        aicoachScore=60))) is False


# ---------------------------------------------------------------- 预览接线

class _FakeClient:
    """build_task_preview 只用到这三个方法就够（作业任务点不触发模式识别）。"""

    def __init__(self, task: FanyaTask):
        self.task = task

    def get_course_tasks(self, course_id, clazz_id):
        return [self.task]

    def load_task_items(self, task):
        task.items = list(task.items)

    def check_allow_study(self, encry_task_user_id):
        return True, ""

    def get_study_url(self, encry_task_user_id, encrypt_plan_id, study_jump_type=0):
        # AI实践 子模式识别会走到这里；窗口已关 → 识别失败，但不影响成绩字段
        raise FanyaTaskError("任务点学习时间：2026-09-07 00:00至2026-09-13 23:59，"
                             "当前不在学习时间内")


def test_preview_exposes_scores_and_counts_below_threshold():
    low_ai = _item(31, plan_type=15, score=35,
                   extra=_breakthrough(enableAicoach=1, aicoachScore=60))
    low_work = _item(32, plan_type=0, score={"totalScore": 44.5},
                     extra=_breakthrough(enableWorkScore=1, workScore=50))
    good_work = _item(33, plan_type=0, score={"totalScore": 92.0},
                      extra=_breakthrough(enableWorkScore=1, workScore=50))
    no_score = _item(34, plan_type=10)

    task = FanyaTask(task_id=1, name="任务", encry_task_user_id="tu",
                     items=[low_ai, low_work, good_work, no_score])
    preview = build_task_preview(None, [{"courseId": "1", "clazzId": "2",
                                         "title": "测试课程"}],
                                 client=_FakeClient(task), probe_windows=False)

    assert preview["summary"]["belowThresholdCount"] == 2
    items = {i["planId"]: i for i in preview["courses"][0]["tasks"][0]["items"]}
    assert items[31]["score"] == 35.0 and items[31]["threshold"] == 60.0
    assert items[31]["belowThreshold"] is True
    assert items[32]["belowThreshold"] is True
    assert items[33]["belowThreshold"] is False
    # 没有分数的任务点：字段齐全，但不判「未达标」
    assert items[34]["score"] is None and items[34]["threshold"] is None
    assert items[34]["belowThreshold"] is False

    task_entry = preview["courses"][0]["tasks"][0]
    assert task_entry["belowThresholdCount"] == 2
    assert "expiredCount" in task_entry


# ---------------------------------------------------------------- 低分作业重做

def test_parse_redo_times_from_judged_page():
    # 真机原样（2026-10-16「Logistic模型作业」判定页）：算式形式，不能让 int() 去啃
    assert _parse_redo_times("function redoWork() {\n\tvar redoTimes = 0 + 0 - 0;\n") == 0
    assert _parse_redo_times("var redoTimes = 2 + 1;") == 3
    assert _parse_redo_times("var redoTimes = 3 - 1;") == 2
    # 负数兜底成 0（平台不会给负数次数）
    assert _parse_redo_times("var redoTimes = 0 - 1;") == 0
    # 读不到 / 结构变了 / 表达式里有变量：都不猜
    assert _parse_redo_times("<html>没有这个变量</html>") is None
    assert _parse_redo_times("var redoTimes = total - 1;") is None
    assert _parse_redo_times("") is None


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.encoding = None


class _RedoClient:
    """作业判定页链路：get_study_url 给跳转，session.get 给页面。"""

    def __init__(self, page: str):
        self.page = page
        self.urls: list[str] = []

        class _Session:
            def __init__(self, outer):
                self.outer = outer

            def get(self, url, **kw):
                self.outer.urls.append(url)
                return _FakeResponse(self.outer.page)

        self.session = _Session(self)

    def get_study_url(self, encry_task_user_id, encrypt_plan_id, study_jump_type=0):
        return {"url": f"https://mooc1.chaoxing.com/mooc-ans/mooc2/work/view?"
                       f"workId=56706878&enc=abc"}


def test_probe_work_redo_times_only_touches_low_score_works():
    low = _item(41, plan_type=4, score={"totalScore": 44.5},
                extra=_breakthrough(enableWorkScore=1, workScore=50))
    good = _item(42, plan_type=4, score={"totalScore": 92.0},
                 extra=_breakthrough(enableWorkScore=1, workScore=50))
    doc = _item(43, plan_type=11, score=0,
                extra=_breakthrough(enableWorkScore=1, workScore=50))
    task = _task([low, good, doc])

    client = _RedoClient("function redoWork() {\n\tvar redoTimes = 0 + 0 - 0;\n")
    _probe_work_redo_times(client, task)

    assert low.extra["redo_times"] == 0
    assert "redo_times" not in good.extra, "达标作业不该多花请求"
    assert "redo_times" not in doc.extra, "文档没有重做概念"
    assert len(client.urls) == 1


def test_probe_work_redo_times_survives_unreadable_page():
    low = _item(44, plan_type=4, score={"totalScore": 44.5},
                extra=_breakthrough(enableWorkScore=1, workScore=50))
    client = _RedoClient("<html>页面结构变了</html>")
    _probe_work_redo_times(client, _task([low]))
    # 读不到就不写字段：留 None 让上层说「请到课程页面手动操作」
    assert "redo_times" not in low.extra


class _PreviewClient:
    """带作业判定页的最小预览客户端（probe_windows=True 会走窗口 + 重做探测）。"""

    def __init__(self, task: FanyaTask, page: str = ""):
        self.task = task
        self.client = _RedoClient(page)
        self.session = self.client.session

    def get_course_tasks(self, course_id, clazz_id):
        return [self.task]

    def load_task_items(self, task):
        task.items = list(task.items)

    def check_allow_study(self, encry_task_user_id):
        return True, ""

    def get_study_url(self, encry_task_user_id, encrypt_plan_id, study_jump_type=0):
        return self.client.get_study_url(encry_task_user_id, encrypt_plan_id)


def test_preview_marks_low_score_work_as_manual():
    low = _item(51, plan_type=4, name="Logistic模型作业",
                score={"totalScore": 44.5},
                extra=_breakthrough(enableWorkScore=1, workScore=50))
    good = _item(52, plan_type=4, score={"totalScore": 92.0},
                 extra=_breakthrough(enableWorkScore=1, workScore=50))
    task = FanyaTask(task_id=1, name="任务", encry_task_user_id="tu",
                     items=[low, good])

    client = _PreviewClient(task, "var redoTimes = 0 + 0 - 0;")
    preview = build_task_preview(None, [{"courseId": "1", "clazzId": "2",
                                         "title": "数学建模"}],
                                 client=client, probe_windows=True)

    items = {i["planId"]: i for i in preview["courses"][0]["tasks"][0]["items"]}
    assert items[51]["belowThreshold"] is True
    assert items[51]["redoTimes"] == 0
    assert items[51]["status"] == "manual", "低分作业不能继续算「可自动完成」"
    assert "44.5" in items[51]["note"] and "达标线 50" in items[51]["note"]
    assert "请老师开启重做" in items[51]["note"]
    assert items[52]["status"] != "manual"

    summary = preview["summary"]
    assert summary["belowThresholdCount"] == 1
    assert summary["manualCount"] == 1
    # 低分作业只算「待处理 + 需人工」，不再计进可自动完成
    assert summary["pendingCount"] == 2


def test_task_counts_do_not_advertise_low_score_work_as_completable():
    """任务卡片上的计数必须排除低分作业。

    回归：任务级计数原来直接数 ``item.supported``，而低分作业被改写成 manual 后
    这个字段仍是 True，于是卡片会显示「未完成 2 · 可完成 2」，其中 1 个是工具
    根本不会碰的作业 —— 用户按这个提示去跑，只会得到「已经提交过了」。
    """
    low = _item(61, plan_type=4, name="Logistic模型作业",
                score={"totalScore": 44.5},
                extra=_breakthrough(enableWorkScore=1, workScore=50))
    good = _item(62, plan_type=4, score={"totalScore": 92.0},
                 extra=_breakthrough(enableWorkScore=1, workScore=50))
    task = FanyaTask(task_id=1, name="任务", encry_task_user_id="tu",
                     items=[low, good])

    client = _PreviewClient(task, "var redoTimes = 0 + 0 - 0;")
    preview = build_task_preview(None, [{"courseId": "1", "clazzId": "2",
                                         "title": "数学建模"}],
                                 client=client, probe_windows=True)

    entry = preview["courses"][0]["tasks"][0]
    assert entry["belowThresholdCount"] == 1
    assert entry["pendingCount"] == 2
    assert entry["completableCount"] == 1, "低分作业不该出现在「可完成」里"
    assert entry["expiredCount"] == 0

    summary = preview["summary"]
    assert summary["completableCount"] == 1
    # 任务级与汇总级必须一致
    assert summary["pendingCount"] == entry["pendingCount"]

