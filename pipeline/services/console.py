"""Terminal helpers shared by the pipeline commands, which also run as background jobs with no terminal attached."""
import sys
import time

DEFAULT_WAIT_SECONDS = 300


def interactive() -> bool:
    """True when a person is at a terminal that can answer prompts."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def wait_for_person(prompt: str, done, timeout: float = DEFAULT_WAIT_SECONDS, poll: float = 2.0) -> bool:
    """Ask a person to finish something in the (headed) browser.

    At a terminal this waits for Enter. In a background job it polls `done()` for up to `timeout` seconds instead.
    Returns done().
    """
    if interactive():
        input(prompt)
        return done()
    print(f"{prompt.rstrip(': ')} (no terminal attached; waiting up to {timeout:.0f}s)", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if done():
            return True
        time.sleep(poll)
    return done()
