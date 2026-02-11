import asyncio
from collections import deque
from loguru import logger
from mcp.server.fastmcp import FastMCP
from rttt.connectors.base import Connector
from rttt.connectors.middleware import AsyncMiddleware
from rttt.event import Event, EventType
from rttt.utils import parse_listen


class MCPMiddleware(AsyncMiddleware):
    """Middleware that runs an MCP server for AI tool integration.

    Collects events into ring buffers and exposes MCP tools for
    reading terminal/log output and sending commands to the device.
    Uses Streamable HTTP transport.
    """

    def __init__(self, connector: Connector, listen: str = "127.0.0.1:8090", max_lines: int = 5000):
        super().__init__(connector)
        self.host, self.port = parse_listen(listen)
        self.max_lines = max_lines
        self._terminal_lines = deque(maxlen=max_lines)
        self._log_lines = deque(maxlen=max_lines)
        self._terminal_cursor = 0
        self._log_cursor = 0
        self._terminal_event = asyncio.Event()
        self._mcp = FastMCP("RTTT", host=self.host, port=self.port, log_level="CRITICAL", stateless_http=True)
        self._register_tools()
        self._server_task = None

    def open(self):
        super().open()
        self._server_task = asyncio.run_coroutine_threadsafe(
            self._mcp.run_streamable_http_async(), self._loop
        )
        logger.info(f'MCP server starting on http://{self.host}:{self.port}/mcp')

    def close(self):
        if self._server_task:
            self._server_task.cancel()
            try:
                self._server_task.result(timeout=3)
            except Exception:
                pass
        super().close()

    async def _process(self, event: Event):
        if event.type in (EventType.OUT, EventType.IN):
            direction = "in" if event.type == EventType.IN else "out"
            self._terminal_lines.append({"direction": direction, "text": event.data})
            self._terminal_cursor += 1
            self._terminal_event.set()
        elif event.type == EventType.LOG:
            self._log_lines.append(event.data)
            self._log_cursor += 1

    def _register_tools(self):
        middleware = self

        @self._mcp.tool()
        async def send_command(command: str, timeout: float = 2.0) -> dict:
            """Send a shell command to the connected device via RTT.

            Waits up to `timeout` seconds for the device to finish responding,
            then returns all terminal output produced after the command along
            with a `log_cursor` that can be passed to `read_log` to fetch only
            the logs that appeared since this command was sent.
            """
            try:
                log_cursor = middleware._log_cursor
                terminal_cursor = middleware._terminal_cursor
                middleware._terminal_event.clear()
                middleware.connector.handle(Event(EventType.IN, command))

                # Wait for output lines to arrive; stop after `timeout` seconds
                # of silence (no new terminal data).
                deadline = asyncio.get_event_loop().time() + timeout
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    middleware._terminal_event.clear()
                    try:
                        await asyncio.wait_for(middleware._terminal_event.wait(), timeout=remaining)
                        # Got new data — reset the silence deadline
                        deadline = asyncio.get_event_loop().time() + 0.3
                    except asyncio.TimeoutError:
                        break

                # Collect output lines that appeared after the command
                # Use cursor difference to slice from the right end of the deque,
                # which is safe even when the deque has wrapped around.
                new_count = middleware._terminal_cursor - terminal_cursor
                all_lines = list(middleware._terminal_lines)
                output = all_lines[-new_count:] if new_count > 0 else []
                # Filter out the echo of the sent command
                output = [line for line in output if line["direction"] != "in"]
                return {
                    "status": "ok",
                    "command": command,
                    "output": output,
                    "log_cursor": log_cursor,
                    "log_cursor_end": middleware._log_cursor,
                }
            except Exception as e:
                return {
                    "status": "error",
                    "command": command,
                    "error": str(e),
                }

        @self._mcp.tool()
        def read_terminal(lines: int = 100) -> list:
            """Read recent terminal output. Returns JSON array with direction (in/out) and text."""
            return list(middleware._terminal_lines)[-lines:]

        @self._mcp.tool()
        def read_log(lines: int = 100, after_cursor: int = -1) -> list:
            """Read recent log output from the device. Returns JSON array of log lines.

            If `after_cursor` is provided (from a previous `send_command` result),
            returns only the log lines that appeared after that cursor position.
            Otherwise returns the last `lines` entries.
            """
            all_logs = list(middleware._log_lines)
            if after_cursor >= 0:
                # log_cursor is the total count at command time.
                # The deque holds the last max_lines entries.
                # Offset into the deque: how many entries from the end were after the cursor.
                total = middleware._log_cursor
                available = len(all_logs)
                skip = after_cursor - (total - available)
                if skip < 0:
                    skip = 0
                result = all_logs[skip:]
            else:
                result = all_logs[-lines:]
            return result

        @self._mcp.tool()
        def status() -> dict:
            """Get RTT session statistics: total and buffered terminal/log line counts and current cursors."""
            return {
                "terminal_total": middleware._terminal_cursor,
                "terminal_buffered": len(middleware._terminal_lines),
                "log_total": middleware._log_cursor,
                "log_buffered": len(middleware._log_lines),
                "buffer_size": middleware.max_lines,
            }
