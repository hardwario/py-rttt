from prompt_toolkit.widgets import TextArea, SearchToolbar, Frame, HorizontalLine, ProgressBar, Box, Button
from prompt_toolkit.layout.containers import HSplit, VSplit, Window, WindowAlign, ConditionalContainer, FloatContainer, Float
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.margins import NumberedMargin, ScrollbarMargin
from prompt_toolkit.layout.dimension import LayoutDimension
from prompt_toolkit.history import FileHistory
from prompt_toolkit.layout.layout import Layout
from datetime import datetime
from prompt_toolkit.filters import Condition
from prompt_toolkit.application import get_app
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

    def is_show_status_bar(self):
        return self.show_status_bar

    # How a transport is named to the user; anything else falls back to its id.
    CONN_LABELS = {'rtt': 'Device'}

    def set_conn(self, source, status, error=''):
        self.conn[source] = {'status': status, 'error': error}

    def conn_down(self):
        """Sources whose transport is not currently connected."""
        return [s for s, v in sorted(self.conn.items()) if v.get('status') != 'connected']

    def conn_title(self):
        down = self.conn_down()
        if not down:
            return ''
        labels = [self.CONN_LABELS.get(s, s.upper()) for s in down]
        return f'{" and ".join(labels)} is not connected'

    def conn_detail(self):
        for source in self.conn_down():
            error = self.conn.get(source, {}).get('error')
            if error:
                return error
        return ''

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


def create_status_bar(state):
    """
    Create the status bar for the console.
    """
    def get_statusbar_text():
        items = [
            ('class:title', ' HARDWARIO RTTT Console     '),
            ('class:title', ' <F3> Focus '),
            ('class:title', ' <F4> Reconnect '),
            ('class:title', ' <F5> Pause ') if state.scroll_to_end else ('class:yellow', ' <F5> Pause '),
            ('class:title', ' <F8> Clear '),
            ('class:title', ' <F10> Exit (or Ctrl-<F10>) '),
            ('class:title', ' [Shift-]<Tab> Cycle '),
        ]
        # No disconnect indicator here: the overlay stays up for as long as the
        # transport is down, so a second copy on the bar is just noise.
        return items

    def get_statusbar_time():
        return datetime.now().strftime('%b %d, %Y  %H:%M:%S')

    return ConditionalContainer(
        content=VSplit([
            Window(
                FormattedTextControl(get_statusbar_text), style="class:status"
            ),
            Window(
                FormattedTextControl(get_statusbar_time),
                style="class:status.right",
                width=24,
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

    hs_logger = HSplit([
        logger_window,
        logger_search
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
                        Window(FormattedTextControl(lambda: state.conn_title()), height=1,
                               align=WindowAlign.CENTER, style="fg:#ff4444 bold"),
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
            filter=Condition(lambda: bool(state.conn_down())),
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
                status_bar
            ]
        ),
        floats=[flash_overlay, conn_overlay],
        style="bg:#111111 fg:#eeeeee",
    )

    return root_container, input_field, terminal_window, logger_window
