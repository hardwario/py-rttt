import asyncio
import os
import threading

import yaml
from loguru import logger


def ensure_loop(name: str = 'RTTT') -> tuple[asyncio.AbstractEventLoop, threading.Thread | None]:
    """Get or create an asyncio event loop.

    If called from async context, returns the running loop (no thread).
    Otherwise creates a new loop running in a background thread.
    """
    try:
        loop = asyncio.get_running_loop()
        return loop, None
    except RuntimeError:
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=run, daemon=True, name=name)
        thread.start()
        ready.wait(timeout=1.0)
        return loop, thread


def shutdown_loop(loop: asyncio.AbstractEventLoop, thread: threading.Thread | None):
    """Stop and clean up an event loop created by ensure_loop."""
    if thread and loop.is_running():
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
    if not loop.is_closed():
        loop.close()


def parse_listen(listen: str, default_host: str = '127.0.0.1', default_port: int = 8090) -> tuple[str, int]:
    """Parse a listen address string into (host, port).

    Accepts formats: 'host:port', ':port', 'port'.
    """
    if ':' in listen:
        host, port_str = listen.rsplit(':', 1)
        return (host or default_host), int(port_str)
    return default_host, int(listen)


def load_configs(paths: list[str]) -> tuple[dict, list[tuple[str, dict]]]:
    """Load and deep-merge YAML configs from the given paths in order.

    Missing files are skipped. Files must contain a YAML mapping at the top
    level or they are ignored with a warning. Returns the merged dict plus a
    list of `(absolute_path, source_dict)` in load order (low priority first).
    """
    merged: dict = {}
    sources: list[tuple[str, dict]] = []
    for cf in paths:
        if not os.path.exists(cf):
            continue
        logger.debug('Loading config from: {}', cf)
        with open(cf, 'r') as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            logger.warning(f'Config {cf} is not a mapping, skipping')
            continue
        sources.append((os.path.abspath(cf), data))
        merged = deep_merge(merged, data)
    return merged, sources


def deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict: recursively merge override into base.

    For keys present in both, nested dicts are merged per-key; other types
    are replaced by the override value. Neither input is mutated.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def truncate_path(path, max_length=100):
    """Truncate path to last 4 directory levels if longer than max_length."""
    if len(path) <= max_length:
        return path
    parts = path.replace("\\", "/").split("/")
    # Keep last 4 parts (3 dirs + filename)
    if len(parts) > 4:
        return ".../" + "/".join(parts[-4:])
    return path
