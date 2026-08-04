import os
import pylink
import shlex
import subprocess
import time
import threading
from loguru import logger
from rttt.connectors.base import Connector
from rttt.event import Event, EventType, conn_event

CONN_SOURCE = 'rtt'


class PyLinkRTTConnector(Connector):

    def __init__(self, jlink: pylink.JLink, terminal_buffer=0, logger_buffer=1, latency=50, block_address=None,
                 flash_cmd=None, device=None, serial=None, speed=None, write_timeout=2.0,
                 power_check_interval=1.0, min_target_voltage=1000,
                 auto_reconnect=False, reconnect_interval=3.0) -> None:
        super().__init__()
        self.jlink = jlink
        self.write_timeout = write_timeout
        self.power_check_interval = power_check_interval
        self.min_target_voltage = min_target_voltage
        self._next_power_check = 0.0
        self._seen_target_power = False
        self.auto_reconnect = auto_reconnect
        self.reconnect_interval = reconnect_interval
        self._watchdog = None
        self._watchdog_stop = threading.Event()
        self._reconnect_now = threading.Event()
        self.block_address = block_address
        self.rtt_read_delay = latency / 1000.0
        self.is_running = False
        self.thread = None
        # terminal_buffer and logger_buffer accept an index or a buffer name.
        # Names are resolved against the up descriptors on every start(), so a
        # reflash that moves the buffers is picked up; the public attributes
        # stay indices either way.
        self._terminal_spec = terminal_buffer
        self._logger_spec = logger_buffer
        self.terminal_buffer = terminal_buffer if isinstance(terminal_buffer, int) else 0
        self.terminal_buffer_up_size = 0
        self.terminal_buffer_down_size = 0
        self.logger_buffer = logger_buffer if isinstance(logger_buffer, int) else 1
        self.log_up_size = 0
        self.flash_cmd = flash_cmd
        self.device = device
        self.serial = serial
        self.speed = speed
        self._op_lock = threading.Lock()
        # None until the first CONN event, so the initial connect is reported.
        self._conn_up = None

    def request_reconnect(self):
        """Ask for an immediate re-attach.

        Returns at once: the work happens on the watchdog thread, so a UI
        calling this from a key handler or a button is never blocked for the
        seconds an attach can take.
        """
        self._reconnect_now.set()

    def _reconnect_watchdog(self):
        """Re-attach RTT on request, or while auto_reconnect is on and down.

        Runs on its own thread because reattaching means stop() then start(),
        and stop() joins the read thread — driving that from the read thread
        itself would deadlock.
        """
        while not self._watchdog_stop.is_set():
            requested = self._reconnect_now.wait(self.reconnect_interval)
            if self._watchdog_stop.is_set():
                break

            if requested:
                self._reconnect_now.clear()
            elif not (self.auto_reconnect and self._conn_up is False):
                continue
            else:
                # Automatic retries stay off the probe while the board has no
                # power: the attach cannot succeed and each attempt takes
                # seconds. An explicit request is still honoured, so asking
                # for it gets a real answer rather than silence.
                if self._seen_target_power:
                    try:
                        if self.jlink.hardware_status.VTarget < self.min_target_voltage:
                            continue
                    except Exception:
                        continue

            if not self._op_lock.acquire(blocking=False):
                continue
            try:
                logger.info('Re-attaching RTT')
                try:
                    self.stop()
                except Exception as e:
                    logger.warning(f'Reconnect: stop failed: {e}')
                try:
                    # rtt_start() on its own is not enough once the target has
                    # dropped: the DLL keeps its connection to a device that is
                    # no longer there and every attach fails with 'Unspecified
                    # error'. Re-establishing it first is what makes the retry
                    # able to succeed. Needs the device, so a connector built
                    # without one can only try the plain attach.
                    if self.device:
                        try:
                            self._reopen_jlink()
                        except Exception as e:
                            logger.warning(f'Reconnect: reopening the probe failed: {e}')
                    self.start()
                except Exception as e:
                    # start() reports nothing on failure, so keep the console's
                    # warning accurate and try again on the next tick.
                    logger.warning(f'Reconnect: attach failed: {e}')
                    self._emit_conn(False, f'Reconnecting failed: {e}')
            finally:
                self._op_lock.release()

    def _check_target_power(self):
        """Report a target that lost power, using the probe's measured VTref.

        Needed because a silent link is unreadable from rtt_read alone: on an
        unpowered board it returns nothing rather than failing, which is
        indistinguishable from a device that simply has nothing to say.

        Only ever reports a disconnect. Power coming back does not mean the
        session works again — the firmware reboots and the old RTT control
        block is stale — so recovery is left to arriving data or an explicit
        reconnect.
        """
        now = time.monotonic()
        if now < self._next_power_check:
            return
        self._next_power_check = now + self.power_check_interval

        try:
            voltage = self.jlink.hardware_status.VTarget
        except Exception as e:
            self._emit_conn(False, f'J-Link: {e}')
            return

        if voltage >= self.min_target_voltage:
            self._seen_target_power = True
        elif self._seen_target_power:
            # Only trusted once a healthy reading has been seen, so a probe
            # that cannot measure VTref and always reports 0 never trips this.
            self._emit_conn(False, f'Target has no power (VTref {voltage} mV)')

    def _emit_conn(self, up, error=''):
        """Emit a CONN event, but only when the state actually changed.

        The read task retries a dead link every cycle, so emitting per failure
        would flood the console and the log file.
        """
        if self._conn_up is up:
            return
        self._conn_up = up
        self._emit(conn_event(CONN_SOURCE, 'connected' if up else 'disconnected', error))

    @staticmethod
    def _descriptor_name(desc):
        """Buffer name from a descriptor, tolerating undecodable bytes."""
        try:
            return desc.name
        except UnicodeDecodeError:
            return desc.acName.decode('utf-8', errors='replace')

    def _resolve_buffer_names(self, num_up):
        """Map any buffer given by name onto its index.

        Returns False when a requested name is not present yet. Buffers
        register one by one during boot, so the caller treats that like an
        uninitialized control block and retries.
        """
        names = None
        for attr, spec in (('terminal_buffer', self._terminal_spec),
                           ('logger_buffer', self._logger_spec)):
            if not isinstance(spec, str):
                continue

            if names is None:
                names = {}
                for i in range(num_up):
                    try:
                        names[self._descriptor_name(self.jlink.rtt_get_buf_descriptor(i, 1))] = i
                    except pylink.errors.JLinkException:
                        break

            if spec not in names:
                logger.info(f'RTT buffer {spec!r} not registered yet, retrying search...')
                return False

            setattr(self, attr, names[spec])
            logger.info(f'RTT buffer {spec!r} resolved to index {names[spec]}')

        return True

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

            resolved = num_up is not None and self._resolve_buffer_names(num_up)

            attached = False
            while resolved and num_up > self.terminal_buffer:
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
            name = self._descriptor_name(desc)
            logger.info(f'Up buffer {i}: {name} <Index={desc.BufferIndex}, Size={desc.SizeOfBuffer}>')
            if i == self.terminal_buffer:
                self.terminal_buffer_up_size = desc.SizeOfBuffer
            elif i == self.logger_buffer:
                self.log_up_size = desc.SizeOfBuffer
        for i in range(num_down):
            desc = self.jlink.rtt_get_buf_descriptor(i, 0)
            name = self._descriptor_name(desc)
            logger.info(f'Down buffer {i}: {name} <Index={desc.BufferIndex}, Size={desc.SizeOfBuffer}>')
            if i == self.terminal_buffer:
                self.terminal_buffer_down_size = desc.SizeOfBuffer

        self.thread = threading.Thread(target=self._read_task, daemon=True)
        self.thread.start()
        self._emit_conn(True)

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
        self._emit_conn(False)

    def open(self):
        super().open()

        self._watchdog_stop.clear()
        self._watchdog = threading.Thread(target=self._reconnect_watchdog, daemon=True)
        self._watchdog.start()

        try:
            self.start()
        except Exception as e:
            if not self.auto_reconnect:
                self._watchdog_stop.set()
                raise
            # With auto reconnect asked for, a target that is not there yet is
            # not a startup failure: come up disconnected and let the watchdog
            # attach once it appears.
            logger.warning(f'RTT not available yet: {e}')
            self._emit_conn(False, str(e))

        self._emit(Event(EventType.OPEN, ''))
        logger.info('RTT opened')

    def close(self):
        super().close()
        logger.info('Closing RTT')
        self._watchdog_stop.set()
        if self._watchdog:
            self._watchdog.join(timeout=self.reconnect_interval + 1.0)
            self._watchdog = None
        self.stop()
        self._emit(Event(EventType.CLOSE, ''))
        logger.info('RTT closed')

    def handle(self, event: Event):
        logger.info(f'handle: {event.type} {event.data}')
        if event.type == EventType.IN:
            if not self._write_line(event.data):
                # The command never reached the target, so do not echo it back
                # as if it had been sent.
                return
        elif event.type == EventType.FLASH:
            self.flash(event.data.get('file'), event.data.get('addr', 0))
            return
        self._emit(event)

    def _write_line(self, line):
        """Write one line to the down buffer. Returns False if it did not go out.

        Never raises. handle() runs on the console's key-handler thread, where
        an exception tears down the whole prompt_toolkit event loop — sending a
        command to an unpowered target used to kill the console outright.
        """
        logger.info(f'RTT write shell buffer {self.terminal_buffer} buffer size {self.terminal_buffer_down_size}')

        if not self.terminal_buffer_down_size:
            # Not a dropped link: the buffer exists but the firmware never
            # sized it, which `reconnect` recovers from. Reads may be fine, so
            # this must not claim the transport is down.
            logger.error(f'Shell buffer DOWN {self.terminal_buffer} has zero size')
            return False

        data = bytearray(f'{line}\n', "utf-8")
        total = 0
        deadline = time.monotonic() + self.write_timeout
        while total < len(data):
            chunk = data[total:total + self.terminal_buffer_down_size]
            try:
                written = self.jlink.rtt_write(self.terminal_buffer, list(chunk))
            except pylink.errors.JLinkException as e:
                logger.error(f'RTT write failed: {e}')
                self._emit_conn(False, f'J-Link: {e}')
                return False
            if written <= 0:
                # A target that stops draining the buffer would otherwise spin
                # here forever, freezing the console on its own key handler.
                if time.monotonic() >= deadline:
                    logger.error('RTT write timed out, target is not reading the shell buffer')
                    self._emit_conn(False, 'Target is not reading the shell buffer')
                    return False
                time.sleep(0.005)
                continue
            total += written

        # A write that landed is proof the link works, so it clears a stale
        # disconnect that empty reads alone can never clear.
        self._emit_conn(True)
        return True

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

            allowed = ('.hex', '.bin', '.elf', '.srec')
            if self.flash_cmd:
                # external tools handle more formats (e.g. nrfjprog modem .zip)
                allowed += ('.zip',)
            ext = os.path.splitext(file_path)[1].lower()
            if ext not in allowed:
                self._emit(Event(EventType.FLASH, {
                    "status": "error", "file": file_path,
                    "error": f"Unsupported file format: {ext}"
                }))
                return

            self._emit(Event(EventType.FLASH, {"status": "start", "file": file_path}))

            was_running = self.is_running
            if was_running:
                self.stop()

            if self.flash_cmd:
                try:
                    self._flash_external(file_path, addr)
                    self._emit(Event(EventType.FLASH, {
                        "status": "done", "file": file_path
                    }))
                except Exception as e:
                    logger.error(f'Flash: external command failed: {e}')
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
                return

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

    def _flash_external(self, file_path: str, addr: int):
        """Flash by running the user-provided external command.

        The J-Link connection is closed for the duration of the command
        (external tools need the debug probe) and reopened afterwards.
        Raises on any failure.
        """
        if '{file}' not in self.flash_cmd:
            raise Exception('flash command must contain the {file} placeholder')
        try:
            # The command template itself is trusted (CLI flag or a config
            # that passed the shell-trust prompt), but substituted values are
            # not — file paths arrive from MCP clients — so quote them.
            cmd = self.flash_cmd.format(
                file=shlex.quote(file_path), addr=f'0x{addr:X}',
                device=shlex.quote(str(self.device or '')),
                serial=shlex.quote(str(self.serial or '')))
        except (KeyError, IndexError) as e:
            raise Exception(f'Unknown placeholder in flash command: {e}') from e

        logger.info(f'Flash: running external command: {cmd}')
        self.jlink.close()
        try:
            proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            deadline = time.monotonic() + 900
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    logger.info(f'Flash: {line}')
                    self._emit(Event(EventType.FLASH, {
                        "status": "progress", "action": "External", "message": line,
                    }))
                if time.monotonic() > deadline:
                    proc.kill()
                    raise Exception('Flash command timed out (15 min)')
            ret = proc.wait(timeout=60)
            if ret != 0:
                raise Exception(f'Flash command failed with exit code {ret}')
            logger.info('Flash: external command complete')
        finally:
            self._reopen_jlink()

    def _reopen_jlink(self):
        """Reopen the J-Link connection after an external tool used the probe."""
        self.jlink.open(serial_no=self.serial)
        try:
            self.jlink.disable_dialog_boxes()
        except Exception:
            pass
        self.jlink.set_speed(self.speed or 2000)
        self.jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)
        for attempt in range(3):
            try:
                self.jlink.connect(self.device)
                return
            except pylink.errors.JLinkException as e:
                if attempt < 2:
                    logger.warning(f'J-Link: reconnect attempt {attempt + 1} failed: {e}, retrying...')
                    time.sleep(0.5)
                else:
                    raise Exception(f'J-Link reconnect failed: {e}') from e

    def _read_task(self):
        while self.is_running:
            # Read up to the full buffer size per cycle — with a smaller chunk
            # a burst (e.g. boot logs) fills the target-side ring buffer faster
            # than we drain it and the firmware drops log lines.
            channels = [
                (self.terminal_buffer, self.terminal_buffer_up_size, EventType.OUT),
                (self.logger_buffer, self.log_up_size, EventType.LOG)
            ]
            failure = None
            got_data = False
            for idx, num_bytes, event_type in channels:
                if idx is None:
                    continue
                try:
                    try:
                        data = self.jlink.rtt_read(idx, num_bytes)
                    except pylink.errors.JLinkException as e:
                        raise Exception(f'J-Link: {e}') from e
                    if data:
                        got_data = True
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
                    failure = e
                    logger.error(f'Error reading RTT buffer {idx}: {e}')

            if self.is_running:
                # A read that returns nothing is not evidence of a live target:
                # on an unpowered board rtt_read does not fail, it just comes
                # back empty, exactly like an idle device. So only real data
                # clears a disconnect — otherwise the empty reads that follow a
                # failed write would immediately undo it and the warning would
                # merely blink.
                if failure is not None:
                    self._emit_conn(False, str(failure))
                elif got_data:
                    self._emit_conn(True)
                else:
                    self._check_target_power()

            time.sleep(self.rtt_read_delay)
