"""任务调度：后台线程执行 + 线程安全事件队列 + SSE 流。

- 安装/下载在后台线程跑，进度通过 ``Task.emit`` 写入带锁的事件队列；
- ``snapshot`` 供界面轮询（低频、可重复读取）；
- ``stream`` 供 SSE 消费（一次性、可多订阅者各自从游标读取）。
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Optional

from engine.downloader import DownloadCancelled

MAX_EVENTS = 512
MAX_TASK_LOGS = 300
MAX_GLOBAL_LOGS = 2000

# 任务结束后在列表中保留的时间，方便用户看到结果，之后自动移除。
FINISHED_RETENTION_SECONDS = 3.0


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}


class Task:
    def __init__(self, task_id: str, name: str) -> None:
        self.id = task_id
        self.name = name
        self.status = TaskStatus.PENDING
        self.error = ""
        self.result: Optional[dict] = None
        self.created_at = time.time()
        self.finished_at: Optional[float] = None

        self._cond = threading.Condition()
        self._events: deque[tuple[int, dict]] = deque(maxlen=MAX_EVENTS)
        self._seq = 0
        self._logs: deque[str] = deque(maxlen=MAX_TASK_LOGS)
        self._last_progress: dict = {}
        self._cancel = threading.Event()

    # ------------------------------------------------------------------ 进度
    def emit(self, event: dict) -> None:
        """线程安全地写入一个事件（进度 / 阶段 / 日志）。"""
        with self._cond:
            self._seq += 1
            self._events.append((self._seq, event))
            # 阶段事件（phase）也要成为「最近进度」，否则任务进入加载器 / 整合包
            # 阶段后，界面会一直停在上一个下载阶段的 100%。
            if event.get("type") in ("progress", "phase"):
                self._last_progress = event
            self._cond.notify_all()

    def log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._cond:
            self._logs.append(f"[{stamp}] {line}")
        self.emit({"type": "log", "line": line})

    @property
    def percent(self) -> float:
        progress = self._last_progress
        if progress.get("type") == "phase" and progress.get("total"):
            done = float(progress.get("done") or 0.0)
            total = float(progress.get("total") or 0.0)
            return min(100.0, max(0.0, done / total * 100)) if total else 0.0
        return float(progress.get("percent") or 0.0)

    def check_cancel(self) -> bool:
        """传给 Downloader 的 cancel 回调。"""
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()
        self.emit({"type": "cancel"})

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def logs(self) -> list[str]:
        with self._cond:
            return list(self._logs)

    def snapshot(self) -> dict:
        with self._cond:
            return {
                "id": self.id,
                "name": self.name,
                "status": self.status.value,
                "percent": self.percent,
                "progress": dict(self._last_progress),
                "error": self.error,
                "result": self.result,
                "logs": list(self._logs)[-60:],
                "created_at": self.created_at,
                "finished_at": self.finished_at,
            }

    # ------------------------------------------------------------------ 流式
    def stream(self, *, heartbeat: float = 15.0) -> Iterator[str]:
        """以 SSE 格式产出事件，直到任务进入终态。"""
        cursor = 0
        yield _sse({"type": "open", "task": self.id})
        while True:
            with self._cond:
                pending = [(seq, ev) for seq, ev in self._events if seq > cursor]
                if pending:
                    cursor = pending[-1][0]
                else:
                    self._cond.wait(timeout=heartbeat)
                    pending = [(seq, ev) for seq, ev in self._events if seq > cursor]
                    if pending:
                        cursor = pending[-1][0]
                    elif self.status in TERMINAL_STATUSES:
                        break
                    else:
                        yield ": ping\n\n"
                        continue

            for _, event in pending:
                yield _sse(event)

            if self.status in TERMINAL_STATUSES:
                # 终态后把剩余事件吐完即结束。
                with self._cond:
                    remaining = [(seq, ev) for seq, ev in self._events if seq > cursor]
                    cursor = remaining[-1][0] if remaining else cursor
                for _, event in remaining:
                    yield _sse(event)
                break
        yield _sse(self.snapshot() | {"type": "end"})


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._global_logs: deque[str] = deque(maxlen=MAX_GLOBAL_LOGS)
        self._revision = 0
        self._log_file: Optional[Path] = None

    # ------------------------------------------------------------------ 日志
    def set_log_file(self, path: Path) -> None:
        """设置日志落盘位置；超过 5 MB 时轮转为 ``*.1``。"""
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
                backup = path.with_name(path.name + ".1")
                backup.unlink(missing_ok=True)
                path.replace(backup)
        except OSError:
            self._log_file = None
            return
        self._log_file = path

    @property
    def log_file(self) -> Optional[Path]:
        return self._log_file

    def log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self._global_logs.append(f"[{stamp}] {line}")
            self._revision += 1
            path = self._log_file
            if path is not None:
                try:
                    full = time.strftime("%Y-%m-%d %H:%M:%S")
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(f"[{full}] {line}\n")
                except OSError:
                    pass

    def global_logs(self) -> tuple[list[str], int]:
        """返回 (全部日志, 版本号)，界面据此判断是否需要重绘。"""
        with self._lock:
            return list(self._global_logs), self._revision

    # ------------------------------------------------------------------ 任务
    def create(self, name: str, fn: Callable[[Task], None]) -> Task:
        task = Task(uuid.uuid4().hex[:12], name)
        with self._lock:
            self._tasks[task.id] = task

        thread = threading.Thread(target=self._run, args=(task, fn), daemon=True)
        thread.start()
        return task

    def _run(self, task: Task, fn: Callable[[Task], None]) -> None:
        task.status = TaskStatus.RUNNING
        task.emit({"type": "start", "name": task.name})
        self.log(f"开始：{task.name}")
        try:
            fn(task)
        except DownloadCancelled:
            task.status = TaskStatus.CANCELLED
            task.log("已取消")
            self.log(f"已取消：{task.name}")
        except Exception as exc:  # noqa: BLE001 - 任务失败只记录，不拖垮进程
            if task.cancelled:
                # 用户已点取消，各引擎抛出的是业务异常（LoaderError / ModpackError 等），
                # 统一按「已取消」收尾，避免界面显示成失败并弹出一堆堆栈。
                task.status = TaskStatus.CANCELLED
                task.error = ""
                task.log("已取消")
                self.log(f"已取消：{task.name}")
            else:
                task.status = TaskStatus.FAILED
                task.error = str(exc)
                task.log(f"失败：{exc}")
                for line in traceback.format_exc().splitlines()[-6:]:
                    task.log(line)
                self.log(f"失败：{task.name} - {exc}")
        else:
            if task.cancelled:
                task.status = TaskStatus.CANCELLED
            else:
                task.status = TaskStatus.DONE
                self.log(f"完成：{task.name}")
        finally:
            task.finished_at = time.time()
            task.emit({"type": "finish", "status": task.status.value})
            self._schedule_removal(task.id)

    def _schedule_removal(self, task_id: str) -> None:
        """任务结束后延迟移除，避免任务列表无限增长。"""
        timer = threading.Timer(
            FINISHED_RETENTION_SECONDS, self._remove_task, args=(task_id,)
        )
        timer.daemon = True
        timer.start()

    def _remove_task(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def snapshot(self, task_id: str) -> Optional[dict]:
        task = self.get(task_id)
        return task.snapshot() if task else None

    def list_snapshots(self, limit: int = 20) -> list[dict]:
        with self._lock:
            tasks = list(self._tasks.values())
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return [t.snapshot() for t in tasks[:limit]]

    def stream(self, task_id: str, *, heartbeat: float = 15.0) -> Iterator[str]:
        task = self.get(task_id)
        if task is None:
            yield _sse({"type": "error", "error": "task not found"})
            return
        yield from task.stream(heartbeat=heartbeat)

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None:
            return False
        task.cancel()
        return True


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"