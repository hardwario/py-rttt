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
    # Cross hysteresis (55) so a trim fires.
    for i in range(40, 56):
        console._buffer_insert_text(buf, f'log {i}\n')
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
    # 25 → 34 crosses 33 (=30*1.1) → trim to 30.
    for i in range(25, 34):
        console._buffer_insert_text(buf, f'row {i}\n')
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
    for i in range(15, 23):  # cross 22 → trim
        console._buffer_insert_text(buf, f'new {i}\n')
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
    # Cross 22 → drop = 23-20 = 3 on first trim; scroll 8→5.
    for i in range(18, 23):
        console._buffer_insert_text(buf, f'L{i}\n')
    assert buf.text.count('\n') == 20
    assert window.vertical_scroll == 5


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
