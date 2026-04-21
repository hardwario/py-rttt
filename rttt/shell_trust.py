import hashlib
import os
import sys
import click
from loguru import logger


TRUST_FILE = os.path.expanduser('~/.hardwario/rttt_allowed_shells')


def extract_shell_entries(substitutions: dict | None) -> dict[str, str]:
    if not substitutions:
        return {}
    result = {}
    for name, spec in substitutions.items():
        if isinstance(spec, dict) and 'shell' in spec:
            result[name] = str(spec['shell'])
    return result


def compute_hash(shell_entries: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(shell_entries):
        h.update(name.encode('utf-8'))
        h.update(b'\0')
        h.update(shell_entries[name].encode('utf-8'))
        h.update(b'\n')
    return h.hexdigest()


def load_trusted() -> dict[str, str]:
    if not os.path.exists(TRUST_FILE):
        return {}
    trusted = {}
    try:
        with open(TRUST_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.rstrip('\n')
                if not line or line.startswith('#'):
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2:
                    trusted[parts[1]] = parts[0]
    except Exception as e:
        logger.warning(f'Failed to read trust file {TRUST_FILE}: {e}')
    return trusted


def save_trusted(trusted: dict[str, str]) -> None:
    d = os.path.dirname(TRUST_FILE)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(TRUST_FILE, 'w', encoding='utf-8') as f:
        for path in sorted(trusted):
            f.write(f'{trusted[path]}  {path}\n')


def ensure_shell_trust(sources: list[tuple[str, dict]], trust_shells: bool) -> None:
    """Prompt the user to approve shell substitutions from each config source.

    For every source file that declares `shell:` substitutions, check the
    persisted trust hash. If unknown or changed, prompt (or auto-accept with
    --trust-shells). Exits the process if the user declines any source.
    Silent no-op when no source has shell substitutions.
    """
    pending: list[tuple[str, dict[str, str], str]] = []
    for path, data in sources:
        shell_entries = extract_shell_entries((data or {}).get('substitutions'))
        if not shell_entries:
            continue
        pending.append((path, shell_entries, compute_hash(shell_entries)))

    if not pending:
        return

    trusted = load_trusted()
    needs_prompt = [(path, entries, h) for path, entries, h in pending if trusted.get(path) != h]
    if not needs_prompt:
        return

    if trust_shells:
        for path, _entries, h in needs_prompt:
            logger.info(f'Trusting shell substitutions via --trust-shells for {path}')
            trusted[path] = h
        save_trusted(trusted)
        return

    if not sys.stdin.isatty():
        paths = ', '.join(p for p, _, _ in needs_prompt)
        click.secho(
            f'Shell substitutions in {paths} require confirmation. '
            f'Run interactively once or pass --trust-shells.',
            err=True, fg='red')
        sys.exit(1)

    for path, entries, h in needs_prompt:
        click.secho(f'\nConfig {path} defines shell substitutions:', fg='yellow')
        for name, command in entries.items():
            click.echo(f'  {name}: {command}')
        click.echo()

        if not click.confirm('Allow these commands to run when expanded?', default=False):
            click.secho('Shell substitutions declined. Exiting.', err=True, fg='red')
            sys.exit(1)

        trusted[path] = h

    save_trusted(trusted)
    click.secho(f'Trust saved to {TRUST_FILE}', fg='green')
