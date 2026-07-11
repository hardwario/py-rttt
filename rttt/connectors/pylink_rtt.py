import os
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
        self.thread = None
        self.terminal_buffer = terminal_buffer
        self.terminal_buffer_up_size = 0
        self.terminal_buffer_down_size = 0
        self.logger_buffer = logger_buffer
        self.log_up_size = 0
        self._op_lock = threading.Lock()

    def start(self):
        """Start RTT and the read thread."""
        self._cache = {0: '', 1: ''}

        logger.info(f"Starting RTT{' control block found at 0x{:08X}'.format(self.block_address) if self.block_address else ''}")

        # Right after flash + reset the search may hit a stale control block
        # left in RAM with zeroed descriptors (or at a different address than
        # the new firmware uses), so restart the search until the terminal
        # buffer reports a non-zero size.
        deadline = time.monotonic() + 15.0
        logger_deadline = None
        while True:
            try:
                self.jlink.rtt_start(self.block_address)
            except pylink.errors.JLinkException as e:
                raise Exception(f'J-Link: {e}') from e

            num_up = None
            while time.monotonic() < deadline:
                try:
                    num_up = self.jlink.rtt_get_num_up_buffers()
                    num_down = self.jlink.rtt_get_num_down_buffers()
                    break
                except pylink.errors.JLinkRTTException:
                    time.sleep(0.1)
                except pylink.errors.JLinkException as e:
                    raise Exception(f'J-Link: {e}') from e

            attached = False
            while num_up is not None and num_up > self.terminal_buffer:
                # The firmware registers RTT buffers one by one during boot
                # (terminal first, logger later), so wait until both report
                # a non-zero size — attaching in between leaves the logger
                # buffer size cached as 0 and logs dead for the whole session.
                terminal_ready = self.jlink.rtt_get_buf_descriptor(self.terminal_buffer, 1).SizeOfBuffer > 0
                logger_ready = num_up <= self.logger_buffer
                if not logger_ready:
                    logger_ready = self.jlink.rtt_get_buf_descriptor(self.logger_buffer, 1).SizeOfBuffer > 0
                if terminal_ready and logger_ready:
                    attached = True
                    break
                if not terminal_ready:
                    # stale/zeroed control block — restart the search below
                    break
                # Terminal is up, only the logger buffer is missing: give the
                # firmware a short window to register it, then continue
                # without logs — some firmwares have no RTT log backend and
                # their logger buffer size stays 0 forever. Descriptors are
                # read live, no need to restart the search.
                if logger_deadline is None:
                    logger_deadline = time.monotonic() + 3.0
                if time.monotonic() >= logger_deadline:
                    logger.warning('Logger buffer not initialized, continuing without logs')
                    attached = True
                    break
                time.sleep(0.1)

            if attached:
                logger.info(f'RTT started, {num_up} up bufs, {num_down} down bufs.')
                break

            try:
                self.jlink.rtt_stop()
            except pylink.errors.JLinkException:
                pass

            if time.monotonic() >= deadline:
                raise Exception('Failed to find RTT block')

            logger.info('RTT control block not initialized yet, retrying search...')
            time.sleep(0.2)

        if num_up == 0:
            raise Exception('No RTT up buffers found')

        if num_up < self.terminal_buffer:
            raise Exception(f'Shell buffer UP {self.terminal_buffer} not found')

        if num_up < self.logger_buffer:
            raise Exception(f'Log buffer UP {self.logger_buffer} not found')

        if num_down < self.terminal_buffer:
            raise Exception(f'Shell buffer DOWN {self.terminal_buffer} not found')

        self.is_running = True

        for i in range(num_up):
            desc = self.jlink.rtt_get_buf_descriptor(i, 1)
            try:
                name = desc.name
            except UnicodeDecodeError:
                name = desc.acName.decode('utf-8', errors='replace')
            logger.info(f'Up buffer {i}: {name} <Index={desc.BufferIndex}, Size={desc.SizeOfBuffer}>')
            if i == self.terminal_buffer:
                self.terminal_buffer_up_size = desc.SizeOfBuffer
            elif i == self.logger_buffer:
                self.log_up_size = desc.SizeOfBuffer
        for i in range(num_down):
            desc = self.jlink.rtt_get_buf_descriptor(i, 0)
            try:
                name = desc.name
            except UnicodeDecodeError:
                name = desc.acName.decode('utf-8', errors='replace')
            logger.info(f'Down buffer {i}: {name} <Index={desc.BufferIndex}, Size={desc.SizeOfBuffer}>')
            if i == self.terminal_buffer:
                self.terminal_buffer_down_size = desc.SizeOfBuffer

        self.thread = threading.Thread(target=self._read_task, daemon=True)
        self.thread.start()

    def reset(self, halt=False):
        """Reset the target. Restarts the RTT session unless halting."""
        was_running = self.is_running
        if was_running:
            self.stop()
        try:
            self.jlink.reset(ms=10, halt=halt)
        finally:
            if was_running and not halt:
                self.start()

    def stop(self):
        """Stop the read thread and RTT."""
        if not self.is_running:
            return
        self.is_running = False
        if self.thread:
            self.thread.join()
            self.thread = None
        self.jlink.rtt_stop()

    def open(self):
        super().open()
        self.start()
        self._emit(Event(EventType.OPEN, ''))
        logger.info('RTT opened')

    def close(self):
        super().close()
        logger.info('Closing RTT')
        self.stop()
        self._emit(Event(EventType.CLOSE, ''))
        logger.info('RTT closed')

    def handle(self, event: Event):
        logger.info(f'handle: {event.type} {event.data}')
        if event.type == EventType.IN:
            logger.info(f'RTT write shell buffer {self.terminal_buffer} buffer size {self.terminal_buffer_down_size}')
            if not self.terminal_buffer_down_size:
                raise Exception(f'Shell buffer DOWN {self.terminal_buffer} has zero size')
            data = bytearray(f'{event.data}\n', "utf-8")
            total = 0
            while total < len(data):
                chunk = data[total:total + self.terminal_buffer_down_size]
                try:
                    written = self.jlink.rtt_write(self.terminal_buffer, list(chunk))
                except pylink.errors.JLinkException as e:
                    raise Exception(f'J-Link: {e}') from e
                if written <= 0:
                    time.sleep(0.005)
                    continue
                total += written
        elif event.type == EventType.FLASH:
            self.flash(event.data.get('file'), event.data.get('addr', 0))
            return
        self._emit(event)

    def flash(self, file_path: str, addr: int = 0):
        """Flash firmware file to device. Stops RTT, flashes, restarts RTT."""
        if not self._op_lock.acquire(blocking=False):
            self._emit(Event(EventType.FLASH, {
                "status": "error", "file": file_path,
                "error": "Flash operation already in progress"
            }))
            return

        try:
            if not os.path.isfile(file_path):
                self._emit(Event(EventType.FLASH, {
                    "status": "error", "file": file_path,
                    "error": f"File not found: {file_path}"
                }))
                return

            ext = os.path.splitext(file_path)[1].lower()
            if ext not in ('.hex', '.bin', '.elf', '.srec'):
                self._emit(Event(EventType.FLASH, {
                    "status": "error", "file": file_path,
                    "error": f"Unsupported file format: {ext}"
                }))
                return

            self._emit(Event(EventType.FLASH, {"status": "start", "file": file_path}))

            was_running = self.is_running
            if was_running:
                self.stop()

            progress_calls = 0

            def on_progress(action, progress_string, percentage):
                nonlocal progress_calls
                progress_calls += 1
                if isinstance(action, bytes):
                    action = action.decode('utf-8', errors='replace')
                if isinstance(progress_string, bytes):
                    progress_string = progress_string.decode('utf-8', errors='replace')
                if action == "Erase":
                    return
                self._emit(Event(EventType.FLASH, {
                    "status": "progress", "action": action,
                    "message": progress_string,
                    "percentage": min(percentage, 100),
                }))

            try:
                logger.info(f'Flash: flashing {file_path} at 0x{addr:08X}')
                # Reset and halt before programming (same sequence as JLinkExe
                # and nrfjprog). Without it the flash loader RAMCode fails to
                # start on a running nRF91 (TF-M + modem) and the DLL silently
                # programs nothing while still returning success.
                self.jlink.reset(ms=10, halt=True)
                # flash_file return value has no significance (see pylink docs)
                self.jlink.flash_file(file_path, addr, on_progress=on_progress)
                # A real flash download reports hundreds of progress callbacks
                # (Compare/Erase/Program/Verify). Zero callbacks means the DLL
                # flash loader never ran and the device was NOT programmed,
                # even though flash_file returned success.
                if progress_calls == 0:
                    raise Exception(
                        'Flash loader did not run (no progress reported) — the device '
                        'was most likely not programmed. Reset the device and try again.')
                logger.info(f'Flash: complete ({progress_calls} progress callbacks)')
                # Drop the DLL flash cache — after a flash download it serves
                # memory reads of flash ranges from cache, which can mask a
                # failed program operation with phantom content.
                try:
                    self.jlink.exec_command('InvalidateCache')
                except pylink.errors.JLinkException as e:
                    logger.warning(f'InvalidateCache failed: {e}')
                self.jlink.reset(ms=10, halt=False)
                self._emit(Event(EventType.FLASH, {
                    "status": "done", "file": file_path
                }))
            except pylink.errors.JLinkException as e:
                logger.error(f'J-Link: {e}')
                self._try_resume()
                self._emit(Event(EventType.FLASH, {
                    "status": "error", "file": file_path, "error": f'J-Link: {e}'
                }))
            except Exception as e:
                logger.error(f'Flash error: {e}')
                self._try_resume()
                self._emit(Event(EventType.FLASH, {
                    "status": "error", "file": file_path, "error": str(e)
                }))

            if was_running:
                try:
                    self.start()
                except Exception as e:
                    logger.error(f'Flash: failed to restart RTT: {e}')
                    self._emit(Event(EventType.FLASH, {
                        "status": "error", "file": file_path,
                        "error": f"RTT restart failed: {e}"
                    }))
        finally:
            self._op_lock.release()

    def _try_resume(self):
        """Best-effort reset+go so a failure does not leave the target halted."""
        try:
            self.jlink.reset(ms=10, halt=False)
        except pylink.errors.JLinkException as e:
            logger.warning(f'Resume after failure failed: {e}')

    def _read_task(self):
        while self.is_running:
            # Read up to the full buffer size per cycle — with a smaller chunk
            # a burst (e.g. boot logs) fills the target-side ring buffer faster
            # than we drain it and the firmware drops log lines.
            channels = [
                (self.terminal_buffer, self.terminal_buffer_up_size, EventType.OUT),
                (self.logger_buffer, self.log_up_size, EventType.LOG)
            ]
            for idx, num_bytes, event_type in channels:
                if idx is None:
                    continue
                try:
                    try:
                        data = self.jlink.rtt_read(idx, num_bytes)
                    except pylink.errors.JLinkException as e:
                        raise Exception(f'J-Link: {e}') from e
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
                except Exception as e:
                    logger.error(f'Error reading RTT buffer {idx}: {e}')
            time.sleep(self.rtt_read_delay)
