"""Tests of the settings window module that need no display (UI-1). What the window does on a
screen is checked with ``tools/wayland-smoke/smoke_preferences.py``; its logic is tested in
``test_preferences_model.py``."""

from __future__ import annotations

import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest

from slideshow_lock import preferences
from slideshow_lock.preferences import PreferencesWindow, _choice_labels, _transition_labels
from slideshow_lock.preferences_model import (
    CHOICES,
    INTERVAL_POSITIONS,
    INTERVAL_STOPS,
    TRANSITION_CHOICES,
    Draft,
    PreferencesModel,
    SaveResult,
)
from slideshow_lock.settings import (
    KEY_IDLE_TIMEOUT_SECONDS,
    KEY_ORDER,
    KEY_PICTURE_FOLDER,
    Settings,
)


def test_every_choice_has_a_label_in_the_same_order():
    labels = _choice_labels()
    assert set(labels) == set(CHOICES)
    for key, values in CHOICES.items():
        assert len(labels[key]) == len(values), key  # the drop-down index is the choice's index
        assert len(set(labels[key])) == len(values), key  # two choices never look the same


def test_every_transition_choice_has_a_label_in_the_same_order():
    labels = _transition_labels()
    assert len(labels) == len(TRANSITION_CHOICES)  # the drop-down index is the choice's index
    assert len(set(labels)) == len(labels)


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
        _draft=SimpleNamespace(preview_values=dict),
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

    def get_show_screenshots(self):
        return False


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
        _draft=SimpleNamespace(preview_values=dict, save=lambda: SaveResult(True, "")),
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
    monkeypatch.setattr(preferences.Adw, "Application", FakeApplication)
    monkeypatch.setattr(preferences.Gio, "SettingsSchemaSource", FakeSchemas)
    callback = object()
    assert preferences.main([], before_preview=callback) == 0
    assert preferences.main([]) == 0
    assert given == [callback, None]


def _stores_before_preview(source):
    """True if the constructor *source* has, as a statement of its own body, ``self._before_preview
    = before_preview`` and ``before_preview`` is one of its parameters."""
    function = ast.parse(textwrap.dedent(source)).body[0]
    parameters = {a.arg for a in function.args.args + function.args.kwonlyargs}
    return "before_preview" in parameters and any(
        isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Name)
        and node.value.id == "before_preview"
        and any(
            isinstance(t, ast.Attribute)
            and isinstance(t.value, ast.Name)
            and t.value.id == "self"
            and t.attr == "_before_preview"
            for t in node.targets
        )
        for node in function.body
    )


def test_the_constructor_stores_the_callback_it_is_given():
    """The constructor itself cannot run here (it builds a real Gtk window, which needs a display),
    so the one line that keeps the callback is read from its source: without it the Preview button
    silently stops closing the overview. This shows the line is there, not that the button calls
    the callback (only a real window does)."""
    assert _stores_before_preview(inspect.getsource(PreferencesWindow.__init__))


_CONSTRUCTOR = """
def __init__(self, settings, application=None, before_preview=None):
    super().__init__()
    {line}
"""


@pytest.mark.parametrize(
    "line, keeps",
    [
        ("self._before_preview = before_preview", True),
        ("self._before_preview = None", False),
        ("self._callback = before_preview", False),
        ("pass", False),
        ("if False:\n        self._before_preview = before_preview", False),
    ],
)
def test_the_constructor_check_tells_a_constructor_that_drops_the_callback_apart(line, keeps):
    """Negative control of the check above, on constructors written out here (so it does not
    depend on the real one): only the first one keeps the callback."""
    assert _stores_before_preview(_CONSTRUCTOR.format(line=line)) is keeps


# -- every edit is stored the moment it is made -----------------------------------------------


def _stored(key):
    """What a process other than the window reads from the settings."""
    other = Settings()
    return {
        KEY_IDLE_TIMEOUT_SECONDS: other.get_idle_timeout_seconds,
        KEY_ORDER: other.get_order,
        KEY_PICTURE_FOLDER: other.get_picture_folder,
    }[key]()


def _editing_stand_in(draft=None):
    """What the edit handlers, ``_report`` and ``_on_close_request`` read from the window,
    on a real ``Draft`` over the real settings: they run, they are not mocked."""
    log, labels = [], []
    stand_in = SimpleNamespace(
        _updating=False,
        _closed=False,
        _preview=None,
        _draft=draft or Draft(PreferencesModel(Settings())),
        status=SimpleNamespace(set_label=labels.append),
        refresh=lambda: log.append("refresh"),
        _show_folder=lambda: log.append("show_folder"),
        _show_interval=lambda: log.append("show_interval"),
        _move_interval_slider=lambda position: log.append(("move", position)),
        log=log,
        labels=labels,
    )
    stand_in._commit_folder = lambda: log.append("commit_folder")
    stand_in._report = lambda result: PreferencesWindow._report(stand_in, result)
    return stand_in


def test_a_number_field_stores_the_edit_at_once():
    window = _editing_stand_in()
    spin = SimpleNamespace(get_value_as_int=lambda: 300)
    PreferencesWindow._on_int(window, KEY_IDLE_TIMEOUT_SECONDS, spin)
    assert _stored(KEY_IDLE_TIMEOUT_SECONDS) == 300
    assert window._draft.pending == {} and window.labels[-1] == "Saved."


def test_a_number_field_set_by_the_window_itself_stores_nothing():
    window = _editing_stand_in()
    window._updating = True
    spin = SimpleNamespace(get_value_as_int=lambda: 300)
    PreferencesWindow._on_int(window, KEY_IDLE_TIMEOUT_SECONDS, spin)
    assert window._draft.pending == {} and _stored(KEY_IDLE_TIMEOUT_SECONDS) == 120


def test_the_transition_length_slider_stores_the_edit_at_once():
    window = _editing_stand_in()
    window.duration_scale = SimpleNamespace(get_value=lambda: 2.5)
    PreferencesWindow._on_duration(window)
    assert Settings().get_transition_duration() == 2.5
    assert window._draft.pending == {}


def test_the_transition_length_slider_set_by_the_window_itself_stores_nothing():
    window = _editing_stand_in()
    window._updating = True
    window.duration_scale = SimpleNamespace(get_value=lambda: 2.5)
    PreferencesWindow._on_duration(window)
    assert Settings().get_transition_duration() == 1.0


def test_a_drop_down_stores_the_edit_at_once():
    window = _editing_stand_in()
    window.order_drop = SimpleNamespace(get_selected=lambda: CHOICES[KEY_ORDER].index("name"))
    PreferencesWindow._on_choice(window, KEY_ORDER)
    assert _stored(KEY_ORDER) == "name" and window._draft.pending == {}


def test_the_transition_drop_down_stores_the_choice_of_its_index():
    window = _editing_stand_in()
    window.transition_drop = SimpleNamespace(
        get_selected=lambda: TRANSITION_CHOICES.index("random")
    )
    PreferencesWindow._on_transition(window)
    assert Settings().get_transitions() != ["ken-burns"]  # the random mix is stored
    assert window._draft.value("transitions") == "random"


def test_the_folder_field_stores_the_folder_when_it_is_left(tmp_path):
    window = _editing_stand_in()
    window.folder_row = SimpleNamespace(get_text=lambda: str(tmp_path))
    PreferencesWindow._commit_folder(window)
    assert _stored(KEY_PICTURE_FOLDER) == str(tmp_path) and window._draft.pending == {}
    assert "show_folder" in window.log  # the field shows it the way it was stored


def test_a_refused_edit_says_why_and_puts_the_fields_back():
    window = _editing_stand_in()
    spin = SimpleNamespace(get_value_as_int=lambda: 0)
    PreferencesWindow._on_int(window, KEY_IDLE_TIMEOUT_SECONDS, spin)
    assert window._draft.pending == {} and _stored(KEY_IDLE_TIMEOUT_SECONDS) == 120
    assert window.labels and window.labels[-1] != ""
    assert "refresh" in window.log


def test_an_edit_that_cannot_be_stored_says_so_is_thrown_away_and_the_fields_go_back(monkeypatch):
    monkeypatch.setattr(Settings, "set_order", lambda self, value: False)
    window = _editing_stand_in()
    window.order_drop = SimpleNamespace(get_selected=lambda: CHOICES[KEY_ORDER].index("name"))
    PreferencesWindow._on_choice(window, KEY_ORDER)
    assert window.labels[-1] not in ("", "Saved.")
    assert not window._draft.dirty  # not kept to be tried again behind the user's back
    assert "refresh" in window.log and _stored(KEY_ORDER) == "random"


# -- there is no Save button, and nothing scrolls -----------------------------------------------


def test_the_window_has_no_save_button_and_no_scrolled_area():
    """The constructor cannot run here, so its source is read: no Save button, no
    ``Adw.PreferencesPage`` (that one scrolls) and no scrolled window. The real widget tree is
    checked by ``tools/wayland-smoke/smoke_preferences.py``."""
    source = inspect.getsource(PreferencesWindow.__init__)
    assert "save_button" not in source and "Save" not in source
    assert "PreferencesPage" not in source and "ScrolledWindow" not in source
    assert not hasattr(PreferencesWindow, "_update_save_button")


def _layout(source):
    """What the constructor *source* puts where, read from its statements (the constructor builds a
    real window and cannot run here): ``children`` maps a container variable to the ``self.<name>``
    widgets it is given, in order; ``kinds`` maps a variable to the constructor it is made by
    (``Gtk.Box``, ``Adw.HeaderBar``)."""
    function = ast.parse(textwrap.dedent(source)).body[0]
    children, kinds = {}, {}
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
        ):
            kinds[node.targets[0].id] = f"{node.value.func.value.id}.{node.value.func.attr}"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.attr in ("append", "pack_start", "pack_end", "add_suffix", "add_prefix")
            and len(node.args) == 1
        ):
            container, argument = node.func.value.id, node.args[0]
            if (
                isinstance(argument, ast.Attribute)
                and isinstance(argument.value, ast.Name)
                and argument.value.id == "self"
            ):
                children.setdefault(container, []).append(argument.attr)
    return children, kinds


def test_the_footer_holds_the_preview_button_the_status_and_the_version_in_that_order():
    children, kinds = _layout(inspect.getsource(PreferencesWindow.__init__))
    footers = [name for name, held in children.items() if "preview_button" in held]
    assert len(footers) == 1 and kinds[footers[0]] == "Gtk.Box"
    assert children[footers[0]] == ["preview_button", "status", "version_label"]


def test_the_header_bar_is_given_nothing_of_the_window_s_own():
    """The header bar stays for the window controls: nothing of ``self`` is placed in it."""
    children, kinds = _layout(inspect.getsource(PreferencesWindow.__init__))
    headers = [name for name, kind in kinds.items() if kind == "Adw.HeaderBar"]
    assert headers  # the check looks at something
    assert all(name not in children for name in headers)


def _changing_window():
    """A stand-in whose controls are read through ``controls``, so one handler can be run on a value
    and then on the stored value."""
    window = _editing_stand_in()
    controls = {
        "spin": 120,
        "duration": 1.0,
        "order": CHOICES[KEY_ORDER].index("random"),
        "transition": TRANSITION_CHOICES.index("ken-burns"),
        "pan": False,
        "screenshots": False,
        "folder": "",
        "interval": INTERVAL_POSITIONS[INTERVAL_STOPS.index(10)],
    }
    window.controls = controls
    window.idle_spin = SimpleNamespace(get_value_as_int=lambda: controls["spin"])
    window.duration_scale = SimpleNamespace(get_value=lambda: controls["duration"])
    window.order_drop = SimpleNamespace(get_selected=lambda: controls["order"])
    window.transition_drop = SimpleNamespace(get_selected=lambda: controls["transition"])
    window.pan_switch = SimpleNamespace(get_active=lambda: controls["pan"])
    window.screenshots_switch = SimpleNamespace(get_active=lambda: controls["screenshots"])
    window.folder_row = SimpleNamespace(get_text=lambda: controls["folder"])
    window.interval_scale = SimpleNamespace(get_value=lambda: controls["interval"])
    window._commit_folder_real = lambda: PreferencesWindow._commit_folder(window)
    return window, controls


def _read_all():
    other = Settings()
    return (
        other.get_idle_timeout_seconds(),
        other.get_slide_interval_seconds(),
        other.get_transition_duration(),
        other.get_order(),
        other.get_transitions(),
        other.get_pan_portrait_images(),
        other.get_show_screenshots(),
        other.get_picture_folder(),
    )


#: Per control: (name in ``controls``, the value that changes it, the handler that reads it).
_AUTOSAVE_CASES = {
    "number": (
        "spin",
        300,
        lambda w: PreferencesWindow._on_int(w, KEY_IDLE_TIMEOUT_SECONDS, w.idle_spin),
    ),
    "interval": (
        "interval",
        INTERVAL_POSITIONS[INTERVAL_STOPS.index(30)],
        lambda w: PreferencesWindow._on_interval(w),
    ),
    "duration": ("duration", 2.5, lambda w: PreferencesWindow._on_duration(w)),
    "choice": (
        "order",
        CHOICES[KEY_ORDER].index("name"),
        lambda w: PreferencesWindow._on_choice(w, KEY_ORDER),
    ),
    "transition": (
        "transition",
        TRANSITION_CHOICES.index("zoom"),
        lambda w: PreferencesWindow._on_transition(w),
    ),
    "pan": ("pan", True, lambda w: PreferencesWindow._on_pan(w)),
    "screenshots": ("screenshots", True, lambda w: PreferencesWindow._on_screenshots(w)),
    "folder": ("folder", "CHANGED", lambda w: w._commit_folder_real()),
}


@pytest.mark.parametrize("case", sorted(_AUTOSAVE_CASES))
def test_a_changed_field_is_stored_the_moment_it_changes_with_nothing_left_to_save(case, tmp_path):
    window, controls = _changing_window()
    before = _read_all()
    name, value, handler = _AUTOSAVE_CASES[case]
    controls[name] = str(tmp_path) if value == "CHANGED" else value
    handler(window)
    after = _read_all()
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(changed) == 1, (before, after)  # exactly the field that was changed
    assert not window._draft.dirty and window.labels[-1] == "Saved."


@pytest.mark.parametrize("case", sorted(_AUTOSAVE_CASES))
def test_a_change_put_back_is_stored_again(case, tmp_path):
    window, controls = _changing_window()
    before = _read_all()
    name, value, handler = _AUTOSAVE_CASES[case]
    original = controls[name]
    controls[name] = str(tmp_path) if value == "CHANGED" else value
    handler(window)
    assert _read_all() != before
    controls[name] = original
    handler(window)
    assert _read_all() == before and not window._draft.dirty


def test_closing_the_window_keeps_a_folder_that_was_typed_and_stores_nothing_else():
    window = _editing_stand_in()
    seen = []
    listener = Settings()
    listener.connect_changed(seen.append)
    assert PreferencesWindow._on_close_request(window, None) is False  # the window may close
    assert window.log[0] == "commit_folder" and seen == []
    assert window._closed is True


def test_closing_the_window_with_a_value_that_cannot_be_stored_logs_it_and_still_closes(
    monkeypatch, caplog
):
    """Only a draft left over (a store that failed and was not thrown away) can be in this case."""
    monkeypatch.setattr(Settings, "set_order", lambda self, value: False)
    window = _editing_stand_in()
    window._draft.edit_choice(KEY_ORDER, "name")
    assert PreferencesWindow._on_close_request(window, None) is False
    assert window._closed is True
    assert any("could not be saved at close" in m for m in caplog.messages)


def test_the_preview_runs_on_the_stored_values_which_are_the_values_of_the_window(
    monkeypatch, tmp_path
):
    """Every value of the window is stored the moment it is changed, so the preview, which reads
    the settings through ``SessionSettings`` (nothing replaced: the draft is empty), shows what the
    window shows."""
    seen = {}

    class Controller:
        running = True

        def connect_stopped(self, _callback):
            pass

    def fake_build_source(settings):
        seen["source_settings"] = settings
        return SimpleNamespace(start=lambda: None)

    def fake_start_preview(settings, _source, _app=None):
        seen["preview_settings"] = settings
        return Controller()

    window = _window_stand_in()
    window._draft = Draft(PreferencesModel(Settings()))
    stored = Settings()
    assert stored.set_order("name") and stored.set_transitions(["zoom"])
    assert stored.set_picture_folder(str(tmp_path))
    assert stored.set_slide_interval_seconds(1) and stored.set_transition_duration(2.5)
    monkeypatch.setattr(preferences, "build_source", fake_build_source)
    monkeypatch.setattr(preferences, "start_preview", fake_start_preview)
    PreferencesWindow._start_preview(window)

    shown = seen["preview_settings"]
    assert seen["source_settings"] is shown  # the source and the preview read the same values
    assert shown.get_order() == "name"
    assert shown.get_transitions() == ["zoom"]
    assert shown.get_picture_folder() == str(tmp_path)
    assert shown.get_slide_interval_seconds() == 1
    assert shown.get_transition_duration() == 2.5
    assert shown.get_scaling() == "fill"
    assert not window._draft.dirty  # nothing is waiting to be saved


def test_the_preview_keeps_the_folder_field_before_it_reads_the_values(monkeypatch):
    order = []
    _preview_ready(monkeypatch, order)
    window = _window_stand_in()
    window._commit_folder = lambda: order.append("commit_folder")
    PreferencesWindow._start_preview(window)
    assert order == ["commit_folder", "start_preview"]


def test_the_folder_field_that_still_shows_the_folder_in_effect_keeps_and_says_nothing(tmp_path):
    """Leaving the field (focus) runs this each time: with nothing changed it must do nothing, not
    clear the status line of an earlier message."""
    window = _editing_stand_in()
    window.folder_row = SimpleNamespace(get_text=lambda: "")  # the default folder in effect: ""
    PreferencesWindow._commit_folder(window)
    assert window.log == [] and window.labels == []
    window._draft.edit_folder(str(tmp_path))
    window.folder_row = SimpleNamespace(get_text=lambda: f"  {tmp_path}  ")
    PreferencesWindow._commit_folder(window)
    assert window.log == [] and window.labels == []  # the same folder, only whitespace around it
