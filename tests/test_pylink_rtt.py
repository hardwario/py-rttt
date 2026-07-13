import os
import pytest
import rttt.connectors.pylink_rtt as pylink_rtt_module
from rttt.connectors.pylink_rtt import PyLinkRTTConnector
from rttt.event import EventType


class FakeBufDesc:
    def __init__(self, index, size, name=''):
        self.BufferIndex = index
        self.SizeOfBuffer = size
        self.name = name


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
        return FakeBufDesc(index, self._next_size((index, up)))

    def rtt_read(self, index, num_bytes):
        return []

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
