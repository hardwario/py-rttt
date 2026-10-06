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
