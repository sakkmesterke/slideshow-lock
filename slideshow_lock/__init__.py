"""Single-source application identity constant (D17).

The `.desktop` file name, the GSettings schema id, the RPM package name, the
systemd unit name and the gettext domain are all meant to derive from this one
value. Nobody picks their own stand-in name; everything references ``APP_ID``.
"""

APP_ID = "io.github.sakkmesterke.SlideshowLock"
