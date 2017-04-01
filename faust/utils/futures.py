"""Async I/O Future utilities."""
import asyncio
from collections import Sized, deque
from typing import Any, Awaitable, Callable
from .services import Service
from ..types import TaskArg

__all__ = ['Group', 'done_future']

TaskStartedHandler = Callable[[asyncio.Task], Awaitable]
TaskStoppedHandler = Callable[[asyncio.Task], Awaitable]
TaskErrorHandler = Callable[[asyncio.Task, Exception], Awaitable]


def done_future(result: Any = None, *,
                loop: asyncio.AbstractEventLoop = None) -> asyncio.Future:
    f = (loop or asyncio.get_event_loop()).create_future()
    f.set_result(result)
    return f


class Group(Service, Sized):

    #: Tasks to start.
    _starting: deque

    #: List of started tasks to wait for.
    _running: deque

    #: Number of tasks in group.
    _size: int

    def __init__(self, *,
                 on_task_started: TaskStartedHandler = None,
                 on_task_error: TaskErrorHandler = None,
                 on_task_stopped: TaskStoppedHandler = None,
                 loop: asyncio.AbstractEventLoop = None) -> None:
        self._on_task_started = on_task_started
        self._on_task_error = on_task_error
        self._on_task_stopped = on_task_stopped
        self._starting = deque()
        self._running = deque()
        self._size = 0
        super().__init__(loop=loop)

    def spawn(self, task: TaskArg) -> Awaitable:
        # Note: This does not actually start the task,
        #       and `await group.start()` needs to be called.
        fut = self._start_task(task)
        self._starting.append(fut)
        return fut

    async def _start_task(self, task: TaskArg) -> None:
        _task = asyncio.Task(task, loop=self.loop)
        self._running.append(_task)
        self._size += 1
        if self._on_task_started is not None:
            await self._on_task_started(_task)
        try:
            await self._execute_task(_task)
        finally:
            self._size -= 1

    async def _execute_task(self, task: asyncio.Task) -> None:
        try:
            await task
        except Exception as exc:
            if self._on_task_error is not None:
                await self._on_task_error(task, exc)
            raise

    async def on_start(self) -> None:
        for task in self._starting:
            asyncio.ensure_future(task, loop=self.loop)
        self._starting.clear()

    async def joinall(self) -> None:
        while not self.should_stop:
            if self._running:
                while self._running:
                    task = self._running.popleft()
                    await task
                    if self._on_task_stopped is not None:
                        await self._on_task_stopped(task)
            else:
                await asyncio.sleep(1.0)

    def __len__(self) -> int:
        return self._size