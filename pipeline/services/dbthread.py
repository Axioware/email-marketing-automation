"""Database calls made while Playwright's browser is open.

Playwright's sync API runs an asyncio event loop in the calling thread while the browser is open, and Django refuses
database access from a thread with a running event loop (SynchronousOnlyOperation). Functions decorated with
`outside_event_loop` run as usual normally, and on a single worker thread (with its own connection) while such a loop
is running. Django's safety check stays on.
"""
import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor

_executor: ThreadPoolExecutor | None = None


def event_loop_running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def outside_event_loop(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not event_loop_running():
            return func(*args, **kwargs)
        global _executor
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline-db")
        return _executor.submit(func, *args, **kwargs).result()

    return wrapper


def close_worker_connection() -> None:
    """Close the worker thread's database connection (if the worker was used)."""
    if _executor is not None:
        from django.db import connections

        _executor.submit(connections.close_all).result()
