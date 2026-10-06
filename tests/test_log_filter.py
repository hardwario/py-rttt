"""Log filter (F7) view over the capped raw log."""

from rttt.connectors.demo import DemoConnector
from rttt.console import Console


def _fill_levels(console):
    for i, lvl in enumerate(['D', 'I', 'W', 'E', 'I', 'W']):
        console._append_log_line(f'# {i}.0 <{lvl}> log {i} noise\n')


def test_filter_substring():
    console = Console(DemoConnector(delay=10), history_file=None)
    _fill_levels(console)
    console._apply_log_filter('noise')
    assert console.state.log_filter == 'noise'
    assert 'FILTER:' not in ''  # status checked separately
    text = console.logger_buffer.text
    assert text.count('\n') == 6
    console._apply_log_filter('log 1')
    assert 'log 1' in console.logger_buffer.text
    assert 'log 2' not in console.logger_buffer.text


def test_filter_level_wrn_shows_wrn_and_err():
    console = Console(DemoConnector(delay=10), history_file=None)
    _fill_levels(console)
    console._apply_log_filter('level:wrn')
    text = console.logger_buffer.text
    assert '<W>' in text and '<E>' in text
    assert '<D>' not in text and '<I>' not in text


def test_filter_regex():
    console = Console(DemoConnector(delay=10), history_file=None)
    _fill_levels(console)
    console._apply_log_filter(r're:log [12]')
    text = console.logger_buffer.text
    assert 'log 1' in text and 'log 2' in text
    assert 'log 3' not in text


def test_filter_live_append():
    console = Console(DemoConnector(delay=10), history_file=None)
    console._apply_log_filter('level:err')
    console._append_log_line('# 1.0 <I> skip\n')
    console._append_log_line('# 2.0 <E> keep\n')
    assert 'keep' in console.logger_buffer.text
    assert 'skip' not in console.logger_buffer.text


def test_clear_filter_restores_all():
    console = Console(DemoConnector(delay=10), history_file=None)
    _fill_levels(console)
    console._apply_log_filter('level:err')
    assert console.logger_buffer.text.count('\n') == 1
    console._clear_log_filter()
    assert console.logger_buffer.text.count('\n') == 6
    assert console.state.log_filter == ''


def test_status_shows_filter():
    from rttt.ui import create_status_bar
    console = Console(DemoConnector(delay=10), history_file=None)
    console.state.log_filter = 'level:wrn'
    # Invoke left formatter via creating status bar internals
    bar = create_status_bar(console.state)
    # Walk to FormattedTextControl
    vs = bar.content
    left = vs.children[0]
    text = left.content.text()
    joined = ''.join(t for _, t in text)
    assert 'FILTER: level:wrn' in joined


def test_filter_gutter_keeps_absolute_line_numbers():
    console = Console(DemoConnector(delay=10), history_file=None)
    _fill_levels(console)
    # Absolute numbers 1..6 for the six seeded lines.
    assert console._log_view_abs_line_nos == [1, 2, 3, 4, 5, 6]
    console._apply_log_filter('level:wrn')
    # W at raw index 2 (abs 3), E at 3 (abs 4), W at 5 (abs 6).
    assert console._log_view_abs_line_nos == [3, 4, 6]
    # Margin callback matches.
    margin = console.logger_window.window.left_margins[0]
    assert margin.get_line_number(0) == 3
    assert margin.get_line_number(1) == 4
    assert margin.get_line_number(2) == 6


def test_filter_open_refocuses_input():
    """F7 must land focus on the filter so the first typed char is kept."""
    import asyncio
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    async def _run():
        console = Console(DemoConnector(delay=10), history_file=None)
        with create_pipe_input() as inp:
            app = console.app
            app.output = DummyOutput()
            app.input = inp

            async def interact():
                await asyncio.sleep(0)
                console._open_log_filter()
                # Allow the refocus background task a few turns.
                for _ in range(8):
                    await asyncio.sleep(0)
                    if console.has_focus(console.filter_field):
                        break
                assert console.has_focus(console.filter_field)
                inp.send_text('wrn')
                await asyncio.sleep(0.05)
                assert console.filter_field.buffer.text == 'wrn'
                app.exit()

            app.create_background_task(interact())
            await app.run_async()

    asyncio.run(_run())


def test_filtered_paused_append_keeps_absolute_top_without_trim():
    """Filter + F5: rebuild-on-append must not drift the pinned absolute top."""
    console = Console(DemoConnector(delay=10), history_file=None, max_lines=0)
    for i in range(80):
        lvl = ['I', 'W', 'E', 'D'][i % 4]
        console._append_log_line(f'# {i}.0 <{lvl}> line {i}\n')
    console._apply_log_filter('level:wrn')
    buf = console.logger_buffer
    window = console.logger_window.window
    window.vertical_scroll = 5
    buf.cursor_position = buf.document.translate_row_col_to_index(5, 0)
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    console.state.paused_appended = 0
    console._pin_viewports()
    top_before = console._log_view_abs_line_nos[window.vertical_scroll]
    pin = console._pinned_abs_top[buf]
    content_before = buf.text.splitlines()[window.vertical_scroll]
    # Simulate Document defaulting to EOF before restore (pre-fix symptom).
    buf.cursor_position = len(buf.text)
    for i in range(80, 300):
        lvl = ['I', 'W', 'E', 'D'][i % 4]
        console._append_log_line(f'# {i}.0 <{lvl}> line {i}\n')
    assert console._line_offset.get(buf, 0) == 0
    assert window.vertical_scroll >= 0
    top_after = console._log_view_abs_line_nos[window.vertical_scroll]
    assert top_after == top_before, (top_before, top_after)
    assert console._pinned_abs_top[buf] == pin
    assert buf.text.splitlines()[window.vertical_scroll] == content_before
    # Cursor must stay off EOF so Window cannot chase the stream.
    assert buf.cursor_position < len(buf.text)


def test_filtered_view_gutter_skips_phantom_trailing_line():
    """Trailing newline makes Document.line_count = N+1; gutter must not number it."""
    console = Console(DemoConnector(delay=10), history_file=None)
    _fill_levels(console)
    console._apply_log_filter('level:wrn')
    nos = console._log_view_abs_line_nos
    assert nos == [3, 4, 6]
    buf = console.logger_buffer
    # Join of newline-terminated lines → phantom empty last Document row.
    assert buf.text.endswith('\n')
    assert buf.document.line_count == len(nos) + 1
    margin = console.logger_window.window.left_margins[0]
    assert margin.get_line_number(0) == 3
    assert margin.get_line_number(1) == 4
    assert margin.get_line_number(2) == 6
    assert margin.get_line_number(len(nos)) is None
    assert margin.get_line_number(len(nos) + 5) is None


def test_f7_key_focuses_filter_on_first_press():
    """F7 binding (not only _open_log_filter) must focus filter immediately."""
    import asyncio
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    async def _run():
        console = Console(DemoConnector(delay=10), history_file=None)
        with create_pipe_input() as inp:
            app = console.app
            app.output = DummyOutput()
            app.input = inp

            async def interact():
                try:
                    await asyncio.sleep(0)
                    # Focus Log first (the GUI race: F7 while Log focused).
                    app.layout.focus(console.logger_window)
                    await asyncio.sleep(0)
                    inp.send_text('\x1b[18~')  # F7
                    focused = False
                    for _ in range(20):
                        await asyncio.sleep(0.01)
                        if console.state.filter_editing and console.has_focus(
                                console.filter_field):
                            focused = True
                            break
                    assert focused, (
                        console.state.filter_editing,
                        app.layout.current_control,
                    )
                    inp.send_text('wrn')
                    await asyncio.sleep(0.05)
                    assert console.filter_field.buffer.text == 'wrn'
                finally:
                    app.exit()

            app.create_background_task(interact())
            await app.run_async()

    asyncio.run(_run())
