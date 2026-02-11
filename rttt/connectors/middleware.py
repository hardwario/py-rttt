import asyncio
from abc import abstractmethod
from loguru import logger
from rttt.connectors.base import Connector
from rttt.event import Event
from rttt.utils import ensure_loop, shutdown_loop


class Middleware(Connector):

    def __init__(self, connector: Connector):
        super().__init__()
        self.connector = connector
        self.connector.on(self._on)

    def open(self):
        self.connector.open()

    def close(self):
        self.connector.close()

    def handle(self, event: Event):
        self.connector.handle(event)

    def _on(self, event: Event):
        self._emit(event)


class AsyncMiddleware(Middleware):
    """Middleware with a background asyncio loop for async processing.

    Events are immediately passed downstream via _emit, then enqueued
    for async background processing (e.g. file logging).

    Override `_process(event)` for background work.
    """

    def __init__(self, connector: Connector):
        super().__init__(connector)
        self._accepting = False
        self._loop = None
        self._queue = None
        self._thread = None
        self._task = None

    def open(self):
        self._loop, self._thread = ensure_loop(
            name=f"RTTT-{self.__class__.__name__}-{id(self)}"
        )
        self._queue = asyncio.Queue()
        self._task = self._loop.create_task(self._worker())
        self._accepting = True
        super().open()

    def close(self):
        self._accepting = False
        super().close()
        if self._loop and self._loop.is_running():
            async def _shutdown():
                await self._queue.join()
                self._task.cancel()
            future = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
            try:
                future.result(timeout=5)
            except Exception:
                pass
        shutdown_loop(self._loop, self._thread)
        self._thread = None
        self._loop = None

    def _on(self, event: Event):
        self._emit(event)
        if self._accepting:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    async def _worker(self):
        while True:
            try:
                event = await self._queue.get()
                try:
                    await self._process(event)
                except Exception as e:
                    logger.error(f"Error in {self.__class__.__name__}._process: {e}")
                self._queue.task_done()
            except asyncio.CancelledError:
                break

    @abstractmethod
    async def _process(self, event: Event):
        pass


class BufferedMiddleware(AsyncMiddleware):
    """Middleware that buffers events in a queue before passing downstream.

    Events are NOT emitted immediately - they are processed first.
    Override `_process(event)` to transform/filter events.
    Return the event to emit it, or None to drop it.
    """

    def _on(self, event: Event):
        if self._accepting:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    async def _worker(self):
        while True:
            try:
                event = await self._queue.get()
                try:
                    result = await self._process(event)
                    if result is not None:
                        self._emit(result)
                except Exception as e:
                    logger.error(f"Error in {self.__class__.__name__}._process: {e}")
                self._queue.task_done()
            except asyncio.CancelledError:
                break

    @abstractmethod
    async def _process(self, event: Event) -> Event | None:
        pass
