import asyncio
import json
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
    """Extract the tool's dict result from FastMCP call_tool output."""
    if isinstance(result, dict):
        return result
    return json.loads(result[0].text)


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
        result = await m._mcp.call_tool('read_log', {'after_cursor': 3})
        logs = result[0] if isinstance(result, tuple) else result
        return m, logs

    m, _ = asyncio.run(run())
    assert m._log_cursor == 5


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

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

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


def test_stop_reports_the_disconnect():
    # Unlike the stop inside a reconnect, this one leaves RTT down, so the
    # console has to show it.
    m, conn = make_rtt_middleware()

    async def run():
        await m._mcp.call_tool('stop', {})
        assert ('stop', True) in conn.calls, 'stop hid a real disconnect'

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
        assert conn.calls == [], 'a live session was torn down anyway'

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
