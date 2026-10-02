"""Test-level hard timeout, so a hang in the code under test fails the test.

Without a guard, a blocking call (for example ``open()`` on a FIFO) stalls the
whole pytest run until the CI job is killed, and the failure shows up as a job
timeout instead of a named failing test.
"""

from __future__ import annotations

import contextlib
import signal
import time

import pytest

#: Upper bound for any single test in the modules that import ``per_test_deadline``.
PER_TEST_DEADLINE_SECONDS = 60


class HardTimeout(BaseException):
    """Raised by ``hard_timeout``. A ``BaseException`` on purpose: the code under test
    has broad ``except Exception`` handlers, which must not swallow the alarm and let a
    hung call pass as a skipped file."""


@contextlib.contextmanager
def hard_timeout(seconds: float):
    """Raise ``HardTimeout`` in the main thread if the block runs longer than *seconds*.

    Guards nest. An enclosing guard (for example the per-test deadline) keeps running
    while an inner one is active: the inner timer is never longer than what is left of
    the outer one, and on exit (normally or by firing) the outer timer is re-armed with
    its remaining time instead of being cleared. Uses ``SIGALRM``/``ITIMER_REAL``, so
    it works in the main thread on POSIX only.
    """

    def on_alarm(signum, frame):
        raise HardTimeout(f"hard timeout after {seconds}s: the code under test hung")

    outer_remaining, _interval = signal.getitimer(signal.ITIMER_REAL)
    entered = time.monotonic()
    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(
        signal.ITIMER_REAL, min(seconds, outer_remaining) if outer_remaining else seconds
    )
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if outer_remaining:
            left = outer_remaining - (time.monotonic() - entered)
            signal.setitimer(signal.ITIMER_REAL, max(left, 0.001))


@pytest.fixture(autouse=True)
def per_test_deadline():
    with hard_timeout(PER_TEST_DEADLINE_SECONDS):
        yield
