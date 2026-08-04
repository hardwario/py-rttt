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


def test_status_bar_carries_no_disconnect_indicator():
    # The overlay holds for the whole outage, so the bar does not repeat it.
    from rttt.ui import create_status_bar

    state = State()
    state.set_conn('rtt', 'disconnected', 'gone')
    bar = create_status_bar(state)
    control = bar.content.children[0].content

    assert 'DISCONNECTED' not in ''.join(t for _, t in control.text())


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


def test_conn_overlay_wording():
    state = State()
    assert state.conn_title() == ''
    assert state.conn_detail() == ''

    state.set_conn('rtt', 'disconnected', 'J-Link: Unspecified error.')
    assert state.conn_title() == 'Device is not connected'
    assert state.conn_detail() == 'J-Link: Unspecified error.'

    state.set_conn('rtt', 'connected')
    assert state.conn_title() == ''

    # unknown transports fall back to their id
    state.set_conn('mqtt', 'disconnected', 'not authorised')
    assert state.conn_title() == 'MQTT is not connected'


def test_conn_overlay_visible_only_while_down():
    from rttt.ui import create_layout

    state = State()
    root, _, _, _ = create_layout(state, None)
    overlay = root.floats[-1].content
    assert not overlay.filter()

    state.set_conn('rtt', 'disconnected', 'boom')
    assert overlay.filter()

    state.set_conn('rtt', 'connected')
    assert not overlay.filter()


def drive_chain(jlink, **kwargs):
    """Wire a connector through a middleware into a State, as the console does."""
    import sys
    sys.path.insert(0, 'tests')
    from rttt.connectors.pylink_rtt import PyLinkRTTConnector
    from rttt.connectors.substitution import SubstitutionMiddleware

    leaf = PyLinkRTTConnector(jlink, **kwargs)
    chain = SubstitutionMiddleware(leaf)
    state = State()

    def on_event(e):
        if e.type == EventType.CONN:
            state.set_conn(e.data['source'], e.data['status'], e.data['error'])

    chain.on(on_event)
    return leaf, chain, state


def test_overlay_stays_up_while_device_is_unpowered():
    # Reported from hardware: the dialog only blinked. Empty reads used to
    # count as proof of life and cleared the disconnect a cycle later.
    import time
    import pylink as pylink_mod
    from test_pylink_rtt import FakeJLink

    jlink = FakeJLink()
    leaf, chain, state = drive_chain(jlink, power_check_interval=0.5,
                                     min_target_voltage=1000)
    chain.open()
    assert state.conn_down() == []

    def dead_write(index, data):
        raise pylink_mod.errors.JLinkException('Unspecified error.')

    jlink.rtt_read = lambda i, n: []
    jlink.rtt_write = dead_write
    jlink.vtarget = 0

    chain.handle(Event(EventType.IN, 'help'))
    assert state.conn_down() == ['rtt']

    try:
        # many read cycles must not clear it
        samples = []
        for _ in range(10):
            time.sleep(0.1)
            samples.append(bool(state.conn_down()))
        assert all(samples), f'overlay blinked: {samples}'
        assert state.conn_title() == 'Device is not connected'
    finally:
        leaf.is_running = False
        if leaf.thread:
            leaf.thread.join()


def test_overlay_appears_without_any_command_being_sent():
    # Pull the power and type nothing: only the measured VTref can notice.
    import time
    from test_pylink_rtt import FakeJLink

    jlink = FakeJLink()
    leaf, chain, state = drive_chain(jlink, power_check_interval=0.2,
                                     min_target_voltage=1000)
    chain.open()
    assert state.conn_down() == []

    jlink.rtt_read = lambda i, n: []
    jlink.vtarget = 0

    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not state.conn_down():
            time.sleep(0.05)

        assert state.conn_down() == ['rtt'], 'power loss never noticed'
        assert 'no power' in state.conn_detail()

        # and it stays, rather than blinking
        time.sleep(0.6)
        assert state.conn_down() == ['rtt']
    finally:
        leaf.is_running = False
        if leaf.thread:
            leaf.thread.join()


def test_f4_toggles_the_checkbox_and_reaches_the_leaf_connector():
    import sys
    sys.path.insert(0, 'tests')
    from test_pylink_rtt import FakeJLink
    from rttt.connectors.pylink_rtt import PyLinkRTTConnector
    from rttt.connectors.substitution import SubstitutionMiddleware
    from rttt.console import Console

    leaf = PyLinkRTTConnector(FakeJLink())
    console = Console(SubstitutionMiddleware(leaf))

    # find the F4 handler the way prompt_toolkit would
    handler = None
    for binding in console.app.key_bindings.bindings:
        if any(getattr(k, 'name', str(k)) == 'f4' or str(k).endswith('F4') for k in binding.keys):
            handler = binding.handler
    assert handler is not None, 'F4 is not bound'

    assert console.state.auto_reconnect is False
    assert leaf.auto_reconnect is False

    handler(None)
    assert console.state.auto_reconnect is True
    assert leaf.auto_reconnect is True, 'the flag never reached the leaf connector'

    handler(None)
    assert console.state.auto_reconnect is False
    assert leaf.auto_reconnect is False


def test_status_bar_shows_the_reconnect_checkbox():
    from rttt.ui import create_status_bar

    state = State()
    bar = create_status_bar(state)
    control = bar.content.children[0].content

    assert any('<F4> Reconnect [ ]' in t for _, t in control.text())
    state.auto_reconnect = True
    assert any('<F4> Reconnect [x]' in t for _, t in control.text())
