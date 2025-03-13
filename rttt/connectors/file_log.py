from datetime import datetime
from typing import Callable
import os
from loguru import logger
from rttt.event import Event, EventType
from rttt.connectors.base import Connector


class FileLogConnector(Connector):

    lut = {
        EventType.LOG: ' # ',
        EventType.OUT: ' > ',
        EventType.IN: ' < ',
    }

    def __init__(self, connector: Connector, file_path: str, text: str = '') -> None:
        super().__init__()
        self.open_text = text
        self.connector = connector
        self.connector.on(self._on)
        logger.info(f'file_path: {file_path}')
        d = os.path.dirname(file_path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.fd = open(file_path, 'a')

    def open(self):
        self.fd.write(f'{ "*" * 80 }\n')
        center_text = f'{self.open_text:^74}'
        self.fd.write(f'***{center_text}***\n')
        self.fd.write(f'{ "*" * 80 }\n')
        self.fd.flush()
        self.connector.open()

    def close(self):
        self.connector.close()

    def handle(self, event: Event):
        self.connector.handle(event)

    def _on(self, event: Event):
        logger.info(f'on: {event.type} {event.data}')
        prefix = self.lut.get(event.type, None)
        if prefix:
            self._console_log(prefix, event.data)
        self._emit(event)

    def _console_log(self, prefix, line):
        t = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:23]
        self.fd.write(f'{t}{prefix}{line}\n')
        self.fd.flush()
