from abc import ABCMeta, abstractmethod
from typing import Callable
from loguru import logger
from rttt.event import Event


class Connector(metaclass=ABCMeta):

    def __init__(self):
        self._handlers = []

    def on(self, handler: Callable[[Event], None]):
        self._handlers.append(handler)

    def off(self, handler: Callable[[Event], None]):
        try:
            self._handlers.remove(handler)
        except ValueError:
            logger.error(f'handler {handler} not found')

    def _emit(self, event: Event):
        for handler in self._handlers:
            handler(event)

    @abstractmethod
    def open(self):
        pass

    @abstractmethod
    def close(self):
        pass

    @abstractmethod
    def handle(self, event: Event):
        pass
