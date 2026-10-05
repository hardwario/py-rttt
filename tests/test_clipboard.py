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
