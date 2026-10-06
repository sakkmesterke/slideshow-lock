"""Tests of the settings window module that need no display (UI-1). What the window does on a
screen is checked with ``tools/wayland-smoke/smoke_preferences.py``; its logic is tested in
``test_preferences_model.py``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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
        _before_preview=None,
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


class CountingSettings:
    """A Settings stand-in that counts the change listeners it holds, to see them go."""

    created = []

    def __init__(self):
        self.listeners = []
        self.disconnects = 0
        CountingSettings.created.append(self)

    def connect_changed(self, callback):
        self.listeners.append(callback)
        return len(self.listeners)

    def disconnect_changed(self):
        self.disconnects += 1
        self.listeners = []

    def get_picture_folder(self):
        return "/nonexistent-picture-folder"

    def get_order(self):
        return "name"


class RecordingSource:
    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


def _window_stand_in():
    """What `_start_preview`, `_preview_finished` and `_on_close_request` read from the window."""
    labels = []
    stand_in = SimpleNamespace(
        _preview=None,
        _before_preview=None,
        _closed=False,
        _commit_folder=lambda: None,
        get_application=lambda: None,
        status=SimpleNamespace(set_label=labels.append),
        preview_button=SimpleNamespace(set_sensitive=lambda _value: None),
        _release_preview=PreferencesWindow._release_preview,
        labels=labels,
    )
    stand_in._preview_finished = lambda: PreferencesWindow._preview_finished(stand_in)
    return stand_in


@pytest.mark.parametrize("failing", ["settings", "build_source", "source_start", "start_preview"])
def test_a_preview_that_fails_to_start_takes_down_what_it_started(monkeypatch, caplog, failing):
    """The source is stopped and the preview's own settings drop their listeners, whichever step
    failed; a step that never ran leaves nothing to take down, and the error path raises nothing."""
    CountingSettings.created = []
    source = RecordingSource()

    def make_settings():
        if failing == "settings":
            raise RuntimeError("no schema")
        return CountingSettings()

    def make_source(settings):
        if failing == "build_source":
            raise RuntimeError("no source")
        settings.connect_changed(lambda _key: None)  # what source_from_settings does
        if failing == "source_start":
            source.start = lambda: (_ for _ in ()).throw(RuntimeError("walk failed"))
        return source

    def fake_start_preview(settings, _source, _application=None):
        raise RuntimeError("windows failed")

    monkeypatch.setattr(preferences, "Settings", make_settings)
    monkeypatch.setattr(preferences, "build_source", make_source)
    monkeypatch.setattr(preferences, "start_preview", fake_start_preview)
    stand_in = _window_stand_in()

    PreferencesWindow._start_preview(stand_in)

    assert stand_in._preview is None
    assert stand_in.labels == ["The preview could not be started, see the log."]
    made = CountingSettings.created
    if failing == "settings":
        assert made == [] and source.calls == []
    elif failing == "build_source":
        assert [s.disconnects for s in made] == [1]  # no source to stop, listeners dropped
        assert source.calls == []
        assert "stopping the preview source failed" not in caplog.text  # none to stop, none asked
    else:
        assert source.calls.count("stop") == 1
        assert [(s.disconnects, s.listeners) for s in made] == [(1, [])]


def test_a_failing_cleanup_after_a_failed_start_is_logged_not_raised(monkeypatch):
    class Broken(RecordingSource):
        def stop(self):
            raise RuntimeError("stop failed")

    class BrokenSettings(CountingSettings):
        def disconnect_changed(self):
            self.disconnects += 1  # counted before it fails: it was asked to run
            raise RuntimeError("disconnect failed")

    CountingSettings.created = []
    monkeypatch.setattr(preferences, "Settings", BrokenSettings)
    monkeypatch.setattr(preferences, "build_source", lambda _settings: Broken())
    monkeypatch.setattr(
        preferences, "start_preview", lambda *_args: (_ for _ in ()).throw(RuntimeError("x"))
    )
    stand_in = _window_stand_in()
    PreferencesWindow._start_preview(stand_in)  # must not raise
    assert stand_in.labels == ["The preview could not be started, see the log."]
    # the failing stop does not skip the drop of the listeners: each has its own try
    assert [s.disconnects for s in CountingSettings.created] == [1]


def _start_a_preview(monkeypatch, *, running=True, idle_queue=None):
    """A preview whose source is the real one (so its listener is the real one) on counting
    settings; the controller stand-in takes its listener the way the real controller does. Returns
    the window stand-in, the settings, the controller's stop callbacks and the source's stops.
    With `idle_queue` a list, `GLib.idle_add` only queues the function there (the real order);
    without, it runs it at once."""
    CountingSettings.created = []
    stopped = []

    class Controller:
        def __init__(self, settings):
            self.running = running
            settings.connect_changed(lambda _key: None)  # the controller's own listener

        def connect_stopped(self, callback):
            stopped.append(callback)

        def stop(self, reason="requested"):
            for callback in stopped:
                callback(reason)

    real_build_source = preferences.build_source
    source_stops = []

    def build_source_with_a_count(settings):
        source = real_build_source(settings)
        real_stop = source.stop
        source.stop = lambda: (source_stops.append("stop"), real_stop())
        return source

    monkeypatch.setattr(preferences, "Settings", CountingSettings)
    monkeypatch.setattr(preferences, "build_source", build_source_with_a_count)
    monkeypatch.setattr(preferences, "start_preview", lambda s, _src, _app=None: Controller(s))
    monkeypatch.setattr(
        preferences.GLib,
        "idle_add",
        (lambda func: func()) if idle_queue is None else idle_queue.append,
    )
    stand_in = _window_stand_in()
    PreferencesWindow._start_preview(stand_in)
    (settings,) = CountingSettings.created
    return stand_in, settings, stopped, source_stops


def test_the_listeners_of_a_preview_are_dropped_when_it_ends(monkeypatch):
    stand_in, settings, stopped, source_stops = _start_a_preview(monkeypatch)
    assert len(settings.listeners) == 2  # the source's and the controller's
    assert settings.disconnects == 0 and source_stops == []

    stopped[0]("input")  # the controller reports the end; the window finishes the preview

    assert settings.listeners == []
    assert settings.disconnects == 1
    assert source_stops == ["stop"]
    assert stand_in._preview is None


def test_closing_the_window_under_a_preview_drops_its_listeners(monkeypatch):
    stand_in, settings, _stopped, source_stops = _start_a_preview(monkeypatch)
    assert len(settings.listeners) == 2

    PreferencesWindow._on_close_request(stand_in, None)

    assert settings.listeners == []
    assert settings.disconnects == 1  # once: the stop listener and the close do not repeat it
    assert source_stops == ["stop"]
    assert stand_in._preview is None


def test_closing_the_window_releases_the_preview_before_the_queued_idle_runs(monkeypatch):
    """The controller's stop only queues `_preview_finished` on the main loop; the window's close
    must release the preview itself, because the window is gone by the time that idle runs."""
    idle = []
    stand_in, settings, _stopped, source_stops = _start_a_preview(monkeypatch, idle_queue=idle)

    PreferencesWindow._on_close_request(stand_in, None)

    assert len(idle) == 1  # the stop queued it; it has not run yet
    assert settings.disconnects == 1 and source_stops == ["stop"]
    assert stand_in._preview is None

    idle.pop()()  # the late idle finds nothing left to release

    assert settings.disconnects == 1 and source_stops == ["stop"]


def test_a_preview_with_no_monitor_drops_its_listeners(monkeypatch):
    stand_in, settings, _stopped, source_stops = _start_a_preview(monkeypatch, running=False)
    assert settings.listeners == []
    assert settings.disconnects == 1
    assert source_stops == ["stop"]
    assert stand_in._preview is None
    assert stand_in.labels == ["There is no monitor to show the preview on."]


def _preview_ready(monkeypatch, order):
    """``start_preview`` and its inputs replaced by stand-ins that note their turn in *order*."""

    class Controller:
        running = True

        def connect_stopped(self, _callback):
            pass

    def fake_start_preview(_settings, _source, _app=None):
        order.append("start_preview")
        return Controller()

    monkeypatch.setattr(preferences, "Settings", lambda: object())
    monkeypatch.setattr(
        preferences, "build_source", lambda _settings: SimpleNamespace(start=lambda: None)
    )
    monkeypatch.setattr(preferences, "start_preview", fake_start_preview)


def test_the_preview_button_runs_the_callback_of_its_caller_before_the_preview_starts(monkeypatch):
    order = []
    _preview_ready(monkeypatch, order)
    stand_in = _window_stand_in()
    stand_in._before_preview = lambda: order.append("before_preview")
    PreferencesWindow._start_preview(stand_in)
    assert order == ["before_preview", "start_preview"]
    assert stand_in._preview is not None


def test_a_failing_callback_does_not_stop_the_preview(monkeypatch, caplog):
    order = []
    _preview_ready(monkeypatch, order)
    stand_in = _window_stand_in()

    def broken():
        raise RuntimeError("the bus is gone")

    stand_in._before_preview = broken
    PreferencesWindow._start_preview(stand_in)
    assert order == ["start_preview"]
    assert stand_in._preview is not None
    assert any("the step before the preview failed" in m for m in caplog.messages)


def test_the_window_keeps_the_callback_it_is_given_and_main_hands_it_on(monkeypatch):
    """Both links of the chain: ``main`` to the window, the window's ``_start_preview`` (above)."""
    given = []

    class FakeWindow:
        def __init__(self, settings, application=None, before_preview=None):
            given.append(before_preview)

        def present(self):
            pass

    class FakeApplication:
        def __init__(self, **_kwargs):
            self._on_activate = None

        def connect(self, _signal, callback):
            self._on_activate = callback

        def run(self, _argv):
            self._on_activate(self)
            return 0

    class FakeSchemas:
        @staticmethod
        def get_default():
            return SimpleNamespace(lookup=lambda _id, _recursive: object())

    monkeypatch.setattr(preferences, "PreferencesWindow", FakeWindow)
    monkeypatch.setattr(preferences, "Settings", lambda: object())
    monkeypatch.setattr(preferences.Gtk, "Application", FakeApplication)
    monkeypatch.setattr(preferences.Gio, "SettingsSchemaSource", FakeSchemas)
    callback = object()
    assert preferences.main([], before_preview=callback) == 0
    assert preferences.main([]) == 0
    assert given == [callback, None]
