from prompt_toolkit.widgets import TextArea, SearchToolbar, Frame, HorizontalLine, ProgressBar, Box, Button
from prompt_toolkit.layout.containers import HSplit, VSplit, Window, WindowAlign, ConditionalContainer, FloatContainer, Float
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import LayoutDimension
from prompt_toolkit.layout.margins import NumberedMargin
from prompt_toolkit.formatted_text.base import StyleAndTextTuples
from typing import Callable
from prompt_toolkit.history import FileHistory
from datetime import datetime
import time
import asyncio
from prompt_toolkit.filters import Condition
from rttt.lexer import LogLexer


class State:

    SHOW_ALL = 0
    SHOW_TERMINAL = 1
    SHOW_LOGGER = 2

    def __init__(self):
        self.show_status_bar = True
        self.scroll_to_end = True
        self.show = self.SHOW_ALL
        self.app = None
        self.flash_visible = False
        self.flash_file = ""
        self.flash_action = ""
        self.flash_error = ""
        self.flash_bar = None
        # transport source -> {'status': ..., 'error': ...}, from CONN events
        self.conn = {}
        self.auto_reconnect = False
        # Set by Console so the dialog can reach the connector that owns the
        # transport; no-ops when the connector does not support reconnecting.
        self.on_reconnect = None
        self.on_auto_reconnect = None
        self.auto_reconnect_button = None
        # Ephemeral status toast (copy feedback, etc.); cleared by expiry or task.
        self.message = ''
        self.message_expires = 0.0
        # Lines appended to either pane while paused (combined); Log pause badge.
        self.paused_appended = 0
        # Log filter expression (None/'' = show all); status shows FILTER: …
        self.log_filter = ''
        self.filter_editing = False

    def show_message(self, text, seconds=2.0):
        """Show an ephemeral toast on the right of the status bar (replaces clock)."""
        self.message = text or ''
        self.message_expires = time.monotonic() + seconds if text else 0.0
        if not self.app:
            return
        self.app.invalidate()
        # Schedule auto-clear only while the app loop is running; otherwise
        # current_message() still hides it after message_expires.
        if not getattr(self.app, 'is_running', False):
            return

        async def _clear(expected=text, expires=self.message_expires):
            delay = max(0.0, expires - time.monotonic())
            await asyncio.sleep(delay)
            if self.message == expected and self.message_expires == expires:
                self.message = ''
                self.message_expires = 0.0
                if self.app:
                    self.app.invalidate()

        self.app.create_background_task(_clear())

    def current_message(self):
        if self.message and time.monotonic() < self.message_expires:
            return self.message
        return ''

    def is_show_status_bar(self):
        return self.show_status_bar

    # How a transport is named to the user; anything else falls back to its id.
    CONN_LABELS = {'rtt': 'Device'}

    def set_conn(self, source, status, error=''):
        self.conn[source] = {'status': status, 'error': error}

    def conn_down(self):
        """Sources whose transport is not currently connected."""
        return [s for s, v in sorted(self.conn.items()) if v.get('status') != 'connected']

    # A session that is down because someone asked for it, mapped to how it is
    # explained. Kept apart from a dropped link: telling the user their device
    # is not connected when they stopped it themselves is just wrong.
    CONN_INTENDED = {
        'stopped': ('{labels} reading is stopped',
                    'Press <F4>, or use the MCP start tool, to read again'),
        'released': ('{labels} probe is released',
                     'Another tool has the probe; use the MCP jlink_open tool to take it back'),
    }

    def show_conn_overlay(self):
        """Whether the Connection overlay should be on screen.

        Both overlays are centred floats, so drawing them together leaves
        neither readable. Flash wins while it is up: it takes the link down by
        design and says more about what is happening. The connection state is
        still there once it closes, so nothing is lost.
        """
        return bool(self.conn_down()) and not self.flash_visible

    def conn_intended(self):
        """The intended-stop wording for the down sources, when they share one.

        None when a source dropped rather than being stopped, which is what
        tells the display to treat it as a fault.
        """
        statuses = {self.conn.get(s, {}).get('status') for s in self.conn_down()}
        if len(statuses) != 1:
            return None
        return self.CONN_INTENDED.get(statuses.pop())

    def conn_title(self):
        down = self.conn_down()
        if not down:
            return ''
        labels = ' and '.join(self.CONN_LABELS.get(s, s.upper()) for s in down)
        intended = self.conn_intended()
        if intended:
            return intended[0].format(labels=labels)
        return f'{labels} is not connected'

    def conn_detail(self):
        for source in self.conn_down():
            error = self.conn.get(source, {}).get('error')
            if error:
                return error
        intended = self.conn_intended()
        return intended[1] if intended else ''

    def reconnect(self):
        if self.on_reconnect:
            self.on_reconnect()

    def set_auto_reconnect(self, enabled):
        self.auto_reconnect = bool(enabled)
        if self.on_auto_reconnect:
            self.on_auto_reconnect(self.auto_reconnect)

    def is_show_terminal(self):
        return self.show == self.SHOW_TERMINAL

    def is_show_logger(self):
        return self.show == self.SHOW_LOGGER

    def is_show_all(self):
        return self.show == self.SHOW_ALL

    def show_terminal(self):
        self.show = self.SHOW_TERMINAL

    def show_logger(self):
        self.show = self.SHOW_LOGGER

    def show_all(self):
        self.show = self.SHOW_ALL

    def scroll_to_end_toggle(self):
        self.scroll_to_end = not self.scroll_to_end
        return self.scroll_to_end

    def has_focus(self, value):
        return self.app.layout.has_focus(value) if self.app else False

    def set_app(self, app):
        self.app = app


class OffsetNumberedMargin(NumberedMargin):
    """Line numbers that stay absolute after oldest-line trimming / filtering.

    ``get_offset`` returns how many lines have been dropped from the start of
    the buffer; the default display is ``lineno + 1 + offset``.

    ``get_line_number(lineno)``, when set, overrides that (used by the filtered
    Log view so each visible row keeps its original absolute number).
    """

    def __init__(self, get_offset: Callable[[], int],
                 get_line_number: Callable[[int], int] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.get_offset = get_offset
        self.get_line_number = get_line_number

    def get_width(self, get_ui_content):
        ui = get_ui_content()
        if self.get_line_number is not None and ui.line_count > 0:
            last = None
            for i in range(ui.line_count - 1, -1, -1):
                last = self.get_line_number(i)
                if last is not None:
                    break
            if last is not None:
                return max(3, len(f"{max(1, last)}") + 1)
        line_count = ui.line_count + max(0, self.get_offset())
        return max(3, len(f"{line_count}") + 1)

    def create_margin(self, window_render_info, width: int, height: int) -> StyleAndTextTuples:
        offset = max(0, int(self.get_offset() or 0))
        relative = self.relative()
        style = "class:line-number"
        style_current = "class:line-number.current"
        current_lineno = window_render_info.ui_content.cursor_position.y
        result: StyleAndTextTuples = []
        last_lineno = None
        y = 0
        for y, lineno in enumerate(window_render_info.displayed_lines):
            if lineno != last_lineno:
                if lineno is not None:
                    if self.get_line_number is not None:
                        display = self.get_line_number(lineno)
                    else:
                        display = lineno + 1 + offset
                    # None = phantom empty row after a trailing newline — no gutter.
                    if display is None:
                        result.append(("", " " * width))
                    elif lineno == current_lineno:
                        if relative:
                            result.append((style_current, "%i" % display))
                        else:
                            result.append(
                                (style_current, ("%i " % display).rjust(width)))
                    else:
                        if relative:
                            display = abs(lineno - current_lineno) - 1
                        result.append((style, ("%i " % display).rjust(width)))
            last_lineno = lineno
            result.append(("", "\n"))
        if self.display_tildes():
            while y < window_render_info.window_height:
                result.append(("class:tilde", "~\n"))
                y += 1
        return result


def create_terminal_window():
    """
    Create the interactive terminal window.
    """
    terminal_search = SearchToolbar(ignore_case=True, vi_mode=True)
    terminal_window = TextArea(
        text="",
        scrollbar=True,
        line_numbers=True,
        focusable=True,
        focus_on_click=True,
        read_only=True,
        search_field=terminal_search,
    )
    return terminal_window, terminal_search


def create_terminal_input(state: State, history_file=None):
    """
    Create the terminal input field.
    """
    input_history = FileHistory(history_file) if history_file else None
    input_search = SearchToolbar(ignore_case=True)
    input_field = TextArea(
        height=1,
        prompt=lambda: [('class:cyan', 'Command: ')] if state.has_focus(input_field) else 'Command: ',
        style="class:input-field",
        multiline=False,
        wrap_lines=False,
        search_field=input_search,
        history=input_history,
        focusable=True,
        focus_on_click=True,
    )
    return input_field, input_search


def create_logger_window():
    """
    Create the logger window.
    """
    logger_search = SearchToolbar(ignore_case=True, vi_mode=True)
    logger_window = TextArea(
        scrollbar=True,
        line_numbers=True,
        focusable=True,
        focus_on_click=True,
        read_only=True,
        search_field=logger_search,
        lexer=LogLexer(),
    )
    return logger_window, logger_search


def create_log_pause_badge(state):
    """PAUSED +N strip on the bottom edge of the Log pane (not the status bar).

    Combined Terminal+Log append count since pause. Hidden while scrolling.
    Placed directly under the Log TextArea so the Filter row stays put when
    the badge appears (putting it below Filter made Filter jump up one line
    and look like the badge stole its row).
    """
    def get_text():
        n = state.paused_appended
        label = f' PAUSED +{n} ' if n else ' PAUSED '
        return [('class:paused', label)]

    return ConditionalContainer(
        content=Window(
            FormattedTextControl(get_text),
            height=LayoutDimension.exact(1),
            style='class:paused',
        ),
        filter=Condition(lambda: not state.scroll_to_end),
    )


def format_toast_fragments(toast):
    """Herdr-style clipboard toast: green border, dark panel, green check.

    Renders in the status-bar right slot (replacing the clock) as a compact
    one-line badge so pane heights never jump.
    """
    return [
        ('class:toast.border', '│'),
        ('class:toast.icon', ' ✓ '),
        ('class:toast.text', toast),
        ('class:toast.border', ' │'),
    ]


def create_status_bar(state):
    """
    Create the status bar for the console.

    Ephemeral toasts live in this single row so pane heights never jump when
    a message appears (a separate toast strip pushed both panes up one line
    and made drag hit the wrong line).

    Toasts replace the clock on the right as a herdr-style badge (green
    border, dark panel, green check); left-side hints stay visible.
    The PAUSED +N badge sits on the Log pane bottom edge instead.
    """
    def get_statusbar_text():
        paused = not state.scroll_to_end
        items = [('class:title', ' RTTT ')]

        # Keep hints short so Copy / Mouse drag stay visible around 80–100 cols.
        # Hints stay up while a toast is showing on the right (replacing clock).
        f5_style = 'class:yellow' if paused else 'class:title'
        f5_label = ' F5 Resume ' if paused else ' F5 Pause '
        if state.log_filter:
            items.append(('class:yellow', f' FILTER: {state.log_filter} '))
        items.extend([
            ('class:title', ' F3 Focus '),
            ('class:title', ' F4 Reconn '),
            (f5_style, f5_label),
            ('class:title', ' F7 Filter '),
            ('class:title', ' F8 Clear '),
            ('class:title', ' Ctrl-Q Quit '),
            ('class:title', ' Ctrl-C Copy '),
            ('class:title', ' Mouse drag - copy '),
        ])
        # No disconnect indicator here: the overlay stays up for as long as the
        # transport is down, so a second copy on the bar is just noise.
        return items

    def get_statusbar_right():
        toast = state.current_message()
        if toast:
            # Herdr-style compact badge: green border, dark panel, green check.
            return format_toast_fragments(toast)
        return datetime.now().strftime('%H:%M:%S')

    def right_width():
        toast = state.current_message()
        if toast:
            # Cap so a long hint cannot crush the left cheatsheet entirely.
            # +6 for "│ ✓ " … " │" border/icon chrome around the message.
            return LayoutDimension.exact(min(max(len(toast) + 6, 12), 56))
        return LayoutDimension.exact(10)

    return ConditionalContainer(
        content=VSplit([
            Window(
                FormattedTextControl(get_statusbar_text), style="class:status"
            ),
            Window(
                FormattedTextControl(get_statusbar_right),
                style="class:status.right",
                width=right_width,
                align=WindowAlign.RIGHT,
            ),
        ],
            height=LayoutDimension.exact(1),
            style="class:statusbar",),
        filter=Condition(state.is_show_status_bar)
    )


def create_layout(state, history_file):
    """
    Create the layout
    """
    input_field, input_search = create_terminal_input(state, history_file)
    terminal_window, terminal_search = create_terminal_window()
    logger_window, logger_search = create_logger_window()

    status_bar = create_status_bar(state)

    hs_terminal = HSplit(
        [
            terminal_window,
            terminal_search,
            HorizontalLine(),
            input_field,
            input_search,
        ]
    )

    # Always present so F7 can focus on the first press (ConditionalContainer
    # stays zero-height until the next layout pass and drops the first key).
    filter_field = TextArea(
        height=1,
        prompt='Filter: ',
        style='class:input-field',
        multiline=False,
        wrap_lines=False,
        focusable=True,
        focus_on_click=True,
    )
    pause_badge = create_log_pause_badge(state)
    # Order matters: PAUSED sits on the Log's bottom edge; Filter stays below
    # so enabling pause does not slide Filter up into the badge's slot.
    hs_logger = HSplit([
        logger_window,
        pause_badge,
        logger_search,
        filter_field,
    ])

    flash_bar = ProgressBar()
    state.flash_bar = flash_bar

    flash_overlay = Float(
        content=ConditionalContainer(
            content=Box(
                body=Frame(
                    body=HSplit([
                        Window(FormattedTextControl(lambda: state.flash_file), height=1, align=WindowAlign.CENTER),
                        Window(height=1),
                        ConditionalContainer(
                            content=HSplit([
                                Window(FormattedTextControl(lambda: state.flash_action), height=1, align=WindowAlign.CENTER),
                                flash_bar,
                            ]),
                            filter=Condition(lambda: not state.flash_error),
                        ),
                        ConditionalContainer(
                            content=Window(FormattedTextControl(lambda: state.flash_error), height=1, align=WindowAlign.CENTER, style="fg:#ff4444 bold"),
                            filter=Condition(lambda: bool(state.flash_error)),
                        ),
                    ]),
                    title="Flash",
                ),
                style="bg:#222222 fg:#eeeeee",
            ),
            filter=Condition(lambda: state.flash_visible),
        ),
    )

    def auto_reconnect_label():
        return f'[{"x" if state.auto_reconnect else " "}] Auto reconnect'

    auto_reconnect_button = Button(auto_reconnect_label(), width=22)

    def toggle_auto_reconnect():
        state.set_auto_reconnect(not state.auto_reconnect)
        auto_reconnect_button.text = auto_reconnect_label()

    auto_reconnect_button.handler = toggle_auto_reconnect
    state.auto_reconnect_button = auto_reconnect_button

    reconnect_button = Button('Reconnect', handler=lambda: state.reconnect(), width=13)
    state.reconnect_button = reconnect_button

    # Same treatment as a flash failure: a dropped transport otherwise looks
    # exactly like a device that has nothing to say.
    conn_overlay = Float(
        content=ConditionalContainer(
            content=Box(
                body=Frame(
                    body=HSplit([
                        # Red for a link that broke; plain for one the user
                        # stopped themselves, which is not a fault to alarm
                        # anyone about.
                        Window(FormattedTextControl(lambda: state.conn_title()), height=1,
                               align=WindowAlign.CENTER,
                               style=lambda: 'bold' if state.conn_intended() else 'fg:#ff4444 bold'),
                        ConditionalContainer(
                            content=Window(FormattedTextControl(lambda: state.conn_detail()), height=1,
                                           align=WindowAlign.CENTER),
                            filter=Condition(lambda: bool(state.conn_detail())),
                        ),
                        Window(height=1),
                        VSplit([
                            reconnect_button,
                            Window(width=2),
                            auto_reconnect_button,
                        ], align=WindowAlign.CENTER, padding=1),
                        # F4 is the way in: the buttons need focusing before
                        # Enter reaches them, which nothing about them shows.
                        Window(
                            FormattedTextControl(lambda: [(
                                'class:conn-hint',
                                '<F4> reconnect   |   click, or <Tab> then <Enter>',
                            )]),
                            height=1,
                            align=WindowAlign.CENTER,
                        ),
                    ]),
                    title="Connection",
                ),
                style="bg:#222222 fg:#eeeeee",
            ),
            filter=Condition(state.show_conn_overlay),
        ),
    )

    root_container = FloatContainer(
        content=HSplit(
            [
                ConditionalContainer(
                    content=VSplit(
                        [
                            Frame(hs_terminal, title="Interactive Terminal"),
                            Frame(hs_logger, title="Device Log")
                        ]
                    ),
                    filter=Condition(state.is_show_all)),
                ConditionalContainer(
                    content=hs_terminal,
                    filter=Condition(state.is_show_terminal)
                ),
                ConditionalContainer(
                    content=hs_logger,
                    filter=Condition(state.is_show_logger)
                ),
                # Toast is rendered inside the status bar (see create_status_bar)
                # so this row must not appear — it used to shift both panes up.
                status_bar
            ]
        ),
        floats=[flash_overlay, conn_overlay],
        style="bg:#111111 fg:#eeeeee",
    )

    return root_container, input_field, terminal_window, logger_window, filter_field
