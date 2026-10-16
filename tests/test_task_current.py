"""``GET /api/task/current``：界面刷新/重开窗口后要能把仍在跑的任务认回来。

背景：任务跑在后端进程里，刷新页面不会停任务，但任务 id 只存在页面内存里。
界面每次启动都问一次这个接口；没有它就会出现「任务在跑、界面却说没有运行中的
任务」，连停止按钮都找不到（实测踩过）。这里钉住挑选规则与响应形状。
"""

import app as app_module


def _client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_no_running_task(monkeypatch):
    monkeypatch.setattr(app_module, "task_status", {})
    assert app_module.running_task() == {}

    body = _client().get("/api/task/current").get_json()
    assert body["status"] is True
    assert body["data"] == {}
    assert "没有运行中的任务" in body["msg"]


def test_learn_task_is_reported(monkeypatch):
    monkeypatch.setattr(app_module, "task_status", {
        "13800138000_1792127301": {
            "status": "running",
            "start_time": 1792127301.6,
            "current_course": "数学建模",
            "current_chapter": "3.4 铅球掷远",
        },
    })
    current = app_module.running_task()
    assert current["task_id"] == "13800138000_1792127301"
    assert current["kind"] == "learn"
    assert current["current_course"] == "数学建模"
    assert current["current_task"] == "3.4 铅球掷远"


def test_newest_running_wins_and_finished_tasks_are_ignored(monkeypatch):
    monkeypatch.setattr(app_module, "task_status", {
        "old": {"status": "running", "start_time": 1.0, "kind": "fanya-run"},
        "new": {"status": "running", "start_time": 2.0, "kind": "fanya-preview"},
        "done": {"status": "completed", "start_time": 3.0},
        "stopped": {"status": "stopped", "start_time": 4.0},
    })
    current = app_module.running_task()
    # 最近启动的优先；非 running 的一律不算
    assert current["task_id"] == "new"
    # fanya 家族（含预览）走 /api/fanya/result/<id>，kind 要收敛成 fanya
    assert current["kind"] == "fanya"
