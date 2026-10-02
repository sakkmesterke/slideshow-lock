"""Temporary CI negative control: a skipped test must trip the no-skip gate. Removed in a later commit."""

import pytest


def test_skips_on_purpose():
    pytest.skip("negative control for the no-skip gate")
