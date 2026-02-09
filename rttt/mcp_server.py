import os
import sys
import re
import asyncio
import time
from collections import deque

import click
from loguru import logger
from mcp.server.fastmcp import FastMCP

from rttt import __version__ as version
from rttt.connectors import PyLinkRTTConnector, FileLogConnector, DemoConnector
from rttt.event import Event, EventType
from rttt.cli import get_default_map, IntOrHexParamType, DEFAULT_LOG_FILE

_connector = None
_terminal_lines = deque(maxlen=1000)
_logger_lines = deque(maxlen=1000)
_connected = False
_device_info = {}

mcp = FastMCP("rttt")


def _event_handler(event: Event):
    global _connected
    if event.type == EventType.OUT:
        _terminal_lines.append(event.data)
    elif event.type == EventType.IN:
        _terminal_lines.append(f"> {event.data}")
    elif event.type == EventType.LOG:
        _logger_lines.append(event.data)
    elif event.type == EventType.OPEN:
        _connected = True
    elif event.type == EventType.CLOSE:
        _connected = False


@mcp.tool()
async def send_command(command: str) -> str:
    """Send a shell command to the embedded device via RTT terminal.
    Returns recent terminal output after the command."""
    if not _connected:
        return "Error: Not connected to device"

    before = len(_terminal_lines)
    _connector.handle(Event(EventType.IN, command))

    for _ in range(10):
        await asyncio.sleep(0.1)
        if len(_terminal_lines) > before + 1:
            break

    new_lines = list(_terminal_lines)[before:]
    return "\n".join(new_lines) if new_lines else "(no output)"


@mcp.tool()
async def read_terminal(lines: int = 50) -> str:
    """Read recent lines from the RTT terminal output buffer."""
    recent = list(_terminal_lines)[-lines:]
    return "\n".join(recent) if recent else "(no terminal output)"


@mcp.tool()
async def read_logs(lines: int = 50) -> str:
    """Read recent lines from the RTT logger buffer."""
    recent = list(_logger_lines)[-lines:]
    return "\n".join(recent) if recent else "(no log output)"


@mcp.tool()
async def get_status() -> str:
    """Get RTT connection status and device info."""
    status = "connected" if _connected else "disconnected"
    info_lines = [f"Status: {status}"]
    for k, v in _device_info.items():
        info_lines.append(f"{k}: {v}")
    info_lines.append(f"Terminal buffer lines: {len(_terminal_lines)}")
    info_lines.append(f"Logger buffer lines: {len(_logger_lines)}")
    return "\n".join(info_lines)


@mcp.tool()
async def wait_for_output(pattern: str, timeout: int = 30) -> str:
    """Wait for a regex pattern to appear in new terminal or log output.
    Returns the matching line or a timeout message."""
    if not _connected:
        return "Error: Not connected to device"

    regex = re.compile(pattern)
    start_terminal = len(_terminal_lines)
    start_logger = len(_logger_lines)
    start_time = time.time()

    while time.time() - start_time < timeout:
        tlines = list(_terminal_lines)
        for line in tlines[min(start_terminal, len(tlines)):]:
            if regex.search(line):
                return f"Match found in terminal: {line}"

        llines = list(_logger_lines)
        for line in llines[min(start_logger, len(llines)):]:
            if regex.search(line):
                return f"Match found in logs: {line}"

        await asyncio.sleep(0.1)

    return f"Timeout: pattern '{pattern}' not found after {timeout}s"


@click.command('rttt-mcp')
@click.version_option(version, prog_name='rttt-mcp')
@click.option('--demo', is_flag=True, help='Use demo connector (no hardware needed).')
@click.option('--serial', type=int, metavar='SERIAL_NUMBER', help='J-Link serial number.')
@click.option('--device', type=str, metavar='DEVICE', help='J-Link Device name.')
@click.option('--speed', type=int, metavar='SPEED', help='J-Link clock speed in kHz.', default=2000, show_default=True)
@click.option('--reset', is_flag=True, help='Reset application firmware.')
@click.option('--address', metavar='ADDRESS', type=IntOrHexParamType(), help='RTT block address.')
@click.option('--terminal-buffer', type=int, default=0, help='RTT Terminal buffer index.', show_default=True)
@click.option('--logger-buffer', type=int, default=1, help='RTT Logger buffer index.', show_default=True)
@click.option('--latency', type=int, default=50, help='Latency for RTT readout in ms.', show_default=True)
@click.option('--console-file', type=click.Path(writable=True), default=None, help='Also log to file.')
def cli(demo, serial, device, speed, reset, address, terminal_buffer, logger_buffer, latency, console_file):
    """HARDWARIO RTTT MCP Server for Claude Code integration."""
    global _connector, _device_info
    import pylink

    if demo:
        _device_info = {'mode': 'demo'}
        _connector = DemoConnector()
    else:
        if not device:
            raise click.UsageError("--device is required (or use --demo)")

        jlink = pylink.JLink()
        jlink.open(serial_no=serial)
        jlink.set_speed(speed)
        jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)

        logger.info(f'J-Link serial_number: {jlink.serial_number}')
        logger.info(f'J-Link firmware_version: {jlink.firmware_version}')

        jlink.connect(device)

        if reset:
            jlink.reset()
            jlink.go()
            time.sleep(1)

        _device_info = {
            'device': device,
            'serial': serial or jlink.serial_number,
            'speed': f'{speed} kHz',
        }

        _connector = PyLinkRTTConnector(jlink, terminal_buffer, logger_buffer, latency, block_address=address)

    if console_file:
        text = f'MCP - Device: {_device_info.get("device", "demo")}'
        _connector = FileLogConnector(_connector, console_file, text=text)

    _connector.on(_event_handler)
    _connector.open()

    try:
        mcp.run(transport="stdio")
    finally:
        _connector.close()


def main():
    """MCP server entry point."""
    os.makedirs(os.path.expanduser("~/.hardwario"), exist_ok=True)

    logger.remove()
    logger.add(DEFAULT_LOG_FILE,
               format='{time} | {level} | {name}.{function}: {message}',
               level='TRACE',
               rotation='10 MB',
               retention=3)

    logger.debug('MCP Argv: {}', sys.argv)
    logger.debug('Version: {}', version)

    try:
        with logger.catch(reraise=True, exclude=KeyboardInterrupt):
            default_map = get_default_map()
            logger.debug('Loaded config: {}', default_map)
            cli(auto_envvar_prefix='RTTT', default_map=default_map)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f'MCP server error: {e}')
        click.secho(str(e), err=True, fg='red')
        sys.exit(1)
