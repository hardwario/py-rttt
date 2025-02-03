
from typing import Callable
import pylink
import time
import threading
from loguru import logger
from rttt.connectors.base import Connector
from rttt.event import Event, EventType


class PyLinkRTTConnector(Connector):

    def __init__(self, jlink: pylink.JLink, terminal_buffer=0, logger_buffer=1, latency=50, block_address=None,) -> None:
        super().__init__()
        self.jlink = jlink
        self.block_address = block_address
        self.rtt_read_delay = latency / 1000.0
        self.is_running = False
        self.terminal_buffer = terminal_buffer
        self.terminal_buffer_up_size = 0
        self.terminal_buffer_down_size = 0
        self.logger_buffer = logger_buffer
        self.log_up_size = 0

    def open(self):
        self._cache = {0: '', 1: ''}

        logger.info(f"Opening RTT{' control block found at 0x{:08X}'.format(self.block_address) if self.block_address else ''}")
        self.jlink.rtt_start(self.block_address)

        for _ in range(100):
            try:
                num_up = self.jlink.rtt_get_num_up_buffers()
                num_down = self.jlink.rtt_get_num_down_buffers()
                logger.info(f'RTT started, {num_up} up bufs, {num_down} down bufs.')
                break
            except pylink.errors.JLinkRTTException:
                time.sleep(0.1)
        else:
            raise Exception('Failed to find RTT block')

        if num_up == 0:
            raise Exception('No RTT down buffers found')

        if num_up < self.terminal_buffer:
            raise Exception(f'Shell buffer UP {self.terminal_buffer} not found')

        if num_up < self.logger_buffer:
            raise Exception(f'Log buffer UP {self.logger_buffer} not found')

        if num_down < self.terminal_buffer:
            raise Exception(f'Shell buffer DOWN {self.terminal_buffer} not found')

        self.is_running = True

        for i in range(num_up):
            desc = self.jlink.rtt_get_buf_descriptor(i, 1)
            logger.info(f'Up buffer {i}: {desc}')
            if i == self.terminal_buffer:
                self.terminal_buffer_up_size = desc.SizeOfBuffer
            elif i == self.logger_buffer:
                self.log_up_size = desc.SizeOfBuffer
        for i in range(num_down):
            desc = self.jlink.rtt_get_buf_descriptor(i, 0)
            logger.info(f'Down buffer {i}: {desc}')
            if i == self.terminal_buffer:
                self.terminal_buffer_down_size = desc.SizeOfBuffer

        self.thread = threading.Thread(target=self._read_task, daemon=True)
        self.thread.start()

        self._emit(Event(EventType.OPEN, ''))
        logger.info('RTT opened')

    def close(self):
        logger.info('Closing RTT')
        if not self.is_running:
            return
        self.is_running = False
        self.thread.join()
        self.jlink.rtt_stop()
        self._emit(Event(EventType.CLOSE, ''))
        logger.info('RTT closed')

    def handle(self, event: Event):
        logger.info(f'handle: {event.type} {event.data}')
        if event.type == EventType.IN:
            logger.info(f'RTT write shell buffer {self.terminal_buffer} bufer size {self.terminal_buffer_down_size}')
            data = bytearray(f'{event.data}\n', "utf-8")
            for i in range(0, len(data), self.terminal_buffer_down_size):
                chunk = data[i:i + self.terminal_buffer_down_size]
                self.jlink.rtt_write(self.terminal_buffer, list(chunk))
        self._emit(event)

    def _read_task(self):
        while self.is_running:
            channels = [
                (self.terminal_buffer, min(1000, self.terminal_buffer_up_size), EventType.OUT),
                (self.logger_buffer, min(1000, self.log_up_size), EventType.LOG)
            ]
            for idx, num_bytes, event_type in channels:
                if idx is None:
                    continue

                data = self.jlink.rtt_read(idx, num_bytes)
                if data:
                    lines = bytes(data).decode('utf-8', errors="backslashreplace")
                    if lines:
                        lines = self._cache[idx] + lines

                        while True:
                            end = lines.find('\n')
                            if end < 0:
                                self._cache[idx] = lines
                                break

                            line = lines[:end]
                            lines = lines[end + 1:]

                            if line.endswith('\r'):
                                line = line[:-1]

                            self._emit(Event(event_type, line))

            time.sleep(self.rtt_read_delay)
