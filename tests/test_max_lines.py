"""Buffer line-cap / trim behaviour."""

from prompt_toolkit.document import Document
from prompt_toolkit.selection import SelectionState, SelectionType

from rttt.connectors.demo import DemoConnector
from rttt.console import Console, DEFAULT_MAX_LINES, TRIM_HYSTERESIS


def test_default_max_lines_constant():
    assert DEFAULT_MAX_LINES == 10000
    assert TRIM_HYSTERESIS == 0.10


def test_trim_drops_oldest_when_over_hysteresis():
    console = Console(DemoConnector(delay=10), history_file=None, max_lines=100)
    buf = console.terminal_buffer
    # Cross the 110% threshold → one trim down to max.
    for i in range(111):
        console._buffer_insert_text(buf, f'log {i}\n')
    n = buf.text.count('\n')
    assert n == 100, n
    assert buf.text.startswith('log 11\n'), buf.text[:40]
    assert console._line_offset[buf] == 11


def test_trim_shifts_sticky_selection():
    console = Console(DemoConnector(delay=10), history_file=None, max_lines=50)
    buf = console.terminal_buffer
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    for i in range(40):
        console._buffer_insert_text(buf, f'log {i}\n')
    start = buf.text.index('log 30\n')
    end = start + len('log 30\n')
    console._apply_span(buf, (start, end))
    assert console._selected_text(buf) == 'log 30\n'
    # While paused, appends must not trim (pin / selection stay put).
    for i in range(40, 56):
        console._buffer_insert_text(buf, f'log {i}\n')
    assert buf.text.count('\n') == 56
    assert console._selected_text(buf) == 'log 30\n'
    # Forced trim still shifts the sticky span with the text.
    console._trim_oldest_lines(buf, force=True)
    assert buf.text.count('\n') == 50
    assert console._selected_text(buf) == 'log 30\n'
    assert 'log 0\n' not in buf.text
    assert console._line_offset[buf] == 6


def test_trim_during_paused_drag_keeps_selection():
    console = Console(DemoConnector(delay=10), history_file=None, max_lines=30)
    buf = console.terminal_buffer
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    for i in range(25):
        console._buffer_insert_text(buf, f'row {i}\n')
    start = buf.text.index('row 20\n')
    end = start + len('row 20')
    buf.selection_state = SelectionState(start, SelectionType.CHARACTERS)
    buf.cursor_position = end
    console._drag_buffer = buf
    console._drag_was_scrolling = False
    # Paused: no auto-trim; selection stays without index shifting.
    for i in range(25, 34):
        console._buffer_insert_text(buf, f'row {i}\n')
    assert buf.text.count('\n') == 34
    assert buf.selection_state is not None
    assert 'row 20' in console._selected_text(buf)
    console._trim_oldest_lines(buf, force=True)
    assert buf.text.count('\n') == 30
    assert buf.selection_state is not None
    assert 'row 20' in console._selected_text(buf)


def test_trim_drops_selection_fully_trimmed_away():
    console = Console(DemoConnector(delay=10), history_file=None, max_lines=20)
    buf = console.terminal_buffer
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    for i in range(15):
        console._buffer_insert_text(buf, f'old {i}\n')
    start = buf.text.index('old 0\n')
    end = start + len('old 0\n')
    console._apply_span(buf, (start, end))
    for i in range(15, 23):
        console._buffer_insert_text(buf, f'new {i}\n')
    # Still paused — no trim yet; selection intact.
    assert buf.text.count('\n') == 23
    assert console._selected_text(buf) == 'old 0\n'
    console._trim_oldest_lines(buf, force=True)
    assert buf.text.count('\n') == 20
    assert console._selection_span is None
    assert buf.selection_state is None or console._selected_text(buf) == ''


def test_paused_vertical_scroll_adjusted_on_trim():
    console = Console(DemoConnector(delay=10), history_file=None, max_lines=20)
    buf = console.terminal_buffer
    window = console.terminal_window.window
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    for i in range(18):
        console._buffer_insert_text(buf, f'L{i}\n')
    window.vertical_scroll = 8
    console._pinned_abs_top[buf] = console._rel_to_abs_top(buf, 8)
    # Paused appends do not trim; force one trim to check abs→rel remap.
    for i in range(18, 23):
        console._buffer_insert_text(buf, f'L{i}\n')
    assert buf.text.count('\n') == 23
    console._trim_oldest_lines(buf, force=True)
    assert buf.text.count('\n') == 20
    assert window.vertical_scroll == 5
    assert console._pinned_abs_top[buf] == 8


def test_cli_max_lines_option():
    from click.testing import CliRunner
    from rttt.cli import cli
    runner = CliRunner()
    result = runner.invoke(cli, ['--help'])
    assert result.exit_code == 0
    assert '--max-lines' in result.output


def test_max_lines_zero_disables_trim():
    console = Console(DemoConnector(delay=10), history_file=None, max_lines=0)
    buf = console.terminal_buffer
    for i in range(50):
        console._buffer_insert_text(buf, f'x {i}\n')
    assert buf.text.count('\n') == 50


def test_paused_log_trim_rebuild_keeps_viewport():
    """Forced Log trim must remap the pin; Document() must not yank to EOF."""
    console = Console(DemoConnector(delay=10), history_file=None, max_lines=20)
    buf = console.logger_buffer
    window = console.logger_window.window
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    for i in range(18):
        console._append_log_line(f'# {i}.0 <I> line {i}\n')
    window.vertical_scroll = 5
    console._pinned_abs_top[buf] = console._rel_to_abs_top(buf, 5)
    buf.cursor_position = len(buf.text)
    console._clamp_cursor_to_pinned(buf)
    top_abs_before = console._log_view_abs_line_nos[window.vertical_scroll]
    for i in range(18, 23):
        console._append_log_line(f'# {i}.0 <I> line {i}\n')
    # Deferred while paused.
    assert len(console._log_lines) == 23
    dropped = console._trim_log_line_list(force=True)
    assert dropped > 0
    console._rebuild_log_view()
    assert len(console._log_lines) == 20
    pin = window.vertical_scroll
    assert console._log_view_abs_line_nos[pin] == top_abs_before
    assert buf.cursor_position < len(buf.text)
    newest = f'line {22}'
    assert newest in buf.text
    first_line = buf.text.splitlines()[pin]
    assert f'line {top_abs_before - 1}' in first_line


def test_paused_terminal_append_keeps_pinned_scroll():
    console = Console(DemoConnector(delay=10), history_file=None, max_lines=0)
    buf = console.terminal_buffer
    window = console.terminal_window.window
    for i in range(40):
        console._buffer_insert_text(buf, f'T{i}\n')
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    window.vertical_scroll = 10
    # Cursor not at EOF so pin uses vertical_scroll (not follow-tail heuristic).
    buf.cursor_position = buf.document.translate_row_col_to_index(10, 0)
    console._pin_viewports()
    assert console._pinned_abs_top[buf] == 10
    assert window.vertical_scroll == 10
    for i in range(40, 55):
        console._buffer_insert_text(buf, f'T{i}\n')
    assert window.vertical_scroll == 10
    assert console._pinned_abs_top[buf] == 10


def test_f5_follow_tail_pins_absolute_across_trim():
    """F5 while follow-tail: vertical_scroll is often stale 0 (unrendered).

    Paused appends must not trim away the pin. A forced trim still keeps the
    same absolute top (relative scroll shrinks with the offset).
    """
    console = Console(DemoConnector(delay=10), history_file=None, max_lines=40)
    buf = console.terminal_buffer
    window = console.terminal_window.window
    for i in range(40):
        console._buffer_insert_text(buf, f'L{i}\n')
    buf.cursor_position = len(buf.text)
    assert window.vertical_scroll == 0
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    console.state.paused_appended = 0
    console._pin_viewports()
    abs_top = console._pinned_abs_top[buf]
    assert abs_top > 0, abs_top
    expected = f'L{abs_top}'
    assert buf.text.splitlines()[window.vertical_scroll] == expected
    for i in range(40, 45):
        console._buffer_insert_text(buf, f'L{i}\n')
    assert console._line_offset[buf] == 0  # no trim while paused
    assert buf.text.count('\n') == 45
    console._trim_oldest_lines(buf, force=True)
    assert console._line_offset[buf] == 5
    assert console._pinned_abs_top[buf] == abs_top
    assert window.vertical_scroll == abs_top - 5
    assert buf.text.splitlines()[window.vertical_scroll] == expected


def test_f5_follow_tail_log_pins_absolute_across_trim():
    console = Console(DemoConnector(delay=10), history_file=None, max_lines=40)
    buf = console.logger_buffer
    window = console.logger_window.window
    for i in range(40):
        console._append_log_line(f'# {i}.0 <I> line {i}\n')
    buf.cursor_position = len(buf.text)
    assert window.vertical_scroll == 0
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    console._pin_viewports()
    abs_top = console._pinned_abs_top[buf]
    assert abs_top > 0
    gutter_before = console._log_view_abs_line_nos[window.vertical_scroll]
    for i in range(40, 45):
        console._append_log_line(f'# {i}.0 <I> line {i}\n')
    assert console._line_offset[buf] == 0
    assert len(console._log_lines) == 45
    dropped = console._trim_log_line_list(force=True)
    assert dropped > 0
    console._rebuild_log_view()
    assert console._line_offset[buf] == 5
    gutter_after = console._log_view_abs_line_nos[window.vertical_scroll]
    assert gutter_after == gutter_before
    assert console._pinned_abs_top[buf] == abs_top


def test_paused_trim_clamps_when_pin_fully_trimmed_away():
    console = Console(DemoConnector(delay=10), history_file=None, max_lines=20)
    buf = console.terminal_buffer
    window = console.terminal_window.window
    for i in range(25):
        console._buffer_insert_text(buf, f'L{i}\n')
    buf.cursor_position = len(buf.text)
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    console._pin_viewports()
    # Pin early content; paused appends keep it (no trim).
    console._pinned_abs_top[buf] = console._line_offset.get(buf, 0)
    console._apply_pinned_viewport(buf)
    pinned = console._pinned_abs_top[buf]
    offset_at_pause = console._line_offset[buf]
    n_at_pause = buf.text.count('\n')
    for i in range(25, 60):
        console._buffer_insert_text(buf, f'L{i}\n')
    # No further trim while paused (offset frozen; buffer grew).
    assert console._line_offset[buf] == offset_at_pause
    assert buf.text.count('\n') == n_at_pause + 35
    # Forced wipe of the pin → clamp to oldest remaining.
    console._trim_oldest_lines(buf, force=True)
    assert console._line_offset[buf] > pinned
    assert window.vertical_scroll == 0
    assert console._pinned_abs_top[buf] == console._line_offset[buf]
    assert buf.text.splitlines()[0].startswith('L')


def test_paused_does_not_trim_past_max_lines():
    """Pause freezes scrollback; cap is enforced on resume."""
    console = Console(DemoConnector(delay=10), history_file=None, max_lines=30)
    buf = console.terminal_buffer
    for i in range(30):
        console._buffer_insert_text(buf, f'T{i}\n')
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    console._pin_viewports()
    for i in range(30, 80):
        console._buffer_insert_text(buf, f'T{i}\n')
    assert buf.text.count('\n') == 80
    assert console._line_offset[buf] == 0
    # Resume path trims back to the cap.
    console.state.scroll_to_end = True
    console._clear_pinned_scroll()
    console._trim_all_panes_to_cap()
    assert buf.text.count('\n') == 30
    assert console._line_offset[buf] == 50
