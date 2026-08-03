"""CONN events reaching the status bar and the console log file."""
import asyncio
import pytest
from rttt.connectors.base import Connector
from rttt.connectors.file_log import FileLogMiddleware
from rttt.event import Event, EventType, conn_event
from rttt.ui import State


class FakeConnector(Connector):
    def open(self):
        pass

    def close(self):
        pass

    def handle(self, event: Event):
        pass


def test_conn_event_shape():
    e = conn_event('rtt', 'disconnected', 'boom')
    assert e.type == EventType.CONN
    assert e.data == {'source': 'rtt', 'status': 'disconnected', 'error': 'boom'}


def test_state_tracks_sources_independently():
    state = State()
    assert state.conn_down() == []

    state.set_conn('rtt', 'connected')
    state.set_conn('mqtt', 'disconnected', 'not authorised')
    assert state.conn_down() == ['mqtt']

    state.set_conn('rtt', 'disconnected', 'gone')
    assert state.conn_down() == ['mqtt', 'rtt']

    state.set_conn('mqtt', 'connected')
    assert state.conn_down() == ['rtt']


def test_status_bar_shows_disconnected_sources():
    from rttt.ui import create_status_bar

    state = State()
    bar = create_status_bar(state)
    # reach the text callable the bar renders
    control = bar.content.children[0].content
    assert 'DISCONNECTED' not in ''.join(t for _, t in control.text())

    state.set_conn('rtt', 'disconnected', 'gone')
    rendered = control.text()
    assert any('RTT DISCONNECTED' in t for _, t in rendered)
    # rendered in an attention colour, not the plain title style
    assert any('DISCONNECTED' in t and style != 'class:title' for style, t in rendered)


def test_file_log_records_conn_transitions(tmp_path):
    path = tmp_path / 'console.log'
    mw = FileLogMiddleware(FakeConnector(), str(path))

    async def run():
        await mw._process(conn_event('rtt', 'connected'))
        await mw._process(conn_event('rtt', 'disconnected', 'Cannot read from target'))

    mw._loop = asyncio.new_event_loop()
    try:
        mw._loop.run_until_complete(run())
    finally:
        mw._loop.close()
    mw.fd.flush()

    body = path.read_text()
    assert 'rtt connected' in body
    assert 'rtt disconnected: Cannot read from target' in body
    # timestamped like every other logged line, so history shows when it dropped
    assert any(line.split(' @ ')[0].strip() and ' @ ' in line for line in body.splitlines())
