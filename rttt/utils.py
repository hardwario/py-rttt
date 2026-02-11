import asyncio
import threading


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
