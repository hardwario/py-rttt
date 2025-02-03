from prompt_toolkit.widgets import TextArea, SearchToolbar, Frame, HorizontalLine, ProgressBar, Dialog, Box, Label
from prompt_toolkit.layout.containers import HSplit, VSplit, Window, WindowAlign, ConditionalContainer, FloatContainer, Float
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.margins import NumberedMargin, ScrollbarMargin
from prompt_toolkit.layout.dimension import LayoutDimension
from prompt_toolkit.history import FileHistory
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.formatted_text import FormattedText
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

    def is_show_status_bar(self):
        return self.show_status_bar

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
        return [
            ('class:title', ' HARDWARIO RTTT Console     '),
            ('class:title', ' <F3> Focus '),
            ('class:title', ' <F5> Pause ') if state.scroll_to_end else ('class:yellow', ' <F5> Pause '),
            ('class:title', ' <F8> Clear '),
            ('class:title', ' <F10> Exit (or Ctrl-<F10>) '),
            ('class:title', ' [Shift-]<Tab> Cycle '),
        ]

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
        floats=[
        ],
        style="bg:#111111 fg:#eeeeee",
    )

    return root_container, input_field, terminal_window, logger_window
