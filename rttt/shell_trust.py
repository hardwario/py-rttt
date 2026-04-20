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


def ensure_shell_trust(config_path: str | None, substitutions: dict | None, trust_shells: bool) -> None:
    """Prompt the user to approve shell substitutions and persist the decision.

    Exits the process if the user declines. A silent no-op when no shell
    substitutions are configured.
    """
    shell_entries = extract_shell_entries(substitutions)
    if not shell_entries:
        return

    current_hash = compute_hash(shell_entries)
    key = config_path or '<inline>'

    trusted = load_trusted()
    if trusted.get(key) == current_hash:
        return

    if trust_shells:
        logger.info(f'Trusting shell substitutions via --trust-shells for {key}')
        trusted[key] = current_hash
        save_trusted(trusted)
        return

    if not sys.stdin.isatty():
        click.secho(
            f'Shell substitutions in {key} require confirmation. '
            f'Run interactively once or pass --trust-shells.',
            err=True, fg='red')
        sys.exit(1)

    click.secho(f'\nConfig {key} defines shell substitutions:', fg='yellow')
    for name, command in shell_entries.items():
        click.echo(f'  {name}: {command}')
    click.echo()

    if not click.confirm('Allow these commands to run when expanded?', default=False):
        click.secho('Shell substitutions declined. Exiting.', err=True, fg='red')
        sys.exit(1)

    trusted[key] = current_hash
    save_trusted(trusted)
    click.secho(f'Trust saved to {TRUST_FILE}', fg='green')
