import os
import asyncio
from loguru import logger
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.styles import Style, Priority
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.key_binding.bindings.mouse import load_mouse_bindings
from prompt_toolkit.document import Document
from prompt_toolkit.clipboard import ClipboardData, InMemoryClipboard
from prompt_toolkit.clipboard.pyperclip import PyperclipClipboard
from prompt_toolkit.filters import Condition
from rttt.ui import State, create_layout
from rttt.utils import truncate_path
from rttt.connectors.base import Connector
from rttt.event import Event, EventType


def _make_clipboard():
    """The system clipboard, falling back to an in-process one.

    pyperclip needs a helper on the system (xclip, xsel, wl-clipboard) and
    raises when it cannot find one -- over SSH without X forwarding, or in a
    bare container. Copying within the session still works in that case; it
    just does not reach other applications.
    """
    clipboard = PyperclipClipboard()
    try:
        clipboard.set_data(ClipboardData(''))
    except Exception as e:
        logger.warning(f'No system clipboard ({e}); copying stays in-session')
        return InMemoryClipboard()
    return clipboard


def _is_right_button_release(data):
    """Whether a raw VT100 mouse packet is the right button being released.

    Under SGR this is the release, so a click acts once. The older typical
    encoding has no per-button release, so there the press is all there is --
    which is also why it cannot double-fire.
    """
    parsed = _mouse_button_action(data)
    if parsed is None or parsed[0] != 2:
        return False
    if data[2] == '<':
        return parsed[1] is False        # SGR: act on the release
    return True                          # typical: press is the only event


def _is_left_button_release(data):
    """Whether a raw VT100 mouse packet is the left button being let go.

    The typical encoding cannot say which button was released, so there a plain
    press stands in -- it is the only event that encoding delivers.
    """
    parsed = _mouse_button_action(data)
    if parsed is None or parsed[0] != 0:
        return False
    if data[2] == '<':
        return parsed[1] is False
    return True


def _mouse_button_action(data):
    """(button, pressed) from a raw VT100 mouse packet, or None.

    button is 0 left, 1 middle, 2 right. A drag reports the button still held
    with bit 5 set; that is neither a press nor a release and returns None, so
    acting on a press does not fire again for every pixel of movement. The
    wheel (bit 6) is None as well.
    """
    if not data.startswith('\x1b[') or len(data) < 4:
        return None

    if data[2] == '<':
        # SGR: ESC[<flags;x;y then M (press or motion) or m (release).
        final = data[-1]
        if final not in 'Mm':
            return None
        try:
            flags = int(data[3:-1].split(';')[0])
        except (ValueError, IndexError):
            return None
        if flags & 0x60:                 # bit 5 motion, bit 6 wheel
            return None
        return flags & 0x03, final == 'M'

    if data[2] == 'M' and len(data) >= 6:
        # Typical: ESC[M then three offset-by-32 bytes, no per-button release.
        flags = ord(data[3]) - 32
        if flags & 0x60:
            return None
        return flags & 0x03, True

    return None


class Console:

    def __init__(self, connector: Connector, history_file=None):
        self.connector = connector
        self.state = State()
        self.exception = None
        self._wire_reconnect()

        if history_file:
            d = os.path.dirname(history_file)
            if d:
                os.makedirs(d, exist_ok=True)

        root_container, input_field, terminal_window, logger_window = create_layout(self.state, history_file)
        self.input_field = input_field
        self.input_field.accept_handler = self._input_accept_handler

        bindings = KeyBindings()

        def _copy_selection():
            """Copy the focused pane's selection. True if anything was copied."""
            for window in (terminal_window, logger_window, self.input_field):
                if self.has_focus(window):
                    buffer = window.buffer
                    break
            else:
                return False
            if buffer.selection_state is None:
                return False
            data = buffer.copy_selection()
            if not data.text:
                return False
            try:
                self.app.clipboard.set_data(data)
            except Exception as e:
                logger.error(e)
                return False
            return True

        def _paste_into_input():
            """Paste the clipboard into the command line. True if anything went in."""
            try:
                data = self.app.clipboard.get_data()
            except Exception as e:
                logger.error(e)
                return False
            if not data.text:
                return False
            self.input_field.buffer.paste_clipboard_data(data)
            return True

        self._copy_selection = _copy_selection
        self._paste_into_input = _paste_into_input

        @bindings.add("c-insert", eager=True)  # TODO: check
        @bindings.add("c-c", eager=True)
        def _(event):
            _copy_selection()

        # prompt_toolkit routes every mouse packet through a single binding on
        # Keys.Vt100MouseEvent, and a binding of our own *replaces* it rather
        # than layering over it -- returning NotImplemented does not fall
        # through. Without delegating, adding a right-click handler silently
        # took click-to-focus and drag-to-select away from every pane.
        _builtin_mouse = next(
            b.handler for b in load_mouse_bindings().bindings
            if Keys.Vt100MouseEvent in b.keys)

        @bindings.add(Keys.Vt100MouseEvent)
        def _(event):
            """Right click copies, or pastes when there is no selection.

            One button and no menu, because the useful action is never
            ambiguous. Everything else -- focus, drag-select, scroll -- is
            prompt_toolkit's own handling, called below.
            """
            if _is_right_button_release(event.data):
                if not _copy_selection():
                    _paste_into_input()
                return None

            result = _builtin_mouse(event)

            # Clicking into an output pane means working in it, and following
            # output would move the text out from under a selection while it is
            # being made. So a click there pauses, exactly as F5 does.
            #
            # On the release, not the press: prompt_toolkit moves the focus on
            # the release, so that is the first point at which the pane the
            # click landed in can be identified.
            if _is_left_button_release(event.data) and self.state.scroll_to_end:
                if self.has_focus(terminal_window) or self.has_focus(logger_window):
                    self.state.scroll_to_end_toggle()

            return result

        @bindings.add("f6", eager=True)
        def _(event):
            self.state.toggle_mouse()

        def _selectable_buffer():
            """The read-only pane holding the focus, if either does."""
            for window in (terminal_window, logger_window):
                if self.has_focus(window):
                    return window.buffer
            return None

        def _extend_selection(move):
            """Grow the selection in the focused pane by one cursor move.

            prompt_toolkit binds no selection keys of its own, so without this
            there is no way to mark text in a read-only pane at all -- and
            Ctrl-C had nothing to copy.
            """
            buffer = _selectable_buffer()
            if buffer is None:
                return
            offset = move(buffer)
            if not offset:
                # Nowhere to go -- at the end of the pane, which is where
                # following output leaves the cursor. Marking a selection here
                # gives Ctrl-C an empty one to copy, which looks like copying
                # is broken.
                return
            if buffer.selection_state is None:
                buffer.start_selection()
            buffer.cursor_position += offset

        @bindings.add("s-left", eager=True)
        def _(event):
            _extend_selection(lambda b: -1 if b.cursor_position else 0)

        @bindings.add("s-right", eager=True)
        def _(event):
            _extend_selection(lambda b: 1 if b.cursor_position < len(b.text) else 0)

        @bindings.add("s-up", eager=True)
        def _(event):
            _extend_selection(lambda b: b.document.get_cursor_up_position())

        @bindings.add("s-down", eager=True)
        def _(event):
            _extend_selection(lambda b: b.document.get_cursor_down_position())

        @bindings.add("s-home", eager=True)
        def _(event):
            _extend_selection(
                lambda b: -len(b.document.current_line_before_cursor))

        @bindings.add("s-end", eager=True)
        def _(event):
            _extend_selection(
                lambda b: len(b.document.current_line_after_cursor))

        @bindings.add("c-a", eager=True)
        def _(event):
            buffer = _selectable_buffer()
            if buffer is None:
                return
            buffer.cursor_position = 0
            buffer.start_selection()
            buffer.cursor_position = len(buffer.text)

        @bindings.add("f4", eager=True)
        def _(event):
            self.state.reconnect()

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
            mouse_support=Condition(self._wants_mouse),
            full_screen=True,
            refresh_interval=1,
            enable_page_navigation_bindings=True,
            clipboard=_make_clipboard(),
            style=Style.from_dict({
                'border': '#888888',
                'message': 'bg:#bbee88 #222222',
                'statusbar': 'noreverse bg:gray #000000',
                'progress-bar.used': 'bg:#4488cc',
                # Without these the overlay's buttons look identical whether
                # they hold the focus or not, so there is no way to tell what
                # Enter would press.
                'button': '#eeeeee',
                'button.focused': 'bg:#4488cc #ffffff bold',
                'button.arrow': 'bold',
                'conn-hint': '#888888',
            }, priority=Priority.MOST_PRECISE)
        )

        self.state.set_app(self.app)

    def run(self):
        async def event_task():
            with logger.catch(message='event_task', reraise=True):
                while True:
                    event = await self.events.get()
                    logger.debug(f'event: {str(event.type)} {event.data}')
                    if event.type == EventType.LOG:
                        self._buffer_insert_text(self.logger_buffer, f'{event.data}\n')
                    elif event.type == EventType.OUT:
                        self._buffer_insert_text(self.terminal_buffer, f'{event.data}\n')
                    elif event.type == EventType.IN:
                        self._buffer_insert_text(self.terminal_buffer, f'{event.data}\n')
                    elif event.type == EventType.FLASH:
                        data = event.data
                        status = data.get("status", "")
                        if status == "start":
                            self.state.flash_file = truncate_path(data.get("file", ""))
                            self.state.flash_action = ""
                            self.state.flash_error = ""
                            self.state.flash_bar.percentage = 0
                            self.state.flash_visible = True
                        elif status == "progress":
                            self.state.flash_bar.percentage = min(data.get("percentage", 0), 100)
                            self.state.flash_action = data.get("action", "")
                        elif status == "done":
                            self.state.flash_visible = False
                        elif status == "error":
                            self.state.flash_error = data.get("message", "Flash error")
                        self.app.invalidate()
                    elif event.type == EventType.CONN:
                        data = event.data
                        self.state.set_conn(data.get("source", ""),
                                            data.get("status", ""),
                                            data.get("error", ""))
                        self.app.invalidate()

        def pre_run():
            self.events = asyncio.Queue()

            def connector_handle_event(event: Event):
                try:
                    self.events.put_nowait(event)
                except Exception:
                    logger.exception(f"Failed to queue event: {event}")

            self.connector.on(connector_handle_event)
            self.app.create_background_task(event_task())
            self.connector.open()

        try:
            self.app.run(pre_run=pre_run)
        finally:
            self.connector.close()

    def exit(self, exception=None):
        self.exception = exception
        self.app.exit()

    def has_focus(self, window):
        return self.app.layout.has_focus(window)

    def _wants_mouse(self):
        """Whether to ask the terminal for mouse reporting.

        On by default: clicking a pane focuses it, dragging selects, right click
        copies. F6 turns it off so click-and-drag goes back to the terminal
        emulator, whose own selection reaches across both panes and works where
        the clipboard here does not.

        An overlay overrides the toggle, because its buttons have to be
        clickable for anyone who has not found F6 or Tab.
        """
        if self.state.show_conn_overlay():
            return True
        return self.state.mouse_enabled

    def _leaf(self):
        """The connector at the end of the middleware chain, which owns the
        transport and therefore the reconnecting."""
        conn = self.connector
        while hasattr(conn, 'connector'):
            conn = conn.connector
        return conn

    def _wire_reconnect(self):
        """Let the dialog and F4 drive the connector's reconnecting.

        Both are no-ops on a connector that does not support it, rather than
        offering a button that quietly does nothing.
        """
        leaf = self._leaf()

        if hasattr(leaf, 'request_reconnect'):
            self.state.on_reconnect = leaf.request_reconnect
        else:
            logger.info(f'{type(leaf).__name__} cannot reconnect on request')

        if hasattr(leaf, 'auto_reconnect'):
            self.state.auto_reconnect = bool(leaf.auto_reconnect)

            def set_auto(enabled):
                leaf.auto_reconnect = enabled

            self.state.on_auto_reconnect = set_auto
        else:
            logger.info(f'{type(leaf).__name__} has no auto reconnect')

    def _input_accept_handler(self, buff: Buffer) -> bool:
        # Anything raised here propagates into prompt_toolkit's key processor
        # and tears down the event loop, so a connector that fails to deliver a
        # command must not be able to take the console with it. Connectors
        # report delivery problems as CONN events, which the status bar shows.
        with logger.catch(message='_input_accept_handler'):
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
