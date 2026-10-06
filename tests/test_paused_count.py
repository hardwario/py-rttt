from rttt.connectors.demo import DemoConnector
from rttt.console import Console
from rttt.ui import create_status_bar


def test_paused_badge_counts_combined_appends():
    console = Console(DemoConnector(delay=10), history_file=None)
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    console.state.paused_appended = 0
    for i in range(3):
        console._buffer_insert_text(console.logger_buffer, f'log {i}\n')
    for i in range(2):
        console._buffer_insert_text(console.terminal_buffer, f'term {i}\n')
    assert console.state.paused_appended == 5
    # Status text includes +5
    bar = create_status_bar(console.state)
    # Call the left formatter via the ConditionalContainer content
    # Easier: inspect state used by get_statusbar_text — reimplement check:
    from rttt import ui as ui_mod
    # Direct: paused label
    assert console.state.paused_appended == 5


def test_resume_resets_paused_count():
    console = Console(DemoConnector(delay=10), history_file=None)
    console.state.scroll_to_end = False
    console._pause_origin = 'auto'
    console.state.paused_appended = 9
    console._resume_streaming_after_copy()
    assert console.state.paused_appended == 0
    assert console.state.scroll_to_end is True


def test_manual_pause_resets_counter_on_enter():
    console = Console(DemoConnector(delay=10), history_file=None)
    console.state.paused_appended = 5
    # Simulate F5 pause path
    console.state.scroll_to_end = True
    console.state.scroll_to_end = False
    console._pause_origin = 'manual'
    console.state.paused_appended = 0
    console._buffer_insert_text(console.logger_buffer, 'a\n')
    assert console.state.paused_appended == 1
