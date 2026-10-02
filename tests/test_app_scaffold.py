from slideshow_lock import APP_ID, app_display_name


def test_app_id_matches_d17():
    # D17: the application identifier lives in a single constant; the
    # .desktop file name, GSettings schema id, RPM package name, unit name
    # and gettext domain are all meant to derive from this one value.
    assert APP_ID == "io.github.trensoft.slideshowlock"


def test_app_display_name_is_translatable_placeholder():
    assert app_display_name() == "Slideshow Lock"
