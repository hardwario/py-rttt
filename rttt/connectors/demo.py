"""Synthetic connector for GUI / clipboard testing without a J-Link probe.

Selected like a probe serial number: ``--serial DEMO`` (or ``--demo``). Emits
the same CONN / OUT / LOG events a real session would, so overlays and copy
feedback can be exercised end-to-end.
"""

import threading
import time
from loguru import logger
from rttt.event import Event, EventType, conn_event
from rttt.connectors.base import Connector

# Same source id as PyLinkRTTConnector so the Connection overlay keeps saying
# "Device" rather than inventing a second transport name.
CONN_SOURCE = 'rtt'

# Expert-facing serial / SN token used on the CLI.
DEMO_SERIAL = 'DEMO'


class DemoConnector(Connector):
    def __init__(self, delay=0.5, auto_reconnect=False, reconnect_interval=0.5) -> None:
        super().__init__()
        self.i = 0
        self.delay = delay
        self.auto_reconnect = auto_reconnect
        self.reconnect_interval = reconnect_interval
        self.is_running = False
        self.thread = None
        self._watchdog = None
        self._watchdog_stop = threading.Event()
        self._reconnect_now = threading.Event()
        self._conn_up = None
        self._pause_output = False
        self._lock = threading.Lock()
        # lines per second; delay = 1/rate. Default matches delay=0.5 → 2 Hz.
        self._rate = (1.0 / delay) if delay else 2.0

    _HELP = (
        'Demo commands:\n'
        '  help              Show this list\n'
        '  rate              Print current lines/sec\n'
        '  rate <n>          Set lines/sec (e.g. rate 20)\n'
        '  burst <n>         Emit n lines immediately\n'        '  (log lines use # n.0 <D|I|W|E> for filter level:)\n'
        '  disconnect        Simulate link drop\n'
        '  reconnect         Re-attach (also F4 / overlay)'
    )

    def handle(self, event: Event):
        logger.info(f'handle: {event.type} {event.data}')
        # Echo input back as OUT so the interactive pane is usable in demo.
        if event.type == EventType.IN:
            text = event.data if isinstance(event.data, str) else str(event.data)
            if self._handle_demo_command(text):
                return
            # While the link is down, do not echo — otherwise the pane looks
            # alive and the overlay's "disconnected" state is confusing.
            if self._conn_up:
                self._emit(Event(EventType.OUT, text))
            return
        self._emit(event)

    def _handle_demo_command(self, text: str) -> bool:
        """Run a built-in demo command. True if consumed (no echo)."""
        raw = text.strip()
        if not raw:
            return False
        parts = raw.split()
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd == 'help' or raw.lower() in ('demo help',):
            self._emit(Event(EventType.OUT, self._HELP))
            return True

        if cmd == 'rate':
            if not args:
                self._emit(Event(
                    EventType.OUT,
                    f'Demo rate: {self._rate:g} lines/sec (delay {self.delay:g}s)'))
                return True
            try:
                n = float(args[0])
            except ValueError:
                self._emit(Event(EventType.OUT, 'usage: rate <lines-per-second>'))
                return True
            if n <= 0:
                self._emit(Event(EventType.OUT, 'rate must be > 0'))
                return True
            with self._lock:
                self._rate = n
                self.delay = 1.0 / n
            self._emit(Event(
                EventType.OUT, f'Demo rate set to {n:g} lines/sec'))
            return True

        if cmd == 'burst':
            if len(args) != 1:
                self._emit(Event(EventType.OUT, 'usage: burst <n>'))
                return True
            try:
                n = int(args[0])
            except ValueError:
                self._emit(Event(EventType.OUT, 'usage: burst <n>'))
                return True
            if n < 0:
                self._emit(Event(EventType.OUT, 'burst count must be >= 0'))
                return True
            self._burst(n)
            return True

        if cmd == 'disconnect' or raw.lower() == 'demo disconnect':
            self._simulate_disconnect(
                'Demo disconnect (type reconnect, press F4, or click Reconnect)'
            )
            return True
        if cmd == 'reconnect' or raw.lower() == 'demo reconnect':
            self.request_reconnect()
            return True
        return False

    def _burst(self, n: int):
        """Emit n LOG/OUT lines as fast as possible (for selection stress tests)."""
        with self._lock:
            if self._pause_output or self._conn_up is not True:
                alive = False
            else:
                alive = True
                batch = []
                for _ in range(n):
                    self.i += 1
                    k = self.i
                    if k % 2 == 0:
                        lvl = ('D', 'I', 'W', 'E')[(k // 2) % 4]
                        batch.append(Event(
                            EventType.LOG, f'# {k}.0 <{lvl}> log {k}'))
                    else:
                        batch.append(Event(EventType.OUT, f'term {k}'))
        if not alive:
            self._emit(Event(EventType.OUT, 'burst ignored (link down)'))
            return
        for ev in batch:
            self._emit(ev)
        self._emit(Event(EventType.OUT, f'Demo burst: {n} lines'))

    def open(self):
        super().open()
        logger.info('open')
        self.is_running = True
        self._watchdog_stop.clear()
        self.thread = threading.Thread(target=self._task, daemon=True)
        self.thread.start()
        self._watchdog = threading.Thread(target=self._reconnect_watchdog, daemon=True)
        self._watchdog.start()
        self._emit_conn(True)

    def close(self):
        super().close()
        logger.info('close')
        self._watchdog_stop.set()
        self._reconnect_now.set()
        if self._watchdog and self._watchdog.is_alive():
            self._watchdog.join(timeout=2)
        if not self.is_running:
            return
        self.is_running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        self._emit_conn(False)
        self._emit(Event(EventType.CLOSE, ''))

    def request_reconnect(self):
        """Ask for an immediate re-attach (F4 / overlay button).

        Returns at once; the watchdog thread performs the reconnect so a UI
        key handler is never blocked.
        """
        self._reconnect_now.set()

    def _simulate_disconnect(self, error='Demo link down'):
        with self._lock:
            self._pause_output = True
            self._emit_conn(False, error)

    def _emit_conn(self, up, error='', status=None):
        if self._conn_up is up and status is None:
            return
        self._conn_up = up
        if status is None:
            status = 'connected' if up else 'disconnected'
        self._emit(conn_event(CONN_SOURCE, status, error))

    def _reconnect_watchdog(self):
        while not self._watchdog_stop.is_set():
            requested = self._reconnect_now.wait(self.reconnect_interval)
            if self._watchdog_stop.is_set():
                break
            if requested:
                self._reconnect_now.clear()
            elif not (self.auto_reconnect and self._conn_up is False):
                continue

            logger.info('Demo re-attaching')
            # Brief down → up so the overlay path is exercised even when
            # already connected (explicit F4). If already down, just come back up.
            with self._lock:
                self._pause_output = True
                if self._conn_up is True:
                    self._emit_conn(False, 'Demo reconnecting…')
            time.sleep(0.05)
            with self._lock:
                self._pause_output = False
                self._emit_conn(True)

    def _task(self):
        self._emit(Event(EventType.OPEN, ''))
        while self.is_running:
            with self._lock:
                # Both flags under the same lock so a disconnect cannot race
                # with a tick and leak one more OUT/LOG while the overlay is up.
                emit_now = (not self._pause_output) and (self._conn_up is True)
                if emit_now:
                    self.i += 1
                    n = self.i
            if emit_now:
                if n % 2 == 0:
                    # Rotate dbg/inf/wrn/err so Log filter level: presets work.
                    lvl = ('D', 'I', 'W', 'E')[(n // 2) % 4]
                    self._emit(Event(
                        EventType.LOG, f'# {n}.0 <{lvl}> log {n}'))
                else:
                    self._emit(Event(EventType.OUT, f'term {n}'))
            time.sleep(self.delay)
