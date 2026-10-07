"""Tests for ``slideshow_lock.preview_app``: the command line, the per-run settings, and the
worker thread that ``start_preview`` must not leave behind.

The module imports GTK 4 (the typelib is enough: nothing here opens a display). In CI the
GTK 4 typelib is installed and checked by the verify step, so this module is never skipped
there; on a machine without it the import error is the honest answer, not a silent skip.
"""

from __future__ import annotations

import logging
import signal
from types import SimpleNamespace

import pytest

from slideshow_lock import preview_app
from slideshow_lock.preview import INPUT_CLOSE, INPUT_KEY, INPUT_MOTION, ThreadWorker
from slideshow_lock.preview_app import (
    HOLD_REASON,
    PREVIEW_LIMIT_SECONDS,
    Gtk,  # the module's own Gtk: the same typelib version
    IdleHold,
    SessionSettings,
    build_source,
    overrides_from_args,
    start_preview,
)
from slideshow_lock.settings import (
    KEY_HARDWARE_ACCELERATION,
    KEY_ORDER,
    KEY_PAN_PORTRAIT_IMAGES,
    KEY_PICTURE_FOLDER,
    KEY_SCALING,
    KEY_SLIDE_INTERVAL_SECONDS,
    KEY_TRANSITION_ORDER,
    KEY_TRANSITIONS,
    Settings,
)
from slideshow_lock.transitions import ALL_TRANSITIONS, RANDOM_POOL
from tests.test_image_source import FakeWatcher, ManualScheduler, make_image, started
from tests.test_preview import FakeClock, FakeScaler, FakeSettings, FakeWindow, _pump
from tests.timeout_guard import (
    per_test_deadline,  # noqa: F401  (autouse fixture)
)


def parsed(*argv):
    return preview_app._parse(list(argv))


# -- the command line ---------------------------------------------------------------------------


def test_no_option_replaces_no_setting():
    assert overrides_from_args(parsed()) == {}


def test_pan_is_replaced_only_when_asked_for():
    assert KEY_PAN_PORTRAIT_IMAGES not in overrides_from_args(parsed("--scaling", "fit"))
    assert overrides_from_args(parsed("--pan")) == {KEY_PAN_PORTRAIT_IMAGES: True}


def test_every_option_replaces_its_own_setting_and_nothing_else():
    got = overrides_from_args(
        parsed("--folder", "/pics", "--interval", "7", "--order", "name", "--scaling", "fit")
    )
    assert got == {
        KEY_PICTURE_FOLDER: "/pics",
        KEY_SLIDE_INTERVAL_SECONDS: 7,
        KEY_ORDER: "name",
        KEY_SCALING: "fit",
    }


@pytest.mark.parametrize("name", ALL_TRANSITIONS)
def test_a_transition_is_replaced_by_a_list_of_that_one_name(name):
    assert overrides_from_args(parsed("--transition", name)) == {KEY_TRANSITIONS: [name]}


def test_transition_none_replaces_the_setting_with_the_empty_list_the_cut():
    assert overrides_from_args(parsed("--transition", "none")) == {KEY_TRANSITIONS: []}


def test_transition_random_is_the_eight_in_random_order():
    assert overrides_from_args(parsed("--transition", "random")) == {
        KEY_TRANSITIONS: list(RANDOM_POOL),
        KEY_TRANSITION_ORDER: "random",
    }
    assert "blur" not in RANDOM_POOL and "ken-burns" not in RANDOM_POOL and len(RANDOM_POOL) == 8


def test_the_transition_is_replaced_only_when_asked_for():
    assert KEY_TRANSITIONS not in overrides_from_args(parsed("--scaling", "fit"))


@pytest.mark.parametrize("name", ["sparkle", "Crossfade", "random,wipe", ""])
def test_a_name_that_is_not_a_transition_is_refused_by_the_command_line(name, capsys):
    with pytest.raises(SystemExit) as stop:
        parsed("--transition", name)
    assert stop.value.code == 2
    assert "--transition" in capsys.readouterr().err


@pytest.mark.parametrize("interval", ["0", "-5", "86400", "100000"])
def test_an_interval_outside_one_to_86399_seconds_is_refused(interval):
    with pytest.raises(ValueError, match="between 1 and 86399 seconds"):
        overrides_from_args(parsed("--interval", interval))


@pytest.mark.parametrize("interval", ["1", "3600", "7200", "86399"])
def test_the_ends_of_the_interval_range_are_accepted(interval):
    assert overrides_from_args(parsed("--interval", interval))[KEY_SLIDE_INTERVAL_SECONDS] == int(
        interval
    )


def test_main_prints_the_reason_and_returns_2_for_a_bad_interval(capsys):
    assert preview_app.main(["--interval", "0"]) == 2
    assert "between 1 and 86399 seconds" in capsys.readouterr().err


# -- the settings of one run ----------------------------------------------------------------------


class Stored:
    """The stored settings: every getter answers a value of its own, and a write is an error."""

    def get_picture_folder(self):
        return "stored-folder"

    def get_order(self):
        return "stored-order"

    def get_scaling(self):
        return "stored-scaling"

    def get_slide_interval_seconds(self):
        return 99

    def get_pan_portrait_images(self):
        return False

    def get_transitions(self):
        return ["stored-transition"]

    def connect_changed(self, callback):
        self.callback = callback
        return 7


def test_a_replaced_setting_wins_and_every_other_one_is_the_stored_one():
    settings = SessionSettings(
        Stored(), {KEY_SCALING: "fit", KEY_PAN_PORTRAIT_IMAGES: True, KEY_SLIDE_INTERVAL_SECONDS: 3}
    )
    assert settings.get_scaling() == "fit"
    assert settings.get_pan_portrait_images() is True
    assert settings.get_slide_interval_seconds() == 3
    assert settings.get_picture_folder() == "stored-folder"
    assert settings.get_order() == "stored-order"


def test_a_replaced_transition_wins_and_an_empty_list_is_a_replacement_too():
    assert SessionSettings(Stored(), {KEY_TRANSITIONS: ["fade-black"]}).get_transitions() == [
        "fade-black"
    ]
    assert SessionSettings(Stored(), {KEY_TRANSITIONS: []}).get_transitions() == []
    assert SessionSettings(Stored(), {}).get_transitions() == ["stored-transition"]


def test_without_replacements_every_setting_is_the_stored_one():
    settings = SessionSettings(Stored(), {})
    assert settings.get_scaling() == "stored-scaling"
    assert settings.get_slide_interval_seconds() == 99
    assert settings.get_pan_portrait_images() is False


def test_a_replacement_equal_to_false_or_zero_still_counts_as_a_replacement():
    settings = SessionSettings(Stored(), {KEY_PAN_PORTRAIT_IMAGES: False, KEY_ORDER: ""})
    assert settings.get_pan_portrait_images() is False
    assert settings.get_order() == ""


def test_changes_of_the_stored_settings_still_reach_the_listener():
    stored = Stored()
    seen = []
    assert SessionSettings(stored, {}).connect_changed(seen.append) == 7
    stored.callback("scaling")
    assert seen == ["scaling"]


# -- the worker thread --------------------------------------------------------------------------


def test_start_preview_closes_its_worker_thread_when_the_preview_stops(tmp_path, monkeypatch):
    make_image(tmp_path / "a.png")
    source = started(tmp_path, (FakeWatcher(), ManualScheduler()))
    windows = [FakeWindow()]
    workers = []

    class Recording(ThreadWorker):
        def __init__(self):
            super().__init__()
            workers.append(self)

    monkeypatch.setattr(preview_app, "open_monitor_windows", lambda: windows)
    monkeypatch.setattr(preview_app, "ImageScaler", FakeScaler)
    monkeypatch.setattr(preview_app, "ThreadWorker", Recording)
    controller = start_preview(FakeSettings(), source)
    assert _pump(lambda: windows[0].frames)  # the worker thread ran and delivered a picture
    (worker,) = workers
    thread = worker._thread
    assert thread.is_alive()
    windows[0].fire_input(INPUT_KEY)
    assert not controller.running
    thread.join(5)
    assert not thread.is_alive()  # no thread left behind per preview


def test_start_preview_lets_the_hardware_acceleration_setting_decide_the_effects(
    tmp_path, monkeypatch
):
    """The preview of the settings window reads the switch through its ``SessionSettings`` (the
    edits not saved yet included), so the Preview button shows what the switch would do."""
    make_image(tmp_path / "a.png")
    source = started(tmp_path, (FakeWatcher(), ManualScheduler()))
    followed = []
    monkeypatch.setattr(preview_app, "open_monitor_windows", lambda: [FakeWindow()])
    monkeypatch.setattr(preview_app, "ImageScaler", FakeScaler)
    monkeypatch.setattr(
        preview_app, "follow_hardware_acceleration", lambda settings: followed.append(settings)
    )
    settings = FakeSettings()
    controller = start_preview(settings, source)
    assert followed == [settings]
    controller.stop("test")


def test_the_session_settings_replace_the_hardware_acceleration_switch_for_the_run():
    stored = SimpleNamespace(get_hardware_acceleration=lambda: True)
    assert preview_app.SessionSettings(stored, {}).get_hardware_acceleration() is True
    replaced = {KEY_HARDWARE_ACCELERATION: False}
    assert preview_app.SessionSettings(stored, replaced).get_hardware_acceleration() is False


@pytest.mark.parametrize("animations", [True, False])
def test_start_preview_hands_the_desktops_animation_choice_to_the_controller(
    tmp_path, monkeypatch, animations
):
    """With animations off no tall panning frame is wanted: the scaler is asked for pan only when
    the desktop's choice (``animations_enabled``) allows the scroll."""
    make_image(tmp_path / "a.png")
    source = started(tmp_path, (FakeWatcher(), ManualScheduler()))
    windows = [FakeWindow()]
    scalers = []

    class Recording(FakeScaler):
        def __init__(self):
            super().__init__()
            scalers.append(self)

    monkeypatch.setattr(preview_app, "open_monitor_windows", lambda: windows)
    monkeypatch.setattr(preview_app, "ImageScaler", Recording)
    monkeypatch.setattr(preview_app, "animations_enabled", lambda: animations)
    controller = start_preview(FakeSettings(pan=True), source)
    try:
        assert _pump(lambda: windows[0].frames)
    finally:
        controller.stop()
    (scaler,) = scalers
    assert {call[3] for call in scaler.calls} == {animations}  # the pan argument of prepare


# -- the idle request of a manual preview ---------------------------------------------------------


class FakeApplication:
    """``Gtk.Application`` as far as the preview uses it: ``inhibit`` and ``uninhibit``."""

    def __init__(self, *, cookie=41, inhibit_error=None, uninhibit_error=None):
        self.cookie = cookie
        self.inhibit_error = inhibit_error
        self.uninhibit_error = uninhibit_error
        self.inhibit_calls = []
        self.uninhibit_calls = []

    def inhibit(self, window, flags, reason):
        self.inhibit_calls.append((window, flags, reason))
        if self.inhibit_error is not None:
            raise self.inhibit_error
        return self.cookie

    def uninhibit(self, cookie):
        self.uninhibit_calls.append(cookie)
        if self.uninhibit_error is not None:
            raise self.uninhibit_error

    @property
    def held(self):
        return len(self.inhibit_calls) - len(self.uninhibit_calls)


def preview_with(tmp_path, monkeypatch, application, windows=None, clock=None):
    """*clock*: the time the preview runs on (a ``FakeClock`` the test moves); none given, a fresh
    one that nobody moves, so that no real GLib timer is left behind."""
    make_image(tmp_path / "a.png")
    source = started(tmp_path, (FakeWatcher(), ManualScheduler()))
    windows = [FakeWindow()] if windows is None else windows
    clock = FakeClock() if clock is None else clock
    monkeypatch.setattr(preview_app, "open_monitor_windows", lambda: windows)
    monkeypatch.setattr(preview_app, "ImageScaler", FakeScaler)
    monkeypatch.setattr(preview_app, "GLibClock", lambda: clock)
    controller = start_preview(FakeSettings(), source, application)
    return controller, windows


def test_a_preview_with_an_application_asks_the_desktop_not_to_idle_once_it_shows(
    tmp_path, monkeypatch
):
    application = FakeApplication()
    controller, windows = preview_with(tmp_path, monkeypatch, application)
    try:
        assert controller.running
        # no window to hang it on (the windows come and go), the idle flag and nothing else
        assert application.inhibit_calls == [(None, Gtk.ApplicationInhibitFlags.IDLE, HOLD_REASON)]
        assert application.held == 1
    finally:
        controller.stop()


def test_the_request_is_made_for_the_gtk_window_of_the_first_preview_window(tmp_path, monkeypatch):
    """GTK 4.8 on Wayland logs a critical error for a request without a window (measured in
    headless mutter), so the real ``PreviewWindow`` hands its GTK window over."""

    class WithGtkWindow(FakeWindow):
        def __init__(self, gtk_window):
            super().__init__()
            self.gtk_window = gtk_window

    first, second = object(), object()
    application = FakeApplication()
    controller, _windows = preview_with(
        tmp_path, monkeypatch, application, [WithGtkWindow(first), WithGtkWindow(second)]
    )
    try:
        assert [call[0] for call in application.inhibit_calls] == [first]
    finally:
        controller.stop()


@pytest.mark.parametrize("kind", [INPUT_KEY, INPUT_MOTION, INPUT_CLOSE])
def test_input_on_a_preview_window_gives_the_request_back(tmp_path, monkeypatch, kind):
    application = FakeApplication(cookie=77)
    controller, windows = preview_with(tmp_path, monkeypatch, application)
    windows[0].fire_input(kind)
    assert not controller.running
    assert application.uninhibit_calls == [77]  # the cookie it was given, once


def test_a_stop_from_outside_gives_the_request_back_and_a_second_stop_changes_nothing(
    tmp_path, monkeypatch
):
    application = FakeApplication(cookie=5)
    controller, _windows = preview_with(tmp_path, monkeypatch, application)
    controller.stop("settings window closed")
    controller.stop("again")
    assert application.uninhibit_calls == [5]


def test_input_on_any_of_several_windows_gives_it_back_once(tmp_path, monkeypatch):
    application = FakeApplication()
    controller, windows = preview_with(
        tmp_path, monkeypatch, application, [FakeWindow(), FakeWindow()]
    )
    assert application.held == 1  # one request for the preview, not one per monitor
    windows[1].fire_input(INPUT_KEY)
    windows[0].fire_input(INPUT_KEY)
    assert application.uninhibit_calls == [41]


def test_a_second_preview_after_the_first_asks_again_and_gives_that_back_too(tmp_path, monkeypatch):
    application = FakeApplication()
    for _round in range(2):
        controller, windows = preview_with(tmp_path, monkeypatch, application)
        windows[0].fire_input(INPUT_KEY)
    assert len(application.inhibit_calls) == 2
    assert application.held == 0


# -- the two minute limit of a manual preview -----------------------------------------------------


def test_the_limit_is_two_minutes_and_a_constant():
    assert PREVIEW_LIMIT_SECONDS == 120


def test_a_preview_ends_by_itself_after_the_limit_and_gives_the_request_back(tmp_path, monkeypatch):
    application = FakeApplication(cookie=9)
    clock = FakeClock()
    controller, windows = preview_with(tmp_path, monkeypatch, application, clock=clock)
    clock.advance(PREVIEW_LIMIT_SECONDS - 1)
    assert controller.running and application.held == 1  # not a second early
    assert application.uninhibit_calls == []
    clock.advance(1)
    assert not controller.running
    assert windows[0].closed == 1  # the window closes the way it does for input
    assert application.uninhibit_calls == [9]  # the cookie it was given, once
    assert application.held == 0


def test_the_ended_preview_names_the_limit_as_the_reason(tmp_path, monkeypatch):
    clock = FakeClock()
    controller, _windows = preview_with(tmp_path, monkeypatch, FakeApplication(), clock=clock)
    reasons = []
    controller.connect_stopped(reasons.append)
    clock.advance(PREVIEW_LIMIT_SECONDS)
    assert reasons == ["time limit"]


def test_the_limit_closes_the_worker_thread_too(tmp_path, monkeypatch):
    closed = []

    class Recording(ThreadWorker):
        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(preview_app, "ThreadWorker", Recording)
    clock = FakeClock()
    preview_with(tmp_path, monkeypatch, FakeApplication(), clock=clock)
    clock.advance(PREVIEW_LIMIT_SECONDS)
    assert closed == [True]


def test_a_preview_without_an_application_has_the_limit_too(tmp_path, monkeypatch):
    clock = FakeClock()
    controller, _windows = preview_with(tmp_path, monkeypatch, None, clock=clock)
    clock.advance(PREVIEW_LIMIT_SECONDS)
    assert not controller.running


@pytest.mark.parametrize("how", ["key", "motion", "close", "stop from outside"])
def test_a_preview_that_ended_before_the_limit_leaves_no_timer_to_go_off_later(
    tmp_path, monkeypatch, how
):
    application = FakeApplication(cookie=3)
    clock = FakeClock()
    controller, windows = preview_with(tmp_path, monkeypatch, application, clock=clock)
    clock.advance(30)
    if how == "stop from outside":
        controller.stop("settings window closed")
    else:
        windows[0].fire_input({"key": INPUT_KEY, "motion": INPUT_MOTION, "close": INPUT_CLOSE}[how])
    assert clock.pending == 0  # not the limit's timer either
    clock.advance(PREVIEW_LIMIT_SECONDS * 10)  # nothing is left to fire
    assert application.uninhibit_calls == [3]  # no second give-back


def test_a_stale_limit_timer_does_not_touch_a_later_preview(tmp_path, monkeypatch):
    """Each preview has its own timer: the first one's limit has no say over the second."""
    application = FakeApplication()
    first_clock, second_clock = FakeClock(), FakeClock()
    first, first_windows = preview_with(tmp_path, monkeypatch, application, clock=first_clock)
    first_windows[0].fire_input(INPUT_KEY)
    second, _windows = preview_with(tmp_path, monkeypatch, application, clock=second_clock)
    try:
        first_clock.advance(PREVIEW_LIMIT_SECONDS * 2)
        assert second.running and application.held == 1
    finally:
        second.stop()


def test_an_error_while_giving_back_at_the_limit_is_logged_and_the_preview_still_ends(
    tmp_path, monkeypatch, caplog
):
    application = FakeApplication(uninhibit_error=RuntimeError("no such cookie"))
    clock = FakeClock()
    controller, windows = preview_with(tmp_path, monkeypatch, application, clock=clock)
    with caplog.at_level(logging.WARNING):
        clock.advance(PREVIEW_LIMIT_SECONDS)
    assert not controller.running and windows[0].closed == 1
    assert any("giving back" in m for m in caplog.messages)


def test_no_monitor_sets_no_limit_timer(tmp_path, monkeypatch):
    clock = FakeClock()
    controller, _windows = preview_with(tmp_path, monkeypatch, FakeApplication(), [], clock=clock)
    assert not controller.running
    assert clock.pending == 0


def test_no_monitor_means_no_request(tmp_path, monkeypatch):
    application = FakeApplication()
    controller, _windows = preview_with(tmp_path, monkeypatch, application, windows=[])
    assert not controller.running
    assert application.inhibit_calls == [] and application.uninhibit_calls == []


def test_a_start_that_fails_asks_for_nothing(tmp_path, monkeypatch):
    make_image(tmp_path / "a.png")
    source = started(tmp_path, (FakeWatcher(), ManualScheduler()))
    application = FakeApplication()

    def broken():
        raise RuntimeError("no display")

    monkeypatch.setattr(preview_app, "open_monitor_windows", broken)
    with pytest.raises(RuntimeError):
        start_preview(FakeSettings(), source, application)
    assert application.inhibit_calls == []


def test_without_an_application_the_preview_asks_for_nothing_and_still_runs(
    tmp_path, monkeypatch, caplog
):
    with caplog.at_level(logging.WARNING):
        controller, windows = preview_with(tmp_path, monkeypatch, None)
    assert controller.running
    assert caplog.records == []  # not even a failed attempt that was logged and forgotten
    windows[0].fire_input(INPUT_KEY)
    assert not controller.running


def test_a_desktop_that_does_not_accept_the_request_is_logged_and_the_preview_runs(
    tmp_path, monkeypatch, caplog
):
    application = FakeApplication(cookie=0)  # a backend that answers 0 (the Wayland one never does)
    with caplog.at_level(logging.WARNING):
        controller, windows = preview_with(tmp_path, monkeypatch, application)
    try:
        assert controller.running
        assert "did not accept" in caplog.text
    finally:
        controller.stop()
    assert application.uninhibit_calls == []  # there is nothing to give back


def test_a_request_that_raises_is_logged_and_the_preview_runs(tmp_path, monkeypatch, caplog):
    application = FakeApplication(inhibit_error=RuntimeError("boom"))
    with caplog.at_level(logging.WARNING):
        controller, windows = preview_with(tmp_path, monkeypatch, application)
    try:
        assert controller.running
        assert "could not keep the screen awake (boom)" in caplog.text
    finally:
        controller.stop()
    assert application.uninhibit_calls == []


def test_giving_back_that_fails_is_logged_and_the_worker_thread_still_closes(
    tmp_path, monkeypatch, caplog
):
    application = FakeApplication(uninhibit_error=RuntimeError("gone"))
    workers = []

    class Recording(ThreadWorker):
        def __init__(self):
            super().__init__()
            workers.append(self)

    monkeypatch.setattr(preview_app, "ThreadWorker", Recording)
    controller, windows = preview_with(tmp_path, monkeypatch, application)
    assert _pump(lambda: windows[0].frames)
    thread = workers[0]._thread
    with caplog.at_level(logging.WARNING):
        windows[0].fire_input(INPUT_KEY)
    assert "giving back the request" in caplog.text and "(gone)" in caplog.text
    thread.join(5)
    assert not thread.is_alive()
    assert application.uninhibit_calls == [41]  # tried once, not retried into a second error


def test_the_request_is_not_repeated_and_not_given_back_without_being_taken():
    application = FakeApplication()
    hold = IdleHold(application)
    hold.give_back()  # never taken
    assert application.uninhibit_calls == []
    hold.take()
    hold.take()
    assert len(application.inhibit_calls) == 1
    hold.give_back()
    hold.give_back()
    assert application.uninhibit_calls == [41]


def test_a_refused_request_is_asked_again_by_the_next_take():
    application = FakeApplication(cookie=0)
    hold = IdleHold(application)
    hold.take()
    application.cookie = 9
    hold.take()
    hold.give_back()
    assert len(application.inhibit_calls) == 2
    assert application.uninhibit_calls == [9]


# -- main: the preview that is still up when the application shuts down ---------------------------


class StubController:
    def __init__(self):
        self.running = True
        self.stops = []
        self.listeners = []

    def connect_stopped(self, callback):
        self.listeners.append(callback)

    def stop(self, reason="requested"):
        self.stops.append(reason)
        self.running = False


class StubGtkApplication:
    """Runs ``activate`` and then ``shutdown`` the way ``Gtk.Application.run`` does."""

    instances = []

    def __init__(self, application_id=None):
        self.handlers = {}
        self.held = 0
        self.quit_calls = 0
        StubGtkApplication.instances.append(self)

    def connect(self, name, callback):
        self.handlers[name] = callback

    def hold(self):
        self.held += 1

    def quit(self):
        self.quit_calls += 1

    def run(self, _argv):
        self.handlers["activate"](self)
        if "shutdown" in self.handlers:
            self.handlers["shutdown"](self)
        return 0


def test_main_hands_the_application_to_the_preview_and_stops_it_at_shutdown(monkeypatch):
    controller = StubController()
    given = []
    source = SimpleNamespace(start=lambda: None, stop=lambda: given.append("source stopped"))
    StubGtkApplication.instances.clear()
    monkeypatch.setattr(
        preview_app,
        "Gtk",
        SimpleNamespace(
            Application=StubGtkApplication, ApplicationInhibitFlags=Gtk.ApplicationInhibitFlags
        ),
    )
    monkeypatch.setattr(
        preview_app.Gio.SettingsSchemaSource,
        "get_default",
        staticmethod(lambda: SimpleNamespace(lookup=lambda _id, _recursive: object())),
    )
    monkeypatch.setattr(preview_app, "Settings", lambda: FakeSettings())
    monkeypatch.setattr(preview_app, "build_source", lambda _settings: source)

    def fake_start_preview(_settings, _source, application=None):
        given.append(application)
        return controller

    monkeypatch.setattr(preview_app, "start_preview", fake_start_preview)
    signal_handlers = {}

    def fake_unix_signal_add(_priority, signum, callback):
        signal_handlers[signum] = callback
        return 1

    monkeypatch.setattr(preview_app.GLib, "unix_signal_add", fake_unix_signal_add)
    assert preview_app.main([]) == 0
    (application,) = StubGtkApplication.instances
    assert given[0] is application  # the preview is given the application it asks through
    assert controller.stops == ["application ended"]  # still up at shutdown: stopped, request back
    assert given[-1] == "source stopped"
    # SIGINT and SIGTERM quit the application (once each, and the source is removed), so the
    # shutdown above runs for them too
    assert set(signal_handlers) == {signal.SIGINT, signal.SIGTERM}
    for handler in signal_handlers.values():
        before = application.quit_calls
        assert handler() is False
        assert application.quit_calls == before + 1


class AskingGtkApplication(FakeApplication):
    """An application that takes and gives back idle requests (and counts them), and is activated
    twice before it shuts down: a second start on the same application id."""

    instances = []
    between_starts = None  # what happens between the two starts (a test sets it)

    def __init__(self, application_id=None):
        super().__init__()
        self.handlers = {}
        self.hold_calls = 0
        AskingGtkApplication.instances.append(self)

    def connect(self, name, callback):
        self.handlers[name] = callback

    def hold(self):
        self.hold_calls += 1

    def quit(self):
        pass

    def run(self, _argv):
        self.handlers["activate"](self)
        if self.between_starts is not None:
            self.between_starts()
        self.handlers["activate"](self)  # the second start: GTK hands it to the running instance
        self.handlers["shutdown"](self)
        return 0


def _main_with_a_real_preview(tmp_path, monkeypatch, clock, windows, between_starts=None):
    """``main`` with the real ``start_preview`` over fake windows, scaler and clock: what its
    second activation does is observable in the requests and timers the preview leaves."""
    make_image(tmp_path / "a.png")
    sources = []

    def build(_settings):
        source = started(tmp_path, (FakeWatcher(), ManualScheduler()))
        sources.append(source)
        return source

    AskingGtkApplication.instances.clear()
    monkeypatch.setattr(
        AskingGtkApplication,
        "between_starts",
        staticmethod(between_starts) if between_starts else None,
    )
    monkeypatch.setattr(
        preview_app,
        "Gtk",
        SimpleNamespace(
            Application=AskingGtkApplication, ApplicationInhibitFlags=Gtk.ApplicationInhibitFlags
        ),
    )
    monkeypatch.setattr(
        preview_app.Gio.SettingsSchemaSource,
        "get_default",
        staticmethod(lambda: SimpleNamespace(lookup=lambda _id, _recursive: object())),
    )
    monkeypatch.setattr(preview_app, "Settings", lambda: FakeSettings())
    monkeypatch.setattr(preview_app, "build_source", build)
    monkeypatch.setattr(preview_app, "open_monitor_windows", lambda: windows)
    monkeypatch.setattr(preview_app, "ImageScaler", FakeScaler)
    monkeypatch.setattr(preview_app, "GLibClock", lambda: clock)
    monkeypatch.setattr(preview_app.GLib, "unix_signal_add", lambda *_args: 1)
    monkeypatch.setattr(preview_app.GLib, "idle_add", lambda *_args: 1)  # no main loop here
    assert preview_app.main([]) == 0
    (application,) = AskingGtkApplication.instances
    return application, sources


def test_a_second_start_while_the_preview_is_up_changes_nothing(tmp_path, monkeypatch):
    """A second ``activate`` on the same application id used to build a second controller and a
    second idle request, and only the last was given back at shutdown (measured: one request
    stayed on the desktop). It is ignored now: one preview, one request, one limit timer."""
    clock = FakeClock()
    windows = [FakeWindow()]
    application, sources = _main_with_a_real_preview(tmp_path, monkeypatch, clock, windows)
    assert len(sources) == 1  # no second source, so no second controller
    assert len(application.inhibit_calls) == 1  # one request ...
    assert application.held == 0  # ... and it is given back at shutdown, none left
    assert application.uninhibit_calls == [application.cookie]
    assert application.hold_calls == 1  # the application is held once, not once per start
    assert windows[0].closed == 1
    assert clock.pending == 0  # the limit timer of the first preview is gone, no second one


def test_a_second_start_does_not_start_a_second_limit_timer(tmp_path, monkeypatch):
    """The timer belongs to the first controller: a second one would end a later preview early."""
    clock = FakeClock()
    seen = []
    real_call_later = clock.call_later

    def spy(delay, fn):
        seen.append(delay)
        return real_call_later(delay, fn)

    clock.call_later = spy
    _main_with_a_real_preview(tmp_path, monkeypatch, clock, [FakeWindow()])
    assert seen.count(PREVIEW_LIMIT_SECONDS) == 1


def test_a_start_after_the_preview_has_ended_starts_a_new_one(tmp_path, monkeypatch):
    """The guard is for a preview that is up: one that ended (input) does not block the next."""
    clock = FakeClock()
    windows = [FakeWindow()]
    application, sources = _main_with_a_real_preview(
        tmp_path,
        monkeypatch,
        clock,
        windows,
        between_starts=lambda: windows[0].fire_input(INPUT_KEY),
    )
    assert len(sources) == 2
    assert len(application.inhibit_calls) == 2
    assert application.held == 0  # both given back


# -- the image source ------------------------------------------------------------------------------


def test_the_preview_source_leaves_out_pictures_no_loader_can_read(tmp_path, monkeypatch):
    """``build_source`` is what ``main`` and the smoke use: with the loader probe in place."""
    import os

    from tests.test_scaling_gdk import RED, save, solid

    save(tmp_path, "a.png", solid(20, 10, RED), "png")
    (tmp_path / "b.webp").write_bytes(b"RIFF\x10\x00\x00\x00WEBPVP8 " + bytes(40))
    from tests.test_scaling_gdk import _without_loader

    _without_loader(monkeypatch, "webp")  # whether this machine has a WebP loader is not assumed
    settings = Settings()
    assert settings.set_picture_folder(str(tmp_path)) and settings.set_order("name")
    source = build_source(settings)
    source.start()
    try:
        assert _pump(lambda: source.scan_complete)  # the scan runs on the GLib main loop
        assert [os.path.basename(p) for p in source.images()] == ["a.png"]
    finally:
        source.stop()
