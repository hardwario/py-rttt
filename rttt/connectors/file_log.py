from datetime import datetime
import os
from loguru import logger
from rttt.event import Event, EventType
from rttt.connectors.base import Connector
from rttt.connectors.middleware import AsyncMiddleware


class FileLogMiddleware(AsyncMiddleware):

    lut = {
        EventType.LOG: ' # ',
        EventType.OUT: ' > ',
        EventType.IN: ' < ',
    }

    def __init__(self, connector: Connector, file_path: str, text: str = '') -> None:
        super().__init__(connector)
        self.open_text = text
        logger.info(f'file_path: {file_path}')
        d = os.path.dirname(file_path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.fd = None
        try:
            self.fd = open(file_path, 'a', encoding='utf-8')
        except Exception as e:
            logger.error(f"Failed to open log file {file_path}: {e}")

    def open(self):
        if self.fd:
            try:
                self.fd.write(f'{"*" * 80}\n')
                center_text = f'{self.open_text:^74}'
                self.fd.write(f'***{center_text}***\n')
                self.fd.write(f'{"*" * 80}\n')
                self.fd.flush()
            except Exception as e:
                logger.error(f"Failed to write header to log file: {e}")
        super().open()

    def close(self):
        super().close()
        if self.fd:
            try:
                self.fd.close()
            except Exception as e:
                logger.error(f"Failed to close log file: {e}")
            self.fd = None

    async def _process(self, event: Event):
        prefix = self.lut.get(event.type, None)
        if prefix and self.fd:
            t = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:23]
            line = f'{t}{prefix}{event.data}\n'
            await self._loop.run_in_executor(None, self._write_line, line)

    def _write_line(self, line):
        try:
            self.fd.write(line)
            self.fd.flush()
        except Exception as e:
            logger.error(f"Failed to write to log file: {e}")
