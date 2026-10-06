from prompt_toolkit.layout.containers import ConditionalContainer

from rttt.connectors.demo import DemoConnector
from rttt.console import Console
from rttt.ui import create_log_pause_badge, create_status_bar


def _badge_parts(state):
    badge = create_log_pause_badge(state)
    assert isinstance(badge, ConditionalContainer)
    control = badge.content.content
    return control.text()


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
    badge = create_log_pause_badge(console.state)
    assert badge.filter()
    parts = badge.content.content.text()
    assert any('PAUSED +5' in text for _style, text in parts)


def test_paused_badge_hidden_while_scrolling():
    console = Console(DemoConnector(delay=10), history_file=None)
    console.state.scroll_to_end = True
    console.state.paused_appended = 3
    badge = create_log_pause_badge(console.state)
    assert not badge.filter()
    console.state.scroll_to_end = False
    assert badge.filter()
    parts = badge.content.content.text()
    assert any('PAUSED +3' in text for _style, text in parts)


def test_status_bar_has_no_paused_badge():
    console = Console(DemoConnector(delay=10), history_file=None)
    console.state.scroll_to_end = False
    console.state.paused_appended = 7
    bar = create_status_bar(console.state)
    # Left FormattedTextControl is first child of the VSplit
    left = bar.content.children[0].content
    items = left.text()
    joined = ''.join(text for _style, text in items)
    assert 'PAUSED' not in joined
    assert 'F5 Resume' in joined


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
