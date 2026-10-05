"""System clipboard for the TUI: OSC 52 (SSH-safe) plus optional pyperclip."""

from __future__ import annotations

import base64
import os
import sys
from typing import Callable, Optional

from loguru import logger
from prompt_toolkit.clipboard.base import Clipboard, ClipboardData
from prompt_toolkit.selection import SelectionType

# OSC 52 payloads grow by ~4/3 in base64; many terminals (tmux, VTE) truncate
# around 64–100 KB of base64. Cap the source text so the sequence stays small.
OSC52_MAX_CHARS = 60000

# Optional sink for tests: called with the raw escape sequence that would be
# written to the tty. Production leaves this None and writes via the app.
_EmitFn = Callable[[str], None]


def _in_tmux() -> bool:
    return bool(os.environ.get('TMUX'))


def _wrap_tmux(seq: str) -> str:
    """DCS passthrough so tmux forwards OSC 52 to the outer terminal."""
    # Double each ESC inside the payload, then wrap: DCS tmux ; <seq> ST
    escaped = seq.replace('\x1b', '\x1b\x1b')
    return f'\x1bPtmux;{escaped}\x1b\\'


def build_osc52_sequence(text: str, *, tmux: Optional[bool] = None) -> str:
    """Return the OSC 52 set-clipboard sequence for *text* (BEL-terminated)."""
    payload = base64.b64encode(text.encode('utf-8')).decode('ascii')
    seq = f'\x1b]52;c;{payload}\a'
    if tmux is None:
        tmux = _in_tmux()
    if tmux:
        seq = _wrap_tmux(seq)
    return seq


def prefer_pyperclip() -> bool:
    """True when a local GUI clipboard is more likely to work than only OSC 52.

    pyperclip talks to the machine where the process runs. Over SSH that is
    usually the remote host with no display, so we skip it there and rely on
    OSC 52 reaching the user's terminal instead.
    """
    if os.environ.get('SSH_CONNECTION') or os.environ.get('SSH_CLIENT'):
        return False
    if sys.platform == 'darwin' or sys.platform.startswith('win'):
        return True
    return bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


class HybridClipboard(Clipboard):
    """In-memory clipboard that mirrors to the terminal (OSC 52) and pyperclip.

    Inspired by euporie's Osc52Clipboard and its PyperclipClipboard that
    swallows PyperclipException (prompt_toolkit #1412).
    """

    def __init__(
        self,
        *,
        emit: Optional[_EmitFn] = None,
        use_pyperclip: Optional[bool] = None,
        max_chars: int = OSC52_MAX_CHARS,
    ) -> None:
        self._data = ClipboardData()
        self._emit = emit
        self._use_pyperclip = prefer_pyperclip() if use_pyperclip is None else use_pyperclip
        self._max_chars = max_chars
        # Last set_data outcome for UI feedback: 'ok', 'truncated', 'empty'.
        self.last_status = 'ok'
        self.last_error = ''

    def set_data(self, data: ClipboardData) -> None:
        self._data = data
        text = data.text or ''
        if not text:
            self.last_status = 'empty'
            self.last_error = ''
            return

        osc_ok = self._emit_osc52(text)
        clip_ok = self._emit_pyperclip(text) if self._use_pyperclip else False

        if osc_ok or clip_ok:
            self.last_status = 'ok'
            self.last_error = ''
        else:
            self.last_status = 'failed'
            self.last_error = (
                'Clipboard unavailable (try OSC 52 / tmux set-clipboard on)'
            )

    def set_text(self, text: str) -> None:
        kind = SelectionType.LINES if '\n' in text else SelectionType.CHARACTERS
        self.set_data(ClipboardData(text, kind))

    def get_data(self) -> ClipboardData:
        return self._data

    def _emit_osc52(self, text: str) -> bool:
        if len(text) > self._max_chars:
            logger.warning(
                f'OSC 52 clipboard: truncating {len(text)} chars to {self._max_chars}'
            )
            text = text[: self._max_chars]
        try:
            seq = build_osc52_sequence(text)
            if self._emit is not None:
                self._emit(seq)
                return True
            return self._write_to_app_output(seq)
        except Exception as e:
            logger.debug(f'OSC 52 clipboard failed: {e}')
            return False

    def _write_to_app_output(self, seq: str) -> bool:
        try:
            from prompt_toolkit.application.current import get_app

            output = get_app().output
        except Exception:
            return False
        try:
            write_raw = getattr(output, 'write_raw', None)
            if write_raw is not None:
                write_raw(seq)
            else:
                output.write(seq)
            output.flush()
            return True
        except Exception as e:
            logger.debug(f'OSC 52 write failed: {e}')
            return False

    def _emit_pyperclip(self, text: str) -> bool:
        try:
            import pyperclip
        except ImportError:
            return False
        try:
            pyperclip.copy(text)
            return True
        except pyperclip.PyperclipException as e:
            # Never raise — see prompt_toolkit #1412.
            logger.debug(f'pyperclip failed: {e}')
            return False
        except Exception as e:
            logger.debug(f'pyperclip failed: {e}')
            return False
