# -*- coding: utf-8 -*-
"""「生成后暂停、逐条审批」的待审队列。

设计目的：**在用户明确批准之前，任何会真实写入账号的内容都不提交。**
处理器把「已经准备好的内容 + 一个提交回调」登记到这里，前端拉取列表给用户看，
用户点批准后才执行回调。

* 只存在内存里：进程退出即失效（草稿本来也不该跨进程长期挂着）。
* 每个条目带 ``preview``（人看的预览文本）与 ``submit_fn``（无参回调）。
* ``decide()`` 只允许决策一次，避免重复提交。
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from loguru import logger

_ids = itertools.count(1)


@dataclass
class ApprovalEntry:
    id: str
    kind: str                      # course_work / discussion / ai_code / ...
    label: str                     # 「课程 / 任务 / 任务点」的可读描述
    preview: str                   # 生成的答案预览（给人看）
    submit_fn: Optional[Callable[[], tuple[bool, str]]] = None
    created: float = field(default_factory=time.time)
    status: str = "pending"        # pending / approved / rejected / failed
    result: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "preview": self.preview,
            "created": self.created,
            "status": self.status,
            "result": self.result,
            "extra": self.extra,
        }


class ApprovalStore:
    """线程安全的待审队列。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, ApprovalEntry] = {}

    def add(self, kind: str, label: str, preview: str,
            submit_fn: Optional[Callable[[], tuple[bool, str]]] = None,
            extra: Optional[dict] = None) -> ApprovalEntry:
        entry = ApprovalEntry(
            id=f"ap-{next(_ids)}", kind=kind, label=label, preview=preview,
            submit_fn=submit_fn, extra=extra or {},
        )
        with self._lock:
            self._items[entry.id] = entry
        logger.info("待审批项已登记 [{}] {}（{} 字预览）", entry.id, label, len(preview))
        return entry

    def list(self, include_decided: bool = False) -> list[dict]:
        with self._lock:
            entries = list(self._items.values())
        if not include_decided:
            entries = [e for e in entries if e.status == "pending"]
        return [e.to_dict() for e in sorted(entries, key=lambda x: x.created)]

    def get(self, entry_id: str) -> Optional[ApprovalEntry]:
        with self._lock:
            return self._items.get(entry_id)

    def decide(self, entry_id: str, approve: bool) -> tuple[bool, str]:
        """批准（执行提交回调）或拒绝（丢弃）。只能决策一次。"""
        entry = self.get(entry_id)
        if entry is None:
            return False, f"待审批项 {entry_id} 不存在（可能已随进程重启失效）"
        if entry.status != "pending":
            return False, f"该项已经是 {entry.status} 状态，不能重复处理"

        if not approve:
            entry.status = "rejected"
            entry.result = "已拒绝，未提交任何内容"
            logger.info("已拒绝待审批项 [{}] {}", entry.id, entry.label)
            return True, entry.result

        if entry.submit_fn is None:
            entry.status = "failed"
            entry.result = "该项没有可执行的提交动作"
            return False, entry.result

        try:
            ok, msg = entry.submit_fn()
        except Exception as exc:  # noqa: BLE001 - 提交失败要如实反馈
            entry.status = "failed"
            entry.result = f"提交异常: {type(exc).__name__}: {exc}"
            logger.error("待审批项 [{}] 提交异常: {}", entry.id, exc)
            return False, entry.result

        entry.status = "approved" if ok else "failed"
        entry.result = msg
        # 失败时必须把服务端原话写进日志，否则只看到「失败」无从排查
        if ok:
            logger.info("待审批项 [{}] 已批准 → 成功：{}", entry.id, msg)
        else:
            logger.error("待审批项 [{}] 提交失败：{}", entry.id, msg)
        return ok, msg

    def clear(self, keep_decided: bool = False) -> int:
        with self._lock:
            if keep_decided:
                self._items = {k: v for k, v in self._items.items()
                               if v.status != "pending"}
            else:
                self._items.clear()
        return len(self._items)


STORE = ApprovalStore()
