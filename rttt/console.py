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
from prompt_toolkit.clipboard.base import ClipboardData
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.selection import SelectionState, SelectionType
from rttt.clipboard import HybridClipboard
from rttt.ui import State, create_layout
from rttt.utils import truncate_path
from rttt.connectors.base import Connector
from rttt.event import Event, EventType


class Console:

    def __init__(self, connector: Connector, history_file=None):
        self.connector = connector
        self.state = State()
        self.exception = None
        self._wire_reconnect()
        self._drag_buffer = None  # buffer under an in-progress mouse drag, if any
        # True when THIS drag flipped scroll_to_end off (pause deferred to MOVE).
        self._paused_for_drag = False
        # scroll_to_end was True at MOUSE_DOWN — used to decide auto-resume.
        self._drag_was_scrolling = False
        # None | 'auto' (drag) | 'manual' (F5). Auto-resume only when 'auto'.
        self._pause_origin = None
        # Last successful copy payload so Ctrl-C can re-toast after the highlight
        # is gone (auto-copy clears nothing, but clicks / append races might).
        self._last_copied = ''
        # Exclusive-end span (start, end) we re-apply after streaming appends.
        self._selection_span = None  # tuple[Buffer, int, int] | None

        if history_file:
            d = os.path.dirname(history_file)
            if d:
                os.makedirs(d, exist_ok=True)

        root_container, input_field, terminal_window, logger_window = create_layout(self.state, history_file)
        self.input_field = input_field
        self.input_field.accept_handler = self._input_accept_handler
        self.terminal_window = terminal_window
        self.logger_window = logger_window

        bindings = KeyBindings()

        @bindings.add("c-insert", eager=True)
        @bindings.add("c-c", eager=True)
        def _(event):
            # One pane only: focused Terminal XOR Log. Command box re-toasts
            # the last single-pane copy without merging buffers.
            buf = self._focused_copy_buffer()
            self._copy_from_buffer(buf, clear_selection=bool(buf))

        @bindings.add("f4", eager=True)
        def _(event):
            self.state.reconnect()

        @bindings.add("f5", eager=True)
        def _(event):
            if self.state.scroll_to_end_toggle():
                # Resuming auto-scroll should drop any highlight; otherwise
                # jumping the cursor to the end would stretch the selection.
                self._pause_origin = None
                self._selection_span = None
                for buf in (self.terminal_buffer, self.logger_buffer):
                    buf.exit_selection()
                    buf.cursor_position = len(buf.text)
            else:
                self._pause_origin = 'manual'

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

        # Select-to-copy: when a drag (or double-click word) selection finishes,
        # mirror it to the system clipboard with toast feedback.
        self._install_select_to_copy(terminal_window)
        self._install_select_to_copy(logger_window)
        # Right-click is separate from left-drag selection (must not pause,
        # start a selection, or steal pane focus). RMB on Log/Terminal copies;
        # RMB on the Command line pastes.
        self._install_right_click_paste(self.input_field)

        self.app = Application(
            layout=Layout(root_container, focused_element=self.input_field),
            key_bindings=bindings,
            # Mouse reporting stays on so in-app selection (and select-to-copy)
            # works in every view, including the split layout. Hold Shift while
            # dragging to fall through to the terminal's native selection when
            # the emulator supports that bypass.
            mouse_support=True,
            full_screen=True,
            refresh_interval=1,
            enable_page_navigation_bindings=True,
            clipboard=HybridClipboard(),
            style=Style.from_dict({
                'border': '#888888',
                'message': 'bg:#bbee88 #222222',
                'statusbar': 'noreverse bg:gray #000000',
                # Visible pause: reverse yellow stands out on the gray bar.
                'paused': 'reverse bold bg:ansiyellow #000000',
                'yellow': 'bg:ansiyellow #000000 bold',
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
        except EOFError:
            # stdin closed (piped trust answer, hung-up tty). Soft-exit
            # instead of a red traceback — trust flow flushes /dev/tty first.
            logger.info('TUI stdin closed (EOF)')
        finally:
            self.connector.close()

    def exit(self, exception=None):
        self.exception = exception
        self.app.exit()

    def has_focus(self, window):
        return self.app.layout.has_focus(window)

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
        # Overlay buttons can steal focus after reconnect; keep typing here.
        if getattr(self, 'app', None):
            try:
                self.app.layout.focus(self.input_field)
            except Exception:
                pass
        return False  # false to keep the text in the buffer

    def _buffer_insert_text(self, buffer, line):
        # prompt_toolkit's Buffer._text_changed() always clears selection_state.
        # Re-apply our sticky exclusive-end span after append so highlights
        # survive streaming OUT/LOG (and Ctrl-C still sees them).
        dragging = getattr(self, '_drag_buffer', None) is buffer
        span = None
        drag_sel = None  # (orig, cursor, type) while a drag is in progress
        if dragging:
            # Keep the in-progress mouse selection across appends. Skipping
            # restore here caused first-drag failures when demo/device logs
            # arrived between MOUSE_DOWN and MOUSE_UP.
            if buffer.selection_state is not None:
                drag_sel = (
                    buffer.selection_state.original_cursor_position,
                    buffer.cursor_position,
                    buffer.selection_state.type,
                )
        else:
            sticky = self._selection_span
            if sticky is not None and sticky[0] is buffer:
                span = sticky[1], sticky[2]
            elif buffer.selection_state is not None:
                # Unbumped emacs selection: (lo, hi) is already a half-open
                # cut range (text[lo:hi]); remember it as sticky so later
                # appends keep the same highlight.
                lo, hi = sorted([
                    buffer.cursor_position,
                    buffer.selection_state.original_cursor_position,
                ])
                if hi > lo:
                    span = (lo, hi)
        changed = buffer._set_text(buffer.text + line)
        if changed:
            buffer._text_changed()
            if span is not None:
                self._apply_span(buffer, span)
            elif drag_sel is not None:
                orig, cur, stype = drag_sel
                # Append is at the end — absolute indices in the viewport stay valid.
                buffer.selection_state = SelectionState(orig, stype)
                buffer.cursor_position = min(cur, len(buffer.text))
            elif dragging:
                pass  # between DOWN and first MOVE — no selection yet
            elif self.state.scroll_to_end:
                buffer.cursor_position = len(buffer.text)

    def _apply_span(self, buffer, span):
        start, end = span
        end = min(max(end, 0), len(buffer.text))
        start = min(max(start, 0), end)
        if end <= start:
            buffer.exit_selection()
            if self._selection_span is not None and self._selection_span[0] is buffer:
                self._selection_span = None
            return
        # Place cursor at exclusive end so PTK's emacs cut/highlight match the
        # sticky span (text[start:end]) without a further +1.
        buffer.selection_state = SelectionState(start, SelectionType.CHARACTERS)
        buffer.cursor_position = end
        self._selection_span = (buffer, start, end)

    def _make_mouse_selection_inclusive(self, buffer):
        """Include the character under the mouse release (GUI-like).

        prompt_toolkit Emacs mode uses an exclusive end at the cursor, so a
        drag that ends on the last wanted character drops it from both the
        highlight and cut_selection. Bump the higher endpoint by one and
        remember the sticky exclusive-end span.
        """
        ss = buffer.selection_state
        if ss is None:
            return
        a = ss.original_cursor_position
        b = buffer.cursor_position
        lo, hi = (a, b) if a <= b else (b, a)
        if hi <= lo:
            return
        new_hi = min(hi + 1, len(buffer.text))
        if b >= a:
            buffer.cursor_position = new_hi
        else:
            ss.original_cursor_position = new_hi
        self._selection_span = (buffer, lo, new_hi)

    def _selected_text(self, buffer):
        """Text covered by the current (or sticky) selection.

        Sticky spans are half-open [start, end) and already include the
        mouse-up character when set by _make_mouse_selection_inclusive.
        """
        if buffer is None:
            return ''
        sticky = self._selection_span
        if sticky is not None and sticky[0] is buffer:
            start, end = sticky[1], min(sticky[2], len(buffer.text))
            if end > start:
                return buffer.text[start:end]
        if buffer.selection_state is None:
            return ''
        lo, hi = sorted([
            buffer.cursor_position,
            buffer.selection_state.original_cursor_position,
        ])
        if hi <= lo:
            return ''
        # Emacs half-open range (no sticky yet).
        return buffer.text[lo:hi]

    def _pause_auto_scroll_for_selection(self, toast=True):
        """Same effect as F5 Pause: freeze scroll-to-end while selecting.

        Called on the first real drag MOVE (not plain click). Streaming
        OUT/LOG keeps appending, but the viewport no longer jumps.
        Origin is set to 'auto' so MOUSE_UP can resume after a successful copy.

        Returns True when this call flipped scroll_to_end off. toast=False
        avoids invalidate() racing an in-progress drag.
        """
        if not self.state.scroll_to_end:
            return False
        self.state.scroll_to_end = False
        self._pause_origin = 'auto'
        if toast:
            self.state.show_message('Paused for selection (F5 resumes)', seconds=1.5)
        return True

    def _resume_streaming_after_copy(self):
        """Undo an auto-pause: scroll again and drop the highlight (text is copied)."""
        self.state.scroll_to_end = True
        self._pause_origin = None
        self._selection_span = None
        for buf in (self.terminal_buffer, self.logger_buffer):
            buf.exit_selection()
            buf.cursor_position = len(buf.text)

    def _clear_other_pane_selection(self, buffer):
        """Copy is always one pane only — drop selection on the sibling."""
        other = None
        if buffer is self.terminal_buffer:
            other = self.logger_buffer
        elif buffer is self.logger_buffer:
            other = self.terminal_buffer
        if other is None:
            return
        other.exit_selection()
        if self._selection_span is not None and self._selection_span[0] is other:
            self._selection_span = None

    def _focused_copy_buffer(self):
        """Buffer for Ctrl-C: focused Log XOR Terminal, never both."""
        if self.has_focus(self.terminal_window):
            return self.terminal_buffer
        if self.has_focus(self.logger_window):
            return self.logger_buffer
        return None

    def _install_select_to_copy(self, text_area):
        control = text_area.control
        original = control.mouse_handler

        def mouse_handler(mouse_event):
            # Focus on press so the first drag (Command still focused) can
            # select+copy. Pause only once the mouse actually moves and a
            # selection starts — plain click must not freeze the stream.
            buf = text_area.buffer
            # Right-click: copy only. Do NOT call the BufferControl handler —
            # its MOUSE_DOWN would exit_selection before we can copy on UP.
            # Also must not pause, start a drag, or change pane focus.
            if mouse_event.button == MouseButton.RIGHT:
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    self._right_click_copy_pane(buf)
                return None
            if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                self._drag_buffer = buf
                self._paused_for_drag = False
                self._drag_was_scrolling = self.state.scroll_to_end
                try:
                    if self.app is not None:
                        self.app.layout.current_control = control
                except Exception:
                    pass
                self._clear_other_pane_selection(buf)
                # New click replaces any sticky selection on this buffer.
                if self._selection_span is not None and self._selection_span[0] is buf:
                    self._selection_span = None

            result = original(mouse_event)

            if (
                mouse_event.event_type == MouseEventType.MOUSE_MOVE
                and self._drag_buffer is buf
                and mouse_event.button != MouseButton.NONE
                and not self._paused_for_drag
                and self._drag_was_scrolling
                and buf.selection_state is not None
            ):
                self._paused_for_drag = self._pause_auto_scroll_for_selection(
                    toast=False)

            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                self._drag_buffer = None
                auto_paused = self._paused_for_drag
                self._paused_for_drag = False
                copied = False
                if buf.selection_state is not None:
                    self._make_mouse_selection_inclusive(buf)
                    if self._selected_text(buf):
                        copied = bool(
                            self._copy_from_buffer(buf, clear_selection=False))
                    else:
                        buf.exit_selection()
                        if (
                            self._selection_span is not None
                            and self._selection_span[0] is buf
                        ):
                            self._selection_span = None
                else:
                    buf.exit_selection()
                    if (
                        self._selection_span is not None
                        and self._selection_span[0] is buf
                    ):
                        self._selection_span = None

                if copied and self._pause_origin == 'auto':
                    # Streaming was on before the drag — restore it; text is
                    # already on the clipboard (Ctrl-C re-toasts via _last_copied).
                    n = len(self._last_copied)
                    self._resume_streaming_after_copy()
                    self.state.show_message(
                        f'Copied {n} char{"s" if n != 1 else ""} — resumed')
                elif not copied and auto_paused:
                    # Aborted drag that had auto-paused — do not leave Pause on.
                    self._resume_streaming_after_copy()
                # Manual F5 pause: stay paused; sticky highlight kept on copy.
            return result

        control.mouse_handler = mouse_handler

    def _right_click_copy_pane(self, buffer):
        """Right-click on Log/Terminal: copy that pane's selection (Ctrl-C path).

        Does not change focus or pause state. No selection → same as Ctrl-C:
        re-toast ``_last_copied``, or ``Nothing selected``.
        """
        return self._copy_from_buffer(buffer, clear_selection=True)

    def _install_right_click_paste(self, text_area):
        """Right-click on the Command line focuses it and pastes clipboard text."""
        control = text_area.control
        original = control.mouse_handler

        def mouse_handler(mouse_event):
            if mouse_event.button == MouseButton.RIGHT:
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    self._paste_into_command()
                return None
            return original(mouse_event)

        control.mouse_handler = mouse_handler

    def _read_paste_text(self):
        """Text to paste into Command: local pyperclip, else in-app last copy.

        Never issues OSC 52 clipboard *read* queries (unsupported / unreliable).
        Over SSH (HybridClipboard ``_use_pyperclip`` False) we only use the
        in-memory HybridClipboard / ``_last_copied``.
        """
        clipboard = self.app.clipboard if getattr(self, 'app', None) else None
        use_pp = bool(getattr(clipboard, '_use_pyperclip', False)) if clipboard else False
        if use_pp:
            try:
                import pyperclip
                text = pyperclip.paste()
                if text:
                    return text
            except Exception:
                pass
        if clipboard is not None:
            try:
                data = clipboard.get_data()
                if data is not None and data.text:
                    return data.text
            except Exception:
                pass
        return self._last_copied or ''

    @staticmethod
    def _command_paste_payload(text: str) -> str:
        """Normalize clipboard text for the single-line Command field.

        Strip a trailing newline (common when copying a whole line). If more
        than one line remains, keep only the first — the input is height=1 /
        multiline=False, so embedding newlines would corrupt the command row.
        """
        if not text:
            return ''
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        text = text.rstrip('\n')
        if '\n' in text:
            text = text.split('\n', 1)[0]
        return text

    def _paste_into_command(self):
        """Focus Command and insert paste text at the cursor; toast the result."""
        raw = self._read_paste_text()
        text = self._command_paste_payload(raw if raw is not None else '')
        if not text:
            self.state.show_message('Clipboard empty')
            return False
        try:
            if self.app is not None:
                self.app.layout.focus(self.input_field)
        except Exception:
            pass
        self.input_field.buffer.insert_text(text, fire_event=False)
        n = len(text)
        self.state.show_message(f'Pasted {n} char{"s" if n != 1 else ""}')
        return True

    def _copy_from_buffer(self, buffer, clear_selection=True):
        """Copy the current selection (or last copy) to the hybrid clipboard.

        Returns True on a successful clipboard write.
        """
        text = ''
        if buffer is not None:
            text = self._selected_text(buffer)

        if not text and self._last_copied:
            text = self._last_copied
            # Re-toast path: no live selection required.
            data = ClipboardData(
                text=text,
                type=SelectionType.LINES if '\n' in text else SelectionType.CHARACTERS,
            )
            return self._push_clipboard(data, keep_selection_buffer=buffer,
                                        clear_selection=False)

        if not text:
            self.state.show_message('Nothing selected')
            return False

        kind = SelectionType.LINES if '\n' in text else SelectionType.CHARACTERS
        data = ClipboardData(text=text, type=kind)

        if clear_selection and buffer is not None:
            buffer.exit_selection()
            if self._selection_span is not None and self._selection_span[0] is buffer:
                self._selection_span = None

        return self._push_clipboard(data, keep_selection_buffer=buffer,
                                    clear_selection=clear_selection)

    def _push_clipboard(self, data, keep_selection_buffer=None, clear_selection=True):
        clipboard = self.app.clipboard if getattr(self, 'app', None) else None
        if clipboard is None:
            return False

        try:
            clipboard.set_data(data)
        except Exception as e:
            logger.error(e)
            self.state.show_message('Copy failed')
            return False

        status = getattr(clipboard, 'last_status', 'ok')
        if status == 'failed':
            hint = getattr(clipboard, 'last_error', '') or (
                'Copy failed — enable OSC 52 / tmux set-clipboard'
            )
            self.state.show_message(hint)
            return False

        self._last_copied = data.text
        n = len(data.text)
        self.state.show_message(f'Copied {n} char{"s" if n != 1 else ""}')
        return True
