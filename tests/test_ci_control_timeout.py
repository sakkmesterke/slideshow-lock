"""Temporary CI negative control: must be cut off by the job timeout. Removed in a later commit."""

import time


def test_hangs_until_job_timeout():
    time.sleep(600)
