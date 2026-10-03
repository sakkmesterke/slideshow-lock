# Slideshow preview (CORE-2)

Modules: `slideshow_lock/preview.py` (controller), `slideshow_lock/preview_window.py` (GTK4
windows), `slideshow_lock/scaling.py` (decode and scale), `slideshow_lock/preview_app.py`
(run it from a terminal). The picture source is the image source of
[`image-source.md`](image-source.md).

Status: automated tests green, live verification pending. How the pictures look on a real
monitor, on a real GPU and with a real compositor session is a human judgement on the reference
machine; section 8 lists exactly what was and was not checked.

## 1. What it does

One fullscreen window per monitor, showing the pictures of the picture folder, each scaled to
the monitor's real pixel size. Any input ends it. It never locks the session (D11): there is no
lock, session or D-Bus call anywhere in these modules, and a test scans the code for one.

Not in this card: locking, the state machine, D-Bus, systemd (CORE-1); cross-fades (a later
round); the settings window (UI-1); changing the default of the pan switch.

## 2. Behaviour

- **Monitors.** One window per monitor, each fullscreen on its own monitor. The same picture is
  shown on all of them and they switch together; each gets a frame scaled to its own size. (One
  picture queue, one cursor: showing different pictures per monitor is not implemented.)
- **Order and interval** come from the settings (`order`, `slide-interval-seconds`) and apply
  without a restart. The interval counts from the moment a picture appears.
- **Scaling** (`scaling` key), always to the exact device-pixel size
  `round(logical size * surface scale)` of the window:
  - `fit`: the whole picture, aspect ratio kept, centred, black bars on the short side.
  - `fill`: the picture covers the monitor, aspect ratio kept, the overflow is cropped from the
    centre. A portrait picture on a landscape monitor therefore fills the monitor's **width**;
    a landscape picture on a portrait monitor fills its **height**. The crop is cut from the
    source before scaling, and has the monitor's aspect ratio to within one source pixel.
- **Pan** (`pan-portrait-images`, **off by default**): with `fill`, a portrait picture that
  overflows the monitor vertically is scrolled slowly from top to bottom, over 90 % of the
  slide interval, instead of being cropped to its middle. Landscape pictures, `fit`, and
  portrait pictures on a portrait monitor are never panned. A picture that would need a frame of
  more than 6 times the monitor's pixels, or taller than 16384 px, is cropped instead. The switch
  is off because the animation redraws every frame (measured below); the default only changes
  after a battery measurement on the reference laptop
  ([`measurements/pan-battery-protocol.md`](measurements/pan-battery-protocol.md)).
- **Scaler.** `videoscale method=lanczos envelope=3` through GStreamer, which is the only path
  that may be called Lanczos-3, and only as "Lanczos as implemented by videoscale" (MEAS-1,
  section 5 item 5: its taps are quantised, it is not bit-exact Lanczos). Nothing here claims
  anything better than that. If GStreamer cannot be used, `GdkPixbuf` `BILINEAR`, with one
  WARNING per process (`[slideshow] Lanczos scaling through GStreamer is unavailable ...`). A
  picture that already has the target size is not resampled.
- **Decoding** is `GdkPixbuf` (on the MEAS-1 stack: GIF, JPEG, PNG, TIFF; WebP and others only if a
  loader is installed, otherwise the picture is skipped and logged). EXIF orientation is applied
  (phone photos are stored sideways), transparent areas are shown black, an animated GIF shows
  its first frame.
- **Next picture first.** The next picture is chosen with `advance()` and decoded and scaled on a
  worker thread while the current one is on screen. If it is not ready when the interval ends,
  it appears the moment it is. A single picture is decoded once and reused.
- **Empty source** (`current()` is `None`): a defined state, not an error. The windows show
  "No pictures to show" on black, one line is logged (`[slideshow-dir] no picture to show ...`,
  WARNING once the folder scan is complete, INFO while it still runs), and the preview carries on
  by itself when a picture turns up. If the last picture on screen is deleted the windows go
  to the same state; a picture that is deleted while it is on screen stays (its pixels are in
  memory).
- **Input.** The window's own GTK controllers: pointer motion, button press, key press, scroll.
  Any one of them, on any monitor's window, stops the preview and closes every window. A pointer
  that simply rests where the window appears is not input: the first position the window sees is
  the baseline, and motion counts once the pointer is 2 px away from it.
- **Live settings.** The interval re-arms the running timer. `scaling` and `pan-portrait-images`
  redo the picture on screen and the prepared one. The folder and the order are applied by
  `source_from_settings`; after an order change the picture that is already prepared is still
  shown next, then the new order.

## 3. Pictures that cannot be shown

`ImageScaler.prepare` raises `ImageSkipped` with a one-line reason; the controller logs
`[slideshow-dir] skipping '<path>': <reason>` (WARNING, burst-limited like the image source's own
log: the first 10 per 60 s, then one count line) and goes to the next picture at once.

| Case | How it is detected |
|---|---|
| decoder error (JPEG header followed by garbage, truncated TIFF or GIF, ...) | the decoder's error |
| truncated JPEG, PNG or BMP with a valid header | marker structure: JPEG without end-of-image, PNG without IEND, BMP shorter than its header says. The `GdkPixbuf` loader accepts all three without any error (measured, see section 7) |
| file still being copied | modified less than 2 s ago, or its size or modification time changed while it was read |
| decompression bomb | the loader is told to produce a 0 x 0 picture as soon as the header announces more than 100 megapixels, before anything is allocated |
| file over 256 MiB, not a regular file (FIFO), unreadable | refused when it is opened; opened `O_NONBLOCK`, so a FIFO cannot hang the worker |

Not detected: a JPEG that is damaged inside its entropy-coded data but structurally complete.
libjpeg conceals that and `GdkPixbuf` reports nothing, so the picture is shown as decoded.

If as many pictures in a row fail as the queue holds, the controller stops trying until the next
interval (one WARNING), keeps what is on screen (or shows the empty-state message if nothing was
shown yet), and tries again at the next interval. No busy loop, no crash. An unexpected exception
inside the scaler is logged and treated as a skip.

## 4. The main loop never decodes or scales

All file reads, decoding and scaling run on one worker thread; the result is handed back to the
main loop with `GLib.idle_add`. The main loop does only cheap things: it wraps the finished pixels
into a texture, and GTK uploads and draws it. This is what the CORE-1 safety condition rests on:
locking before sleep must not wait for picture I/O.

The image source still scans folders on the main loop (see its document); that part is bounded
by its own time budget and is not changed here.

## 5. Where the code stands in the layers

```
ImageSource --current()/advance()/changed--> PreviewController --frames--> PreviewWindow (GTK4)
Settings  ----------changed keys-----------> PreviewController                 ^
                                              |  worker thread: ImageScaler     |
                                              +-- clock, input events ----------+
```

The controller imports no GTK. Its windows, scaler, clock and worker are handed in, so the control
flow is tested without a display. `preview_window.py` is the only GTK module; the 1:1 drawing is
the one MEAS-1 measured (`target_check.py`, modes 3 and 4): a memory texture at its own pixel size,
centred on black.

## 6. Run it

```
glib-compile-schemas data/
GSETTINGS_SCHEMA_DIR=data python3 -m slideshow_lock.preview_app --folder ~/Pictures
```

Options (`--interval`, `--order`, `--scaling`, `--pan`, `--debug`) apply to that run only and are
never written to the settings. Any key, click, scroll or mouse movement ends it.
`start_preview(settings, source)` in the same module is what the service and the settings window
will call.

## 7. Facts measured while building it

All on Debian 12 userland packages run without installing them system-wide: GTK 4.8.3, GStreamer
1.22.0, gdk-pixbuf 2.42.10, mutter 43.8, PyGObject 3.42.2, Python 3.11.2 (software GL). **Not the
RHEL 10.2 versions**; MEAS-1's stack is GTK 4.16 and gdk-pixbuf 2.42.12.

- `Gdk.Surface.get_scale()` (the call MEAS-1 recommends) does not exist before GTK 4.12; the window
  falls back to `get_scale_factor()` (integer scale). On GTK 4.16 the fractional scale is used.
- gdk-pixbuf decodes a JPEG truncated at 30 % of its size without any error and returns a
  half-grey picture, and the progressive loader also accepts a truncated PNG and BMP (a PNG
  through `new_from_file` does raise). A truncated TIFF and a truncated GIF (a 1 x 1 pixel one, at
  every cut point) are reported by the decoder.
- Asking the loader for a 0 x 0 picture from the `size-prepared` signal stops a 30000 x 30000 PNG at
  once (0 ms, 17 MB); abandoning the decode after the size was announced still cost 3.3 s, and
  scaling it down instead cost 34 s and 1 GB.
- Pan costs redraws: with the headless compositor, a static picture was redrawn 1 to 2 times, a
  panning portrait picture 103 times over a 2 s interval and 162 times over 3 s. That is the reason
  for the switch. It says nothing about watts.
- Main-loop gaps (a 10 ms timer, software GL, two virtual monitors): the longest gap was 42 to
  88 ms over about a dozen runs. In the three runs where the gaps were sorted by time it fell
  within 0.6 s after a picture appeared (54 to 78 ms, against 28 to 34 ms at all other times),
  which is texture upload and drawing; with the decoding cached the longest gap was still 44 to
  54 ms. File I/O, decoding and scaling are not on the main loop (tests), but the texture upload
  is, and on software GL it is not small. On a GPU it was not measured.

## 8. Tests, and what they do not prove

- `tests/test_preview.py`: the controller with fake windows and clock around the real image source
  (51 tests): order, interval, switching, live settings, damaged pictures with a negative control,
  empty source, deleted pictures, input on every window and kind, the lock-free proof (every call the
  controller makes is on a list of picture and timing methods, plus a code scan with its own
  negative control), the main loop staying free while a worker thread decodes (and a negative control
  showing the measurement does catch a block).
- `tests/test_scaling.py`: the geometry for 72 monitor and picture combinations, each for `fit`
  and `fill`; JPEG, PNG and BMP completeness; the file checks.
- `tests/test_scaling_gdk.py`: real decoding and scaling of real files: sizes, pixel positions of
  `fit` and `fill` crops, odd strides, EXIF orientation, transparency, truncated and bomb files, the
  fallback. Which scaler produced a frame is read from `Frame.method`.
- `tools/wayland-smoke/run.sh`: the real windows, scaler and source inside a headless mutter with two
  virtual monitors, with real pointer motion, button and scroll events injected through mutter's
  remote-desktop service. It is not part of pytest or CI (it needs a compositor). Key presses are
  fired on the key controller by hand, because a headless mutter without a shell gives no window
  keyboard focus: that checks the wiring, not delivery by the compositor.

Not proven by any of this: how the pictures look, behaviour on a real GPU or with fractional
scaling, real multi-monitor hardware, battery cost of the pan animation, behaviour on RHEL 10.2.
These belong on the manual test list (DOC-2).
