from datetime import datetime

from rttt.connectors.base import Connector
from rttt.connectors.substitution import SubstitutionMiddleware
from rttt.event import Event, EventType


class CaptureConnector(Connector):
    def __init__(self):
        super().__init__()
        self.events: list[Event] = []

    def open(self):
        pass

    def close(self):
        pass

    def handle(self, event: Event):
        self.events.append(event)


def expand(text: str, substitutions: dict | None = None) -> str:
    capture = CaptureConnector()
    middleware = SubstitutionMiddleware(capture, substitutions=substitutions)
    middleware.handle(Event(EventType.IN, text))
    return capture.events[-1].data


def test_utc_now_default_format():
    result = expand('rtc set {{UTC_NOW}}')
    assert result.startswith('rtc set ')
    stamp = result[len('rtc set '):]
    datetime.strptime(stamp, '%Y/%m/%d %H:%M:%S')


def test_utc_now_custom_format():
    result = expand('{{UTC_NOW:%Y-%m-%d}}')
    datetime.strptime(result, '%Y-%m-%d')


def test_local_now_custom_format():
    result = expand('{{LOCAL_NOW:%H:%M:%S}}')
    datetime.strptime(result, '%H:%M:%S')


def test_unix_now_is_integer_string():
    result = expand('{{UNIX_NOW}}')
    assert result.isdigit()


def test_unknown_placeholder_left_intact():
    assert expand('x={{NEZNAMY}}') == 'x={{NEZNAMY}}'


def test_custom_nested_expansion():
    assert expand('{{A}}', {'A': 'x{{B}}', 'B': 'y'}) == 'xy'


def test_cycle_detection_keeps_placeholder():
    result = expand('{{A}}', {'A': '{{B}}', 'B': '{{A}}'})
    assert '{{A}}' in result


def test_custom_overrides_builtin():
    assert expand('{{UTC_NOW}}', {'UTC_NOW': 'fixed'}) == 'fixed'


def test_strftime_format_is_passed_through():
    # strftime's handling of unknown directives (e.g. %Q) is platform-specific.
    # We only care that the call doesn't crash — whatever the OS returns is fine.
    result = expand('{{UTC_NOW:%Y}}')
    assert len(result) == 4 and result.isdigit()


def test_non_in_events_pass_through_untouched():
    capture = CaptureConnector()
    middleware = SubstitutionMiddleware(capture)
    middleware.handle(Event(EventType.OUT, '{{UTC_NOW}}'))
    middleware.handle(Event(EventType.LOG, '{{UTC_NOW}}'))
    assert capture.events[0].data == '{{UTC_NOW}}'
    assert capture.events[1].data == '{{UTC_NOW}}'


def test_multiple_placeholders_in_one_line():
    result = expand('{{A}} and {{B}}', {'A': '1', 'B': '2'})
    assert result == '1 and 2'


def test_empty_substitutions_still_resolves_builtins():
    result = expand('{{UNIX_NOW}}', {})
    assert result.isdigit()


def test_shell_substitution_single_line():
    result = expand('{{GIT}}', {'GIT': {'shell': "echo hello"}})
    assert result == 'hello'


def test_shell_substitution_multiline_default_first_line():
    result = expand('{{M}}', {'M': {'shell': "printf 'a\\nb\\nc\\n'"}})
    assert result == 'a'


def test_shell_substitution_multiline_opt_in_produces_multiple_events():
    capture = CaptureConnector()
    middleware = SubstitutionMiddleware(
        capture,
        substitutions={'M': {'shell': "printf 'a\\nb\\nc\\n'", 'multiline': True}},
    )
    middleware.handle(Event(EventType.IN, '{{M}}'))
    assert [e.data for e in capture.events] == ['a', 'b', 'c']


def test_shell_substitution_failure_keeps_placeholder():
    result = expand('{{X}}', {'X': {'shell': "exit 1"}})
    assert result == '{{X}}'


def test_shell_substitution_can_be_referenced_by_another():
    result = expand(
        '{{HEADER}}',
        {
            'GIT': {'shell': "echo a1b2"},
            'HEADER': 'sha={{GIT}}',
        },
    )
    assert result == 'sha=a1b2'


def test_shell_substitution_respects_cwd(tmp_path):
    (tmp_path / 'marker.txt').write_text('ok\n')
    result = expand(
        '{{LS}}',
        {'LS': {'shell': 'ls marker.txt', 'cwd': str(tmp_path)}},
    )
    assert result == 'marker.txt'


def test_expanded_text_with_newlines_fans_out_to_multiple_events():
    capture = CaptureConnector()
    middleware = SubstitutionMiddleware(
        capture,
        substitutions={'BLOCK': 'line1\nline2\nline3'},
    )
    middleware.handle(Event(EventType.IN, '{{BLOCK}}'))
    assert [e.data for e in capture.events] == ['line1', 'line2', 'line3']
