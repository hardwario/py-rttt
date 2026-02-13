import asyncio
import os
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

    def __init__(self, connector: Connector, listen: str = "127.0.0.1:8090", max_lines: int = 5000, name: str = "Device Console"):
        super().__init__(connector)
        self.host, self.port = parse_listen(listen)
        self.max_lines = max_lines
        self._terminal_lines = deque(maxlen=max_lines)
        self._log_lines = deque(maxlen=max_lines)
        self._terminal_cursor = 0
        self._log_cursor = 0
        self._terminal_event = asyncio.Event()
        self._flash_events = []
        self._flash_done = None
        self._mcp = FastMCP(name, host=self.host, port=self.port, log_level="CRITICAL", stateless_http=True)
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
        elif event.type == EventType.FLASH:
            self._flash_events.append(event.data)
            if event.data.get("status") in ("done", "error") and self._flash_done is not None:
                self._flash_done.set()

    def _register_tools(self):
        middleware = self

        @self._mcp.tool()
        async def send_command(command: str, timeout: float = 2.0) -> dict:
            """Send a shell command to the connected device.

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
        def read_log(lines: int = -1, after_cursor: int = -1, pattern: str = "") -> list:
            """Read log output from the device. Logs are kept in a ring buffer
            and can be read repeatedly. If you need buffer state, use the `status` tool.

            Args:
                lines: Number of lines to return (-1 = all buffered).
                after_cursor: Return only lines after this cursor (from `send_command`).
                pattern: Regex filter (case-insensitive). Only matching lines are returned.
            """
            import re
            all_logs = list(middleware._log_lines)
            if after_cursor >= 0:
                total = middleware._log_cursor
                available = len(all_logs)
                skip = after_cursor - (total - available)
                if skip < 0:
                    skip = 0
                result = all_logs[skip:]
            elif lines >= 0:
                result = all_logs[-lines:] if lines > 0 else []
            else:
                result = all_logs
            if pattern:
                try:
                    regex = re.compile(pattern, re.IGNORECASE)
                    result = [line for line in result if regex.search(line)]
                except re.error:
                    pass
            return result

        @self._mcp.tool()
        def status() -> dict:
            """Get session statistics: total and buffered terminal/log line counts and current cursors."""
            return {
                "terminal_total": middleware._terminal_cursor,
                "terminal_buffered": len(middleware._terminal_lines),
                "log_total": middleware._log_cursor,
                "log_buffered": len(middleware._log_lines),
                "buffer_size": middleware.max_lines,
            }

        @self._mcp.tool()
        async def flash(file_path: str, addr: int = 0) -> dict:
            """Flash a firmware file (.hex, .bin, .elf, .srec) to the target device.

            Stops the connection, programs the flash memory, resets the device,
            and restarts the connection. Progress is reported to the console during flashing.

            Common firmware locations:
            - Zephyr/west: build/merged.hex (or build/<app>/merged.hex)
            - nRF Connect SDK: build/merged.hex
            - Generic: build/firmware.hex, build/output.bin

            Args:
                file_path: Absolute path to the firmware file.
                addr: Start address for flashing (default 0, ignored for .hex files).
            """
            if not os.path.isfile(file_path):
                return {"status": "error", "error": f"File not found: {file_path}"}

            middleware._flash_events = []
            middleware._flash_done = asyncio.Event()

            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    middleware.connector.handle,
                    Event(EventType.FLASH, {"file": file_path, "addr": addr})
                )

                try:
                    await asyncio.wait_for(middleware._flash_done.wait(), timeout=120.0)
                except asyncio.TimeoutError:
                    return {"status": "error", "error": "Flash operation timed out"}

                final = middleware._flash_events[-1] if middleware._flash_events else {}
                result = {
                    "status": final.get("status", "unknown"),
                    "file": file_path,
                    "events": middleware._flash_events,
                }
                if "bytes_flashed" in final:
                    result["bytes_flashed"] = final["bytes_flashed"]
                if "error" in final:
                    result["error"] = final["error"]
                return result

            except Exception as e:
                return {"status": "error", "error": str(e)}
            finally:
                middleware._flash_done = None
