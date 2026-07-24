import asyncio
import hashlib
import os
import socket
import tempfile
from collections import deque
from loguru import logger
from mcp.server.fastmcp import FastMCP
from rttt.connectors.base import Connector
from rttt.connectors.middleware import AsyncMiddleware
from rttt.event import Event, EventType
from rttt.utils import parse_listen


class _BearerAuthMiddleware:
    """ASGI middleware requiring `Authorization: Bearer <token>` on every
    HTTP request (covers both the /mcp endpoint and /upload)."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            headers = dict(scope.get('headers') or [])
            auth = headers.get(b'authorization', b'').decode('latin-1')
            if auth != f'Bearer {self.token}':
                from starlette.responses import JSONResponse
                response = JSONResponse(
                    {"status": "error", "error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _hexdump(addr: int, data: bytes) -> list:
    """Format bytes as classic hexdump lines with address and ASCII column."""
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hexpart = ' '.join(f'{b:02x}' for b in chunk)
        asciipart = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f'0x{addr + off:08X}: {hexpart:<47} |{asciipart}|')
    return lines


class MCPPortInUseError(Exception):
    """Raised when the MCP server port is already bound by another process."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        super().__init__(f'MCP port {host}:{port} is already in use')


class MCPMiddleware(AsyncMiddleware):
    """Middleware that runs an MCP server for AI tool integration.

    Collects events into ring buffers and exposes MCP tools for
    reading terminal/log output and sending commands to the device.
    Uses Streamable HTTP transport.
    """

    def __init__(self, connector: Connector, listen: str = "127.0.0.1:8090", max_lines: int = 5000, name: str = "Device Console", token: str = None):
        super().__init__(connector)
        self.host, self.port = parse_listen(listen)
        self._check_port_available(self.host, self.port)
        self.token = token
        self.max_lines = max_lines
        self._terminal_lines = deque(maxlen=max_lines)
        self._log_lines = deque(maxlen=max_lines)
        self._terminal_cursor = 0
        self._log_cursor = 0
        self._terminal_event = asyncio.Event()
        self._flash_events = []
        self._flash_done = None
        self._flash_activity = None
        self._upload_dir = os.path.join(tempfile.gettempdir(), f'rttt-uploads-{self.port}')
        self._mcp = FastMCP(name, host=self.host, port=self.port, log_level="CRITICAL", stateless_http=True)
        self._register_tools()
        self._register_routes()
        self._server_task = None

    @staticmethod
    def _check_port_available(host: str, port: int) -> None:
        """Verify the TCP port can be bound. Raises MCPPortInUseError otherwise.

        Running two MCP servers on the same port otherwise fails deep inside
        uvicorn's background thread and has been observed to segfault the
        whole process, so we fail fast with a clear error up front.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
        except OSError as e:
            raise MCPPortInUseError(host, port) from e
        finally:
            sock.close()

    async def _serve(self):
        """Run the Streamable HTTP server, optionally wrapped in bearer auth.

        Mirrors FastMCP.run_streamable_http_async, which offers no hook to
        wrap the ASGI app.
        """
        import uvicorn

        app = self._mcp.streamable_http_app()
        if self.token:
            app = _BearerAuthMiddleware(app, self.token)
        config = uvicorn.Config(app, host=self.host, port=self.port, log_level='critical')
        await uvicorn.Server(config).serve()

    def open(self):
        super().open()
        self._server_task = asyncio.run_coroutine_threadsafe(self._serve(), self._loop)
        auth = 'bearer-token auth' if self.token else 'no auth'
        logger.info(f'MCP server starting on http://{self.host}:{self.port}/mcp ({auth})')

    def close(self):
        if self._server_task:
            self._server_task.cancel()
            try:
                self._server_task.result(timeout=3)
            except Exception:
                pass
        super().close()

    def _register_routes(self):
        """Register plain HTTP routes served alongside the /mcp endpoint."""
        middleware = self

        @self._mcp.custom_route('/upload', methods=['POST', 'PUT'])
        async def upload(request):
            """Receive a firmware file as the raw request body and store it
            in a temp directory, returning the server-side path for `flash`.

            Usage: curl --data-binary @fw.hex 'http://host:port/upload?filename=fw.hex'
            """
            from starlette.responses import JSONResponse

            filename = os.path.basename(request.query_params.get('filename', ''))
            if not filename:
                return JSONResponse(
                    {"status": "error", "error": "missing ?filename= query parameter"},
                    status_code=400)
            if os.path.splitext(filename)[1].lower() not in ('.hex', '.bin', '.elf', '.srec'):
                return JSONResponse(
                    {"status": "error", "error": "filename must end with .hex, .bin, .elf or .srec"},
                    status_code=400)

            data = await request.body()
            if not data:
                return JSONResponse({"status": "error", "error": "empty body"}, status_code=400)
            if len(data) > 64 * 1024 * 1024:
                return JSONResponse({"status": "error", "error": "file too large (max 64 MiB)"},
                                    status_code=413)

            os.makedirs(middleware._upload_dir, exist_ok=True)
            path = os.path.join(middleware._upload_dir, filename)
            with open(path, 'wb') as f:
                f.write(data)
            logger.info(f'Upload: stored {len(data)} bytes at {path}')
            return JSONResponse({"status": "ok", "path": path, "size": len(data)})

    def _leaf(self):
        """Descend the middleware chain to the leaf (J-Link) connector."""
        conn = self.connector
        while hasattr(conn, 'connector'):
            conn = conn.connector
        return conn

    async def _run_target_op(self, func, timeout: float = 10.0) -> dict:
        """Run a blocking J-Link operation in an executor with a timeout.

        Target-state operations are serialized through the connector's
        _op_lock (shared with flash), so e.g. a reset cannot interleave
        with a running flash. Returns {"status": "ok", **result} or
        {"status": "error", ...}.
        """
        lock = getattr(self._leaf(), '_op_lock', None)
        if lock is not None and not lock.acquire(blocking=False):
            return {"status": "error",
                    "error": "Another target operation is in progress, retry in a moment"}

        def wrapper():
            # Release inside the executor so the lock is held for as long as
            # the operation actually runs, even past an asyncio timeout.
            try:
                return func()
            finally:
                if lock is not None:
                    lock.release()

        try:
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(loop.run_in_executor(None, wrapper), timeout=timeout)
            out = {"status": "ok"}
            if isinstance(result, dict):
                out.update(result)
            return out
        except asyncio.TimeoutError:
            return {"status": "error", "error": "Operation timed out"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def _process(self, event: Event):
        if event.type in (EventType.OUT, EventType.IN):
            direction = "in" if event.type == EventType.IN else "out"
            self._terminal_lines.append({"direction": direction, "text": event.data})
            self._terminal_cursor += 1
            # Only device output counts as activity — the echo of a sent
            # command must not reset send_command's silence window, or the
            # wait collapses to ~0.3 s regardless of the requested timeout.
            if event.type == EventType.OUT:
                self._terminal_event.set()
        elif event.type == EventType.LOG:
            self._log_lines.append(event.data)
            self._log_cursor += 1
        elif event.type == EventType.FLASH:
            # skip per-sector progress events — hundreds of them per flash
            if event.data.get("status") != "progress":
                self._flash_events.append(event.data)
            if self._flash_activity is not None:
                self._flash_activity.set()
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
                # Drop everything up to and including the echo of this
                # command — late output of a previous command may have
                # landed in the buffer just before this one was sent.
                for i in range(len(output) - 1, -1, -1):
                    if output[i]["direction"] == "in" and output[i]["text"] == command:
                        output = output[i + 1:]
                        break
                # Filter out echoes of any commands
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
            and restarts the connection. Progress is reported to the console during
            flashing. The buffered terminal/log history is cleared, so read_log
            and read_terminal afterwards only show output from the new firmware.

            Common firmware locations:
            - Zephyr/west: build/merged.hex (or build/<app>/merged.hex)
            - nRF Connect SDK: build/merged.hex
            - Generic: build/firmware.hex, build/output.bin

            The path is resolved on the machine this server runs on. If the
            firmware file is on a different machine, upload it first via the
            HTTP endpoint served on the same port:
            `curl --data-binary @fw.hex 'http://<host>:<port>/upload?filename=fw.hex'`
            — the JSON response contains the server-side `path` to pass here.

            Args:
                file_path: Absolute path to the firmware file.
                addr: Start address for flashing (default 0, ignored for .hex files).
            """
            if not os.path.isfile(file_path):
                return {"status": "error", "error": f"File not found: {file_path}"}

            # New firmware means a new session — drop the terminal/log history
            # so read_log/read_terminal only show output from the flashed image.
            # Cursors stay monotonic, so cursors obtained before the flash
            # remain valid and simply return only post-flash lines.
            middleware._terminal_lines.clear()
            middleware._log_lines.clear()

            middleware._flash_events = []
            middleware._flash_done = asyncio.Event()
            middleware._flash_activity = asyncio.Event()

            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    middleware.connector.handle,
                    Event(EventType.FLASH, {"file": file_path, "addr": addr})
                )

                # Wait for completion with a stall detector instead of a fixed
                # overall limit: any flash event (including per-sector progress)
                # counts as activity, so big images over a slow SWD link are
                # fine while a genuinely stuck operation still errors out.
                while not middleware._flash_done.is_set():
                    middleware._flash_activity.clear()
                    try:
                        await asyncio.wait_for(middleware._flash_activity.wait(), timeout=60.0)
                    except asyncio.TimeoutError:
                        return {"status": "error",
                                "error": "Flash operation stalled (no progress for 60 s)"}

                final = middleware._flash_events[-1] if middleware._flash_events else {}
                result = {
                    "status": final.get("status", "unknown"),
                    "file": file_path,
                    "events": middleware._flash_events,
                }
                if "error" in final:
                    result["error"] = final["error"]
                return result

            except Exception as e:
                return {"status": "error", "error": str(e)}
            finally:
                middleware._flash_done = None
                middleware._flash_activity = None

        @self._mcp.tool()
        async def reconnect(address: str = "") -> dict:
            """Restart the RTT connection to the device.

            Use when the RTT session appears stuck — e.g. commands fail with
            "Shell buffer DOWN 0 has zero size", no output arrives anymore,
            or the device was reset/reflashed outside this tool. Does not
            reset the device, only re-attaches to its RTT control block.

            Args:
                address: Optional RTT control block address, e.g. "0x20004000"
                    (the `_SEGGER_RTT` symbol address from the firmware ELF:
                    `nm zephyr.elf | grep _SEGGER_RTT`). Overrides the J-Link
                    auto-search, which can attach to a stale control block left
                    in RAM by a previous firmware. Sticky for subsequent
                    connections; pass "auto" to return to auto-search. Empty
                    keeps the current setting.
            """
            conn = middleware._leaf()

            if address:
                if address.strip().lower() == "auto":
                    conn.block_address = None
                else:
                    try:
                        conn.block_address = int(address, 0)
                    except ValueError:
                        return {"status": "error",
                                "error": f"Invalid address: {address!r} (use hex like 0x20004000, or \"auto\")"}

            def _do():
                conn.stop()
                conn.start()

            return await middleware._run_target_op(_do, timeout=30.0)

        @self._mcp.tool()
        async def reset(halt: bool = False) -> dict:
            """Reset the target device.

            With halt=False the firmware restarts and the RTT session is
            re-attached automatically. With halt=True the CPU stays halted
            at the reset vector and RTT is stopped — use `go` followed by
            `reconnect` to resume.
            """
            conn = middleware._leaf()
            result = await middleware._run_target_op(lambda: conn.reset(halt=halt), timeout=30.0)
            if result["status"] == "ok":
                result["halted"] = halt
            return result

        @self._mcp.tool()
        async def halt() -> dict:
            """Halt the target CPU. RTT keeps working (RAM stays accessible),
            but the firmware stops running until `go` or `reset`."""
            conn = middleware._leaf()

            def _do():
                conn.jlink.halt()
                return {"halted": conn.jlink.halted()}

            return await middleware._run_target_op(_do)

        @self._mcp.tool()
        async def go() -> dict:
            """Resume the halted target CPU."""
            conn = middleware._leaf()

            def _do():
                conn.jlink.restart()
                return {"halted": conn.jlink.halted()}

            return await middleware._run_target_op(_do)

        @self._mcp.tool()
        async def target_status() -> dict:
            """Get target CPU state: halted flag and core identification."""
            conn = middleware._leaf()

            def _do():
                return {
                    "halted": conn.jlink.halted(),
                    "core_id": f'0x{conn.jlink.core_id():08X}',
                    "device": conn.jlink.core_name(),
                }

            return await middleware._run_target_op(_do)

        @self._mcp.tool()
        async def read_memory(address: str, length: int = 64, width: int = 8,
                              to_file: str = "") -> dict:
            """Read target memory (RAM, flash, peripherals) via J-Link.

            Works while the firmware is running. Flash is memory-mapped, so
            this also serves as flash read.

            Args:
                address: Start address, e.g. "0x10000" or decimal.
                length: Number of bytes to read (max 65536 inline, 16 MiB
                    with `to_file`).
                width: Access width in bits: 8, 16 or 32 (use 32 for
                    peripheral registers that ignore byte accesses).
                to_file: Optional path to dump the raw bytes to instead of
                    returning them inline (use for large blocks — the reply
                    then carries only the path, size, sha256 and a short
                    preview). A relative path is placed in the system temp
                    directory. The path is resolved on the machine this
                    server runs on.
            """
            conn = middleware._leaf()
            try:
                addr = int(address, 0)
            except ValueError:
                return {"status": "error", "error": f"Invalid address: {address}"}
            if width not in (8, 16, 32):
                return {"status": "error", "error": "width must be 8, 16 or 32"}
            length = min(int(length), 16 * 1024 * 1024 if to_file else 65536)
            unit = width // 8

            def _read_block(a, n):
                num_units = (n + unit - 1) // unit
                units = conn.jlink.memory_read(a, num_units, nbits=width)
                return b''.join(int(u).to_bytes(unit, 'little') for u in units)[:n]

            if to_file:
                path = to_file if os.path.isabs(to_file) else os.path.join(
                    tempfile.gettempdir(), to_file)

                def _do():
                    digest = hashlib.sha256()
                    total = 0
                    preview = b''
                    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
                    with open(path, 'wb') as f:
                        while total < length:
                            chunk = _read_block(addr + total, min(65536, length - total))
                            f.write(chunk)
                            digest.update(chunk)
                            if not preview:
                                preview = chunk[:64]
                            total += len(chunk)
                    return {
                        "address": f'0x{addr:08X}',
                        "length": total,
                        "file": path,
                        "sha256": digest.hexdigest(),
                        "preview": _hexdump(addr, preview),
                    }

                return await middleware._run_target_op(_do, timeout=30.0 + length / 50000)

            def _do():
                data = _read_block(addr, length)
                return {
                    "address": f'0x{addr:08X}',
                    "length": len(data),
                    "hex": data.hex(),
                    "dump": _hexdump(addr, data),
                }

            return await middleware._run_target_op(_do)

        @self._mcp.tool()
        async def write_memory(address: str, data: str = "", width: int = 8,
                               from_file: str = "") -> dict:
            """Write target memory (RAM or peripheral registers) via J-Link.

            Does NOT program flash — use `write_flash` for that.

            Args:
                address: Start address, e.g. "0x20000000" or decimal.
                data: Hex byte string, e.g. "deadbeef" (little-endian units).
                width: Access width in bits: 8, 16 or 32.
                from_file: Optional path to a file whose raw bytes are written
                    instead of `data` (use for large blocks). The path is
                    resolved on the machine this server runs on.
            """
            conn = middleware._leaf()
            try:
                addr = int(address, 0)
                if from_file:
                    if not os.path.isfile(from_file):
                        return {"status": "error", "error": f"File not found: {from_file}"}
                    with open(from_file, 'rb') as f:
                        raw = f.read()
                else:
                    raw = bytes.fromhex(data.replace(' ', ''))
            except (ValueError, OSError) as e:
                return {"status": "error", "error": str(e)}
            if width not in (8, 16, 32):
                return {"status": "error", "error": "width must be 8, 16 or 32"}
            unit = width // 8
            if len(raw) % unit:
                return {"status": "error", "error": f"data length must be a multiple of {unit} bytes"}
            units = [int.from_bytes(raw[i:i + unit], 'little') for i in range(0, len(raw), unit)]

            def _do():
                written = conn.jlink.memory_write(addr, units, nbits=width)
                return {"address": f'0x{addr:08X}', "units_written": written}

            return await middleware._run_target_op(_do)

        @self._mcp.tool()
        async def write_flash(address: str, data: str = "", from_file: str = "") -> dict:
            """Program internal flash at the given address via J-Link.

            Resets and halts the target, programs (with erase) the given
            bytes, then resets and re-attaches RTT. The firmware reboots.

            Args:
                address: Flash address, e.g. "0x10000".
                data: Hex byte string to program, e.g. "96f3b83d...".
                from_file: Optional path to a file whose raw bytes are
                    programmed instead of `data` (use for large blobs; for
                    whole .hex/.elf images prefer the `flash` tool). The path
                    is resolved on the machine this server runs on.
            """
            conn = middleware._leaf()
            try:
                addr = int(address, 0)
                if from_file:
                    if not os.path.isfile(from_file):
                        return {"status": "error", "error": f"File not found: {from_file}"}
                    with open(from_file, 'rb') as f:
                        raw = f.read()
                else:
                    raw = bytes.fromhex(data.replace(' ', ''))
            except (ValueError, OSError) as e:
                return {"status": "error", "error": str(e)}
            if not raw:
                return {"status": "error", "error": "no data"}

            def _do():
                was_running = conn.is_running
                if was_running:
                    conn.stop()
                try:
                    conn.jlink.reset(ms=10, halt=True)
                    conn.jlink.flash_write(addr, list(raw), nbits=8)
                    try:
                        conn.jlink.exec_command('InvalidateCache')
                    except Exception:
                        pass
                    conn.jlink.reset(ms=10, halt=False)
                finally:
                    if was_running:
                        conn.start()
                return {"address": f'0x{addr:08X}', "bytes": len(raw)}

            return await middleware._run_target_op(_do, timeout=60.0)

        @self._mcp.tool()
        async def read_registers() -> dict:
            """Read core CPU registers (R0-R15, PSR, MSP, PSP, ...).

            The target must be halted (use `halt` first), register access
            on a running Cortex-M is not possible.
            """
            conn = middleware._leaf()

            def _do():
                if not conn.jlink.halted():
                    raise RuntimeError('Target is running — call `halt` first')
                indices = conn.jlink.register_list()
                values = conn.jlink.register_read_multiple(indices)
                return {
                    "registers": {
                        conn.jlink.register_name(idx): f'0x{val:08X}'
                        for idx, val in zip(indices, values)
                    }
                }

            return await middleware._run_target_op(_do)

        @self._mcp.tool()
        async def memory_zones() -> dict:
            """List memory zones the J-Link supports for the current target."""
            conn = middleware._leaf()

            def _do():
                zones = []
                for z in conn.jlink.memory_zones():
                    name = z.sName.decode() if isinstance(z.sName, bytes) else str(z.sName)
                    desc = z.sDesc.decode() if isinstance(z.sDesc, bytes) else str(z.sDesc)
                    zones.append({"name": name, "description": desc,
                                  "virtual_address": f'0x{z.VirtAddr:08X}'})
                return {"zones": zones}

            return await middleware._run_target_op(_do)

        @self._mcp.prompt()
        def debug_device() -> str:
            """How to debug the connected embedded device with this server's tools."""
            return (
                "You are connected to an embedded device (Zephyr/nRF style) over a\n"
                "J-Link RTT session. Available workflows:\n"
                "\n"
                "Inspect what the firmware is doing:\n"
                "- `read_log` shows buffered device logs; filter with `pattern`,\n"
                "  or pass `after_cursor` from a `send_command` result to see only\n"
                "  logs caused by that command.\n"
                "- `send_command` talks to the device shell (try `help`, `kernel uptime`,\n"
                "  `info show`). Increase `timeout` for slow commands. Output belongs to\n"
                "  the sent command only; logs are separate (see `read_log`).\n"
                "\n"
                "Flash new firmware:\n"
                "- `flash` with an absolute path to .hex/.bin/.elf. The device reboots\n"
                "  and RTT re-attaches automatically. Verify with `send_command`\n"
                "  (e.g. `info show`) or by watching `read_log` for the boot banner.\n"
                "- The path must exist on the machine running this server. If your\n"
                "  firmware is local to you, upload it first (same host and port):\n"
                "  curl --data-binary @fw.hex 'http://HOST:PORT/upload?filename=fw.hex'\n"
                "  and pass the returned `path` to `flash`.\n"
                "\n"
                "Low-level debugging (use sparingly, prefer shell/logs):\n"
                "- `target_status` shows whether the CPU runs; `halt` stops it,\n"
                "  `go` resumes, `reset` reboots (halt=True stays at reset vector).\n"
                "- `read_registers` requires a halted CPU (call `halt` first);\n"
                "  PC/LR/SP tell you where the firmware is stuck.\n"
                "- `read_memory`/`write_memory` access RAM and peripherals live;\n"
                "  flash is memory-mapped so `read_memory` also reads flash.\n"
                "- `write_flash` programs internal flash bytes (device reboots).\n"
                "\n"
                "Troubleshooting:\n"
                "- Commands fail or no output arrives: call `reconnect` (re-attaches\n"
                "  RTT without resetting the device), then retry.\n"
                "- Still dead: `reset`, wait a few seconds, retry `send_command`.\n"
                "- Device shell echoes garbage right after boot: wait 2-3 s and retry.\n"
            )
