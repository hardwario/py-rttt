from abc import ABCMeta, abstractmethod
from typing import Callable
from loguru import logger
from rttt.event import Event


class Connector(metaclass=ABCMeta):

    def __init__(self):
        self._handler = None

    def on(self, handler: Callable[[Event], None]):
        if self._handler is not None:
            raise RuntimeError(f"Handler already set on {self.__class__.__name__}, call off() first")
        self._handler = handler

    def off(self):
        self._handler = None

    def _emit(self, event: Event):
        if self._handler:
            try:
                self._handler(event)
            except Exception as e:
                logger.error(f"Error in handler: {e}")

    @abstractmethod
    def open(self):
        pass

    @abstractmethod
    def close(self):
        pass

    @abstractmethod
    def handle(self, event: Event):
        pass
