"""PTY integration tests for mouse select-to-copy (realistic terminal I/O).

These spawn ``rttt --demo`` under a pseudo-terminal, render with pyte, inject
xterm SGR mouse sequences, and decode OSC 52 clipboard payloads from the PTY
output. They catch event-flow bugs that unit tests calling internal handlers
directly cannot see.

Skipped automatically when ``pty`` is unavailable (e.g. Windows CI). A green
run here still does not replace a real-terminal GUI checklist for mouse work.
"""

from __future__ import annotations

import base64
import os
import re
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip('pexpect')
pytest.importorskip('pyte')
pytest.importorskip('pty')

import pexpect
import pyte

COLS = 120
ROWS = 40
REPO = Path(__file__).resolve().parents[1]
OSC52_RE = re.compile(rb'\x1b\]52;c;([A-Za-z0-9+/=]+)\x07')

# Skip the whole module where a controlling PTY cannot be opened.
if not hasattr(os, 'openpty'):
    pytest.skip('no os.openpty on this platform', allow_module_level=True)


def _sgr(button: int, x: int, y: int, *, down: bool = True) -> bytes:
    """1-based xterm SGR mouse report."""
    return f'\x1b[<{button};{x};{y}{"M" if down else "m"}'.encode()


class RtttPty:
    """Drive a live ``rttt --demo`` session and observe screen + OSC 52."""

    def __init__(self, tmp_path: Path):
        self.cols = COLS
        self.rows = ROWS
        self.screen = pyte.Screen(self.cols, self.rows)
        self.stream = pyte.Stream(self.screen)
        self.osc_copies: list[str] = []
        env = os.environ.copy()
        env.update({
            'TERM': 'xterm-256color',
            'COLUMNS': str(self.cols),
            'LINES': str(self.rows),
            # Prefer OSC 52 so copies appear in the PTY byte stream.
            'SSH_CONNECTION': '127.0.0.1 1 127.0.0.1 2',
            'DISPLAY': '',
        })
        env.pop('TMUX', None)
        env.pop('WAYLAND_DISPLAY', None)
        # Avoid loading the developer's ~/.rttt.yaml substitutions / MCP.
        env['HOME'] = str(tmp_path)

        cmd = (
            f'{sys.executable} -m rttt --demo --no-mcp --no-substitutions '
            f'--console-file {tmp_path / "console"} '
            f'--history-file {tmp_path / "history"}'
        )
        self.child = pexpect.spawn(
            cmd,
            cwd=str(REPO),
            env=env,
            dimensions=(self.rows, self.cols),
            encoding=None,
            timeout=20,
        )

    def close(self):
        if self.child is None:
            return
        try:
            if self.child.isalive():
                self.child.send(b'\x11')  # Ctrl-Q
                self.pump(0.4)
        except Exception:
            pass
        try:
            self.child.close(force=True)
        except Exception:
            pass
        self.child = None

    def feed(self, data: bytes):
        for m in OSC52_RE.finditer(data):
            self.osc_copies.append(base64.b64decode(m.group(1)).decode())
        if b'\x1b[6n' in data:
            self.child.send(f'\x1b[{self.rows};{self.cols}R'.encode())
        cleaned = OSC52_RE.sub(b'', data)
        self.stream.feed(cleaned.decode('utf-8', errors='ignore'))

    def pump(self, seconds: float = 0.3) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            try:
                data = self.child.read_nonblocking(size=65536, timeout=0.05)
                if data:
                    self.feed(data)
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                return False
        return True

    def rows_text(self) -> list[str]:
        return [
            ''.join(self.screen.buffer[r][c].data for c in range(self.cols))
            for r in range(self.rows)
        ]

    def status(self) -> str:
        return self.rows_text()[self.rows - 1]

    def toast_area(self) -> str:
        """Text in the floating-toast band just above the status bar.

        The herdr-style Float is height 3 with bottom=1, so it occupies the
        three rows above the status line (clock stays on the status row).
        """
        rows = self.rows_text()
        return ''.join(rows[max(0, self.rows - 5): self.rows - 1])

    def paused_visible(self) -> bool:
        """True when the Log-pane PAUSED badge is on screen."""
        return any('PAUSED' in row for row in self.rows_text())

    def find_in_pane(self, needle: str, pane: str) -> tuple[int, int] | None:
        """Return 0-based (row, col) of needle in the Log or Terminal pane."""
        for r, line in enumerate(self.rows_text()):
            if pane == 'log':
                region, offset = line[60:], 60
            elif pane == 'term':
                region, offset = line[:60], 0
            else:
                region, offset = line, 0
            c = region.find(needle)
            if c >= 0:
                return r, offset + c
        return None

    def list_pane_lines(self, pane: str, prefix: str) -> list[tuple[int, int, str]]:
        out = []
        for r, line in enumerate(self.rows_text()):
            region = line[60:] if pane == 'log' else line[:60]
            offset = 60 if pane == 'log' else 0
            m = re.search(rf'({re.escape(prefix)} \d+)', region)
            if m:
                out.append((r, offset + region.find(m.group(1)), m.group(1)))
        return out

    def wait_for_lines(self, pane: str, prefix: str, count: int, timeout: float = 12.0):
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.2)
            if len(self.list_pane_lines(pane, prefix)) >= count:
                return
        raise AssertionError(
            f'timed out waiting for {count} {prefix!r} lines in {pane}; '
            f'status={self.status()!r}'
        )

    def drag(
        self,
        r0: int,
        c0: int,
        r1: int,
        c1: int,
        *,
        steps: int = 6,
        settle: float = 0.5,
        burst: bool = False,
    ):
        """Drag from 0-based screen (r0,c0) to (r1,c1). Inclusive end cell."""
        x0, y0, x1, y1 = c0 + 1, r0 + 1, c1 + 1, r1 + 1
        parts = [_sgr(0, x0, y0, down=True)]
        for i in range(1, steps + 1):
            t = i / steps
            x = int(x0 + (x1 - x0) * t)
            y = int(y0 + (y1 - y0) * t)
            parts.append(_sgr(32, x, y, down=True))
        parts.append(_sgr(0, x1, y1, down=False))
        if burst:
            self.child.send(b''.join(parts))
            self.pump(settle)
            return
        self.child.send(parts[0])
        self.pump(0.03)
        for p in parts[1:-1]:
            self.child.send(p)
            self.pump(0.03)
        self.child.send(parts[-1])
        self.pump(settle)

    def plain_click(self, r: int, c: int):
        x, y = c + 1, r + 1
        self.child.send(_sgr(0, x, y, down=True))
        self.pump(0.05)
        self.child.send(_sgr(0, x, y, down=False))
        self.pump(0.35)

    def right_click(self, r: int, c: int):
        """SGR right button (button=2) down/up at 0-based screen coords."""
        x, y = c + 1, r + 1
        self.child.send(_sgr(2, x, y, down=True))
        self.pump(0.05)
        self.child.send(_sgr(2, x, y, down=False))
        self.pump(0.45)

    def press_f5(self):
        self.child.send(b'\x1b[15~')
        self.pump(0.35)

    def press_shift_up(self, times: int = 1):
        for _ in range(times):
            self.child.send(b'\x1b[1;2A')
            self.pump(0.08)
        self.pump(0.25)

    def press_ctrl_c(self):
        self.child.send(b'\x03')
        self.pump(0.45)


@pytest.fixture
def pty_app(tmp_path):
    app = RtttPty(tmp_path)
    try:
        app.wait_for_lines('log', 'log', 4)
        app.pump(0.3)
        yield app
    finally:
        app.close()


def test_first_drag_in_log_copies_from_press_to_release(pty_app):
    app = pty_app
    a = app.find_in_pane('log 4', 'log')
    b = app.find_in_pane('log 8', 'log')
    assert a and b, (a, b, app.rows_text()[1:8])
    n = len(app.osc_copies)
    app.drag(a[0], a[1], b[0], b[1] + len('log 8') - 1)
    assert app.osc_copies[n:], 'first drag after start copied nothing'
    text = app.osc_copies[n]
    assert text.startswith('log 4'), text
    assert 'log 8' in text, text
    assert 'term' not in text, text
    assert 'Copied' in app.toast_area()
    assert 'resumed' not in app.toast_area()
    assert not app.paused_visible()


def test_first_drag_in_terminal_after_log_copies(pty_app):
    app = pty_app
    # Prime focus on Log first.
    a = app.find_in_pane('log 4', 'log')
    b = app.find_in_pane('log 6', 'log')
    assert a and b
    app.drag(a[0], a[1], b[0], b[1] + 4)
    app.pump(0.6)
    terms = app.list_pane_lines('term', 'term')
    assert len(terms) >= 3, terms
    n = len(app.osc_copies)
    t0, t1 = terms[0], terms[2]
    app.drag(t0[0], t0[1], t1[0], t1[1] + len(t1[2]) - 1)
    assert app.osc_copies[n:], 'first Terminal drag after Log copied nothing'
    text = app.osc_copies[n]
    assert text.startswith(t0[2]), text
    assert t1[2] in text, text
    assert 'log' not in text, text


def test_second_drag_does_not_use_previous_endpoint(pty_app):
    app = pty_app
    app.press_f5()  # freeze view for stable coordinates
    assert app.paused_visible()
    logs = app.list_pane_lines('log', 'log')
    assert len(logs) >= 4, logs
    n = len(app.osc_copies)
    a, b = logs[0], logs[1]
    app.drag(a[0], a[1], b[0], b[1] + len(b[2]) - 1)
    first = app.osc_copies[n:]
    assert first and first[0].startswith(a[2]), first

    n = len(app.osc_copies)
    c, d = logs[2], logs[3]
    app.drag(c[0], c[1], d[0], d[1] + len(d[2]) - 1)
    second = app.osc_copies[n:]
    assert second, 'second drag copied nothing'
    assert second[0].startswith(c[2]), (
        f'second drag must start at press ({c[2]}), got {second[0]!r}; '
        f'first was {first[0]!r}'
    )
    assert d[2] in second[0], second
    # Must not be "previous release → current release" (b → d).
    assert not second[0].startswith(b[2]), second


def test_auto_pause_copy_resumes_scrolling(pty_app):
    app = pty_app
    # Ensure streaming is on.
    if app.paused_visible():
        app.press_f5()
    logs = app.list_pane_lines('log', 'log')
    assert len(logs) >= 3
    n = len(app.osc_copies)
    a, b = logs[0], logs[2]
    app.drag(a[0], a[1], b[0], b[1] + len(b[2]) - 1)
    assert app.osc_copies[n:]
    toast = app.toast_area()
    assert 'Copied' in toast, toast
    assert 'resumed' not in toast, toast
    assert 'PAUSED' not in ''.join(app.rows_text()), toast


def test_manual_f5_drag_stays_paused(pty_app):
    app = pty_app
    app.press_f5()
    assert app.paused_visible()
    logs = app.list_pane_lines('log', 'log')
    assert len(logs) >= 3
    n = len(app.osc_copies)
    a, b = logs[0], logs[2]
    app.drag(a[0], a[1], b[0], b[1] + len(b[2]) - 1)
    assert app.osc_copies[n:], 'expected a copy while manually paused'
    toast = app.toast_area()
    assert 'PAUSED' in ''.join(app.rows_text()), toast
    assert 'resumed' not in toast, toast
    assert 'resumed' not in app.status()


def test_plain_click_does_not_copy_or_pause(pty_app):
    app = pty_app
    if app.paused_visible():
        app.press_f5()
    pos = app.find_in_pane('log 4', 'log') or app.list_pane_lines('log', 'log')[0][:2]
    n = len(app.osc_copies)
    app.plain_click(pos[0], pos[1])
    assert len(app.osc_copies) == n
    assert not app.paused_visible()


def test_burst_drag_still_copies_exact_span(pty_app):
    """xdotool-like burst of SGR events (no inter-event pump)."""
    app = pty_app
    app.press_f5()
    logs = app.list_pane_lines('log', 'log')
    assert len(logs) >= 3
    a, b = logs[0], logs[2]
    n = len(app.osc_copies)
    app.drag(
        a[0], a[1], b[0], b[1] + len(b[2]) - 1,
        steps=5, burst=True, settle=0.55,
    )
    assert app.osc_copies[n:], 'burst drag copied nothing'
    text = app.osc_copies[n]
    assert text.startswith(a[2]), text
    assert b[2] in text, text


def test_right_click_copies_after_left_drag(pty_app):
    """Left-drag select (manual pause) then RMB copies again via SGR button 2."""
    app = pty_app
    app.press_f5()
    assert app.paused_visible()
    logs = app.list_pane_lines('log', 'log')
    assert len(logs) >= 3
    a, b = logs[0], logs[2]
    n = len(app.osc_copies)
    app.drag(a[0], a[1], b[0], b[1] + len(b[2]) - 1)
    assert app.osc_copies[n:], 'setup left-drag must copy'
    first = app.osc_copies[n]
    # Highlight sticks while manually paused — right-click should copy again.
    n = len(app.osc_copies)
    app.right_click(a[0], a[1] + 1)
    # Either a fresh OSC 52 write, or at least a Copied toast (re-toast path).
    toast = app.toast_area()
    assert 'Copied' in toast or app.osc_copies[n:], (toast, app.osc_copies[n:])
    assert app.paused_visible(), 'right-click must not resume'
    if app.osc_copies[n:]:
        assert a[2] in app.osc_copies[n] or first == app.osc_copies[n]


def test_right_click_without_selection_does_not_pause(pty_app):
    app = pty_app
    if app.paused_visible():
        app.press_f5()
    pos = app.find_in_pane('log 4', 'log') or app.list_pane_lines('log', 'log')[0][:2]
    n = len(app.osc_copies)
    app.right_click(pos[0], pos[1])
    assert not app.paused_visible()
    # May re-toast a prior copy from an earlier test fixture state — just no pause.
    _ = n  # keep prior count unused; toast path is fine


def test_shift_up_extends_selection_then_ctrl_c_copies(pty_app):
    """Shift-Up (CSI 1;2A) grows a keyboard selection; Ctrl-C copies it."""
    app = pty_app
    app.press_f5()  # stable coords + manual pause
    assert app.paused_visible()
    logs = app.list_pane_lines('log', 'log')
    assert len(logs) >= 3, logs
    # Click near the bottom of the visible log so Shift-Up has room to grow.
    r, c, sample = logs[-1]
    app.plain_click(r, c + min(3, max(0, len(sample) - 1)))
    n = len(app.osc_copies)
    app.press_shift_up(3)
    app.press_ctrl_c()
    assert app.osc_copies[n:], 'Shift-Up + Ctrl-C copied nothing'
    # Manual F5 pause must survive keyboard copy.
    assert app.paused_visible()
    assert 'resumed' not in app.toast_area()
    assert 'resumed' not in app.status()

def test_gutter_press_drag_copies_from_press_line(pty_app):
    """Press on the line-number gutter then drag — copy starts on that line."""
    app = pty_app
    app.press_f5()
    assert app.paused_visible()
    logs = app.list_pane_lines('log', 'log')
    assert len(logs) >= 4, logs
    a, b = logs[0], logs[2]
    n = len(app.osc_copies)
    # Log text starts after "││ N " — press on the digit (gutter), not the text.
    # Screen looks like: …││ 1 log 2…  so gutter x is a few cells left of text.
    gutter_x = a[1] - 2
    assert gutter_x > 60, (gutter_x, a)
    app.drag(a[0], gutter_x, b[0], b[1] + len(b[2]) - 1)
    assert app.osc_copies[n:], 'gutter-origin drag copied nothing'
    text = app.osc_copies[n]
    assert a[2] in text, (text, a[2])
    assert b[2] in text, text
    # Must not be a huge tail from the scroll tip.
    assert text.count('\n') < 15, text

def test_drag_past_pane_edge_extends_selection(pty_app):
    """Drag from mid-pane down past the pane — selection still grows/copies."""
    app = pty_app
    app.press_f5()
    assert app.paused_visible()
    logs = app.list_pane_lines('log', 'log')
    assert len(logs) >= 3, logs
    a = logs[0]
    n = len(app.osc_copies)
    # Start on first visible log line text; drag below the log pane into the
    # status / frame fringe so edge-scroll capture extends the selection.
    r0, c0 = a[0], a[1]
    app.drag(r0, c0, min(app.rows - 1, r0 + 20), c0 + 8, steps=10, settle=0.8)
    assert app.osc_copies[n:], 'edge-scroll drag copied nothing'
    text = app.osc_copies[n]
    assert text.startswith(a[2]) or a[2] in text, text
    assert text.count('\n') >= 1, text
