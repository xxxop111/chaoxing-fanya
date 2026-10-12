# -*- coding: utf-8 -*-
"""泛雅任务引擎「视频 / 音频」任务点（planType 10 / 22）。

任务引擎有**自己的**播放页，不走 mooc1 的 video 模块：

```
跳转  POST {task}/userStudyPlan/getToStudyUrl?encryptPlanId=&encryTaskUserId=&studyJumpType=0
      → data.url = https://task.chaoxing.com/videoStudy/learnPage?encryPlanUserId=<EPU>
```

页面把播放参数内嵌成 ``videoLearnVo``：

```js
const videoLearnVo = {
  encryId: "BEDCjrlChRDhp@…",     // 上报用的加密 id
  resourceId: "…",
  currentTime: 0, farthestTimeValue: 0,
  enableDrag: true, enableTripleSpeed: false,
  videoInfo: { videoSource: "https://s2.cldisk.com/…/sd.mp4?…", duration: 808 }
};
```

进度由两个 videojs 插件上报（``/videoPlayback/plugins/…``）：

```
心跳  GET /videoDataLog/dataLog/{duration}/{currentTime}/{status}?encryId=<encryId>
        status: 0=开始播放  1=播放中（页面每 6 秒一次）  2=暂停/播放结束
记录  GET /planUserSchedule/lastCurrentTime/{currentTime}?encryId=<encryId>
```

**注意**：服务端会校验进度合理性 —— 插件收到 ``result=false`` 时会暂停播放并弹窗。
因此本模块默认**按真实时间推进**模拟播放（不是直接跳到末尾），
并通过 ``video_speed`` 提供可选的加速（是否被容忍取决于服务端，实测为准）。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional

import requests
from loguru import logger

TASK_DOMAIN = "https://task.chaoxing.com"

STATUS_PLAY = 0
STATUS_HEARTBEAT = 1
STATUS_STOP = 2

DEFAULT_HEARTBEAT_SECONDS = 6.0

_VO_PATTERN = re.compile(r"videoLearnVo\s*=\s*(\{.*?\})\s*;?\s*(?:const|\n|</script>)", re.S)


class VideoStudyError(Exception):
    """视频/音频学习页解析或上报失败。"""


def parse_video_learn_vo(html: str) -> Optional[dict]:
    """从播放页 HTML 中解析 ``videoLearnVo``（含 encryId 与 duration）。"""
    match = _VO_PATTERN.search(html)
    if not match:
        # 退化为从整页里抓关键字段，兼容结构微调
        return None
    raw = match.group(1)
    try:
        return json.loads(raw)
    except ValueError:
        try:
            # 页面里是合法 JSON，但可能包含 \u0026 之类的转义，json 能直接吃
            return json.loads(raw.encode().decode("unicode_escape"))
        except (ValueError, UnicodeDecodeError):
            return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def report_dot(session: requests.Session, encry_id: str, duration: int, current_time: int,
               status: int, timeout: float = 20.0) -> dict:
    """上报一次播放进度。返回服务端 JSON；``result=False`` 表示被拒绝。"""
    url = f"{TASK_DOMAIN}/videoDataLog/dataLog/{duration}/{current_time}/{status}"
    resp = session.get(
        url,
        params={"encryId": encry_id},
        headers={
            "Referer": f"{TASK_DOMAIN}/videoStudy/learnPage",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise VideoStudyError(f"进度上报返回 HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise VideoStudyError(f"进度上报返回非 JSON: {resp.text[:160]}") from exc


def save_position(session: requests.Session, encry_id: str, current_time: int,
                  timeout: float = 20.0) -> None:
    """记录断点（相当于页面的 lastCurrentTime 上报）。失败不致命。"""
    try:
        session.get(
            f"{TASK_DOMAIN}/planUserSchedule/lastCurrentTime/{current_time}",
            params={"encryId": encry_id},
            headers={"Referer": f"{TASK_DOMAIN}/videoStudy/learnPage"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.debug("记录播放断点失败: {}", exc)


def fetch_learn_vo(session: requests.Session, entry_url: str, timeout: float = 30.0) -> dict:
    """打开播放页并解析 ``videoLearnVo``。"""
    resp = session.get(entry_url, timeout=timeout,
                       headers={"Referer": f"{TASK_DOMAIN}/userStudyPlan/myPlanList"})
    if resp.status_code != 200:
        raise VideoStudyError(f"打开播放页返回 HTTP {resp.status_code}")
    text = resp.content.decode("utf-8", errors="replace")
    vo = parse_video_learn_vo(text)
    if not vo:
        raise VideoStudyError("未能从播放页解析出 videoLearnVo（页面结构可能已变更）")
    if not vo.get("encryId"):
        raise VideoStudyError("videoLearnVo 中缺少 encryId")
    return vo


def simulate_playback(session: requests.Session, vo: dict, *,
                      heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
                      speed: float = 1.0,
                      max_wall_seconds: float = 7200.0,
                      progress_callback: Optional[Callable[[int, int], None]] = None,
                      stop_event: Any = None) -> tuple[bool, str]:
    """按真实时间推进，模拟完整播放一遍。

    返回 ``(是否成功, 说明)``。
    """
    encry_id = vo.get("encryId") or ""
    info = vo.get("videoInfo") or {}
    duration = _as_int(info.get("duration"))
    if duration <= 0:
        return False, "视频时长为 0，无法模拟播放"

    start_at = _as_int(vo.get("farthestTimeValue") or vo.get("currentTime"))
    if start_at < 0:
        start_at = 0
    if start_at >= duration:
        start_at = 0

    speed = max(0.5, min(float(speed or 1.0), 8.0))
    heartbeat_seconds = max(2.0, float(heartbeat_seconds or DEFAULT_HEARTBEAT_SECONDS))

    logger.info(
        "开始模拟播放：时长 {} 秒，起点 {} 秒，心跳 {:.0f}s，倍速 {}",
        duration, start_at, heartbeat_seconds, speed,
    )

    def _check(body: dict, phase: str) -> Optional[str]:
        if body.get("result") is False:
            return f"{phase}被服务端拒绝: {body.get('message') or body.get('msg') or body}"
        return None

    try:
        err = _check(report_dot(session, encry_id, duration, start_at, STATUS_PLAY), "开始播放")
        if err:
            return False, err
    except VideoStudyError as exc:
        return False, str(exc)

    current = start_at
    started = time.time()
    last_reported = current
    while current < duration:
        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
            save_position(session, encry_id, current)
            return False, f"收到停止请求，已记录断点 {current}/{duration} 秒"
        if time.time() - started > max_wall_seconds:
            save_position(session, encry_id, current)
            return False, (f"超过单任务点时间上限（{int(max_wall_seconds)}s），"
                           f"已记录断点 {current}/{duration} 秒")

        time.sleep(heartbeat_seconds)
        current = min(duration, current + int(round(heartbeat_seconds * speed)))
        try:
            err = _check(report_dot(session, encry_id, duration, current, STATUS_HEARTBEAT),
                         "播放心跳")
        except VideoStudyError as exc:
            return False, str(exc)
        if err:
            return False, err
        if callable(progress_callback):
            try:
                progress_callback(current, duration)
            except Exception as exc:  # noqa: BLE001
                logger.debug("视频进度回调失败: {}", exc)
        # 每 30 秒记录一次断点，便于中断后续播
        if current - last_reported >= 30:
            save_position(session, encry_id, current)
            last_reported = current

    try:
        err = _check(report_dot(session, encry_id, duration, duration, STATUS_STOP), "结束播放")
    except VideoStudyError as exc:
        return False, str(exc)
    if err:
        return False, err
    save_position(session, encry_id, duration)
    return True, f"已按真实时间播放完 {duration} 秒"
