"""HybridClipboard: OSC 52 emission, size caps, pyperclip soft-fail."""
import base64
import os
from unittest import mock

import pytest
from prompt_toolkit.clipboard.base import ClipboardData

from rttt.clipboard import (
    HybridClipboard,
    OSC52_MAX_CHARS,
    build_osc52_sequence,
    prefer_pyperclip,
)



def _decode_osc52_text(seq: str) -> str:
    """Extract clipboard text from bare or tmux-wrapped OSC 52."""
    marker = '52;c;'
    i = seq.rfind(marker)
    assert i >= 0, repr(seq[:120])
    rest = seq[i + len(marker):]
    bel = rest.find(chr(7))
    assert bel >= 0, repr(rest[:80])
    return base64.b64decode(rest[:bel]).decode()


@pytest.fixture(autouse=True)
def _clear_tmux_for_default_osc52(monkeypatch, request):
    """Bare OSC 52 tests must not inherit ambient TMUX from the box/shell."""
    if request.node.name in {
        'test_build_osc52_sequence_tmux_passthrough',
        'test_osc52_tmux_passthrough_under_ssh',
    }:
        return
    monkeypatch.delenv('TMUX', raising=False)

def test_build_osc52_sequence_encodes_base64():
    seq = build_osc52_sequence('hi', tmux=False)
    assert seq.startswith('\x1b]52;c;')
    assert seq.endswith('\a')
    payload = seq[len('\x1b]52;c;'):-1]
    assert base64.b64decode(payload) == b'hi'


def test_build_osc52_sequence_tmux_passthrough():
    seq = build_osc52_sequence('x', tmux=True)
    assert seq.startswith('\x1bPtmux;')
    assert seq.endswith('\x1b\\')
    # Inner ESC doubled for tmux DCS.
    assert '\x1b\x1b]52;c;' in seq


def test_hybrid_set_data_emits_osc52_via_sink():
    emitted = []
    clip = HybridClipboard(emit=emitted.append, use_pyperclip=False)
    clip.set_data(ClipboardData('hello'))
    assert clip.get_data().text == 'hello'
    assert clip.last_status == 'ok'
    assert len(emitted) == 1
    assert _decode_osc52_text(emitted[0]) == 'hello'


def test_hybrid_truncates_oversized_payload():
    emitted = []
    clip = HybridClipboard(emit=emitted.append, use_pyperclip=False, max_chars=10)
    clip.set_text('abcdefghijklmnopqrstuvwxyz')
    assert _decode_osc52_text(emitted[0]) == 'abcdefghij'


def test_hybrid_empty_selection_status():
    clip = HybridClipboard(emit=lambda s: None, use_pyperclip=False)
    clip.set_data(ClipboardData(''))
    assert clip.last_status == 'empty'


def test_hybrid_pyperclip_exception_swallowed():
    emitted = []
    clip = HybridClipboard(emit=emitted.append, use_pyperclip=True)

    class Boom(Exception):
        pass

    fake_pyperclip = mock.Mock()
    fake_pyperclip.PyperclipException = Boom
    fake_pyperclip.copy.side_effect = Boom('no display')

    with mock.patch.dict('sys.modules', {'pyperclip': fake_pyperclip}):
        # Force import path inside _emit_pyperclip
        clip.set_text('abc')

    assert clip.last_status == 'ok'  # OSC 52 still succeeded
    assert emitted


def test_hybrid_fails_when_nothing_works():
    clip = HybridClipboard(emit=None, use_pyperclip=False)
    with mock.patch.object(clip, '_write_to_app_output', return_value=False):
        clip.set_text('abc')
    assert clip.last_status == 'failed'
    assert 'OSC 52' in clip.last_error


def test_prefer_pyperclip_skips_ssh(monkeypatch):
    monkeypatch.setenv('SSH_CONNECTION', '1.2.3.4 1 5.6.7.8 2')
    monkeypatch.setenv('DISPLAY', ':0')
    assert prefer_pyperclip() is False


def test_prefer_pyperclip_local_display(monkeypatch):
    monkeypatch.delenv('SSH_CONNECTION', raising=False)
    monkeypatch.delenv('SSH_CLIENT', raising=False)
    monkeypatch.setenv('DISPLAY', ':0')
    monkeypatch.setattr('sys.platform', 'linux')
    assert prefer_pyperclip() is True


def test_osc52_max_chars_constant_in_range():
    assert 50_000 <= OSC52_MAX_CHARS <= 72_000


def test_buffer_insert_preserves_selection():
    """Streaming append must not wipe a mouse selection (PTK clears it in
    _text_changed); otherwise select-to-copy + Ctrl-C sees 'Nothing selected'.
    """
    from prompt_toolkit.document import Document
    from prompt_toolkit.selection import SelectionType
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    buf = console.terminal_buffer
    buf.set_document(Document('hello world\nsecond line\n'), bypass_readonly=True)
    buf.cursor_position = 0
    buf.start_selection(selection_type=SelectionType.CHARACTERS)
    buf.cursor_position = 5
    assert buf.selection_state is not None

    console._buffer_insert_text(buf, 'more\n')
    assert buf.selection_state is not None, 'append cleared the selection'
    assert buf.selection_state.original_cursor_position == 0
    # Auto-scroll must not have dragged the cursor to the end (would stretch).
    assert buf.cursor_position == 5

    console._copy_from_buffer(buf, clear_selection=False)
    assert buf.selection_state is not None
    assert 'Copied' in console.state.current_message() or console.state.message.startswith('Copied')


def test_buffer_insert_skips_scroll_while_dragging():
    from prompt_toolkit.document import Document
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    console.state.scroll_to_end = True
    buf = console.terminal_buffer
    buf.set_document(Document('abc\n'), bypass_readonly=True)
    buf.cursor_position = 1
    console._drag_buffer = buf
    console._buffer_insert_text(buf, 'x\n')
    assert buf.cursor_position == 1, 'drag must pause auto-scroll'



def test_mouse_inclusive_copy_keeps_last_char():
    """Drag ending on the last wanted character must include it (GUI-like).

    PTK Emacs mode excludes the character under the cursor from cut_selection;
    mouse-up bumps the exclusive end so 'log 10' copies in full.
    """
    from prompt_toolkit.document import Document
    from prompt_toolkit.selection import SelectionType
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    console.app.clipboard = __import__('rttt.clipboard', fromlist=['HybridClipboard']).HybridClipboard(
        emit=lambda s: None, use_pyperclip=False
    )
    buf = console.terminal_buffer
    buf.set_document(Document('xx log 10 yy\n'), bypass_readonly=True)
    # Mouse down on 'l', up on '0' (index 8) — emacs would copy 'log 1'.
    buf.cursor_position = 3
    buf.start_selection(selection_type=SelectionType.CHARACTERS)
    buf.cursor_position = 8
    console._make_mouse_selection_inclusive(buf)
    assert console._selected_text(buf) == 'log 10'
    console._copy_from_buffer(buf, clear_selection=False)
    assert console.app.clipboard.get_data().text == 'log 10'
    assert console._last_copied == 'log 10'
    assert 'Copied 6' in console.state.message


def test_selection_survives_many_appends():
    from prompt_toolkit.document import Document
    from prompt_toolkit.selection import SelectionType
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    console.app.clipboard = __import__('rttt.clipboard', fromlist=['HybridClipboard']).HybridClipboard(
        emit=lambda s: None, use_pyperclip=False
    )
    buf = console.terminal_buffer
    buf.set_document(Document('xx log 10 yy\n'), bypass_readonly=True)
    buf.cursor_position = 3
    buf.start_selection(selection_type=SelectionType.CHARACTERS)
    buf.cursor_position = 8
    console._make_mouse_selection_inclusive(buf)
    assert console._selected_text(buf) == 'log 10'

    for i in range(20):
        console._buffer_insert_text(buf, f'more{i}\n')
        assert buf.selection_state is not None, f'selection lost after append {i}'
        assert console._selected_text(buf) == 'log 10', f'text changed after append {i}'

    # Ctrl-C style clear still has last_copied for re-toast.
    console._copy_from_buffer(buf, clear_selection=True)
    assert console._last_copied == 'log 10'
    assert buf.selection_state is None
    console.state.message = ''
    console._copy_from_buffer(buf, clear_selection=True)  # no selection → last copy
    assert 'Copied 6' in console.state.message


def test_ctrl_c_retoasts_last_copy_without_selection():
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console
    from rttt.clipboard import HybridClipboard

    console = Console(DemoConnector(delay=10), history_file=None)
    console.app.clipboard = HybridClipboard(emit=lambda s: None, use_pyperclip=False)
    console._last_copied = 'saved payload'
    console._copy_from_buffer(None, clear_selection=False)
    assert console.app.clipboard.get_data().text == 'saved payload'
    assert 'Copied 13' in console.state.message


def test_drag_start_auto_pauses_scroll():
    """Auto-pause helper freezes scroll and marks origin as auto."""
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    assert console.state.scroll_to_end is True
    console._pause_auto_scroll_for_selection()
    assert console.state.scroll_to_end is False
    assert console._pause_origin == 'auto'
    assert 'Paused' in console.state.message
    console.state.message = ''
    console._pause_auto_scroll_for_selection()
    assert console.state.message == ''


def test_copy_uses_only_focused_pane_not_both():
    """Selection sticky on Log must not leak into Terminal copy."""
    from prompt_toolkit.document import Document
    from prompt_toolkit.selection import SelectionType
    from rttt.clipboard import HybridClipboard
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    console.app.clipboard = HybridClipboard(emit=lambda s: None, use_pyperclip=False)

    term = console.terminal_buffer
    log = console.logger_buffer
    term.set_document(Document('TERM_ONLY_ABC\n'), bypass_readonly=True)
    log.set_document(Document('LOG_ONLY_XYZ\n'), bypass_readonly=True)

    log.cursor_position = 0
    log.start_selection(selection_type=SelectionType.CHARACTERS)
    log.cursor_position = 12
    console._make_mouse_selection_inclusive(log)
    assert 'LOG' in console._selected_text(log)

    console._clear_other_pane_selection(term)
    assert log.selection_state is None

    term.cursor_position = 0
    term.start_selection(selection_type=SelectionType.CHARACTERS)
    term.cursor_position = 12
    console._make_mouse_selection_inclusive(term)
    text = console._selected_text(term)
    assert 'TERM' in text
    assert 'LOG' not in text
    console._copy_from_buffer(term, clear_selection=False)
    clipped = console.app.clipboard.get_data().text
    assert 'TERM' in clipped and 'LOG' not in clipped


def test_osc52_used_when_ssh_skips_pyperclip(monkeypatch):
    """Simulate SSH: prefer_pyperclip False, OSC 52 still carries the payload."""
    from rttt.clipboard import HybridClipboard, prefer_pyperclip, build_osc52_sequence

    monkeypatch.setenv('SSH_CONNECTION', '10.0.0.1 22 10.0.0.2 44000')
    monkeypatch.setenv('DISPLAY', ':0')
    monkeypatch.delenv('TMUX', raising=False)
    assert prefer_pyperclip() is False

    emitted = []
    clip = HybridClipboard(emit=emitted.append, use_pyperclip=False)
    clip.set_text('ssh-payload')
    assert clip.last_status == 'ok'
    assert len(emitted) == 1
    expected = build_osc52_sequence('ssh-payload', tmux=False)
    assert emitted[0] == expected
    assert _decode_osc52_text(emitted[0]) == 'ssh-payload'


def test_osc52_tmux_passthrough_under_ssh(monkeypatch):
    from rttt.clipboard import HybridClipboard, build_osc52_sequence

    monkeypatch.setenv('SSH_CONNECTION', '1 2 3 4')
    monkeypatch.setenv('TMUX', '/tmp/tmux-0/default,123,0')
    emitted = []
    clip = HybridClipboard(emit=emitted.append, use_pyperclip=False)
    clip.set_text('via-tmux')
    assert emitted[0] == build_osc52_sequence('via-tmux', tmux=True)
    assert 'tmux;' in emitted[0]



def test_drag_append_preserves_in_progress_selection():
    """Streaming during an active drag must not wipe selection_state.

    First-drag failures happened when demo/device lines arrived between
    MOUSE_DOWN and MOUSE_UP while _drag_buffer was set and restore was skipped.
    """
    from prompt_toolkit.document import Document
    from prompt_toolkit.selection import SelectionType
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    buf = console.terminal_buffer
    buf.set_document(Document('hello world\n'), bypass_readonly=True)
    buf.cursor_position = 0
    buf.start_selection(selection_type=SelectionType.CHARACTERS)
    buf.cursor_position = 5
    console._drag_buffer = buf
    console.state.scroll_to_end = True  # would otherwise jump cursor to end

    console._buffer_insert_text(buf, 'STREAM\n')
    assert buf.selection_state is not None, 'drag selection cleared by append'
    assert buf.selection_state.original_cursor_position == 0
    assert buf.cursor_position == 5
    assert console._selected_text(buf) == 'hello'


def test_first_drag_from_unfocused_pane_selects_and_copies():
    """First gesture after start (Command focused) must select+copy, then auto-resume.

    prompt_toolkit focuses on mouse-up by default; we focus on mouse-down so
    BufferControl can start a selection in the same drag. Pause starts on MOVE
    (not DOWN); after a successful copy, streaming resumes and highlight clears.
    """
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.document import Document
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseEvent, MouseEventType, MouseButton
    from prompt_toolkit.output import DummyOutput
    from rttt.clipboard import HybridClipboard
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    console.app.clipboard = HybridClipboard(emit=lambda s: None, use_pyperclip=False)
    buf = console.logger_buffer
    buf.set_document(Document('aaaa log LINE1 bbbb\n'), bypass_readonly=True)

    control = console.logger_window.control

    class FakeLine:
        def display_to_source(self, x):
            return x

    control._last_get_processed_line = lambda y: FakeLine()
    handler = control.mouse_handler
    mods = frozenset()

    def ev(x, typ):
        return MouseEvent(
            position=Point(x=x, y=0),
            event_type=typ,
            button=MouseButton.LEFT,
            modifiers=mods,
        )

    with create_pipe_input() as inp:
        with create_app_session(input=inp, output=DummyOutput()) as session:
            session.app = console.app
            console.app.layout.focus(console.input_field)
            console.state.scroll_to_end = True
            console.state.message = ''

            handler(ev(5, MouseEventType.MOUSE_DOWN))
            assert console.has_focus(console.logger_window), 'must focus on press'
            assert console.state.scroll_to_end is True, 'plain press must not pause'
            assert buf.cursor_position == 5

            handler(ev(14, MouseEventType.MOUSE_MOVE))
            assert buf.selection_state is not None, 'move must start selection'
            assert console.state.scroll_to_end is False, 'must auto-pause on move'
            assert console._pause_origin == 'auto'

            # Streaming mid-drag (the GUI race).
            console._buffer_insert_text(buf, 'STREAM\n')
            assert buf.selection_state is not None, 'append wiped drag selection'

            handler(ev(14, MouseEventType.MOUSE_UP))
            assert 'Copied' in console.state.message, console.state.message
            assert 'resumed' in console.state.message
            clipped = console.app.clipboard.get_data().text
            assert clipped, 'clipboard empty after first drag'
            assert 'LINE1' in clipped
            assert console.state.scroll_to_end is True, 'must auto-resume after copy'
            assert console._pause_origin is None
            assert buf.selection_state is None, 'highlight cleared on auto-resume'
            assert console._last_copied and 'LINE1' in console._last_copied


def test_first_drag_after_resume_from_command_focus_copies():
    """After F5 resume, focus often returns to Command — first drag must still copy."""
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.document import Document
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseEvent, MouseEventType, MouseButton
    from prompt_toolkit.output import DummyOutput
    from rttt.clipboard import HybridClipboard
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    console.app.clipboard = HybridClipboard(emit=lambda s: None, use_pyperclip=False)
    buf = console.terminal_buffer
    buf.set_document(Document('xx paste-me yy\n'), bypass_readonly=True)

    control = console.terminal_window.control

    class FakeLine:
        def display_to_source(self, x):
            return x

    control._last_get_processed_line = lambda y: FakeLine()
    handler = control.mouse_handler
    mods = frozenset()

    def ev(x, typ):
        return MouseEvent(
            position=Point(x=x, y=0),
            event_type=typ,
            button=MouseButton.LEFT,
            modifiers=mods,
        )

    with create_pipe_input() as inp:
        with create_app_session(input=inp, output=DummyOutput()) as session:
            session.app = console.app
            # Simulate post-F5: scroll on, Command focused, no sticky selection.
            console.state.scroll_to_end = True
            console._pause_origin = None
            console._selection_span = None
            buf.exit_selection()
            console.app.layout.focus(console.input_field)
            console._last_copied = ''
            console.state.message = ''

            handler(ev(3, MouseEventType.MOUSE_DOWN))
            assert console.state.scroll_to_end is True
            handler(ev(10, MouseEventType.MOUSE_MOVE))
            assert console.state.scroll_to_end is False
            handler(ev(10, MouseEventType.MOUSE_UP))

            assert console.state.scroll_to_end is True, 'auto-resume after copy'
            assert 'Copied' in console.state.message, console.state.message
            assert 'paste-me' in console.app.clipboard.get_data().text


def test_click_without_drag_does_not_pause_or_select():
    """Plain click (no MOVE) must not pause streaming or leave a highlight."""
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.document import Document
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseEvent, MouseEventType, MouseButton
    from prompt_toolkit.output import DummyOutput
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    buf = console.logger_buffer
    buf.set_document(Document('only click\n'), bypass_readonly=True)
    control = console.logger_window.control

    class FakeLine:
        def display_to_source(self, x):
            return x

    control._last_get_processed_line = lambda y: FakeLine()
    handler = control.mouse_handler
    mods = frozenset()

    with create_pipe_input() as inp:
        with create_app_session(input=inp, output=DummyOutput()) as session:
            session.app = console.app
            console.app.layout.focus(console.input_field)
            console.state.scroll_to_end = True
            console.state.message = ''

            down = MouseEvent(
                position=Point(x=2, y=0),
                event_type=MouseEventType.MOUSE_DOWN,
                button=MouseButton.LEFT,
                modifiers=mods,
            )
            up = MouseEvent(
                position=Point(x=2, y=0),
                event_type=MouseEventType.MOUSE_UP,
                button=MouseButton.LEFT,
                modifiers=mods,
            )
            handler(down)
            assert console.state.scroll_to_end is True
            assert console._pause_origin is None
            handler(up)
            assert console.state.scroll_to_end is True
            assert console._pause_origin is None
            assert buf.selection_state is None
            assert console._selection_span is None
            assert 'Paused' not in (console.state.message or '')
            assert 'Copied' not in (console.state.message or '')


def _mouse_harness(console, window, text):
    """Shared setup for select-to-copy mouse simulation tests."""
    from prompt_toolkit.document import Document
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseEvent, MouseEventType, MouseButton
    from rttt.clipboard import HybridClipboard

    console.app.clipboard = HybridClipboard(emit=lambda s: None, use_pyperclip=False)
    buf = window.buffer
    buf.set_document(Document(text), bypass_readonly=True)
    control = window.control

    class FakeLine:
        def display_to_source(self, x):
            return x

    control._last_get_processed_line = lambda y: FakeLine()
    mods = frozenset()

    def ev(x, typ):
        return MouseEvent(
            position=Point(x=x, y=0),
            event_type=typ,
            button=MouseButton.LEFT,
            modifiers=mods,
        )

    return buf, control.mouse_handler, ev


def test_auto_pause_copy_then_auto_resume():
    """Streaming on → drag copy → scroll resumes and highlight clears."""
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseEventType
    from prompt_toolkit.output import DummyOutput
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    buf, handler, ev = _mouse_harness(
        console, console.logger_window, 'aaaa log LINE1 bbbb\n')

    with create_pipe_input() as inp:
        with create_app_session(input=inp, output=DummyOutput()) as session:
            session.app = console.app
            console.app.layout.focus(console.input_field)
            console.state.scroll_to_end = True
            console._pause_origin = None
            console.state.message = ''

            handler(ev(5, MouseEventType.MOUSE_DOWN))
            assert console.state.scroll_to_end is True
            handler(ev(14, MouseEventType.MOUSE_MOVE))
            assert console.state.scroll_to_end is False
            assert console._pause_origin == 'auto'
            handler(ev(14, MouseEventType.MOUSE_UP))

            assert 'Copied' in console.state.message
            assert 'resumed' in console.state.message
            assert console.state.scroll_to_end is True
            assert console._pause_origin is None
            assert buf.selection_state is None
            assert 'LINE1' in console._last_copied

            # Ctrl-C re-toasts via _last_copied after highlight is gone.
            console.state.message = ''
            console._copy_from_buffer(None, clear_selection=False)
            assert 'Copied' in console.state.message
            assert 'LINE1' in console.app.clipboard.get_data().text


def test_manual_f5_pause_then_drag_copy_stays_paused():
    """If the user paused with F5 before the drag, stay paused after copy."""
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseEventType
    from prompt_toolkit.output import DummyOutput
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    buf, handler, ev = _mouse_harness(
        console, console.terminal_window, 'xx paste-me yy\n')

    with create_pipe_input() as inp:
        with create_app_session(input=inp, output=DummyOutput()) as session:
            session.app = console.app
            # Simulate F5 pause (manual).
            console.state.scroll_to_end = False
            console._pause_origin = 'manual'
            console.app.layout.focus(console.input_field)
            console.state.message = ''
            console._last_copied = ''

            handler(ev(3, MouseEventType.MOUSE_DOWN))
            assert console.state.scroll_to_end is False
            assert console._pause_origin == 'manual'
            handler(ev(10, MouseEventType.MOUSE_MOVE))
            # Already paused — must not flip origin to auto.
            assert console._pause_origin == 'manual'
            assert console.state.scroll_to_end is False
            handler(ev(10, MouseEventType.MOUSE_UP))

            assert 'Copied' in console.state.message
            assert 'resumed' not in console.state.message
            assert console.state.scroll_to_end is False
            assert console._pause_origin == 'manual'
            assert buf.selection_state is not None, 'sticky highlight when manually paused'
            assert 'paste-me' in console.app.clipboard.get_data().text


def _right_ev(x, typ, button=None):
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseEvent, MouseEventType, MouseButton
    return MouseEvent(
        position=Point(x=x, y=0),
        event_type=typ,
        button=button or MouseButton.RIGHT,
        modifiers=frozenset(),
    )


def test_right_click_copies_selection_without_pausing():
    """RMB on a pane with a selection copies via Ctrl-C path; no pause/focus steal."""
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.document import Document
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseEventType, MouseButton
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.selection import SelectionType
    from rttt.clipboard import HybridClipboard
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    console.app.clipboard = HybridClipboard(emit=lambda s: None, use_pyperclip=False)
    buf = console.logger_buffer
    buf.set_document(Document('aaaa SELECTME bbbb\n'), bypass_readonly=True)
    control = console.logger_window.control

    class FakeLine:
        def display_to_source(self, x):
            return x

    control._last_get_processed_line = lambda y: FakeLine()
    handler = control.mouse_handler

    with create_pipe_input() as inp:
        with create_app_session(input=inp, output=DummyOutput()) as session:
            session.app = console.app
            console.app.layout.focus(console.input_field)
            console.state.scroll_to_end = True
            console.state.message = ''

            # Sticky selection as after a left-drag with manual pause.
            buf.cursor_position = 5
            buf.start_selection(selection_type=SelectionType.CHARACTERS)
            buf.cursor_position = 12  # exclusive end before inclusive bump
            console._make_mouse_selection_inclusive(buf)
            assert 'SELECTME' in console._selected_text(buf)

            was_scrolling = console.state.scroll_to_end
            focused_before = console.has_focus(console.input_field)

            handler(_right_ev(8, MouseEventType.MOUSE_DOWN))
            assert console.state.scroll_to_end is was_scrolling
            assert console.has_focus(console.input_field) is focused_before
            assert buf.selection_state is not None, 'RMB down must not clear selection'

            handler(_right_ev(8, MouseEventType.MOUSE_UP))
            assert 'Copied' in console.state.message, console.state.message
            assert 'SELECTME' in console.app.clipboard.get_data().text
            assert console.state.scroll_to_end is was_scrolling
            assert console.has_focus(console.input_field) is focused_before
            # clear_selection=True → highlight may go (OK per UX)
            assert console._last_copied and 'SELECTME' in console._last_copied


def test_right_click_no_selection_retoasts_last_copy():
    """No selection → same as Ctrl-C: re-toast _last_copied."""
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.document import Document
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseEventType
    from prompt_toolkit.output import DummyOutput
    from rttt.clipboard import HybridClipboard
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    console.app.clipboard = HybridClipboard(emit=lambda s: None, use_pyperclip=False)
    buf = console.terminal_buffer
    buf.set_document(Document('no selection here\n'), bypass_readonly=True)
    control = console.terminal_window.control

    class FakeLine:
        def display_to_source(self, x):
            return x

    control._last_get_processed_line = lambda y: FakeLine()
    handler = control.mouse_handler
    console._last_copied = 'prior payload'

    with create_pipe_input() as inp:
        with create_app_session(input=inp, output=DummyOutput()) as session:
            session.app = console.app
            console.state.scroll_to_end = True
            console.state.message = ''
            handler(_right_ev(3, MouseEventType.MOUSE_DOWN))
            handler(_right_ev(3, MouseEventType.MOUSE_UP))
            assert 'Copied' in console.state.message
            assert console.app.clipboard.get_data().text == 'prior payload'
            assert console.state.scroll_to_end is True


def test_right_click_nothing_selected_when_empty():
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.document import Document
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseEventType
    from prompt_toolkit.output import DummyOutput
    from rttt.clipboard import HybridClipboard
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    console.app.clipboard = HybridClipboard(emit=lambda s: None, use_pyperclip=False)
    buf = console.logger_buffer
    buf.set_document(Document('empty\n'), bypass_readonly=True)
    control = console.logger_window.control

    class FakeLine:
        def display_to_source(self, x):
            return x

    control._last_get_processed_line = lambda y: FakeLine()
    console._last_copied = ''

    with create_pipe_input() as inp:
        with create_app_session(input=inp, output=DummyOutput()) as session:
            session.app = console.app
            console.state.message = ''
            control.mouse_handler(_right_ev(1, MouseEventType.MOUSE_UP))
            assert console.state.message == 'Nothing selected'


def test_right_click_does_not_start_selection_on_move():
    """Right-drag must not create a selection or pause streaming."""
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.document import Document
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseEventType
    from prompt_toolkit.output import DummyOutput
    from rttt.clipboard import HybridClipboard
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    console.app.clipboard = HybridClipboard(emit=lambda s: None, use_pyperclip=False)
    buf = console.logger_buffer
    buf.set_document(Document('abcdefghijklmnop\n'), bypass_readonly=True)
    control = console.logger_window.control

    class FakeLine:
        def display_to_source(self, x):
            return x

    control._last_get_processed_line = lambda y: FakeLine()
    handler = control.mouse_handler

    with create_pipe_input() as inp:
        with create_app_session(input=inp, output=DummyOutput()) as session:
            session.app = console.app
            console.state.scroll_to_end = True
            console._last_copied = ''
            handler(_right_ev(2, MouseEventType.MOUSE_DOWN))
            handler(_right_ev(10, MouseEventType.MOUSE_MOVE))
            assert buf.selection_state is None
            assert console.state.scroll_to_end is True
            handler(_right_ev(10, MouseEventType.MOUSE_UP))
            assert console.state.message == 'Nothing selected'
            assert console.state.scroll_to_end is True


def test_command_paste_payload_first_line_only():
    from rttt.console import Console
    assert Console._command_paste_payload('one\n') == 'one'
    assert Console._command_paste_payload('one\r\n') == 'one'
    assert Console._command_paste_payload('first\nsecond\n') == 'first'
    assert Console._command_paste_payload('') == ''
    assert Console._command_paste_payload('plain') == 'plain'


def test_right_click_paste_from_pyperclip(monkeypatch):
    """Local GUI path: pyperclip.paste() feeds Command; toast Pasted N chars."""
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseEventType
    from prompt_toolkit.output import DummyOutput
    from rttt.clipboard import HybridClipboard
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console
    import types

    console = Console(DemoConnector(delay=10), history_file=None)
    console.app.clipboard = HybridClipboard(emit=lambda s: None, use_pyperclip=True)
    fake = types.SimpleNamespace(paste=lambda: 'hello from sys\n', copy=lambda t: None)
    # PyperclipException attribute used elsewhere; not needed for paste.
    monkeypatch.setitem(__import__('sys').modules, 'pyperclip', fake)

    control = console.input_field.control
    # Ensure FakeLine not required for input paste (we don't call original on RMB)

    with create_pipe_input() as inp:
        with create_app_session(input=inp, output=DummyOutput()) as session:
            session.app = console.app
            console.app.layout.focus(console.logger_window)
            console.input_field.buffer.text = ''
            console.state.message = ''
            control.mouse_handler(_right_ev(2, MouseEventType.MOUSE_DOWN))
            control.mouse_handler(_right_ev(2, MouseEventType.MOUSE_UP))
            assert console.has_focus(console.input_field)
            assert console.input_field.buffer.text == 'hello from sys'
            assert console.state.message == 'Pasted 14 chars'


def test_right_click_paste_ssh_fallback_to_in_app():
    """SSH / no pyperclip: paste HybridClipboard / _last_copied into Command."""
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseEventType
    from prompt_toolkit.output import DummyOutput
    from rttt.clipboard import HybridClipboard
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    console.app.clipboard = HybridClipboard(emit=lambda s: None, use_pyperclip=False)
    console.app.clipboard.set_text('in-app paste me')
    console._last_copied = 'in-app paste me'

    with create_pipe_input() as inp:
        with create_app_session(input=inp, output=DummyOutput()) as session:
            session.app = console.app
            console.app.layout.focus(console.terminal_window)
            console.input_field.buffer.text = 'cmd '
            console.input_field.buffer.cursor_position = 4
            console.state.message = ''
            console.input_field.control.mouse_handler(
                _right_ev(1, MouseEventType.MOUSE_UP))
            assert console.has_focus(console.input_field)
            assert console.input_field.buffer.text == 'cmd in-app paste me'
            assert 'Pasted' in console.state.message


def test_right_click_paste_empty_clipboard():
    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseEventType
    from prompt_toolkit.output import DummyOutput
    from rttt.clipboard import HybridClipboard
    from rttt.connectors.demo import DemoConnector
    from rttt.console import Console

    console = Console(DemoConnector(delay=10), history_file=None)
    console.app.clipboard = HybridClipboard(emit=lambda s: None, use_pyperclip=False)
    console._last_copied = ''

    with create_pipe_input() as inp:
        with create_app_session(input=inp, output=DummyOutput()) as session:
            session.app = console.app
            console.input_field.buffer.text = ''
            console.state.message = ''
            console.input_field.control.mouse_handler(
                _right_ev(0, MouseEventType.MOUSE_UP))
            assert console.state.message == 'Clipboard empty'
            assert console.input_field.buffer.text == ''
