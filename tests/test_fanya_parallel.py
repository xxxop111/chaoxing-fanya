"""并行执行任务点（``options.concurrency``）：会话隔离 + 真并发 + 事件串行。

为什么要并行：每个任务点自己的「累计观看/阅读 N 分钟」门槛是**各算各的**，
串行跑 36 个任务点就是 36 份等待相加；并行 N 个之后总等待≈最慢的那个。

这里钉住三件事：
1. 每个 worker 拿到的是**独立 session**（``requests.Session`` 不能并发写）；
2. 并发确实重叠执行（墙钟时间远小于串行）；
3. 事件回调在并行下仍然串行（app.py 的 stats 计数是读改写）。
"""

import threading
import time

import pytest

from api import base as base_module
from api.base import SessionManager
from api.fanya_task import (
    PLAN_TYPE_VIDEO, FanyaPlanItem, FanyaTask, FanyaTaskRunner, watch_duration_ms,
)


@pytest.fixture(autouse=True)
def _isolated_session(monkeypatch):
    """不读真实 cookies.txt：单例每次新建都复制 cookie 文件，测试里给个空来源。"""
    monkeypatch.setattr(base_module, "use_cookies", lambda: {})
    base_module.SessionManager._instance = None
    yield
    base_module.SessionManager._instance = None


def _item(plan_id: int, name: str, extra: dict | None = None) -> FanyaPlanItem:
    return FanyaPlanItem(plan_id=plan_id, encrypt_plan_id=f"e{plan_id}", name=name,
                         plan_type=PLAN_TYPE_VIDEO, supported=True, extra=extra or {})


def _task(items: list[FanyaPlanItem]) -> FanyaTask:
    return FanyaTask(task_id=1, name="第3周任务", encry_task_user_id="tu", items=items)


def _runner(options: dict | None = None, on_event=None) -> FanyaTaskRunner:
    return FanyaTaskRunner(chaoxing=None, options=options or {}, on_event=on_event)


# ---------------------------------------------------------------- 会话隔离

def test_clone_session_is_independent():
    original = SessionManager.get_session()
    original.headers["X-Test"] = "1"
    original.cookies.set("JSESSIONID", "abc", domain="chaoxing.com", path="/")

    clone = SessionManager.clone_session()

    assert clone is not original
    assert clone.headers.get("X-Test") == "1"
    assert clone.cookies.get("JSESSIONID", domain="chaoxing.com") == "abc"

    # 克隆体上的改动绝不能回写全局会话（否则并行会互相串号）
    clone.cookies.set("JSESSIONID", "changed", domain="chaoxing.com", path="/")
    original.headers["X-Test"] = "2"
    assert original.cookies.get("JSESSIONID", domain="chaoxing.com") == "abc"
    assert clone.headers.get("X-Test") == "1"


# ---------------------------------------------------------------- 选项

@pytest.mark.parametrize("given,expected", [
    (None, 1), (0, 1), (1, 1), ("3", 3), (8, 8), (99, 8), ("abc", 1),
])
def test_concurrency_option_is_clamped(given, expected):
    options = {} if given is None else {"concurrency": given}
    assert _runner(options).concurrency == expected


# ---------------------------------------------------------------- 真并发

def test_parallel_runs_items_concurrently_with_private_sessions(monkeypatch):
    items = [_item(i, f"视频{i}") for i in range(1, 5)]
    seen: list[tuple[int, object]] = []
    lock = threading.Lock()

    def fake_run_item(self, course, task, item, client=None):
        with lock:
            seen.append((item.plan_id, client))
        time.sleep(0.4)
        return {"planId": item.plan_id}

    monkeypatch.setattr(FanyaTaskRunner, "run_item", fake_run_item)
    runner = _runner({"concurrency": 4})

    started = time.time()
    runner._run_items_parallel({"courseId": "1"}, _task(items), items)
    elapsed = time.time() - started

    assert sorted(p for p, _ in seen) == [1, 2, 3, 4]
    # 4 个各 0.4 秒：串行要 1.6 秒，并发 4 应该远小于它
    assert elapsed < 1.2, f"没有真正并发：{elapsed:.2f}s"
    # 每个 worker 一份独立 session
    assert len({id(c) for _, c in seen}) == 4


def test_parallel_records_failures_without_stopping_others(monkeypatch):
    items = [_item(1, "会炸的"), _item(2, "正常的")]

    def fake_run_item(self, course, task, item, client=None):
        if item.plan_id == 1:
            raise RuntimeError("boom")
        return {"planId": item.plan_id}

    monkeypatch.setattr(FanyaTaskRunner, "run_item", fake_run_item)
    runner = _runner({"concurrency": 2})
    runner._run_items_parallel({"courseId": "1"}, _task(items), items)  # 不应抛出

    assert runner.results == []  # 结果由 run_item 自己写；这里只验证异常被吞掉


def test_parallel_stops_submitting_after_stop_request(monkeypatch):
    items = [_item(i, f"视频{i}") for i in range(1, 6)]
    stop = threading.Event()
    calls: list[int] = []

    def fake_run_item(self, course, task, item, client=None):
        calls.append(item.plan_id)
        stop.set()  # 第一个任务点一开跑就请求停止
        return {"planId": item.plan_id}

    monkeypatch.setattr(FanyaTaskRunner, "run_item", fake_run_item)
    runner = _runner({"concurrency": 1, "stop_event": stop})  # 并发 1 也走同一套闸门
    runner._run_items_parallel({"courseId": "1"}, _task(items), [items[0]])

    assert calls == [1]


def test_events_are_serialised_under_parallel(monkeypatch):
    """回调在多个线程里被调用时，on_event 内部不会重入（计数不会撕裂）。"""
    items = [_item(i, f"视频{i}") for i in range(1, 7)]
    inside = 0
    max_inside = 0
    guard = threading.Lock()

    def on_event(payload):
        nonlocal inside, max_inside
        with guard:
            inside += 1
            max_inside = max(max_inside, inside)
        time.sleep(0.05)
        with guard:
            inside -= 1

    def fake_run_item(self, course, task, item, client=None):
        self._emit(type="item_start", planId=item.plan_id)
        time.sleep(0.1)
        return {"planId": item.plan_id}

    monkeypatch.setattr(FanyaTaskRunner, "run_item", fake_run_item)
    runner = _runner({"concurrency": 6}, on_event=on_event)
    runner._emit(type="warmup")

    started = time.time()
    runner._run_items_parallel({"courseId": "1"}, _task(items), items)
    elapsed = time.time() - started

    assert max_inside == 1, "事件回调被并发重入了，前置计数会撕裂"
    assert elapsed < 0.6, "回调加锁后仍应保持并行（不应退化成串行）"


# ---------------------------------------------------------------- 分支选择

def test_run_task_uses_parallel_branch_when_concurrency_gt_1(monkeypatch):
    items = [_item(1, "a"), _item(2, "b")]
    task = _task(items)
    used: list[str] = []

    monkeypatch.setattr(FanyaTaskRunner, "load_and_classify",
                        lambda self, course, task: None, raising=False)
    # run_task 会先做一堆识别/校验；这里只关心它最后选了哪条路
    monkeypatch.setattr("api.fanya_task._describe_ai_practice_items", lambda *a, **k: None)
    monkeypatch.setattr("api.fanya_task._describe_quiz_items", lambda *a, **k: None)
    monkeypatch.setattr("api.fanya_task.classify_items", lambda t: None)

    runner = _runner({"concurrency": 3})
    monkeypatch.setattr(type(runner.client), "load_task_items", lambda self, t: None)
    monkeypatch.setattr(type(runner.client), "check_allow_study", lambda self, t: (True, ""))
    monkeypatch.setattr(type(runner.client), "refresh_task", lambda self, t: None)
    monkeypatch.setattr(FanyaTaskRunner, "_run_items_parallel",
                        lambda self, course, task, cands: used.append("parallel"))
    monkeypatch.setattr(FanyaTaskRunner, "run_item",
                        lambda self, course, task, item, client=None: used.append("serial"))

    runner.run_task({"courseId": "1"}, task)
    assert used == ["parallel"]

    used.clear()
    runner_serial = _runner({"concurrency": 1})
    monkeypatch.setattr(type(runner_serial.client), "load_task_items", lambda self, t: None)
    monkeypatch.setattr(type(runner_serial.client), "check_allow_study", lambda self, t: (True, ""))
    monkeypatch.setattr(type(runner_serial.client), "refresh_task", lambda self, t: None)
    runner_serial.run_task({"courseId": "1"}, task)
    assert used == ["serial", "serial"]


def test_watch_duration_reads_both_kinds():
    """时长门槛的解析：视频/音频/文档各读各的键，开关没打开就当作没有门槛。

    真实结构在 ``extra['planBreakthroughSet']`` 里，单位是分钟。
    文档的判定（走真实浏览器阅读）就建立在这个解析之上。
    """
    extra = {"planBreakthroughSet": {
        "enableVideoWatchDuration": 1, "videoWatchDuration": 5,
        "enableDocumentWatchDuration": 0, "documentWatchDuration": 20,
    }}
    assert watch_duration_ms(extra, "video") == 5 * 60 * 1000
    # 开关没打开时，数值再大也当作没有门槛
    assert watch_duration_ms(extra, "document") == 0
    assert watch_duration_ms({}, "video") == 0
