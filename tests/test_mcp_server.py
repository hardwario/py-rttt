import asyncio
import json
import os
import socket
import threading
import time
import pytest
from rttt.connectors.base import Connector
from rttt.connectors.mcp_server import MCPMiddleware, _hexdump, _BearerAuthMiddleware
from rttt.event import Event, EventType, conn_event


class FakeConnector(Connector):
    def __init__(self):
        super().__init__()
        self.handled = []
        self._op_lock = threading.Lock()

    def open(self):
        pass

    def close(self):
        pass

    def handle(self, event: Event):
        self.handled.append(event)


def make_middleware():
    conn = FakeConnector()
    # port 0: bind check always succeeds, the HTTP server itself never starts
    return MCPMiddleware(conn, listen='127.0.0.1:0'), conn


def tool_result(result):
    """Extract the tool's dict result from MCPServer call_tool output."""
    assert not result.is_error, result
    return json.loads(result.content[0].text)


def test_hexdump_format():
    lines = _hexdump(0x1000, bytes(range(0x41, 0x41 + 20)))
    assert lines[0].startswith('0x00001000: 41 42 43')
    assert lines[0].endswith('|ABCDEFGHIJKLMNOP|')
    assert lines[1].startswith('0x00001010:')


def test_process_only_out_signals_activity():
    async def run():
        m, _ = make_middleware()
        await m._process(Event(EventType.IN, 'help'))
        assert not m._terminal_event.is_set()
        await m._process(Event(EventType.OUT, 'line'))
        assert m._terminal_event.is_set()
        assert m._terminal_cursor == 2

    asyncio.run(run())


def test_process_skips_flash_progress_events():
    async def run():
        m, _ = make_middleware()
        m._flash_events = []
        await m._process(Event(EventType.FLASH, {'status': 'start'}))
        await m._process(Event(EventType.FLASH, {'status': 'progress', 'percentage': 10}))
        await m._process(Event(EventType.FLASH, {'status': 'done'}))
        assert [e['status'] for e in m._flash_events] == ['start', 'done']

    asyncio.run(run())


def test_send_command_returns_own_output_only():
    # Late output of a previous command must not be attributed to this one.
    async def run():
        m, conn = make_middleware()
        await m._process(Event(EventType.OUT, 'stale output of previous command'))

        async def feeder():
            await asyncio.sleep(0.02)
            await m._process(Event(EventType.IN, 'help'))
            await m._process(Event(EventType.OUT, 'help line 1'))
            await m._process(Event(EventType.OUT, 'help line 2'))

        task = asyncio.create_task(feeder())
        result = tool_result(await m._mcp.call_tool(
            'send_command', {'command': 'help', 'timeout': 0.5}))
        await task

        assert result['status'] == 'ok'
        texts = [line['text'] for line in result['output']]
        assert texts == ['help line 1', 'help line 2']
        assert conn.handled[0].type == EventType.IN
        assert conn.handled[0].data == 'help'

    asyncio.run(run())


def test_send_command_echo_does_not_end_wait():
    # The echo of the sent command must not collapse the silence window —
    # a response arriving after the echo but within the timeout is returned.
    async def run():
        m, _ = make_middleware()

        async def feeder():
            await m._process(Event(EventType.IN, 'slow'))
            await asyncio.sleep(0.3)
            await m._process(Event(EventType.OUT, 'slow response'))

        task = asyncio.create_task(feeder())
        result = tool_result(await m._mcp.call_tool(
            'send_command', {'command': 'slow', 'timeout': 1.0}))
        await task

        assert [line['text'] for line in result['output']] == ['slow response']

    asyncio.run(run())


def test_wait_for_state_passive_match():
    async def run():
        m, _ = make_middleware()

        async def feeder():
            await asyncio.sleep(0.02)
            await m._process(Event(EventType.LOG, 'cloud state: connecting'))
            await asyncio.sleep(0.02)
            await m._process(Event(EventType.LOG, 'cloud state: initialized: yes'))

        task = asyncio.create_task(feeder())
        result = tool_result(await m._mcp.call_tool(
            'wait_for_state',
            {'pattern': 'initialized: yes', 'timeout': 2.0, 'source': 'log'}))
        await task

        assert result['status'] == 'ok'
        assert result['matched'] is True
        assert 'initialized: yes' in result['matched_line']

    asyncio.run(run())


def test_wait_for_state_timeout_returns_state():
    async def run():
        m, _ = make_middleware()

        async def feeder():
            await asyncio.sleep(0.02)
            await m._process(Event(EventType.LOG, 'still booting'))
            await m._process(Event(EventType.LOG, 'not there yet'))

        task = asyncio.create_task(feeder())
        result = tool_result(await m._mcp.call_tool(
            'wait_for_state',
            {'pattern': 'will never appear', 'timeout': 0.5, 'source': 'log'}))
        await task

        assert result['status'] == 'timeout'
        assert result['matched'] is False
        assert result['last_lines']
        assert 'still booting' in result['last_lines']

    asyncio.run(run())


def test_wait_for_state_polls_command():
    async def run():
        m, conn = make_middleware()
        # No matching output ever arrives, so the tool keeps polling until
        # the timeout and we can assert the command was sent at least once.
        result = tool_result(await m._mcp.call_tool(
            'wait_for_state',
            {'pattern': 'never', 'timeout': 0.6, 'command': 'cloud state',
             'poll_interval': 0.2, 'source': 'log'}))

        assert result['status'] == 'timeout'
        sent = [e for e in conn.handled
                if e.type == EventType.IN and e.data == 'cloud state']
        assert len(sent) >= 1

    asyncio.run(run())


def test_wait_for_state_zero_poll_interval_errors():
    # poll_interval=0 with a command would busy-loop and starve the event
    # loop; it must be rejected quickly instead.
    async def run():
        m, _ = make_middleware()
        result = tool_result(await asyncio.wait_for(m._mcp.call_tool(
            'wait_for_state',
            {'pattern': 'never', 'timeout': 60.0, 'command': 'cloud state',
             'poll_interval': 0.0, 'source': 'log'}), timeout=1.0))

        assert result['status'] == 'error'
        assert result['error'] == 'poll_interval must be > 0'

    asyncio.run(run())


def test_wait_for_state_polling_ignores_command_echo():
    # In polling mode the sent command is echoed into the terminal buffer as
    # an "in" line. That echo must not match the pattern as a false positive;
    # only the real device response should.
    async def run():
        m, _ = make_middleware()

        async def feeder():
            await asyncio.sleep(0.02)
            # Echo of the command we send (direction "in") — must be ignored.
            await m._process(Event(EventType.IN, 'cloud state'))
            await asyncio.sleep(0.05)
            # Real device response (direction "out") — must match.
            await m._process(Event(EventType.OUT, 'cloud state initialized'))

        task = asyncio.create_task(feeder())
        result = tool_result(await m._mcp.call_tool(
            'wait_for_state',
            {'pattern': 'cloud state', 'timeout': 2.0,
             'command': 'cloud state', 'poll_interval': 5.0,
             'source': 'terminal'}))
        await task

        assert result['status'] == 'ok'
        assert result['matched'] is True
        assert result['matched_line'] == 'cloud state initialized'

    asyncio.run(run())


def test_wait_for_state_bad_regex():
    async def run():
        m, _ = make_middleware()
        result = tool_result(await m._mcp.call_tool(
            'wait_for_state', {'pattern': '([unclosed', 'timeout': 0.1}))
        assert result['status'] == 'error'
        assert 'regex' in result['error']

    asyncio.run(run())


def test_run_target_op_reports_busy_lock():
    async def run():
        m, conn = make_middleware()
        assert conn._op_lock.acquire(blocking=False)
        try:
            result = await m._run_target_op(lambda: {'x': 1})
        finally:
            conn._op_lock.release()
        assert result['status'] == 'error'
        assert 'in progress' in result['error']

    asyncio.run(run())


def test_run_target_op_releases_lock():
    async def run():
        m, conn = make_middleware()
        result = await m._run_target_op(lambda: {'x': 1})
        assert result == {'status': 'ok', 'x': 1}
        assert conn._op_lock.acquire(blocking=False)
        conn._op_lock.release()

    asyncio.run(run())


def test_run_target_op_returns_error_from_exception():
    async def run():
        m, _ = make_middleware()

        def boom():
            raise RuntimeError('kaput')

        result = await m._run_target_op(boom)
        assert result == {'status': 'error', 'error': 'kaput'}

    asyncio.run(run())


def test_read_log_after_cursor_and_pattern():
    async def run():
        m, _ = make_middleware()
        for i in range(5):
            await m._process(Event(EventType.LOG, f'line {i}'))
        after = await m._mcp.call_tool('read_log', {'after_cursor': 3})
        matched = await m._mcp.call_tool('read_log', {'pattern': r'line [13]'})
        return m, after, matched

    m, after, matched = asyncio.run(run())
    assert m._log_cursor == 5
    assert [c.text for c in after.content] == ['line 3\nline 4']
    assert [c.text for c in matched.content] == ['line 1\nline 3']


def test_read_log_is_one_block_with_line_breaks():
    """Clients may join content blocks without a separator, so every line
    in its own block reached the model as one run-on line."""
    async def run():
        m, _ = make_middleware()
        for i in range(3):
            await m._process(Event(EventType.LOG, f'line {i}'))
        return await m._mcp.call_tool('read_log', {'lines': 2})

    result = asyncio.run(run())
    assert [c.text for c in result.content] == ['line 1\nline 2']


def test_read_terminal_is_one_json_block():
    async def run():
        m, _ = make_middleware()
        await m._process(Event(EventType.IN, 'kernel version'))
        await m._process(Event(EventType.OUT, 'Zephyr version 4.4.0'))
        return await m._mcp.call_tool('read_terminal', {})

    result = asyncio.run(run())
    assert len(result.content) == 1
    assert json.loads(result.content[0].text) == [
        {'direction': 'in', 'text': 'kernel version'},
        {'direction': 'out', 'text': 'Zephyr version 4.4.0'},
    ]


def run_auth_request(token, auth_header):
    """Drive _BearerAuthMiddleware with a fake ASGI request, return (reached, status)."""
    reached = {'value': False}

    async def inner_app(scope, receive, send):
        reached['value'] = True
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})

    middleware = _BearerAuthMiddleware(inner_app, token)
    headers = [(b'authorization', auth_header.encode())] if auth_header else []
    scope = {'type': 'http', 'method': 'GET', 'path': '/mcp', 'headers': headers,
             'query_string': b''}
    sent = []
    requested = []

    async def receive():
        # The request once, then nothing: FileResponse listens for a client
        # disconnect while it streams, and an answer every time would spin.
        if not requested:
            requested.append(True)
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    status = next(m['status'] for m in sent if m['type'] == 'http.response.start')
    return reached['value'], status


def test_bearer_auth_rejects_missing_token():
    reached, status = run_auth_request('secret', None)
    assert not reached
    assert status == 401


def test_bearer_auth_rejects_wrong_token():
    reached, status = run_auth_request('secret', 'Bearer wrong')
    assert not reached
    assert status == 401


def test_bearer_auth_accepts_valid_token():
    reached, status = run_auth_request('secret', 'Bearer secret')
    assert reached
    assert status == 200


def test_flash_missing_file():
    async def run():
        m, _ = make_middleware()
        result = tool_result(await m._mcp.call_tool(
            'flash', {'file_path': '/nonexistent/fw.hex'}))
        assert result['status'] == 'error'
        assert 'File not found' in result['error']

    asyncio.run(run())


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_close_releases_port():
    """close() must free the listening port promptly, so rttt can be
    restarted (or the port reused) right after shutdown."""
    port = _free_port()
    m = MCPMiddleware(FakeConnector(), listen=f'127.0.0.1:{port}')
    m.open()

    # Wait until the HTTP server is actually accepting connections.
    for _ in range(50):
        probe = socket.socket()
        probe.settimeout(0.2)
        try:
            probe.connect(('127.0.0.1', port))
            probe.close()
            break
        except OSError:
            probe.close()
            time.sleep(0.1)
    else:
        m.close()
        pytest.fail('MCP server never started listening')

    m.close()

    # The port must be immediately bindable again — a bare task cancel()
    # would leave the socket lingering and this would raise.
    s = socket.socket()
    s.bind(('127.0.0.1', port))
    s.close()


def test_status_reports_connection_state():
    m, _ = make_middleware()

    async def run():
        result = tool_result(await m._mcp.call_tool('status', {}))
        # nothing seen yet
        assert result['connections'] == {}

        await m._process(conn_event('rtt', 'disconnected', 'Cannot read from target'))
        result = tool_result(await m._mcp.call_tool('status', {}))
        assert result['connections'] == {
            'rtt': {'status': 'disconnected', 'error': 'Cannot read from target'}}

        await m._process(conn_event('rtt', 'connected'))
        result = tool_result(await m._mcp.call_tool('status', {}))
        assert result['connections']['rtt']['status'] == 'connected'

    asyncio.run(run())


def test_status_tracks_sources_independently():
    # An MQTT bridge over an RTT connector: one dropping must not mask the other.
    m, _ = make_middleware()

    async def run():
        await m._process(conn_event('rtt', 'connected'))
        await m._process(conn_event('mqtt', 'disconnected', 'not authorised'))
        result = tool_result(await m._mcp.call_tool('status', {}))
        assert result['connections']['rtt']['status'] == 'connected'
        assert result['connections']['mqtt']['status'] == 'disconnected'
        assert result['connections']['mqtt']['error'] == 'not authorised'

    asyncio.run(run())


class FakeRTTConnector(FakeConnector):
    """A connector with the RTT session surface the start/stop tools drive."""

    class _JLink:
        def __init__(self, owner):
            self._owner = owner

        def close(self):
            self._owner.calls.append('jlink_close')

    def __init__(self, device='NRF9151_XXCA'):
        super().__init__()
        self.calls = []
        self.is_running = True
        self.device = device
        self.serial = 1234
        self.speed = 2000
        self.jlink = self._JLink(self)

    def stop(self, report=True):
        self.calls.append(('stop', report))
        self.is_running = False

    def suspend(self, status='stopped'):
        self.calls.append(('suspend', status))
        self.is_running = False

    def resume(self):
        self.calls.append('resume')

    def start(self):
        self.calls.append('start')
        self.is_running = True

    def _reopen_jlink(self):
        self.calls.append('reopen')


def make_rtt_middleware(**kwargs):
    conn = FakeRTTConnector(**kwargs)
    return MCPMiddleware(conn, listen='127.0.0.1:0'), conn


def test_stop_keeps_the_probe_so_memory_stays_readable():
    # Stopping RTT must not give the probe up: reading registers and memory
    # over a stopped RTT session is a normal thing to want.
    m, conn = make_rtt_middleware()

    async def run():
        result = tool_result(await m._mcp.call_tool('stop', {}))
        assert result['status'] == 'ok'
        assert result['was_reading'] is True
        assert conn.is_running is False
        assert 'jlink_close' not in conn.calls, 'stop released the probe'

    asyncio.run(run())


def test_stop_reports_stopped_rather_than_a_failure():
    # The session really is down, so it has to be reported -- but as something
    # the caller asked for, not as a link that broke.
    m, conn = make_rtt_middleware()

    async def run():
        await m._mcp.call_tool('stop', {})
        assert ('suspend', 'stopped') in conn.calls, 'stop reported nothing'
        assert ('stop', True) not in conn.calls, \
            'stop reported a disconnect, which reads as a fault'

    asyncio.run(run())


def test_start_resumes_reading():
    m, conn = make_rtt_middleware()

    async def run():
        await m._mcp.call_tool('stop', {})
        conn.calls.clear()

        result = tool_result(await m._mcp.call_tool('start', {}))
        assert result['already_reading'] is False
        assert conn.is_running is True
        assert 'start' in conn.calls
        # Plain start does not reopen: that is reconnect's and jlink_open's job.
        assert 'reopen' not in conn.calls

    asyncio.run(run())


def test_start_on_a_live_session_is_a_no_op():
    m, conn = make_rtt_middleware()

    async def run():
        result = tool_result(await m._mcp.call_tool('start', {}))
        assert result['already_reading'] is True
        # resume() is fine on a live session -- it only clears the suspend flag.
        # Restarting one is not.
        assert 'start' not in conn.calls, 'a live session was torn down anyway'
        assert not any(c[0] == 'stop' for c in conn.calls if isinstance(c, tuple))

    asyncio.run(run())


def test_jlink_close_releases_the_probe():
    # Only one process can hold a J-Link, so an external nrfjprog cannot get in
    # until this actually closes it.
    m, conn = make_rtt_middleware()

    async def run():
        result = tool_result(await m._mcp.call_tool('jlink_close', {}))
        assert result['status'] == 'ok'
        assert result['was_reading'] is True
        assert conn.is_running is False
        assert 'jlink_close' in conn.calls, 'the probe was left held'

    asyncio.run(run())


def test_jlink_open_takes_the_probe_back_and_reads_again():
    m, conn = make_rtt_middleware()

    async def run():
        await m._mcp.call_tool('jlink_close', {})
        conn.calls.clear()

        result = tool_result(await m._mcp.call_tool('jlink_open', {}))
        assert result['status'] == 'ok'
        assert result['reading'] is True
        # Reopening has to precede the attach, or it rescues nothing.
        assert conn.calls.index('reopen') < conn.calls.index('start')

    asyncio.run(run())


def test_jlink_open_without_a_device_says_so():
    m, _ = make_rtt_middleware(device=None)

    async def run():
        result = tool_result(await m._mcp.call_tool('jlink_open', {}))
        assert 'No device configured' in result.get('error', '')

    asyncio.run(run())


def test_mcp_stop_suspends_so_auto_reconnect_leaves_it_alone():
    # A stop that auto reconnect undoes three seconds later is not a stop.
    m, conn = make_rtt_middleware()

    async def run():
        await m._mcp.call_tool('stop', {})
        assert ('suspend', 'stopped') in conn.calls, \
            'stop used a plain stop(), which the watchdog reattaches from'

    asyncio.run(run())


def test_mcp_jlink_close_suspends_as_released():
    m, conn = make_rtt_middleware()

    async def run():
        await m._mcp.call_tool('jlink_close', {})
        assert ('suspend', 'released') in conn.calls
        assert 'jlink_close' in conn.calls

    asyncio.run(run())


def test_mcp_start_and_jlink_open_lift_the_suspend():
    m, conn = make_rtt_middleware()

    async def run():
        await m._mcp.call_tool('stop', {})
        conn.calls.clear()
        await m._mcp.call_tool('start', {})
        assert 'resume' in conn.calls, 'start left the session suspended'

        await m._mcp.call_tool('jlink_close', {})
        conn.calls.clear()
        await m._mcp.call_tool('jlink_open', {})
        assert 'resume' in conn.calls, 'jlink_open left the session suspended'

    asyncio.run(run())


def test_wait_for_connection_returns_once_connected():
    m, _ = make_middleware()

    async def run():
        await m._process(conn_event('rtt', 'disconnected', 'Target has no power'))

        async def reconnect_later():
            await asyncio.sleep(0.05)
            await m._process(conn_event('rtt', 'connected'))

        task = asyncio.create_task(reconnect_later())
        result = tool_result(await m._mcp.call_tool(
            'wait_for_connection', {'timeout': 3.0}))
        await task

        assert result['status'] == 'ok'
        assert result['connected'] is True

    asyncio.run(run())


def test_wait_for_connection_returns_at_once_when_already_connected():
    m, _ = make_middleware()

    async def run():
        await m._process(conn_event('rtt', 'connected'))
        result = tool_result(await m._mcp.call_tool(
            'wait_for_connection', {'timeout': 3.0}))
        assert result['connected'] is True
        assert result['elapsed'] < 0.5

    asyncio.run(run())


def test_wait_for_connection_timeout_says_what_is_wrong():
    # The answer has to name the state it is stuck in, or the caller learns
    # only that waiting did not work.
    m, _ = make_middleware()

    async def run():
        await m._process(conn_event('rtt', 'released', ''))
        result = tool_result(await m._mcp.call_tool(
            'wait_for_connection', {'timeout': 0.2}))

        assert result['status'] == 'timeout'
        assert result['connected'] is False
        assert result['state'] == 'released'

    asyncio.run(run())


def test_wait_for_connection_reports_the_disconnect_reason():
    m, _ = make_middleware()

    async def run():
        await m._process(conn_event('rtt', 'disconnected',
                                    'Target has no power (VTref 0 mV)'))
        result = tool_result(await m._mcp.call_tool(
            'wait_for_connection', {'timeout': 0.2}))

        assert result['state'] == 'disconnected'
        assert 'VTref' in result['error']

    asyncio.run(run())


class FakeMemConnector(FakeConnector):
    """A connector whose J-Link reads from a fake memory image."""

    class _JLink:
        def __init__(self, base, image):
            self.base = base
            self.image = image

        def memory_read(self, addr, num_units, nbits=8):
            off = addr - self.base
            return list(self.image[off:off + num_units])

    def __init__(self, base=0x10000, image=bytes(range(256)) * 16):
        super().__init__()
        self.jlink = self._JLink(base, image)


def make_mem_middleware(tmp_path):
    conn = FakeMemConnector()
    m = MCPMiddleware(conn, listen='127.0.0.1:0')
    m._dump_dir = str(tmp_path / 'dumps')
    return m, conn


def test_read_memory_dump_by_name_is_downloadable(tmp_path):
    m, conn = make_mem_middleware(tmp_path)

    async def run():
        return await m._mcp.call_tool(
            'read_memory', {'address': '0x10010', 'length': 300, 'to_file': 'slot0.bin'})

    result = asyncio.run(run())
    assert not result.is_error
    info = json.loads(result.content[0].text)
    path = tmp_path / 'dumps' / 'slot0.bin'
    assert info['status'] == 'ok'
    assert info['file'] == str(path)
    assert path.read_bytes() == conn.jlink.image[0x10:0x10 + 300]
    # No HTTP request behind a direct call_tool, so the URL stays relative.
    assert info['url'] == '/download?filename=slot0.bin'

    link = result.content[1]
    assert link.type == 'resource_link'
    assert link.uri == 'rttt://dumps/slot0.bin'
    assert link.size == 300


def test_read_memory_dump_resource_returns_the_bytes(tmp_path):
    m, conn = make_mem_middleware(tmp_path)

    async def run():
        await m._mcp.call_tool(
            'read_memory', {'address': '0x10000', 'length': 32, 'to_file': 'hdr.bin'})
        return await m._mcp.read_resource('rttt://dumps/hdr.bin')

    contents = asyncio.run(run())
    assert contents[0].content == conn.jlink.image[:32]


def test_read_memory_dump_to_absolute_path_has_no_link(tmp_path):
    m, _ = make_mem_middleware(tmp_path)
    target = tmp_path / 'elsewhere.bin'

    async def run():
        return await m._mcp.call_tool(
            'read_memory', {'address': '0x10000', 'length': 16, 'to_file': str(target)})

    result = asyncio.run(run())
    info = json.loads(result.content[0].text)
    assert info['file'] == str(target)
    assert 'url' not in info
    assert len(result.content) == 1


def test_read_memory_dump_rejects_a_relative_path_with_directories(tmp_path):
    m, _ = make_mem_middleware(tmp_path)

    async def run():
        return tool_result(await m._mcp.call_tool(
            'read_memory', {'address': '0x10000', 'length': 16, 'to_file': '../evil.bin'}))

    result = asyncio.run(run())
    assert result['status'] == 'error'
    assert not (tmp_path / 'evil.bin').exists()


def run_http_get(app, path, query=b'', host=b'127.0.0.1:8090'):
    """Drive an ASGI app with one GET request, return (status, body)."""
    sent = []
    requested = []

    async def receive():
        # The request once, then nothing: FileResponse listens for a client
        # disconnect while it streams, and an answer every time would spin.
        if not requested:
            requested.append(True)
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    scope = {'type': 'http', 'method': 'GET', 'path': path, 'raw_path': path.encode(),
             'query_string': query, 'headers': [(b'host', host)],
             'scheme': 'http', 'server': ('127.0.0.1', 8090), 'client': ('127.0.0.1', 1),
             'root_path': '', 'http_version': '1.1', 'asgi': {'version': '3.0'}}
    asyncio.run(app(scope, receive, send))
    status = next(m['status'] for m in sent if m['type'] == 'http.response.start')
    body = b''.join(m.get('body', b'') for m in sent if m['type'] == 'http.response.body')
    return status, body


def test_download_serves_a_dump_and_nothing_else(tmp_path):
    m, conn = make_mem_middleware(tmp_path)
    asyncio.run(m._mcp.call_tool(
        'read_memory', {'address': '0x10000', 'length': 64, 'to_file': 'a.bin'}))
    (tmp_path / 'secret.txt').write_text('not yours')
    app = m._build_app()

    assert run_http_get(app, '/download', b'filename=a.bin') == (200, conn.jlink.image[:64])
    assert run_http_get(app, '/download', b'filename=../secret.txt')[0] == 404
    assert run_http_get(app, '/download', b'filename=missing.bin')[0] == 404
    assert run_http_get(app, '/download')[0] == 400


def test_tool_annotations_tell_reads_from_writes():
    m, _ = make_middleware()
    tools = {t.name: t.annotations for t in asyncio.run(m._mcp.list_tools())}

    for name in ('read_log', 'read_terminal', 'status', 'target_status',
                 'memory_zones', 'read_registers', 'wait_for_connection'):
        assert tools[name].read_only_hint is True, name
    for name in ('flash', 'write_flash', 'write_memory', 'reset'):
        assert tools[name].read_only_hint is False, name
        assert tools[name].destructive_hint is True, name
    for name in ('stop', 'start', 'jlink_close', 'jlink_open', 'halt', 'go', 'reconnect'):
        assert tools[name].destructive_hint is False, name
    # An absolute to_file can overwrite a file on the server.
    assert tools['read_memory'].read_only_hint is False
    # Every tool talks to the one probe, nothing beyond it.
    assert all(a is not None and a.open_world_hint is False for a in tools.values())


def test_flash_reports_progress_to_the_client(tmp_path):
    """A flash takes ~20 s; progress notifications show the client it moves."""
    from mcp import Client

    firmware = tmp_path / 'fw.hex'
    firmware.write_text(':00000001FF\n')

    class FlashingConnector(FakeConnector):
        def handle(self, event):
            # Called from an executor thread, like the real connector, which
            # emits its events back into the middleware's loop.
            if event.type != EventType.FLASH:
                return
            for data in ({'status': 'start'},
                         {'status': 'progress', 'action': 'Program', 'message': 'Programming', 'percentage': 10},
                         {'status': 'progress', 'action': 'Program', 'message': 'Programming', 'percentage': 10},
                         {'status': 'progress', 'action': 'Verify', 'message': 'Verifying', 'percentage': 100},
                         {'status': 'done'}):
                asyncio.run_coroutine_threadsafe(
                    m._process(Event(EventType.FLASH, data)), loop).result()

    m = MCPMiddleware(FlashingConnector(), listen='127.0.0.1:0')
    reports = []

    async def on_progress(progress, total, message):
        reports.append((progress, total, message))

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        async with Client(m._mcp) as client:
            return await client.call_tool(
                'flash', {'file_path': str(firmware)}, progress_callback=on_progress)

    loop = None
    result = asyncio.run(run())
    assert json.loads(result.content[0].text)['status'] == 'done'
    # The repeated 10 % is not reported twice.
    assert reports == [(10, 100, 'Programming'), (100, 100, 'Verifying')]


def test_flash_replies_only_after_the_connector_returns(tmp_path):
    """The J-Link connector restarts RTT after it reports "done"; the reply
    must wait for that, or the next call races the restart."""
    firmware = tmp_path / 'fw.hex'
    firmware.write_text(':00000001FF\n')
    order = []

    class SlowFinishConnector(FakeConnector):
        def handle(self, event):
            if event.type != EventType.FLASH:
                return
            asyncio.run_coroutine_threadsafe(
                m._process(Event(EventType.FLASH, {'status': 'done'})), loop).result()
            time.sleep(0.2)
            order.append('rtt restarted')

    m = MCPMiddleware(SlowFinishConnector(), listen='127.0.0.1:0')

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        result = tool_result(await m._mcp.call_tool('flash', {'file_path': str(firmware)}))
        order.append('replied')
        return result

    loop = None
    assert asyncio.run(run())['status'] == 'done'
    assert order == ['rtt restarted', 'replied']


def test_download_refuses_a_symlink_in_the_dump_dir(tmp_path):
    m, _ = make_mem_middleware(tmp_path)
    os.makedirs(m._dump_dir)
    (tmp_path / 'secret.txt').write_text('not yours')
    os.symlink(tmp_path / 'secret.txt', os.path.join(m._dump_dir, 'link.bin'))

    assert run_http_get(m._build_app(), '/download', b'filename=link.bin')[0] == 404


def test_custom_routes_get_dns_rebinding_protection(tmp_path):
    """The SDK checks Host on /mcp only; a page rebinding its name to
    127.0.0.1 must not reach /download or /upload either."""
    m, _ = make_mem_middleware(tmp_path)
    asyncio.run(m._mcp.call_tool(
        'read_memory', {'address': '0x10000', 'length': 16, 'to_file': 'a.bin'}))
    app = m._build_app()

    assert run_http_get(app, '/download', b'filename=a.bin', host=b'evil.example')[0] == 421
    assert run_http_get(app, '/download', b'filename=a.bin', host=b'localhost:8090')[0] == 200
    assert run_http_get(app, '/download', b'filename=a.bin', host=b'[::1]:8090')[0] == 200


def test_non_loopback_bind_does_not_check_the_host(tmp_path):
    # Bound to 0.0.0.0 the server is reached under whatever name the network
    # gives it, so there is no allow-list to check against (same as the SDK).
    m = MCPMiddleware(FakeMemConnector(), listen='0.0.0.0:0')
    m._dump_dir = str(tmp_path / 'dumps')
    asyncio.run(m._mcp.call_tool(
        'read_memory', {'address': '0x10000', 'length': 16, 'to_file': 'a.bin'}))

    assert run_http_get(m._build_app(), '/download', b'filename=a.bin', host=b'rttt.lan:8090')[0] == 200


def test_private_dir_refuses_a_planted_symlink(tmp_path):
    from rttt.connectors.mcp_server import _private_dir
    target = tmp_path / 'attacker'
    target.mkdir()
    planted = tmp_path / 'rttt-dumps-8090'
    os.symlink(target, planted)

    with pytest.raises(OSError):
        _private_dir(str(planted))


def test_private_dir_tightens_a_directory_left_open(tmp_path):
    # Directories made by earlier rttt versions got the default umask.
    from rttt.connectors.mcp_server import _private_dir
    d = tmp_path / 'rttt-uploads-8090'
    d.mkdir(mode=0o755)
    d.chmod(0o755)

    _private_dir(str(d))
    assert (d.stat().st_mode & 0o777) == 0o700
