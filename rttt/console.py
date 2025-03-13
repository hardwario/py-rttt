import os
import asyncio
from loguru import logger
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.styles import Style, Priority
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.document import Document
from prompt_toolkit.clipboard.pyperclip import PyperclipClipboard
from prompt_toolkit.filters import Condition
from rttt.ui import State, create_layout
from rttt.connectors.base import Connector
from rttt.event import Event, EventType


class Console:

    def __init__(self, connector: Connector, history_file=None):
        self.connector = connector
        self.state = State()
        self.exception = None

        if history_file:
            d = os.path.dirname(history_file)
            if d:
                os.makedirs(d, exist_ok=True)

        root_container, input_field, terminal_window, logger_window = create_layout(self.state, history_file)
        self.input_field = input_field
        self.input_field.accept_handler = self._input_accept_handler

        bindings = KeyBindings()

        @bindings.add("c-insert", eager=True)  # TODO: check
        @bindings.add("c-c", eager=True)
        def _(event):
            data = None
            if self.has_focus(terminal_window):
                data = terminal_window.buffer.copy_selection()
            elif self.has_focus(logger_window):
                data = logger_window.buffer.copy_selection()
            else:
                return
            if data.text:
                try:
                    event.app.clipboard.set_data(data)
                except Exception as e:
                    logger.error(e)

        @bindings.add("f5", eager=True)
        def _(event):
            if self.state.scroll_to_end_toggle():
                self.terminal_buffer.cursor_position = len(self.terminal_buffer.text)
                self.logger_buffer.cursor_position = len(
                    self.logger_buffer.text)

        @bindings.add("f8", eager=True)
        def _(event):
            self.terminal_buffer.set_document(Document(''), True)
            self.logger_buffer.set_document(Document(''), True)

        @bindings.add("f3", eager=True)
        def _(event):
            if not self.state.is_show_all():
                self.state.show_all()
            elif self.has_focus(self.input_field) or self.has_focus(terminal_window):
                self.state.show_terminal()
            elif self.has_focus(logger_window):
                self.state.show_logger()

        @bindings.add("c-q", eager=True)
        @bindings.add("f10", eager=True)
        @bindings.add("c-f10", eager=True)
        def _(event):
            event.app.exit()

        bindings.add("tab")(focus_previous)
        bindings.add("s-tab")(focus_next)

        self.terminal_buffer = terminal_window.buffer
        self.logger_buffer = logger_window.buffer

        self.app = Application(
            layout=Layout(root_container, focused_element=self.input_field),
            key_bindings=bindings,
            mouse_support=Condition(lambda: not self.state.is_show_all()),
            full_screen=True,
            refresh_interval=1,
            enable_page_navigation_bindings=True,
            clipboard=PyperclipClipboard(),
            style=Style.from_dict({
                'border': '#888888',
                'message': 'bg:#bbee88 #222222',
                'statusbar': 'noreverse bg:gray #000000',
            }, priority=Priority.MOST_PRECISE)
        )

        self.evets = asyncio.Queue()

        self.state.set_app(self.app)

    def _connector_handle_event(self, event: Event):
        self.evets.put_nowait(event)

    def run(self):
        async def event_task():
            with logger.catch(message='event_task', reraise=True):
                while True:
                    event = await self.evets.get()
                    logger.debug(f'event: {str(event.type)} {event.data}')
                    if event.type == EventType.LOG:
                        self._buffer_insert_text(self.logger_buffer, f'{event.data}\n')
                    elif event.type == EventType.OUT:
                        self._buffer_insert_text(self.terminal_buffer, f'{event.data}\n')
                    elif event.type == EventType.IN:
                        self._buffer_insert_text(self.terminal_buffer, f'{event.data}\n')

        def pre_run():
            self.app.create_background_task(event_task())

        self.connector.on(self._connector_handle_event)
        self.connector.open()
        self.app.run(pre_run=pre_run)
        self.connector.close()

    def exit(self, exception=None):
        self.exception = exception
        self.app.exit()

    def has_focus(self, window):
        return self.app.layout.has_focus(window)

    def _input_accept_handler(self, buff: Buffer) -> bool:
        with logger.catch(message='_input_accept_handler', reraise=True):
            text = f'{buff.text}\n'
            for line in text.splitlines():
                self.connector.handle(Event(EventType.IN, line))
            return False  # false to keep the text in the buffer

    def _buffer_insert_text(self, buffer, line):
        changed = buffer._set_text(buffer.text + line)
        if changed:
            if self.state.scroll_to_end:
                buffer.cursor_position = len(buffer.text)
            buffer._text_changed()
