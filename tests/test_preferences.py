"""Tests of the settings window module that need no display (UI-1). What the window does on a
screen is checked with ``tools/wayland-smoke/smoke_preferences.py``; its logic is tested in
``test_preferences_model.py``."""

from __future__ import annotations

from types import SimpleNamespace

from slideshow_lock import preferences
from slideshow_lock.preferences import PreferencesWindow, _choice_labels
from slideshow_lock.preferences_model import CHOICES


def test_every_choice_has_a_label_in_the_same_order():
    labels = _choice_labels()
    assert set(labels) == set(CHOICES)
    for key, values in CHOICES.items():
        assert len(labels[key]) == len(values), key  # the drop-down index is the choice's index
        assert len(set(labels[key])) == len(values), key  # two choices never look the same


def test_the_preview_button_hands_the_windows_application_to_start_preview(monkeypatch):
    """The request that keeps the screen awake is made through the application: if the window did
    not hand it over, the preview would run without one (and without being held back, silently)."""
    application, calls = object(), []

    class Controller:
        running = True

        def connect_stopped(self, _callback):
            pass

    def fake_start_preview(settings, source, app=None):
        calls.append(app)
        return Controller()

    stand_in = SimpleNamespace(
        _preview=None,
        _commit_folder=lambda: None,
        get_application=lambda: application,
        status=SimpleNamespace(set_label=lambda _text: None),
        preview_button=SimpleNamespace(set_sensitive=lambda _value: None),
    )
    monkeypatch.setattr(preferences, "Settings", lambda: object())
    monkeypatch.setattr(
        preferences, "build_source", lambda _settings: SimpleNamespace(start=lambda: None)
    )
    monkeypatch.setattr(preferences, "start_preview", fake_start_preview)
    PreferencesWindow._start_preview(stand_in)
    assert calls == [application]
    assert stand_in._preview is not None
