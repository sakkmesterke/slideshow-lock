"""Tests of the settings window module that need no display (UI-1). What the window does on a
screen is checked with ``tools/wayland-smoke/smoke_preferences.py``; its logic is tested in
``test_preferences_model.py``."""

from __future__ import annotations

from slideshow_lock.preferences import _choice_labels
from slideshow_lock.preferences_model import CHOICES


def test_every_choice_has_a_label_in_the_same_order():
    labels = _choice_labels()
    assert set(labels) == set(CHOICES)
    for key, values in CHOICES.items():
        assert len(labels[key]) == len(values), key  # the drop-down index is the choice's index
        assert len(set(labels[key])) == len(values), key  # two choices never look the same
