"""Drag edge-scroll / wheel-during-selection helpers."""

from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.selection import SelectionState, SelectionType

from rttt.connectors.demo import DemoConnector
from rttt.console import Console


def _fill(console, n=40):
    buf = console.logger_buffer
    text = ''.join(f'line {i:02d} content here\n' for i in range(n))
    buf.set_document(Document(text), bypass_readonly=True)
    return buf


def test_drag_scroll_extends_selection_up():
    console = Console(DemoConnector(delay=10), history_file=None)
    buf = _fill(console, 40)
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    # Selection starts near the bottom of the buffer.
    start = buf.text.index('line 30')
    buf.selection_state = SelectionState(start, SelectionType.CHARACTERS)
    buf.cursor_position = start + 4
    console._drag_buffer = buf
    window = console.logger_window.window
    window.vertical_scroll = 20
    # Fake render_info-ish: call scroll helper; even without render_info it
    # should not crash. With no render_info returns NotImplemented/None.
    result = console._drag_scroll_and_extend(buf, direction=-1)
    # vertical_scroll decreases when render_info exists; without it, no-op.
    assert buf.selection_state is not None
    assert result in (None, NotImplemented)


def test_drag_capture_routes_move_above_pane():
    console = Console(DemoConnector(delay=10), history_file=None)
    buf = _fill(console, 30)
    console.state.scroll_to_end = False
    console._pause_origin = 'auto'
    console._drag_buffer = buf
    console._drag_was_scrolling = True
    console._paused_for_drag = True
    start = buf.text.index('line 10')
    buf.selection_state = SelectionState(start, SelectionType.CHARACTERS)
    buf.cursor_position = start + 5
    console._pane_geom[buf] = {
        'xpos': 0, 'ypos': 5, 'width': 40, 'height': 10, 'left_w': 3,
        'text_area': console.logger_window,
    }
    # Pointer above the pane (y=2 < ypos=5) → scroll/extend path.
    ev = MouseEvent(
        position=Point(x=10, y=2),
        event_type=MouseEventType.MOUSE_MOVE,
        button=MouseButton.LEFT,
        modifiers=frozenset(),
    )
    console._drag_capture_mouse(ev)
    assert console._drag_buffer is buf
    assert buf.selection_state is not None


def test_wheel_during_selection_keeps_anchor():
    console = Console(DemoConnector(delay=10), history_file=None)
    buf = _fill(console, 40)
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    orig = buf.text.index('line 15')
    buf.selection_state = SelectionState(orig, SelectionType.CHARACTERS)
    buf.cursor_position = orig + 8
    anchor = buf.selection_state.original_cursor_position
    # Invoke via pane mouse handler (wheel path).
    handler = console.logger_window.control.mouse_handler
    ev = MouseEvent(
        position=Point(x=4, y=2),
        event_type=MouseEventType.SCROLL_UP,
        button=MouseButton.NONE,
        modifiers=frozenset(),
    )
    handler(ev)
    assert buf.selection_state is not None
    assert buf.selection_state.original_cursor_position == anchor


def test_edge_scroll_start_without_app_loop_steps_once():
    """No running app → one immediate scroll step (unit-test path)."""
    console = Console(DemoConnector(delay=10), history_file=None)
    buf = _fill(console, 40)
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    start = buf.text.index('line 30')
    buf.selection_state = SelectionState(start, SelectionType.CHARACTERS)
    buf.cursor_position = start + 4
    console._drag_buffer = buf
    window = console.logger_window.window
    window.vertical_scroll = 20

    # Fake render_info so _drag_scroll_and_extend can run.
    class _Info:
        content_height = 40
        window_height = 10

    window.render_info = _Info()
    before = window.vertical_scroll
    console._start_edge_scroll(direction=-1, distance=4)
    assert window.vertical_scroll == before - 1
    assert console._edge_scroll_dir == -1
    console._stop_edge_scroll()
    assert console._edge_scroll_dir == 0


def test_drag_scroll_updates_pinned_scroll():
    console = Console(DemoConnector(delay=10), history_file=None)
    buf = _fill(console, 40)
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    console._pinned_scroll[buf] = 20
    window = console.logger_window.window
    window.vertical_scroll = 20

    class _Info:
        content_height = 40
        window_height = 10

    window.render_info = _Info()
    start = buf.text.index('line 25')
    buf.selection_state = SelectionState(start, SelectionType.CHARACTERS)
    buf.cursor_position = start + 3
    console._drag_scroll_and_extend(buf, direction=-1, steps=3)
    assert window.vertical_scroll == 17
    assert console._pinned_scroll[buf] == 17
