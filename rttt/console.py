import os
import asyncio
from loguru import logger
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.styles import Style, Priority
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.document import Document
from prompt_toolkit.clipboard.base import ClipboardData
from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
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
        # True after mouse-up / copy inclusive bump so we do not double-bump.
        self._selection_inclusive = False

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
            self._ctrl_c_copy()

        @bindings.add("f6", eager=True)
        def _(event):
            self.state.toggle_mouse()
            if self.app:
                self.app.invalidate()

        def _pane_focused():
            # Require a pane focus and exclude Command so eager Ctrl-A / Shift
            # bindings never swallow editing keys when focus just moved.
            if self.has_focus(self.input_field):
                return False
            return self.has_focus(terminal_window) or self.has_focus(logger_window)

        pane_focused = Condition(_pane_focused)

        def _command_focused():
            return self.has_focus(self.input_field)

        command_focused = Condition(_command_focused)

        @bindings.add("s-left", filter=pane_focused, eager=True)
        def _(event):
            self._keyboard_extend('left', count=getattr(event, 'arg', 1) or 1)

        @bindings.add("s-right", filter=pane_focused, eager=True)
        def _(event):
            self._keyboard_extend('right', count=getattr(event, 'arg', 1) or 1)

        @bindings.add("s-up", filter=pane_focused, eager=True)
        def _(event):
            self._keyboard_extend('up', count=getattr(event, 'arg', 1) or 1)

        @bindings.add("s-down", filter=pane_focused, eager=True)
        def _(event):
            self._keyboard_extend('down', count=getattr(event, 'arg', 1) or 1)

        @bindings.add("s-home", filter=pane_focused, eager=True)
        def _(event):
            self._keyboard_extend('home')

        @bindings.add("s-end", filter=pane_focused, eager=True)
        def _(event):
            self._keyboard_extend('end')

        @bindings.add("s-pageup", filter=pane_focused, eager=True)
        def _(event):
            self._keyboard_extend('pageup', count=getattr(event, 'arg', 1) or 1)

        @bindings.add("s-pagedown", filter=pane_focused, eager=True)
        def _(event):
            self._keyboard_extend('pagedown', count=getattr(event, 'arg', 1) or 1)

        @bindings.add("c-a", filter=pane_focused, eager=True)
        def _(event):
            self._keyboard_select_all()

        def _escape_clears_selection():
            if self._pause_origin == 'auto':
                return True
            if self._selection_span is not None:
                return True
            for buf in (self.terminal_buffer, self.logger_buffer):
                if buf.selection_state is not None:
                    return True
            return False

        @bindings.add("escape", filter=Condition(_escape_clears_selection), eager=True)
        def _(event):
            self._clear_pane_selection_on_escape()

        # Vi page-navigation binds Ctrl-U to half-page scroll; keep unix-line
        # discard on the Command line (readline-style).
        @bindings.add("c-u", filter=command_focused, eager=True)
        def _(event):
            # Readline unix-line-discard: kill from start of line to the cursor.
            buff = self.input_field.buffer
            if buff.cursor_position:
                buff.delete_before_cursor(count=buff.cursor_position)

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
            # Mouse on by default (select-to-copy). F6 toggles it off so the
            # terminal's own selection works across panes / without clipboard.
            # Hold Shift while dragging for native selection when the emulator
            # supports that bypass. Overlays force mouse on for buttons.
            mouse_support=Condition(self._wants_mouse),
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

    def _wants_mouse(self):
        """Whether to ask the terminal for mouse reporting.

        On by default for in-app select-to-copy. F6 turns it off. An overlay
        overrides the toggle so its buttons stay clickable.
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

    def _ensure_selection_inclusive(self, buffer):
        """Bump an emacs-exclusive selection so copy matches the highlight.

        The block cursor sits on a cell that looks selected but is past the
        exclusive end. Apply the same +1 as mouse-up unless already bumped.
        """
        if buffer.selection_state is None:
            return
        if self._selection_inclusive:
            return
        self._make_mouse_selection_inclusive(buffer)

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
        self._selection_inclusive = True

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

        Called on the first real drag MOVE (not plain click) or keyboard
        extend. Streaming OUT/LOG keeps appending, but the viewport no longer
        jumps. Origin is set to 'auto' so copy / Esc can resume afterwards.

        Never overrides a manual F5 pause. Returns True when this call flipped
        scroll_to_end off. toast=False avoids invalidate() racing a drag.
        """
        if self._pause_origin == 'manual':
            return False
        if not self.state.scroll_to_end:
            return False
        self.state.scroll_to_end = False
        self._pause_origin = 'auto'
        if toast:
            self.state.show_message('Paused for selection (F5 resumes)', seconds=1.5)
        return True

    def _resume_streaming_after_copy(self):
        """Undo an auto-pause: scroll again and drop the highlight (text is copied).

        Manual F5 pause is never lifted here — callers must check origin.
        """
        if self._pause_origin == 'manual':
            return
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

    def _seed_drag(self, buf, control):
        """Record drag bookkeeping shared by content and gutter presses."""
        self._drag_buffer = buf
        self._paused_for_drag = False
        self._drag_was_scrolling = self.state.scroll_to_end
        self._selection_inclusive = False
        try:
            if self.app is not None:
                self.app.layout.current_control = control
        except Exception:
            pass
        self._clear_other_pane_selection(buf)
        if self._selection_span is not None and self._selection_span[0] is buf:
            self._selection_span = None

    def _install_gutter_mouse_bridge(self, text_area):
        """Route line-number margin clicks into the buffer mouse handler.

        prompt_toolkit only registers the BufferControl handler for the text
        area, not the NumberedMargin. A press on the gutter therefore never
        sees MOUSE_DOWN — only MOVE/UP after the pointer enters the text —
        and BufferControl anchors the selection at the old cursor (usually the
        bottom). Map gutter hits to column 0 of that row so the press is the
        real start of the drag.
        """
        window = text_area.window
        control = text_area.control
        orig_write = window.write_to_screen

        def write_to_screen(screen, mouse_handlers, write_position,
                            parent_style, erase_bg, z_index):
            result = orig_write(
                screen, mouse_handlers, write_position,
                parent_style, erase_bg, z_index)
            info = window.render_info
            if info is None or not window.left_margins:
                return result
            try:
                left_w = sum(window._get_margin_width(m) for m in window.left_margins)
            except Exception:
                return result
            if left_w <= 0:
                return result

            def gutter_mouse(mouse_event):
                rel_y = mouse_event.position.y - write_position.ypos
                if rel_y < 0:
                    return NotImplemented
                row_col = info.visible_line_to_row_col.get(rel_y)
                if row_col is None:
                    # Clamp to the last mapped visible line when past content.
                    if not info.visible_line_to_row_col:
                        return NotImplemented
                    rel_y = max(0, min(rel_y, max(info.visible_line_to_row_col)))
                    row_col = info.visible_line_to_row_col.get(rel_y)
                    if row_col is None:
                        return NotImplemented
                row, _col = row_col
                return control.mouse_handler(MouseEvent(
                    position=Point(x=0, y=row),
                    event_type=mouse_event.event_type,
                    button=mouse_event.button,
                    modifiers=mouse_event.modifiers,
                ))

            mouse_handlers.set_mouse_handler_for_range(
                x_min=write_position.xpos,
                x_max=write_position.xpos + left_w,
                y_min=write_position.ypos,
                y_max=write_position.ypos + write_position.height,
                handler=gutter_mouse,
            )
            return result

        window.write_to_screen = write_to_screen

    def _install_select_to_copy(self, text_area):
        control = text_area.control
        original = control.mouse_handler
        self._install_gutter_mouse_bridge(text_area)

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

            # Missed MOUSE_DOWN (gutter press before the bridge, or any path
            # that delivers MOVE with a button held first): seed the drag and
            # synthesize DOWN so the selection anchors here — not at the old
            # scroll-tip cursor.
            if all((
                mouse_event.event_type == MouseEventType.MOUSE_MOVE,
                self._drag_buffer is None,
                mouse_event.button != MouseButton.NONE,
                mouse_event.button != MouseButton.RIGHT,
            )):
                self._seed_drag(buf, control)
                original(MouseEvent(
                    position=mouse_event.position,
                    event_type=MouseEventType.MOUSE_DOWN,
                    button=mouse_event.button,
                    modifiers=mouse_event.modifiers,
                ))

            if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                self._seed_drag(buf, control)

            result = original(mouse_event)

            if all((
                mouse_event.event_type == MouseEventType.MOUSE_MOVE,
                self._drag_buffer is buf,
                mouse_event.button != MouseButton.NONE,
                not self._paused_for_drag,
                self._drag_was_scrolling,
                buf.selection_state is not None,
            )):
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
                        span = self._selection_span
                        if span is not None and span[0] is buf:
                            self._selection_span = None
                else:
                    buf.exit_selection()
                    span = self._selection_span
                    if span is not None and span[0] is buf:
                        self._selection_span = None

                if copied and self._pause_origin == 'auto':
                    # Streaming was on before the drag — restore it; text is
                    # already on the clipboard (Ctrl-C re-toasts via _last_copied).
                    n = len(self._last_copied)
                    self._resume_streaming_after_copy()
                    self.state.show_message(
                        f'Copied {n} char{"s" if n != 1 else ""} — resumed')
                elif copied and self.state.scroll_to_end:
                    # Copied without an auto-pause (should be rare). A sticky
                    # highlight would pin the viewport while follow is on —
                    # clear it so the pane keeps streaming.
                    self._selection_span = None
                    buf.exit_selection()
                    buf.cursor_position = len(buf.text)
                elif not copied and auto_paused:
                    # Aborted drag that had auto-paused — do not leave Pause on.
                    self._resume_streaming_after_copy()
                # Manual F5 pause: stay paused; sticky highlight kept on copy.
            return result

        control.mouse_handler = mouse_handler

    def _right_click_copy_pane(self, buffer):
        """Right-click on Log/Terminal: same copy/resume rules as Ctrl-C.

        Auto-pause resumes with ``— resumed``; manual F5 stays paused.
        Does not change focus. No selection → re-toast ``_last_copied``.
        """
        return self._copy_pane_with_optional_resume(buffer)

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

    def _focused_pane_buffer(self):
        """Read-only pane buffer holding the focus, or None."""
        return self._focused_copy_buffer()

    def _sync_selection_span(self, buffer):
        ss = buffer.selection_state
        if ss is None:
            if self._selection_span is not None and self._selection_span[0] is buffer:
                self._selection_span = None
            return
        lo, hi = sorted([ss.original_cursor_position, buffer.cursor_position])
        if hi > lo:
            self._selection_span = (buffer, lo, hi)
        elif self._selection_span is not None and self._selection_span[0] is buffer:
            self._selection_span = None

    def _pane_window_for_buffer(self, buffer):
        if buffer is self.terminal_buffer:
            return self.terminal_window
        if buffer is self.logger_buffer:
            return self.logger_window
        return None

    def _page_row_count(self, buffer):
        """Visible rows for PageUp/Down selection, falling back to 10."""
        window = self._pane_window_for_buffer(buffer)
        if window is None:
            return 10
        win = getattr(window, 'window', None)
        info = getattr(win, 'render_info', None) if win is not None else None
        if info is not None and getattr(info, 'window_height', None):
            return max(1, int(info.window_height) - 1)
        return 10

    def _keyboard_extend(self, direction, count=1):
        """Grow the selection in the focused pane (Shift+arrows / page).

        Pauses scroll before moving (same auto origin as a mouse drag) so a
        streaming append cannot snap the cursor back mid-gesture. Uses
        Buffer.cursor_up/down so preferred_column is preserved across lines.
        """
        buffer = self._focused_pane_buffer()
        if buffer is None:
            return
        count = max(1, int(count or 1))

        # Freeze streaming first — never overrides a manual F5 pause.
        if self.state.scroll_to_end:
            self._pause_auto_scroll_for_selection(toast=True)

        before = buffer.cursor_position
        if buffer.selection_state is None:
            buffer.start_selection()
            self._clear_other_pane_selection(buffer)
            self._selection_inclusive = False

        if direction == 'left':
            buffer.cursor_position = max(0, buffer.cursor_position - count)
        elif direction == 'right':
            buffer.cursor_position = min(len(buffer.text), buffer.cursor_position + count)
        elif direction == 'up':
            buffer.cursor_up(count=count)
        elif direction == 'down':
            buffer.cursor_down(count=count)
        elif direction == 'home':
            buffer.cursor_position += buffer.document.get_start_of_line_position(
                after_whitespace=False)
        elif direction == 'end':
            buffer.cursor_position += buffer.document.get_end_of_line_position()
        elif direction == 'pageup':
            buffer.cursor_up(count=count * self._page_row_count(buffer))
        elif direction == 'pagedown':
            buffer.cursor_down(count=count * self._page_row_count(buffer))
        else:
            return

        if buffer.cursor_position == before:
            # No movement (e.g. Shift+Up on the first line) — drop an empty mark
            # so Ctrl-C does not look broken.
            ss = buffer.selection_state
            if ss is not None and ss.original_cursor_position == before:
                buffer.exit_selection()
                if self._selection_span is not None and self._selection_span[0] is buffer:
                    self._selection_span = None
            return

        self._sync_selection_span(buffer)

    def _keyboard_select_all(self):
        buffer = self._focused_pane_buffer()
        if buffer is None:
            return
        if self.state.scroll_to_end:
            self._pause_auto_scroll_for_selection(toast=True)
        self._selection_inclusive = False
        buffer.cursor_position = 0
        buffer.start_selection()
        buffer.cursor_position = len(buffer.text)
        self._clear_other_pane_selection(buffer)
        if buffer.text:
            self._selection_span = (buffer, 0, len(buffer.text))
        else:
            buffer.exit_selection()

    def _clear_pane_selection_on_escape(self):
        """Esc: drop highlight; resume only an automatic selection pause."""
        self._selection_span = None
        for buf in (self.terminal_buffer, self.logger_buffer):
            buf.exit_selection()
        if self._pause_origin == 'auto':
            self._resume_streaming_after_copy()

    def _copy_pane_with_optional_resume(self, buffer):
        """Copy one pane; resume when the pause was automatic and text was selected.

        Shared by Ctrl-C and right-click so both honour the same auto/manual
        rules. Manual F5 pause is never lifted.
        """
        if buffer is not None and buffer.selection_state is not None:
            # Match the highlighted range (include the cell under the cursor),
            # same inclusive adjustment mouse-up applies before copy.
            self._ensure_selection_inclusive(buffer)
        had_selection = bool(buffer is not None and self._selected_text(buffer))
        was_auto = self._pause_origin == 'auto'
        if was_auto and had_selection:
            if self._copy_from_buffer(buffer, clear_selection=False):
                n = len(self._last_copied)
                self._resume_streaming_after_copy()
                self.state.show_message(
                    f'Copied {n} char{"s" if n != 1 else ""} — resumed')
                return True
            return False
        # Manual pause: keep sticky highlight (same as mouse select-to-copy).
        keep_sticky = all((
            buffer is not None,
            self._pause_origin == 'manual',
            had_selection,
        ))
        return self._copy_from_buffer(buffer, clear_selection=not keep_sticky)

    def _ctrl_c_copy(self):
        """Ctrl-C / Ctrl-Insert: copy focused pane (shared resume rules)."""
        self._copy_pane_with_optional_resume(self._focused_copy_buffer())

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
