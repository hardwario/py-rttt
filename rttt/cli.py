import os
import sys
import click
import time
import yaml
import pylink
from loguru import logger
from rttt import __version__ as version
from rttt.connectors import PyLinkRTTConnector, FileLogMiddleware, MCPMiddleware, SubstitutionMiddleware, DemoConnector
from rttt.console import Console
from rttt.shell_trust import ensure_shell_trust

DEFAULT_LOG_FILE = os.path.expanduser("~/.hardwario/rttt.log")
DEFAULT_HISTORY_FILE = os.path.expanduser(f"~/.rttt_history")
DEFAULT_CONSOLE_FILE = os.path.expanduser(f"~/.rttt_console")
DEFAULT_JLINK_SPEED_KHZ = 2000
DEFAULT_MCP_LISTEN = '127.0.0.1:8090'

_config: dict = {}
_config_path: str | None = None


def get_default_map():
    for cf in ['.rttt.yaml', os.path.expanduser('~/.rttt.yaml'), os.path.expanduser('~/.config/rttt.yaml')]:
        if os.path.exists(cf):
            logger.debug('Loading config from: {}', cf)
            with open(cf, 'r') as f:
                return yaml.safe_load(f) or {}, os.path.abspath(cf)
    return {}, None


class IntOrHexParamType(click.ParamType):
    name = 'number'

    def convert(self, value, param, ctx):
        try:
            return int(value, 0)
        except ValueError:
            self.fail(f'{value} is not a valid integer or hex value', param, ctx)


@click.command('rttt')
@click.version_option(version, prog_name='rttt')
@click.option('--serial', type=int, metavar='SERIAL_NUMBER', help='J-Link serial number', show_default=True)
@click.option('--device', type=str, metavar='DEVICE', help='J-Link Device name')
@click.option('--speed', type=int, metavar="SPEED", help='J-Link clock speed in kHz', default=DEFAULT_JLINK_SPEED_KHZ, show_default=True)
@click.option('--reset', is_flag=True, help='Reset application firmware.')
@click.option('--address', metavar="ADDRESS", type=IntOrHexParamType(), help='RTT block address.')
@click.option('--terminal-buffer', type=int, help='RTT Terminal buffer index.', show_default=True, default=0)
@click.option('--logger-buffer', type=int, help='RTT Logger buffer index.', show_default=True, default=1)
@click.option('--latency', type=int, help='Latency for RTT readout in ms.', show_default=True, default=50)
@click.option('--history-file', type=click.Path(writable=True), show_default=True, default=DEFAULT_HISTORY_FILE)
@click.option('--console-file', type=click.Path(writable=True), show_default=True, default=DEFAULT_CONSOLE_FILE)
@click.option('--mcp/--no-mcp', is_flag=True, help='Enable MCP server.', show_default=True, default=False)
@click.option('--mcp-listen', type=str, help='MCP server listen address [host:]port.', show_default=True, default=DEFAULT_MCP_LISTEN)
@click.option('--substitutions/--no-substitutions', is_flag=True, default=True, show_default=True, help='Enable template substitutions in terminal input.')
@click.option('--trust-shells', is_flag=True, default=False, help='Trust shell substitutions in config without interactive prompt (for CI/scripts).')
def cli(serial, device, speed, reset, address, terminal_buffer, logger_buffer, latency, history_file, console_file, mcp, mcp_listen, substitutions, trust_shells):
    '''HARDWARIO Real Time Transfer Terminal Console.'''

    if substitutions:
        ensure_shell_trust(_config_path, _config.get('substitutions'), trust_shells)

    if not device:
        device = click.prompt('Device')

    jlink = pylink.JLink()
    jlink.open(serial_no=serial)
    jlink.set_speed(speed)
    jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)

    for label, getter in (
        ('dll version', lambda: jlink.version),
        ('dll compile_date', lambda: jlink.compile_date),
        ('dll path', lambda: jlink._library._path),
        ('serial_number', lambda: jlink.serial_number),
        ('firmware_version', lambda: jlink.firmware_version),
        ('firmware_outdated', jlink.firmware_outdated),
        ('firmware_newer', jlink.firmware_newer),
    ):
        try:
            logger.info(f'J-Link {label}: {getter()}')
        except Exception:
            pass

    for attempt in range(3):
        try:
            jlink.connect(device)
            break
        except pylink.errors.JLinkException as e:
            if attempt < 2:
                logger.warning(f'Connect attempt {attempt + 1} failed: {e}, retrying...')
                time.sleep(0.5)
            else:
                raise

    if reset:
        jlink.reset()
        jlink.go()
        time.sleep(1)

    connector = PyLinkRTTConnector(jlink, terminal_buffer, logger_buffer, latency, block_address=address)

    if substitutions:
        connector = SubstitutionMiddleware(connector, substitutions=_config.get('substitutions'))

    if mcp:
        connector = MCPMiddleware(connector, listen=mcp_listen)

    if console_file:
        text = f'Device: {device} J-Link sn: {serial}' if serial else f'Device: {device}'
        connector = FileLogMiddleware(connector, console_file, text=text)

    console = Console(connector, history_file=history_file)
    console.run()


def main():
    '''Application entry point.'''

    os.makedirs(os.path.expanduser("~/.hardwario"), exist_ok=True)

    logger.remove()
    logger.add(DEFAULT_LOG_FILE,
               format='{time} | {level} | {name}.{function}: {message}',
               level='TRACE',
               rotation='10 MB',
               retention=3)

    logger.debug('Argv: {}', sys.argv)
    logger.debug('Version: {}', version)

    try:
        with logger.catch(reraise=True, exclude=KeyboardInterrupt):
            default_map, config_path = get_default_map()
            logger.debug('Loaded config: {} from {}', default_map, config_path)
            _config.update(default_map)
            global _config_path
            _config_path = config_path
            cli_default_map = {k: v for k, v in default_map.items() if k != 'substitutions'}
            cli(auto_envvar_prefix='RTTT', default_map=cli_default_map)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        # raise e
        click.secho(str(e), err=True, fg='red')
        if os.getenv('DEBUG', False):
            raise e
        sys.exit(1)
