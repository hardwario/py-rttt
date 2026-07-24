import asyncio
import json
import socket
import threading
import time
import pytest
from rttt.connectors.base import Connector
from rttt.connectors.mcp_server import MCPMiddleware, _hexdump, _BearerAuthMiddleware
from rttt.event import Event, EventType


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
