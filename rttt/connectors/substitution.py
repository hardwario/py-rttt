import os
import re
import subprocess
import time
from datetime import datetime
from loguru import logger
from rttt.connectors.base import Connector
from rttt.connectors.middleware import Middleware
from rttt.event import Event, EventType


PLACEHOLDER_RE = re.compile(r'\{\{([A-Z_][A-Z0-9_]*)(?::([^}]*))?\}\}')
DEFAULT_TIME_FORMAT = '%Y/%m/%d %H:%M:%S'
SHELL_TIMEOUT_SECONDS = 5


def _utc_now(fmt: str | None) -> str:
    return datetime.utcnow().strftime(fmt or DEFAULT_TIME_FORMAT)


def _local_now(fmt: str | None) -> str:
    return datetime.now().strftime(fmt or DEFAULT_TIME_FORMAT)


def _unix_now(fmt: str | None) -> str:
    return str(int(time.time()))


BUILTINS = {
    'UTC_NOW': _utc_now,
    'LOCAL_NOW': _local_now,
    'UNIX_NOW': _unix_now,
}


def run_shell(name: str, spec: dict) -> str:
    command = spec.get('shell', '')
    cwd = spec.get('cwd') or None
    multiline = bool(spec.get('multiline', False))
    if cwd:
        cwd = os.path.expanduser(cwd)

    logger.info(f'{name}: exec: {command!r} cwd={cwd or os.getcwd()}')
    result = subprocess.run(
        command,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=SHELL_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        logger.info(f'{name}: exit: {result.returncode} stderr: {result.stderr.strip()!r}')
        raise RuntimeError(
            f'shell exit {result.returncode}: {result.stderr.strip() or result.stdout.strip()}'
        )

    stdout = result.stdout
    value = stdout.strip() if multiline else (stdout.splitlines()[0].strip() if stdout.splitlines() else '')
    logger.info(f'{name}: result: {value!r}')
    return value


class SubstitutionMiddleware(Middleware):

    def __init__(self, connector: Connector, substitutions: dict | None = None) -> None:
        super().__init__(connector)
        self.custom = dict(substitutions) if substitutions else {}

    def handle(self, event: Event):
        if event.type == EventType.IN and isinstance(event.data, str):
            expanded = self._expand(event.data)
            if expanded != event.data:
                lines = expanded.splitlines() or ['']
                for line in lines:
                    self.connector.handle(Event(EventType.IN, line))
                return
        self.connector.handle(event)

    def _expand(self, text: str, _visited: frozenset[str] = frozenset()) -> str:
        def replace(match: re.Match) -> str:
            name = match.group(1)
            fmt = match.group(2)
            original = match.group(0)

            if name in _visited:
                logger.warning(f'Substitution cycle detected for {{{{{name}}}}}')
                return original

            if name in self.custom:
                spec = self.custom[name]
                is_shell = isinstance(spec, dict) and 'shell' in spec
                try:
                    value = self._resolve_custom(name, spec)
                except Exception as e:
                    logger.warning(f'Substitution {{{{{name}}}}} failed: {e}')
                    return original
                if not is_shell:
                    logger.info(f'{name}: result: {value!r}')
                return self._expand(value, _visited | {name})

            resolver = BUILTINS.get(name)
            if resolver is None:
                logger.warning(f'Unknown substitution {{{{{name}}}}}')
                return original

            try:
                value = resolver(fmt)
            except Exception as e:
                logger.warning(f'Substitution {{{{{name}}}}} failed: {e}')
                return original
            logger.info(f'{name}: result: {value!r}')
            return value

        return PLACEHOLDER_RE.sub(replace, text)

    def _resolve_custom(self, name: str, spec) -> str:
        if isinstance(spec, dict) and 'shell' in spec:
            return run_shell(name, spec)
        if isinstance(spec, str):
            return spec
        return str(spec)
