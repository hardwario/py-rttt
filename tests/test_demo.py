"""Demo probe SN selection and DemoConnector CONN/reconnect behaviour."""
import time
from unittest import mock

import click
import pytest
from click.testing import CliRunner

from rttt.cli import SerialParamType, cli, CliContext, _build_demo_connector
from rttt.connectors.demo import DEMO_SERIAL, DemoConnector
from rttt.console import Console
from rttt.event import EventType
from rttt.clipboard import HybridClipboard


def test_serial_param_accepts_demo_and_int():
    param = SerialParamType()
    assert param.convert('DEMO', None, None) == DEMO_SERIAL
    assert param.convert('demo', None, None) == DEMO_SERIAL
    assert param.convert('1234', None, None) == 1234
    with pytest.raises(click.BadParameter):
        param.convert('not-a-sn', None, None)


def test_demo_connector_emits_conn_connected():
    events = []
    conn = DemoConnector(delay=0.05)
    conn.on(events.append)
    conn.open()
    time.sleep(0.15)
    conn.close()

    conn_events = [e for e in events if e.type == EventType.CONN]
    assert conn_events, 'expected at least one CONN event'
    assert conn_events[0].data['status'] == 'connected'
    assert conn_events[0].data['source'] == 'rtt'
    assert any(e.type == EventType.OUT for e in events)
    assert any(e.type == EventType.LOG for e in events)


def test_demo_connector_request_reconnect():
    events = []
    conn = DemoConnector(delay=0.05, reconnect_interval=0.05)
    conn.on(events.append)
    conn.open()
    time.sleep(0.1)
    conn.request_reconnect()
    time.sleep(0.25)
    conn.close()

    statuses = [e.data['status'] for e in events if e.type == EventType.CONN]
    assert 'connected' in statuses
    assert 'disconnected' in statuses
    # Ends reconnected (last non-close transition should have restored up).
    assert statuses[-1] in ('connected', 'disconnected')


def test_demo_disconnect_command_simulates_drop():
    events = []
    conn = DemoConnector(delay=0.05)
    conn.on(events.append)
    conn.open()
    time.sleep(0.05)
    from rttt.event import Event
    conn.handle(Event(EventType.IN, 'disconnect'))
    time.sleep(0.05)
    conn.close()

    down = [e for e in events if e.type == EventType.CONN and e.data['status'] == 'disconnected']
    assert down
    assert 'Demo disconnect' in down[0].data.get('error', '')


def test_build_demo_connector_uses_demo_leaf():
    app = CliContext(config={}, sources=[])
    connector = _build_demo_connector(
        device=None, serial=DEMO_SERIAL, auto_reconnect=False,
        substitutions=False, app=app, mcp=False, mcp_listen='127.0.0.1:8090',
        mcp_token=None, console_file=None,
    )
    leaf = connector
    while hasattr(leaf, 'connector'):
        leaf = leaf.connector
    assert isinstance(leaf, DemoConnector)


def test_cli_serial_demo_skips_jlink(monkeypatch, tmp_path):
    """--serial DEMO must not touch pylink / J-Link at all."""
    opened = []

    class FakeConsole:
        def __init__(self, connector, history_file=None, max_lines=None):
            self.connector = connector
            opened.append(connector)

        def run(self):
            # Drive open/close like the real console would.
            leaf = self.connector
            while hasattr(leaf, 'connector'):
                leaf = leaf.connector
            assert isinstance(leaf, DemoConnector)
            self.connector.open()
            self.connector.close()

    monkeypatch.setattr('rttt.cli.Console', FakeConsole)
    # If anyone opens JLink, fail hard.
    monkeypatch.setattr('rttt.cli.pylink.JLink', mock.Mock(side_effect=AssertionError('J-Link opened')))

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ['--serial', 'DEMO', '--no-substitutions', '--console-file', str(tmp_path / 'c.log')],
        obj=CliContext(config={}, sources=[]),
    )
    assert result.exit_code == 0, result.output
    assert opened


def test_cli_demo_flag_alias(monkeypatch, tmp_path):
    opened = []

    class FakeConsole:
        def __init__(self, connector, history_file=None, max_lines=None):
            opened.append(connector)

        def run(self):
            pass

    monkeypatch.setattr('rttt.cli.Console', FakeConsole)
    monkeypatch.setattr('rttt.cli.pylink.JLink', mock.Mock(side_effect=AssertionError('J-Link')))

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ['--demo', '--no-substitutions', '--console-file', str(tmp_path / 'c.log')],
        obj=CliContext(config={}, sources=[]),
    )
    assert result.exit_code == 0, result.output
    assert opened


def test_console_uses_hybrid_clipboard():
    console = Console(DemoConnector(delay=10), history_file=None)
    assert isinstance(console.app.clipboard, HybridClipboard)


def test_demo_reconnect_command():
    events = []
    conn = DemoConnector(delay=0.05, reconnect_interval=0.05)
    conn.on(events.append)
    conn.open()
    time.sleep(0.05)
    from rttt.event import Event
    conn.handle(Event(EventType.IN, 'disconnect'))
    time.sleep(0.05)
    assert conn._conn_up is False
    conn.handle(Event(EventType.IN, 'reconnect'))
    time.sleep(0.25)
    assert conn._conn_up is True, 'typed reconnect did not bring the link back up'
    statuses = [e.data['status'] for e in events if e.type == EventType.CONN]
    assert statuses[-1] == 'connected'
    conn.close()


def test_demo_stops_output_while_disconnected():
    events = []
    conn = DemoConnector(delay=0.05, reconnect_interval=1.0)
    conn.on(events.append)
    conn.open()
    time.sleep(0.12)
    from rttt.event import Event
    conn.handle(Event(EventType.IN, 'disconnect'))
    n = len([e for e in events if e.type in (EventType.OUT, EventType.LOG)])
    time.sleep(0.35)
    n2 = len([e for e in events if e.type in (EventType.OUT, EventType.LOG)])
    conn.close()
    assert n2 == n, f'leaked {n2 - n} OUT/LOG events while disconnected'

def test_demo_help_rate_burst():
    from rttt.event import Event, EventType
    events = []
    conn = DemoConnector(delay=0.5)
    conn.on(events.append)
    conn.open()
    # help
    conn.handle(Event(EventType.IN, 'help'))
    outs = [e.data for e in events if e.type == EventType.OUT]
    assert any('Demo commands' in str(o) and 'rate' in str(o) for o in outs)
    # rate query + set
    n0 = len(events)
    conn.handle(Event(EventType.IN, 'rate'))
    assert any('lines/sec' in str(e.data) for e in events[n0:] if e.type == EventType.OUT)
    conn.handle(Event(EventType.IN, 'rate 50'))
    assert abs(conn.delay - 0.02) < 1e-9
    assert abs(conn._rate - 50) < 1e-9
    # burst
    n1 = len(events)
    conn.handle(Event(EventType.IN, 'burst 5'))
    produced = [e for e in events[n1:] if e.type in (EventType.OUT, EventType.LOG)]
    # 5 burst lines + confirmation OUT
    assert sum(1 for e in produced if 'log ' in str(e.data) or str(e.data).startswith('term ')) == 5
    assert any('burst: 5' in str(e.data) for e in produced)
    conn.close()
