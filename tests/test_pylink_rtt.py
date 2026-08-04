import time
import threading
import os
import pytest
import rttt.connectors.pylink_rtt as pylink_rtt_module
from rttt.connectors.pylink_rtt import PyLinkRTTConnector
from rttt.event import Event, EventType


class FakeBufDesc:
    def __init__(self, index, size, name=''):
        self.BufferIndex = index
        self.SizeOfBuffer = size
        self.name = name


class FakeHardwareStatus:
    def __init__(self, vtarget):
        self.VTarget = vtarget


class FakeJLink:
    """Minimal stand-in for pylink.JLink driving PyLinkRTTConnector."""

    def open(self, serial_no=None):
        self.calls.append(('open', serial_no))

    def close(self):
        self.calls.append('close')

    def disable_dialog_boxes(self):
        pass

    def set_speed(self, speed):
        self.calls.append(('set_speed', speed))

    def set_tif(self, tif):
        pass

    def connect(self, device):
        self.calls.append(('connect', device))

    def __init__(self):
        self.num_up = 3
        self.num_down = 3
        # sizes[(index, up)] -> list of sizes returned on successive reads
        # (last value repeats forever)
        self.sizes = {
            (0, 1): [1024],
            (1, 1): [4096],
            (2, 1): [0],
            (0, 0): [256],
            (1, 0): [0],
            (2, 0): [0],
        }
        self.calls = []
        self.flash_progress_actions = ['Compare', 'Program', 'Verify']
        # names[(index, up)] -> buffer name reported by the descriptor
        self.names = {}
        # measured target supply voltage in mV, as the probe reports it
        self.vtarget = 3300

    def _next_size(self, key):
        seq = self.sizes[key]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def rtt_start(self, block_address=None):
        self.calls.append('rtt_start')

    def rtt_stop(self):
        self.calls.append('rtt_stop')

    def rtt_get_num_up_buffers(self):
        return self.num_up

    def rtt_get_num_down_buffers(self):
        return self.num_down

    def rtt_get_buf_descriptor(self, index, up):
        return FakeBufDesc(index, self._next_size((index, up)), self.names.get((index, up), ''))

    def rtt_read(self, index, num_bytes):
        return []

    def rtt_write(self, index, data):
        return len(data)

    @property
    def hardware_status(self):
        return FakeHardwareStatus(self.vtarget)

    def reset(self, ms=0, halt=True):
        self.calls.append(('reset', halt))

    def halt(self):
        self.calls.append('halt')

    def exec_command(self, cmd):
        self.calls.append(('exec', cmd))
        return 0

    def flash_file(self, path, addr, on_progress=None):
        self.calls.append(('flash_file', path, addr))
        if on_progress:
            for i, action in enumerate(self.flash_progress_actions):
                on_progress(action, f'{action}...', i * 10)
        return 0


@pytest.fixture
def virtual_clock(monkeypatch):
    """Replace time.monotonic/time.sleep in the module with a virtual clock."""
    state = {'now': 0.0}

    def monotonic():
        return state['now']

    def sleep(seconds):
        state['now'] += seconds

    monkeypatch.setattr(pylink_rtt_module.time, 'monotonic', monotonic)
    monkeypatch.setattr(pylink_rtt_module.time, 'sleep', sleep)
    return state


def make_connector(jlink):
    conn = PyLinkRTTConnector(jlink)
    events = []
    conn.on(lambda e: events.append(e))
    return conn, events


def stop_read_thread(conn):
    conn.is_running = False
    if conn.thread:
        conn.thread.join()
        conn.thread = None


def test_start_reads_buffer_sizes(virtual_clock):
    jlink = FakeJLink()
    conn, _ = make_connector(jlink)
    conn.start()
    stop_read_thread(conn)

    assert conn.terminal_buffer_up_size == 1024
    assert conn.log_up_size == 4096
    assert conn.terminal_buffer_down_size == 256


def test_start_waits_for_logger_buffer(virtual_clock):
    # Logger buffer registers later than the terminal buffer during boot.
    jlink = FakeJLink()
    jlink.sizes[(1, 1)] = [0, 0, 0, 4096]
    conn, _ = make_connector(jlink)
    conn.start()
    stop_read_thread(conn)

    assert conn.log_up_size == 4096


def test_start_continues_without_logger_buffer(virtual_clock):
    # Firmware without an RTT log backend: logger buffer size stays 0.
    jlink = FakeJLink()
    jlink.sizes[(1, 1)] = [0]
    conn, _ = make_connector(jlink)
    conn.start()
    stop_read_thread(conn)

    assert conn.log_up_size == 0
    assert conn.terminal_buffer_up_size == 1024


def test_start_retries_stale_control_block(virtual_clock):
    # Right after flash+reset the search finds a stale block with zeroed
    # terminal descriptor; the search must restart until it is valid.
    jlink = FakeJLink()
    jlink.sizes[(0, 1)] = [0, 0, 1024]
    conn, _ = make_connector(jlink)
    conn.start()
    stop_read_thread(conn)

    assert jlink.calls.count('rtt_stop') >= 2
    assert conn.terminal_buffer_up_size == 1024


def test_start_fails_when_terminal_never_ready(virtual_clock):
    jlink = FakeJLink()
    jlink.sizes[(0, 1)] = [0]
    conn, _ = make_connector(jlink)

    with pytest.raises(Exception, match='Failed to find RTT block'):
        conn.start()


def flash_events(events):
    return [e.data for e in events if e.type == EventType.FLASH]


def test_flash_success(tmp_path, virtual_clock):
    fw = tmp_path / 'fw.hex'
    fw.write_bytes(b':00000001FF\n')

    jlink = FakeJLink()
    conn, events = make_connector(jlink)
    conn.flash(str(fw))

    statuses = [e['status'] for e in flash_events(events)]
    assert statuses[0] == 'start'
    assert statuses[-1] == 'done'
    # reset+halt before programming, reset+go after
    assert ('reset', True) in jlink.calls
    assert ('reset', False) in jlink.calls
    assert ('exec', 'InvalidateCache') in jlink.calls


def test_flash_zero_progress_is_error(tmp_path, virtual_clock):
    # DLL flash loader failing to start reports no progress callbacks while
    # flash_file still returns success — must be treated as a failed flash.
    fw = tmp_path / 'fw.hex'
    fw.write_bytes(b':00000001FF\n')

    jlink = FakeJLink()
    jlink.flash_progress_actions = []
    conn, events = make_connector(jlink)
    conn.flash(str(fw))

    final = flash_events(events)[-1]
    assert final['status'] == 'error'
    assert 'not programmed' in final['error']
    # target must not stay halted after the failure
    assert jlink.calls[-1] == ('reset', False)


def test_flash_rejects_unknown_extension(tmp_path, virtual_clock):
    fw = tmp_path / 'fw.txt'
    fw.write_bytes(b'x')

    jlink = FakeJLink()
    conn, events = make_connector(jlink)
    conn.flash(str(fw))

    final = flash_events(events)[-1]
    assert final['status'] == 'error'
    assert 'Unsupported file format' in final['error']


def test_flash_locked_reports_error(tmp_path, virtual_clock):
    fw = tmp_path / 'fw.hex'
    fw.write_bytes(b':00000001FF\n')

    jlink = FakeJLink()
    conn, events = make_connector(jlink)
    assert conn._op_lock.acquire(blocking=False)
    try:
        conn.flash(str(fw))
    finally:
        conn._op_lock.release()

    final = flash_events(events)[-1]
    assert final['status'] == 'error'
    assert 'in progress' in final['error']


def make_external_connector(jlink, flash_cmd):
    conn = PyLinkRTTConnector(jlink, flash_cmd=flash_cmd,
                              device='NRF9151_XXCA', serial=123, speed=2000)
    events = []
    conn.on(lambda e: events.append(e))
    return conn, events


def test_flash_external_success(tmp_path, virtual_clock):
    fw = tmp_path / 'fw.hex'
    fw.write_bytes(b':00000001FF\n')

    jlink = FakeJLink()
    conn, events = make_external_connector(jlink, 'echo programming {file} on {device}')
    conn.flash(str(fw))

    fevents = flash_events(events)
    assert fevents[-1]['status'] == 'done'
    progress = [e for e in fevents if e['status'] == 'progress']
    assert any('programming' in e['message'] and 'fw.hex' in e['message'] for e in progress)
    # J-Link released for the external tool and reconnected afterwards
    assert 'close' in jlink.calls
    assert ('connect', 'NRF9151_XXCA') in jlink.calls
    assert jlink.calls.index('close') < jlink.calls.index(('connect', 'NRF9151_XXCA'))


def test_flash_external_failure(tmp_path, virtual_clock):
    fw = tmp_path / 'fw.hex'
    fw.write_bytes(b':00000001FF\n')

    jlink = FakeJLink()
    conn, events = make_external_connector(jlink, 'echo {file} && exit 3')
    conn.flash(str(fw))

    final = flash_events(events)[-1]
    assert final['status'] == 'error'
    assert 'exit code 3' in final['error']
    # J-Link must be reconnected even after a failure
    assert ('connect', 'NRF9151_XXCA') in jlink.calls


def test_flash_external_requires_file_placeholder(tmp_path, virtual_clock):
    fw = tmp_path / 'fw.hex'
    fw.write_bytes(b':00000001FF\n')

    jlink = FakeJLink()
    conn, events = make_external_connector(jlink, 'nrfjprog --program firmware.hex')
    conn.flash(str(fw))

    final = flash_events(events)[-1]
    assert final['status'] == 'error'
    assert '{file}' in final['error']


def test_flash_external_quotes_file_path(tmp_path, virtual_clock):
    # A malicious file path must not be able to inject shell commands.
    evil_dir = tmp_path / 'a; touch pwned;'
    evil_dir.mkdir()
    fw = evil_dir / 'fw.hex'
    fw.write_bytes(b':00000001FF\n')

    jlink = FakeJLink()
    conn, events = make_external_connector(jlink, 'echo {file}')
    conn.flash(str(fw))

    assert flash_events(events)[-1]['status'] == 'done'
    assert not (tmp_path / 'pwned').exists()
    assert not os.path.exists('pwned')


def test_flash_external_allows_zip(tmp_path, virtual_clock):
    fw = tmp_path / 'modem.zip'
    fw.write_bytes(b'PK')

    jlink = FakeJLink()
    conn, events = make_external_connector(jlink, 'echo {file}')
    conn.flash(str(fw))

    assert flash_events(events)[-1]['status'] == 'done'


def test_flash_zip_rejected_without_external_cmd(tmp_path, virtual_clock):
    fw = tmp_path / 'modem.zip'
    fw.write_bytes(b'PK')

    jlink = FakeJLink()
    conn, events = make_connector(jlink)
    conn.flash(str(fw))

    final = flash_events(events)[-1]
    assert final['status'] == 'error'
    assert 'Unsupported file format' in final['error']


def test_reset_restarts_rtt(virtual_clock):
    jlink = FakeJLink()
    conn, _ = make_connector(jlink)
    conn.start()
    stop_read_thread(conn)
    conn.is_running = True  # emulate running session for reset()

    jlink.calls.clear()
    conn.reset(halt=False)
    stop_read_thread(conn)

    assert ('reset', False) in jlink.calls
    assert 'rtt_start' in jlink.calls


def test_reset_halt_leaves_rtt_stopped(virtual_clock):
    jlink = FakeJLink()
    conn, _ = make_connector(jlink)
    conn.start()
    stop_read_thread(conn)
    conn.is_running = True

    jlink.calls.clear()
    conn.reset(halt=True)

    assert ('reset', True) in jlink.calls
    assert 'rtt_start' not in jlink.calls
    assert not conn.is_running


class UndecodableName:
    """Descriptor whose .name raises, as pylink does for non-UTF-8 bytes."""

    def __init__(self, index, size, raw):
        self.BufferIndex = index
        self.SizeOfBuffer = size
        self.acName = raw

    @property
    def name(self):
        raise UnicodeDecodeError('utf-8', self.acName, 0, 1, 'invalid start byte')


def test_buffer_names_resolve_to_indices(virtual_clock):
    # Firmware registers Logger first, so the names do not sit on the
    # default 0/1 indices.
    jlink = FakeJLink()
    jlink.names = {(0, 1): 'Logger', (2, 1): 'Terminal', (2, 0): 'Terminal'}
    jlink.sizes[(0, 1)] = [4096]
    jlink.sizes[(2, 1)] = [1024]
    jlink.sizes[(2, 0)] = [256]

    conn = PyLinkRTTConnector(jlink, terminal_buffer='Terminal', logger_buffer='Logger')
    conn.start()
    stop_read_thread(conn)

    assert conn.terminal_buffer == 2
    assert conn.logger_buffer == 0
    assert conn.terminal_buffer_up_size == 1024
    assert conn.log_up_size == 4096
    assert conn.terminal_buffer_down_size == 256


def test_integer_buffers_still_work(virtual_clock):
    # Named lookup must not disturb the index-based default.
    jlink = FakeJLink()
    conn = PyLinkRTTConnector(jlink, terminal_buffer=0, logger_buffer=1)
    conn.start()
    stop_read_thread(conn)

    assert conn.terminal_buffer == 0
    assert conn.logger_buffer == 1
    assert conn.terminal_buffer_up_size == 1024
    assert conn.log_up_size == 4096


def test_buffer_names_are_reresolved_on_restart(virtual_clock):
    # A reflash can move the buffers; the second start() must not reuse the
    # index resolved for the previous firmware.
    jlink = FakeJLink()
    jlink.names = {(0, 1): 'Terminal', (0, 0): 'Terminal'}
    conn = PyLinkRTTConnector(jlink, terminal_buffer='Terminal', logger_buffer=1)
    conn.start()
    stop_read_thread(conn)
    assert conn.terminal_buffer == 0

    jlink.names = {(2, 1): 'Terminal', (2, 0): 'Terminal'}
    jlink.sizes[(2, 1)] = [1024]
    jlink.sizes[(2, 0)] = [256]
    conn.start()
    stop_read_thread(conn)

    assert conn.terminal_buffer == 2


def test_missing_buffer_name_fails_after_deadline(virtual_clock):
    jlink = FakeJLink()
    jlink.names = {(0, 1): 'Terminal', (0, 0): 'Terminal'}

    conn = PyLinkRTTConnector(jlink, terminal_buffer='Terminal', logger_buffer='NoSuchBuffer')
    with pytest.raises(Exception, match='Failed to find RTT block'):
        conn.start()
    stop_read_thread(conn)

    # It kept retrying the search rather than giving up on the first pass.
    assert jlink.calls.count('rtt_start') > 1


def test_buffer_name_appearing_late_is_picked_up(virtual_clock):
    # The named buffer is absent on the first search and shows up on a retry.
    jlink = FakeJLink()
    jlink.names = {(0, 1): 'Terminal', (0, 0): 'Terminal'}

    conn = PyLinkRTTConnector(jlink, terminal_buffer='Terminal', logger_buffer='Logger')

    real = jlink.rtt_start
    state = {'n': 0}

    def rtt_start(block_address=None):
        state['n'] += 1
        if state['n'] == 3:
            jlink.names[(1, 1)] = 'Logger'
        real(block_address)

    jlink.rtt_start = rtt_start

    conn.start()
    stop_read_thread(conn)

    assert conn.terminal_buffer == 0
    assert conn.logger_buffer == 1


def test_undecodable_buffer_name_does_not_break_resolution(virtual_clock):
    jlink = FakeJLink()
    jlink.names = {(1, 1): 'Terminal', (1, 0): 'Terminal'}
    jlink.sizes[(1, 0)] = [256]

    def rtt_get_buf_descriptor(index, up):
        size = jlink._next_size((index, up))
        if index == 0:
            return UndecodableName(index, size, b'\xff\xfe')
        return FakeBufDesc(index, size, jlink.names.get((index, up), ''))

    jlink.rtt_get_buf_descriptor = rtt_get_buf_descriptor

    conn = PyLinkRTTConnector(jlink, terminal_buffer='Terminal', logger_buffer=2)
    conn.start()
    stop_read_thread(conn)

    assert conn.terminal_buffer == 1


def conn_events(events):
    return [e.data for e in events if e.type == EventType.CONN]


def test_conn_emit_dedups_transitions():
    # Deterministic check of the transition guard, no threads involved: the
    # read task calls this every cycle and must not produce an event per call.
    jlink = FakeJLink()
    conn, events = make_connector(jlink)

    conn._emit_conn(True)
    conn._emit_conn(True)
    conn._emit_conn(True)
    assert [e['status'] for e in conn_events(events)] == ['connected']

    conn._emit_conn(False, 'boom')
    conn._emit_conn(False, 'boom')
    assert [e['status'] for e in conn_events(events)] == ['connected', 'disconnected']
    assert conn_events(events)[-1]['error'] == 'boom'

    conn._emit_conn(True)
    assert [e['status'] for e in conn_events(events)] == ['connected', 'disconnected', 'connected']


def test_conn_reported_on_start_and_stop(virtual_clock):
    jlink = FakeJLink()
    conn, events = make_connector(jlink)
    conn.start()
    assert conn_events(events) == [{'source': 'rtt', 'status': 'connected', 'error': ''}]

    conn.stop()
    assert conn_events(events)[-1] == {'source': 'rtt', 'status': 'disconnected', 'error': ''}


def test_conn_disconnect_and_recovery_from_read_task():
    # Real clock on purpose: virtual_clock patches time.sleep on the time
    # module itself, so the read thread would never get to run.
    import pylink as pylink_mod

    jlink = FakeJLink()
    conn, events = make_connector(jlink)
    conn.start()
    assert [e['status'] for e in conn_events(events)] == ['connected']

    def dead_read(index, num_bytes):
        raise pylink_mod.errors.JLinkException('Cannot read from target')

    jlink.rtt_read = dead_read
    time.sleep(0.3)

    down = [e for e in conn_events(events) if e['status'] == 'disconnected']
    assert len(down) == 1, conn_events(events)
    assert 'Cannot read from target' in down[0]['error']

    # Only real data proves the link is back — see the empty-read test below.
    jlink.rtt_read = lambda index, num_bytes: list(b'hello\n') if index == 0 else []
    time.sleep(0.3)
    stop_read_thread(conn)

    assert [e['status'] for e in conn_events(events)] == [
        'connected', 'disconnected', 'connected']


def test_empty_reads_do_not_clear_a_disconnect():
    # Reported from hardware: the warning only blinked. rtt_read on an
    # unpowered target does not fail, it returns nothing, so treating a
    # quiet cycle as proof of life undid the disconnect immediately.
    import pylink as pylink_mod

    jlink = FakeJLink()
    conn, events = make_connector(jlink)
    conn.start()

    def dead_write(index, data):
        raise pylink_mod.errors.JLinkException('Unspecified error.')

    jlink.rtt_write = dead_write
    conn.handle(Event(EventType.IN, 'help'))
    assert [e['status'] for e in conn_events(events)][-1] == 'disconnected'

    # reads keep coming back empty, as they do on a dead target
    time.sleep(0.4)
    stop_read_thread(conn)

    assert [e['status'] for e in conn_events(events)] == ['connected', 'disconnected']


def test_successful_write_clears_a_disconnect():
    jlink = FakeJLink()
    conn, events = make_connector(jlink)
    conn.start()
    stop_read_thread(conn)

    conn._emit_conn(False, 'stale')
    conn.handle(Event(EventType.IN, 'help'))

    assert [e['status'] for e in conn_events(events)] == [
        'connected', 'disconnected', 'connected']


def test_target_power_loss_detected_without_any_write():
    # VTref drops when the board loses power, which is the only signal
    # available while reads are merely silent.
    jlink = FakeJLink()
    conn = PyLinkRTTConnector(jlink, power_check_interval=0.0, min_target_voltage=1000)
    events = []
    conn.on(lambda e: events.append(e))
    conn.start()

    jlink.vtarget = 0
    time.sleep(0.3)
    stop_read_thread(conn)

    down = [e.data for e in events
            if e.type == EventType.CONN and e.data['status'] == 'disconnected']
    assert len(down) == 1, [e.data for e in events if e.type == EventType.CONN]
    assert 'no power' in down[0]['error']
    assert 'VTref 0 mV' in down[0]['error']


def test_probe_that_cannot_measure_vref_never_reports_power_loss():
    # Some probes always report 0 mV; that must not look like a dead target.
    jlink = FakeJLink()
    jlink.vtarget = 0
    conn = PyLinkRTTConnector(jlink, power_check_interval=0.0, min_target_voltage=1000)
    events = []
    conn.on(lambda e: events.append(e))
    conn.start()
    time.sleep(0.3)
    stop_read_thread(conn)

    assert [e.data['status'] for e in events if e.type == EventType.CONN] == ['connected']


def test_target_power_recovery_needs_more_than_voltage():
    # Power returning means the firmware rebooted, so the old RTT control
    # block is stale; the session must not silently claim to be back.
    jlink = FakeJLink()
    conn = PyLinkRTTConnector(jlink, power_check_interval=0.0, min_target_voltage=1000)
    events = []
    conn.on(lambda e: events.append(e))
    conn.start()

    jlink.vtarget = 0
    time.sleep(0.25)
    jlink.vtarget = 3300
    time.sleep(0.25)
    stop_read_thread(conn)

    assert [e.data['status'] for e in events if e.type == EventType.CONN] == [
        'connected', 'disconnected']


def test_conn_stays_quiet_while_healthy():
    jlink = FakeJLink()
    conn, events = make_connector(jlink)
    conn.start()
    time.sleep(0.4)
    stop_read_thread(conn)

    assert [e['status'] for e in conn_events(events)] == ['connected']


def test_write_to_dead_link_does_not_raise():
    # Reported from hardware: unplug the device, send a command, and the
    # exception used to escape into prompt_toolkit and kill the event loop.
    import pylink as pylink_mod

    jlink = FakeJLink()
    conn, events = make_connector(jlink)
    conn.start()
    stop_read_thread(conn)

    def dead_write(index, data):
        raise pylink_mod.errors.JLinkException('Unspecified error.')

    jlink.rtt_write = dead_write
    conn.handle(Event(EventType.IN, 'help'))   # must not raise

    down = [e for e in conn_events(events) if e['status'] == 'disconnected']
    assert len(down) == 1
    assert 'Unspecified error.' in down[0]['error']
    # the command never went out, so it must not be echoed as sent
    assert not [e for e in events if e.type == EventType.IN]


def test_write_times_out_instead_of_spinning_forever():
    # A target that stops draining the buffer made rtt_write return 0 forever;
    # the old loop spun on the console's own key-handler thread.
    jlink = FakeJLink()
    conn = PyLinkRTTConnector(jlink, write_timeout=0.2)
    events = []
    conn.on(lambda e: events.append(e))
    conn.start()
    stop_read_thread(conn)

    jlink.rtt_write = lambda index, data: 0

    # Run it on a thread with a watchdog: without the deadline handle() never
    # returns, and a hanging test is far worse than a failing one.
    done = threading.Event()

    def send():
        conn.handle(Event(EventType.IN, 'help'))
        done.set()

    worker = threading.Thread(target=send, daemon=True)
    worker.start()
    assert done.wait(timeout=5.0), 'write never gave up — it is spinning forever'
    down = [e.data for e in events if e.type == EventType.CONN and e.data['status'] == 'disconnected']
    assert len(down) == 1
    assert 'not reading' in down[0]['error']


def test_write_with_zero_sized_down_buffer_does_not_raise():
    # Buffer present but never sized by the firmware: recoverable via
    # reconnect, and not a dropped link, so no CONN event.
    jlink = FakeJLink()
    conn, events = make_connector(jlink)
    conn.start()
    stop_read_thread(conn)
    conn.terminal_buffer_down_size = 0

    conn.handle(Event(EventType.IN, 'help'))   # must not raise

    assert not [e for e in events if e.type == EventType.IN]
    assert [e.data['status'] for e in events if e.type == EventType.CONN] == ['connected']


def test_successful_write_still_echoes():
    jlink = FakeJLink()
    conn, events = make_connector(jlink)
    conn.start()
    stop_read_thread(conn)

    written = []
    jlink.rtt_write = lambda index, data: (written.append(bytes(data)), len(data))[1]
    conn.handle(Event(EventType.IN, 'help'))

    assert b'help\n' in b''.join(written)
    assert [e.data for e in events if e.type == EventType.IN] == ['help']


def test_auto_reconnect_off_by_default_does_not_reattach():
    import pylink as pylink_mod

    jlink = FakeJLink()
    conn = PyLinkRTTConnector(jlink, reconnect_interval=0.1, power_check_interval=0.0)
    conn.on(lambda e: None)
    conn.open()

    jlink.rtt_read = lambda i, n: (_ for _ in ()).throw(
        pylink_mod.errors.JLinkException('gone'))
    time.sleep(0.5)
    attaches = jlink.calls.count('rtt_start')
    time.sleep(0.5)

    assert jlink.calls.count('rtt_start') == attaches, 'reattached with the flag off'
    conn.close()


def test_auto_reconnect_reattaches_while_down():
    import pylink as pylink_mod

    jlink = FakeJLink()
    conn = PyLinkRTTConnector(jlink, auto_reconnect=True, reconnect_interval=0.1,
                              power_check_interval=0.0)
    conn.on(lambda e: None)
    conn.open()
    before = jlink.calls.count('rtt_start')

    jlink.rtt_read = lambda i, n: (_ for _ in ()).throw(
        pylink_mod.errors.JLinkException('gone'))
    time.sleep(0.6)

    assert jlink.calls.count('rtt_start') > before + 1
    conn.close()


def test_request_reconnect_reattaches_once_with_the_flag_off():
    # F4 and the dialog button must work without auto reconnect being on.
    jlink = FakeJLink()
    conn = PyLinkRTTConnector(jlink, reconnect_interval=5.0, power_check_interval=0.0)
    conn.on(lambda e: None)
    conn.open()
    before = jlink.calls.count('rtt_start')

    conn.request_reconnect()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and jlink.calls.count('rtt_start') == before:
        time.sleep(0.05)

    assert jlink.calls.count('rtt_start') == before + 1, 'request was not honoured'
    conn.close()


def test_reconnect_reopens_the_probe_when_rtt_start_alone_fails():
    # On real hardware rtt_start() keeps failing with 'Unspecified error' after
    # the target dropped: the DLL's connection to it has to be re-established
    # first. Modelled here as an rtt_start that only works once connect() has
    # run again, which the fake previously never did.
    import pylink as pylink_mod

    jlink = FakeJLink()
    # The attach only works while the DLL's connection to the target is live.
    # open() re-establishes it; losing the target clears it. Without that
    # distinction the fake made every rtt_start succeed, which is why this went
    # unnoticed.
    live = {'value': True}

    def connect(device):
        jlink.calls.append(('connect', device))
        live['value'] = True

    def rtt_start(block_address=None):
        jlink.calls.append('rtt_start')
        if not live['value']:
            raise pylink_mod.errors.JLinkException('Unspecified error.')

    jlink.connect = connect
    jlink.rtt_start = rtt_start

    conn = PyLinkRTTConnector(jlink, auto_reconnect=True, reconnect_interval=0.1,
                              power_check_interval=0.0,
                              device='NRF9151_XXCA', serial=1234, speed=2000)
    conn.on(lambda e: None)
    conn.open()

    # The target drops: reads fail and the DLL's connection to it is stale, so
    # every plain rtt_start from here on raises.
    live['value'] = False
    jlink.rtt_read = lambda i, n: (_ for _ in ()).throw(
        pylink_mod.errors.JLinkException('gone'))

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if ('connect', 'NRF9151_XXCA') in jlink.calls:
            break
        time.sleep(0.05)
    conn.close()

    assert ('connect', 'NRF9151_XXCA') in jlink.calls, \
        'reconnect never reopened the probe, so rtt_start could only keep failing'
    # Reopening has to come before an attach, or it rescues nothing.
    reopen_at = jlink.calls.index(('connect', 'NRF9151_XXCA'))
    assert 'rtt_start' in jlink.calls[reopen_at:], \
        'the probe was reopened but no attach followed it'


def test_successful_reconnect_does_not_loop_forever():
    # The stop() inside a reconnect used to report 'disconnected', which is the
    # very condition the watchdog reattaches on, so a working target was torn
    # down and re-attached about once a second forever.
    jlink = FakeJLink()
    conn = PyLinkRTTConnector(jlink, auto_reconnect=True, reconnect_interval=0.1,
                              power_check_interval=0.0,
                              device='NRF9151_XXCA', serial=1234, speed=2000)
    conn.on(lambda e: None)
    conn.open()

    conn.request_reconnect()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and jlink.calls.count('rtt_start') < 2:
        time.sleep(0.02)

    settled = jlink.calls.count('rtt_start')
    time.sleep(0.8)
    after = jlink.calls.count('rtt_start')
    conn.close()

    assert settled >= 2, 'the requested reconnect never ran'
    assert after == settled, \
        f'kept reattaching a healthy session ({after - settled} more attaches in 0.8s)'


def test_reconnect_does_not_report_its_own_stop_as_a_disconnect():
    # The console would otherwise flash 'Device is not connected' on every
    # reconnect, including the ones that work.
    jlink = FakeJLink()
    conn = PyLinkRTTConnector(jlink, reconnect_interval=5.0, power_check_interval=0.0,
                              device='NRF9151_XXCA', serial=1234, speed=2000)
    events = []
    conn.on(lambda e: events.append(e))
    conn.open()
    del events[:]

    conn.request_reconnect()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and jlink.calls.count('rtt_start') < 2:
        time.sleep(0.02)
    time.sleep(0.2)
    # Snapshot before closing: close() reports a disconnect of its own, which is
    # correct and not what this test is about.
    statuses = [e.data.get('status') for e in events if e.type == EventType.CONN]
    conn.close()
    assert 'disconnected' not in statuses, \
        f'a working reconnect reported a disconnect: {statuses}'


def test_request_reconnect_returns_immediately():
    # It runs on the watchdog, so a key handler is never blocked for the
    # seconds an attach can take.
    jlink = FakeJLink()
    conn = PyLinkRTTConnector(jlink, reconnect_interval=5.0)
    conn.on(lambda e: None)
    conn.open()

    started = time.monotonic()
    conn.request_reconnect()
    assert time.monotonic() - started < 0.05
    conn.close()


def test_request_reconnect_tried_even_without_target_power():
    # Asked for explicitly, so it must give a real answer rather than silence.
    jlink = FakeJLink()
    conn = PyLinkRTTConnector(jlink, reconnect_interval=5.0, power_check_interval=0.0,
                              min_target_voltage=1000)
    conn.on(lambda e: None)
    conn.open()
    jlink.rtt_read = lambda i, n: []
    jlink.vtarget = 0
    time.sleep(0.2)
    before = jlink.calls.count('rtt_start')

    conn.request_reconnect()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and jlink.calls.count('rtt_start') == before:
        time.sleep(0.05)

    assert jlink.calls.count('rtt_start') == before + 1
    conn.close()


def test_auto_reconnect_does_not_deadlock_on_close():
    # stop() joins the read thread, so a reattach driven from the read thread
    # itself would deadlock; it runs on its own watchdog instead.
    jlink = FakeJLink()
    conn = PyLinkRTTConnector(jlink, auto_reconnect=True, reconnect_interval=0.05,
                              power_check_interval=0.0)
    conn.on(lambda e: None)
    conn.open()
    conn._emit_conn(False, 'forced')
    time.sleep(0.3)

    done = threading.Event()
    threading.Thread(target=lambda: (conn.close(), done.set()), daemon=True).start()
    assert done.wait(timeout=5.0), 'close() deadlocked against the reconnect watchdog'


def test_auto_reconnect_waits_while_target_has_no_power():
    jlink = FakeJLink()
    conn = PyLinkRTTConnector(jlink, auto_reconnect=True, reconnect_interval=0.1,
                              power_check_interval=0.0, min_target_voltage=1000)
    conn.on(lambda e: None)
    conn.open()

    jlink.rtt_read = lambda i, n: []
    jlink.vtarget = 0
    time.sleep(0.3)
    attaches = jlink.calls.count('rtt_start')
    time.sleep(0.5)

    assert jlink.calls.count('rtt_start') == attaches, 'retried on an unpowered target'
    conn.close()


def test_open_without_rtt_fails_by_default():
    jlink = FakeJLink()
    jlink.sizes[(0, 1)] = [0]
    conn = PyLinkRTTConnector(jlink)
    conn.on(lambda e: None)

    with pytest.raises(Exception, match='Failed to find RTT block'):
        conn.open()


def test_open_without_rtt_succeeds_with_auto_reconnect():
    # Start the console even though the target is not there yet, and attach
    # once it appears.
    jlink = FakeJLink()
    jlink.sizes[(0, 1)] = [0]
    conn = PyLinkRTTConnector(jlink, auto_reconnect=True, reconnect_interval=0.1,
                              power_check_interval=0.0)
    events = []
    conn.on(lambda e: events.append(e))

    conn.open()   # must not raise
    assert [e.data['status'] for e in events if e.type == EventType.CONN] == ['disconnected']

    # target shows up
    jlink.sizes[(0, 1)] = [1024]
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if conn._conn_up:
            break
        time.sleep(0.05)

    assert conn._conn_up is True, 'never attached once the target appeared'
    conn.close()
