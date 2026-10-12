# -*- coding: utf-8 -*-
"""端到端冒烟测试：验证「任务」模块从 HTTP 接口到真实执行的全链路。

用途：改动后端 / 重新打包之后，快速确认 Web 路径没坏。它会**真实执行**你指定的
任务点（例如视频会立即上报观看进度并让任务点变为已完成），所以请只传你确实想
完成的任务点。

用法：

    # 1) 先启动应用（任选其一）
    chaoxing-fanya.exe --no-window
    python desktop_app.py --no-window

    # 2) 再跑冒烟测试：<端口> <planId...>
    python tools/smoke_test.py 5000 4097135

planId 可以从界面上「开始盘点任务」的结果里看到，也可以用：

    python main.py --use-cookies --fanya-tasks        # 只盘点，会打印清单
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 30
POLL_ROUNDS = 80
POLL_INTERVAL = 3.0


def _request(url: str, payload: dict | None = None) -> dict:
    if payload is None:
        req = urllib.request.Request(url, method="GET")
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:200]
        raise SystemExit(f"请求 {url} 返回 HTTP {exc.code}: {body}") from exc


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2

    port = int(argv[1])
    plan_ids = [int(x) for x in argv[2:]]
    base = f"http://127.0.0.1:{port}"

    print(f"[1/4] 健康检查 {base}/api/health")
    print("      ->", _request(f"{base}/api/health"))

    print(f"[2/4] 提交执行请求（planIds={plan_ids}）")
    started = _request(f"{base}/api/fanya/execute", {
        "username": "", "password": "", "use_cookies": True,
        "course_list": [], "plan_ids": plan_ids, "tiku_config": {}, "options": {},
    })
    if not started.get("status"):
        print("      启动失败:", started.get("msg"))
        return 1
    task_id = started["data"]["task_id"]
    print("      task_id =", task_id)

    print("[3/4] 轮询执行结果")
    state: dict = {}
    for _ in range(POLL_ROUNDS):
        time.sleep(POLL_INTERVAL)
        state = _request(f"{base}/api/fanya/result/{task_id}")["data"]
        status = state["task"]["status"]
        if status in ("completed", "error", "stopped"):
            break
    task = state.get("task") or {}
    print(f"      状态={task.get('status')} 统计={task.get('stats')}")
    if task.get("error"):
        print("      错误:", task["error"])

    results = (state.get("result") or {}).get("results") or []
    failed = 0
    for row in results:
        if not row.get("ok") and not row.get("skipped"):
            failed += 1
        print(f"      -> ok={row.get('ok')} skipped={row.get('skipped')} "
              f"verified={row.get('verified')} | {row.get('typeLabel')} "
              f"{str(row.get('name'))[:28]} | {str(row.get('message'))[:70]}")

    print("[4/4] 拉取日志")
    logs = _request(f"{base}/api/logs/{task_id}")["data"]
    print(f"      {len(logs)} 条（末尾 5 条）")
    for entry in logs[-5:]:
        print("       ", entry.get("level"), str(entry.get("message"))[:100])

    print("\n结论:", "全部成功" if not failed else f"有 {failed} 个未成功")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
