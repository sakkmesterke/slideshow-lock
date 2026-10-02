"""Test-level hard timeout, so a hang in the code under test fails the test.

Without a guard, a blocking call (for example ``open()`` on a FIFO) stalls the
whole pytest run until the CI job is killed, and the failure shows up as a job
timeout instead of a named failing test.
"""

from __future__ import annotations

import contextlib
import signal

import pytest

#: Upper bound for any single test in the modules that import ``per_test_deadline``.
PER_TEST_DEADLINE_SECONDS = 60


class HardTimeout(BaseException):
    """Raised by ``hard_timeout``. A ``BaseException`` on purpose: the code under test
    has broad ``except Exception`` handlers, which must not swallow the alarm and let a
    hung call pass as a skipped file."""


@contextlib.contextmanager
def hard_timeout(seconds: float):
    """Raise ``HardTimeout`` in the main thread if the block runs longer than *seconds*."""

    def on_alarm(signum, frame):
        raise HardTimeout(f"hard timeout after {seconds}s: the code under test hung")

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture(autouse=True)
def per_test_deadline():
    with hard_timeout(PER_TEST_DEADLINE_SECONDS):
        yield
