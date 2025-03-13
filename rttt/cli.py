import os
import sys
import click
import time
import pylink
from loguru import logger
from rttt import __version__ as version
from rttt.connectors import PyLinkRTTConnector, FileLogConnector, DemoConnector
from rttt.console import Console

DEFAULT_LOG_FILE = os.path.expanduser("~/.hardwario/rttt.log")
DEFAULT_HISTORY_FILE = os.path.expanduser(f"~/.rttt_history")
DEFAULT_CONSOLE_FILE = os.path.expanduser(f"~/.rttt_console")
DEFAULT_JLINK_SPEED_KHZ = 2000


@click.command('rttt')
@click.version_option(version, prog_name='rttt')
@click.option('--serial', type=int, metavar='SERIAL_NUMBER', help='J-Link serial number')
@click.option('--device', type=str, metavar='DEVICE', help='J-Link Device name', required=True, prompt=True)
@click.option('--speed', type=int, metavar="SPEED", help='J-Link clock speed in kHz', default=DEFAULT_JLINK_SPEED_KHZ, show_default=True)
@click.option('--reset', is_flag=True, help='Reset application firmware.')
@click.option('--terminal-buffer', type=int, help='RTT Terminal buffer index.', show_default=True, default=0)
@click.option('--logger-buffer', type=int, help='RTT Logger buffer index.', show_default=True, default=1)
@click.option('--latency', type=int, help='Latency for RTT readout in ms.', show_default=True, default=50)
@click.option('--history-file', type=click.Path(writable=True), show_default=True, default=DEFAULT_HISTORY_FILE)
@click.option('--console-file', type=click.Path(writable=True), show_default=True, default=DEFAULT_CONSOLE_FILE)
def cli(serial, device, speed, reset, terminal_buffer, logger_buffer, latency, history_file, console_file):
    '''HARDWARIO Real Time Transfer Terminal Console.'''

    jlink = pylink.JLink()
    jlink.open(serial_no=serial)
    jlink.set_speed(speed)
    jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)

    logger.info(f'J-Link dll version: {jlink.version}')
    logger.info(f'J-Link dll compile_date: {jlink.compile_date}')
    try:
        logger.info(f'J-Link dll path: {jlink._library._path}')
    except Exception as _:
        pass
    logger.info(f'J-Link firmware_version: {jlink.firmware_version}')
    logger.info(f'J-Link serial_number: {jlink.serial_number}')

    jlink.connect(device)

    if reset:
        jlink.reset()
        jlink.go()
        time.sleep(1)

    connector = PyLinkRTTConnector(jlink, terminal_buffer, logger_buffer, latency)

    # connector = DemoConnector()
    if console_file:
        text = f'Device: {device} J-Link sn: {serial}' if serial else f'Device: {device}'
        connector = FileLogConnector(connector, console_file, text=text)

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
            cli(auto_envvar_prefix='RTTT')
    except KeyboardInterrupt:
        pass
    except Exception as e:
        # raise e
        click.secho(str(e), err=True, fg='red')
        if os.getenv('DEBUG', False):
            raise e
        sys.exit(1)
