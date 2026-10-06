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


def _console_with_leaf(**kwargs):
    import sys
    sys.path.insert(0, 'tests')
    from test_pylink_rtt import FakeJLink
    from rttt.connectors.pylink_rtt import PyLinkRTTConnector
    from rttt.connectors.substitution import SubstitutionMiddleware
    from rttt.console import Console

    leaf = PyLinkRTTConnector(FakeJLink(), **kwargs)
    return Console(SubstitutionMiddleware(leaf)), leaf


def _binding(console, key):
    for b in console.app.key_bindings.bindings:
        if any(getattr(k, 'name', str(k)).lower().endswith(key) for k in b.keys):
            return b.handler
    return None


def test_f4_asks_for_an_immediate_reconnect():
    console, leaf = _console_with_leaf()
    handler = _binding(console, 'f4')
    assert handler is not None, 'F4 is not bound'

    assert not leaf._reconnect_now.is_set()
    handler(None)
    assert leaf._reconnect_now.is_set(), 'F4 did not reach the connector'


def test_dialog_reconnect_button_asks_the_connector():
    console, leaf = _console_with_leaf()
    assert not leaf._reconnect_now.is_set()
    console.state.reconnect()
    assert leaf._reconnect_now.is_set()


def test_dialog_checkbox_toggles_auto_reconnect_on_the_connector():
    console, leaf = _console_with_leaf()
    button = console.state.auto_reconnect_button

    assert console.state.auto_reconnect is False
    assert leaf.auto_reconnect is False
    assert '[ ] Auto reconnect' in button.text

    button.handler()
    assert console.state.auto_reconnect is True
    assert leaf.auto_reconnect is True, 'the flag never reached the connector'
    assert '[x] Auto reconnect' in button.text

    button.handler()
    assert leaf.auto_reconnect is False
    assert '[ ] Auto reconnect' in button.text


def test_checkbox_starts_ticked_when_the_cli_asked_for_it():
    console, leaf = _console_with_leaf(auto_reconnect=True)
    assert console.state.auto_reconnect is True
    assert '[x] Auto reconnect' in console.state.auto_reconnect_button.text


def test_both_dialog_buttons_are_reachable_with_tab():
    # Buttons in a float are useless if the focus cycle skips them; that is
    # what made the dialog look like it had no working controls.
    import asyncio as _asyncio
    from prompt_toolkit.application import Application
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
    from prompt_toolkit.layout.layout import Layout
    from prompt_toolkit.output import DummyOutput
    from rttt.ui import create_layout

    state = State()
    root, input_field, _terminal, _log = create_layout(state, None)
    state.set_conn('rtt', 'disconnected', 'Target has no power (VTref 0 mV)')

    bindings = KeyBindings()
    bindings.add('tab')(focus_previous)
    bindings.add('s-tab')(focus_next)

    visited = []

    async def main():
        with create_pipe_input() as inp:
            app = Application(layout=Layout(root, focused_element=input_field),
                              key_bindings=bindings, full_screen=True,
                              input=inp, output=DummyOutput())
            state.set_app(app)

            async def probe():
                await _asyncio.sleep(0.1)
                for _ in range(6):
                    inp.send_text('\t')
                    await _asyncio.sleep(0.05)
                    visited.append(app.layout.current_window)
                app.exit()

            app.create_background_task(probe())
            await app.run_async()

    _asyncio.run(main())

    assert state.reconnect_button.window in visited, 'Reconnect is not reachable with Tab'
    assert state.auto_reconnect_button.window in visited, \
        'Auto reconnect is not reachable with Tab'


def test_dialog_says_how_to_press_the_buttons():
    # Nothing about a focused-then-Enter button is discoverable on its own.
    from prompt_toolkit.layout import walk
    from prompt_toolkit.layout.containers import Window
    from rttt.ui import create_layout

    state = State()
    root, _input, _terminal, _log = create_layout(state, None)
    state.set_conn('rtt', 'disconnected', 'gone')

    texts = []
    for container in root.floats:
        for window in walk(container.content):
            if isinstance(window, Window) and hasattr(window.content, 'text'):
                value = window.content.text
                if callable(value):
                    try:
                        value = value()
                    except Exception:
                        continue
                if isinstance(value, list):
                    # Fragments carry a mouse handler as a third item.
                    value = ''.join(fragment[1] for fragment in value)
                if isinstance(value, str):
                    texts.append(value)

    hint = ' '.join(texts)
    assert 'F4' in hint, 'the dialog never mentions the F4 shortcut'
    assert 'Tab' in hint and 'Enter' in hint, \
        'the dialog does not say how to reach and press its buttons'


def test_status_bar_hints_f4():
    from rttt.ui import create_status_bar

    state = State()
    control = create_status_bar(state).content.children[0].content
    assert any('<F4> Reconnect' in t for _, t in control.text())


def test_a_stopped_session_is_not_reported_as_a_fault():
    # `stop` from MCP is deliberate, so the red 'is not connected' warning
    # would be telling the user their device broke when it did not.
    state = State()
    state.set_conn('rtt', 'stopped')

    assert 'not connected' not in state.conn_title().lower(), \
        f'a deliberate stop reads as a failure: {state.conn_title()!r}'
    assert state.conn_title(), 'a stopped session says nothing at all'


def test_a_stopped_session_says_how_to_resume():
    state = State()
    state.set_conn('rtt', 'stopped')
    text = f'{state.conn_title()} {state.conn_detail()}'.lower()
    assert 'start' in text, f'nothing tells the user how to resume: {text!r}'


def test_a_released_probe_says_so():
    state = State()
    state.set_conn('rtt', 'released')
    text = f'{state.conn_title()} {state.conn_detail()}'.lower()
    assert 'probe' in text or 'released' in text, \
        f'a released probe is not explained: {text!r}'


def test_a_real_disconnect_still_reads_as_a_fault():
    state = State()
    state.set_conn('rtt', 'disconnected', 'Target has no power (VTref 0 mV)')

    assert 'not connected' in state.conn_title().lower()
    assert 'VTref' in state.conn_detail()


def test_the_overlay_shows_for_stopped_and_disconnected_alike():
    # Both are states the user needs to see; only the wording differs.
    for status in ('disconnected', 'stopped', 'released'):
        state = State()
        state.set_conn('rtt', status)
        assert state.conn_down() == ['rtt'], f'{status} hid the overlay'

    state = State()
    state.set_conn('rtt', 'connected')
    assert state.conn_down() == []


def test_only_one_overlay_shows_at_a_time():
    # Both floats are centred, so showing both draws them on top of each other
    # and neither is readable. Flashing takes the link down by design, so this
    # combination is reachable whenever a flash fails.
    state = State()
    state.flash_visible = True
    state.set_conn('rtt', 'disconnected', 'Target has no power')

    assert not state.show_conn_overlay(), \
        'the Connection overlay drew over the Flash overlay'
    assert state.flash_visible, 'the Flash overlay must be the one that stays'


def test_the_conn_overlay_returns_once_flashing_ends():
    state = State()
    state.set_conn('rtt', 'disconnected', 'Target has no power')
    state.flash_visible = True
    assert not state.show_conn_overlay()

    state.flash_visible = False
    assert state.show_conn_overlay(), \
        'the disconnect went unreported once the flash overlay closed'


def test_mouse_starts_on():
    # Clicking a pane to focus it and dragging to select is the expected way to
    # copy out of the console, so it works without finding a key first.
    state = State()
    assert state.mouse_enabled is True


def test_f6_toggles_the_mouse():
    console, _ = _console_with_leaf()
    handler = _binding(console, 'f6')
    assert handler is not None, 'F6 is not bound'

    assert console.state.mouse_enabled is True
    handler(None)
    assert console.state.mouse_enabled is False, 'F6 did not turn the mouse off'
    handler(None)
    assert console.state.mouse_enabled is True, 'F6 did not turn it back on'


def test_mouse_support_follows_the_toggle():
    console, _ = _console_with_leaf()
    support = console.app.mouse_support

    assert support(), 'mouse reporting off by default'
    console.state.mouse_enabled = False
    assert not support(), 'F6 did not reach the application'


def test_mouse_support_still_forced_on_for_the_overlay():
    # The Connection dialog's buttons have to be clickable whatever the toggle
    # says, or a user who never found F6 cannot press them.
    console, _ = _console_with_leaf()
    console.state.mouse_enabled = False        # user turned it off
    console.state.set_conn('rtt', 'disconnected', 'gone')
    assert console.app.mouse_support(), 'overlay buttons were left unclickable'


def test_status_bar_shows_the_mouse_state():
    from rttt.ui import create_status_bar

    def mouse_style(state):
        control = create_status_bar(state).content.children[0].content
        for style, text in control.text():
            if 'F6' in text:
                return style
        return None

    state = State()
    on = mouse_style(state)
    assert on is not None, 'nothing tells the user the toggle exists'

    state.mouse_enabled = False
    # Highlighted while OFF, since on is the normal state -- same as F5 Pause.
    assert mouse_style(state) != on, \
        'the bar looks the same whether the mouse is on or off'


def test_right_button_release_is_recognised():
    from rttt.console import _is_right_button_release as is_right

    # SGR release of the right button, with and without modifiers.
    assert is_right('\x1b[<2;5;3m')
    assert is_right('\x1b[<6;5;3m'), 'shift held still means right button'
    assert is_right('\x1b[<18;5;3m'), 'ctrl held still means right button'
    # Typical encoding acts on the press, having no per-button release.
    assert is_right('\x1b[M' + chr(32 + 2) + chr(40) + chr(40))


def test_other_mouse_events_are_left_alone():
    from rttt.console import _is_right_button_release as is_right

    assert not is_right('\x1b[<2;5;3M'), 'acted on the press as well as release'
    assert not is_right('\x1b[<0;5;3m'), 'left button treated as right'
    assert not is_right('\x1b[<1;5;3m'), 'middle button treated as right'
    assert not is_right('\x1b[<64;5;3M'), 'scroll treated as a right click'
    assert not is_right('\x1b[<35;5;3m'), 'plain motion treated as a click'
    assert not is_right('')
    assert not is_right('\x1b[A'), 'an arrow key parsed as a mouse event'


def test_right_click_copies_a_selection():
    console, _ = _console_with_leaf()
    console.terminal_buffer.set_document(__import__(
        'prompt_toolkit.document', fromlist=['Document']).Document('hello world'), True)
    console.app.layout.focus(console.terminal_buffer)

    buffer = console.terminal_buffer
    buffer.cursor_position = 0
    buffer.start_selection()
    buffer.cursor_position = 5

    copied = {}
    console.app.clipboard.set_data = lambda data: copied.setdefault('text', data.text)

    assert console._copy_selection() is True
    assert copied.get('text') == 'hello'


def test_right_click_with_no_selection_pastes_into_the_command_line():
    from prompt_toolkit.clipboard import ClipboardData

    console, _ = _console_with_leaf()
    console.app.clipboard.set_data(ClipboardData('rtc set'))

    assert console._copy_selection() is False, 'copied without a selection'
    assert console._paste_into_input() is True
    assert 'rtc set' in console.input_field.buffer.text


def test_selection_keys_mark_text_in_a_read_only_pane():
    # prompt_toolkit binds none of these itself, so without them there is no way
    # to select in a read-only pane and Ctrl-C has nothing to copy.
    from prompt_toolkit.document import Document

    console, _ = _console_with_leaf()
    console.terminal_buffer.set_document(Document('abcdef'), True)
    console.app.layout.focus(console.terminal_buffer)
    console.terminal_buffer.cursor_position = 0

    handler = _binding(console, 'shiftright')
    assert handler is not None, 'Shift-Right is not bound'
    handler(None)
    handler(None)

    buffer = console.terminal_buffer
    assert buffer.selection_state is not None, 'nothing was selected'
    assert buffer.copy_selection().text == 'ab'


def test_ctrl_a_selects_the_whole_pane():
    from prompt_toolkit.document import Document

    console, _ = _console_with_leaf()
    console.logger_buffer.set_document(Document('line one\nline two'), True)
    console.app.layout.focus(console.logger_buffer)

    handler = _binding(console, 'controla')
    assert handler is not None, 'Ctrl-A is not bound'
    handler(None)

    assert console.logger_buffer.copy_selection().text == 'line one\nline two'


def test_selection_works_from_the_end_of_the_pane():
    # Following output keeps the cursor at the very end, so Shift-Down and
    # Shift-Right have nowhere to go and used to mark an empty selection --
    # Ctrl-C then copied nothing, which read as copying being broken.
    from prompt_toolkit.document import Document

    console, _ = _console_with_leaf()
    text = 'line one\nline two\nline three\n'
    console.logger_buffer.set_document(Document(text), True)
    console.app.layout.focus(console.logger_buffer)
    console.logger_buffer.cursor_position = len(text)

    handler = _binding(console, 'shiftup')
    assert handler is not None
    handler(None)

    selected = console.logger_buffer.copy_selection().text
    assert selected, 'selecting up from the end of the pane copied nothing'


def test_a_move_that_goes_nowhere_marks_nothing():
    from prompt_toolkit.document import Document

    console, _ = _console_with_leaf()
    console.logger_buffer.set_document(Document('one line'), True)
    console.app.layout.focus(console.logger_buffer)
    console.logger_buffer.cursor_position = len('one line')

    # Down from the last line cannot move, so there is nothing to select.
    _binding(console, 'shiftdown')(None)
    assert console.logger_buffer.selection_state is None, \
        'marked an empty selection, so Ctrl-C had nothing to copy'


def test_a_right_click_packet_reaches_the_paste():
    # End to end through the key processor, the way a terminal delivers it.
    import asyncio as _asyncio
    from prompt_toolkit.application import Application
    from prompt_toolkit.clipboard import ClipboardData
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout.layout import Layout
    from prompt_toolkit.output import DummyOutput

    console, _ = _console_with_leaf()

    async def main():
        with create_pipe_input() as inp:
            app = Application(layout=console.app.layout,
                              key_bindings=console.app.key_bindings,
                              mouse_support=True, full_screen=True,
                              clipboard=console.app.clipboard,
                              input=inp, output=DummyOutput())
            console.app = app
            console.state.set_app(app)
            app.clipboard.set_data(ClipboardData('rtc set'))

            async def probe():
                await _asyncio.sleep(0.15)
                inp.send_text('\x1b[<2;5;3m')
                await _asyncio.sleep(0.2)
                app.exit()

            app.create_background_task(probe())
            await app.run_async()

    _asyncio.run(main())
    assert 'rtc set' in console.input_field.buffer.text, \
        'a right click with no selection did not paste'


def _drag(app, inp, press='\x1b[<0;10;3M', move='\x1b[<32;30;5M',
          release='\x1b[<0;30;5m'):
    """Send a left-button drag as a terminal would, after a real render."""
    import asyncio as _asyncio

    async def steps():
        await _asyncio.sleep(0.1)
        app.renderer.render(app, app.layout)
        await _asyncio.sleep(0.05)
        for packet in (press, move, release):
            inp.send_text(packet)
            await _asyncio.sleep(0.1)
        app.exit()

    return steps


def test_clicking_a_pane_focuses_it_and_pauses():
    # Clicking into a pane means working in it. Following output would move the
    # text out from under a selection while it is being made, so a click also
    # pauses -- the same state F5 leaves, and F5 resumes.
    import asyncio as _asyncio
    from prompt_toolkit.application import Application
    from prompt_toolkit.document import Document
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    console, _ = _console_with_leaf()
    console.logger_buffer.set_document(
        Document('\n'.join(f'log line {i}' for i in range(20))), True)
    assert console.state.scroll_to_end is True

    async def main():
        with create_pipe_input() as inp:
            app = Application(layout=console.app.layout,
                              key_bindings=console.app.key_bindings,
                              mouse_support=True, full_screen=True,
                              clipboard=console.app.clipboard,
                              input=inp, output=DummyOutput())
            console.app = app
            console.state.set_app(app)

            async def probe():
                await _asyncio.sleep(0.1)
                app.renderer.render(app, app.layout)
                await _asyncio.sleep(0.05)
                inp.send_text('\x1b[<0;10;3M')      # left press in a pane
                await _asyncio.sleep(0.1)
                inp.send_text('\x1b[<0;10;3m')      # release
                await _asyncio.sleep(0.15)
                app.exit()

            app.create_background_task(probe())
            await app.run_async()

    _asyncio.run(main())

    assert console.has_focus(console.terminal_buffer) or \
        console.has_focus(console.logger_buffer), 'the click did not focus a pane'
    assert console.state.scroll_to_end is False, \
        'clicking a pane left it scrolling, so a selection cannot be held'


def test_a_click_outside_the_panes_does_not_pause():
    # The status bar and the command line are not places a selection is made.
    import asyncio as _asyncio
    from prompt_toolkit.application import Application
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    console, _ = _console_with_leaf()

    async def main():
        with create_pipe_input() as inp:
            app = Application(layout=console.app.layout,
                              key_bindings=console.app.key_bindings,
                              mouse_support=True, full_screen=True,
                              clipboard=console.app.clipboard,
                              input=inp, output=DummyOutput())
            console.app = app
            console.state.set_app(app)

            async def probe():
                await _asyncio.sleep(0.1)
                app.renderer.render(app, app.layout)
                await _asyncio.sleep(0.05)
                inp.send_text('\x1b[<0;5;40M')      # status bar row
                await _asyncio.sleep(0.15)
                app.exit()

            app.create_background_task(probe())
            await app.run_async()

    _asyncio.run(main())
    assert console.state.scroll_to_end is True, 'a click off the panes paused it'
