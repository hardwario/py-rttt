from typing import Callable
import time
import threading
from loguru import logger
from rttt.event import Event, EventType
from rttt.connectors.base import Connector


class DemoConnector(Connector):
    def __init__(self, delay=0.5) -> None:
        super().__init__()
        self.i = 0
        self.delay = delay
        self.is_running = False

    def handle(self, event: Event):
        logger.info(f'handle: {event.type} {event.data}')
        self._emit(event)

    def open(self):
        logger.info('open')
        self.is_running = True
        self.thread = threading.Thread(target=self._task, daemon=True)
        self.thread.start()

    def close(self):
        logger.info('close')
        if not self.is_running:
            return
        self.is_running = False
        self.thread.join()
        self._emit(Event(EventType.CLOSE, ''))

    def _task(self):
        self._emit(Event(EventType.OPEN, ''))
        while self.is_running:
            self.i += 1
            if self.i % 2 == 0:
                self._emit(Event(EventType.LOG, f'log {self.i}'))
            else:
                self._emit(Event(EventType.OUT, f'term {self.i}'))
            time.sleep(self.delay)
