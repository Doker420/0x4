# web/tasks.py
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, Optional
from . import db

log = logging.getLogger("web.tasks")

TASK_HANDLERS: Dict[str, Callable[..., Any]] = {}


def register(kind: str):
    def deco(fn: Callable[..., Any]):
        TASK_HANDLERS[kind] = fn
        return fn
    return deco


class TaskRunner:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._stop.clear()
            self._worker = asyncio.create_task(self._loop(), name="task-runner")
            log.info("TaskRunner запущен")

    async def stop(self) -> None:
        self._stop.set()
        if self._worker:
            try:
                await asyncio.wait_for(self._worker, timeout=5)
            except Exception:
                pass
            self._worker = None
            log.info("TaskRunner остановлен")

    async def submit(self, kind: str, payload: dict) -> int:
        task_id = await db.create_task(kind, payload)
        await self._queue.put(task_id)
        return task_id

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                task_id = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            row = await db.fetch_one("SELECT * FROM tasks WHERE id=?", (task_id,))
            if not row:
                continue

            kind = row["kind"]
            payload = json.loads(row["payload"] or "{}")
            handler = TASK_HANDLERS.get(kind)

            await db.update_task(task_id, status="running", started_at=time.time())

            if not handler:
                await db.update_task(
                    task_id,
                    status="failed",
                    error=f"Нет зарегистрированного обработчика для {kind}",
                    finished_at=time.time(),
                )
                continue

            try:
                result = await handler(payload)
                result_str = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
                await db.update_task(
                    task_id,
                    status="ok",
                    result=result_str[:10000],
                    finished_at=time.time(),
                )
            except Exception as e:
                log.exception("Задача %s (%s) завершилась ошибкой", task_id, kind)
                await db.update_task(
                    task_id,
                    status="failed",
                    error=str(e),
                    finished_at=time.time(),
                )


runner = TaskRunner()
