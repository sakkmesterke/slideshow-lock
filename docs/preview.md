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
the monitor's real pixel size. Any input ends it. It never locks the session (D11): the preview
code makes no call to the session, the screensaver, the login manager or any lock facility, and
two tests look for one (section 8). The `Gtk.Application` of `preview_app` registers itself on
the session bus, which opens one session-bus connection (measured in review); that is application
registration, not a lock call, and it is the only bus traffic of the preview itself.

A preview that is given its `Gtk.Application` (`preview_app` and the settings window both give
theirs) also asks the desktop not to blank the screen under it: `IdleHold` calls
`Gtk.Application.inhibit` with the idle flag and nothing else, and GTK makes the request. The
preview code still names no bus and no session; it is an idle request, not a lock call. Lifetime
and limits: section 2.1.

Not in this card: locking, the state machine, D-Bus, systemd (CORE-1); the settings window
(UI-1); changing the default of the pan switch. Since 1.0.1 a picture can come in with one of ten
transitions (section 2, "Transitions").

## 2. Behaviour

- **Monitors.** One window per monitor, each fullscreen on its own monitor. The same picture is
  shown on all of them and they switch together; each gets a frame scaled to its own size. (One
  picture queue, one cursor: showing different pictures per monitor is not implemented.) A window
  that the compositor has not sized yet can report a placeholder size (1 x 1 on GTK 4.8 under
  mutter, measured); a picture made for a size that a window no longer has when the work is done
  is made again at the new size, so the first picture is not left on screen at the wrong size for a
  whole interval. Real GNOME with a newer GTK was not measured.
- **Order and interval** come from the settings (`order`, `slide-interval-seconds`) and apply
  without a restart. The interval counts from the moment a picture appears.
- **Scaling** (`scaling` key), always to the exact device-pixel size
  `round(logical size * surface scale)` of the window:
  - `fit`: the whole picture, aspect ratio kept, centred, black bars on the short side.
  - `fill` (the default): the picture covers the monitor, aspect ratio kept, the overflow is
    cropped from the centre. A portrait picture on a landscape monitor therefore fills the
    monitor's **width**; a landscape picture on a portrait monitor fills its **height**, and its
    sides are cropped. The crop is cut from the source before scaling, and has the monitor's
    aspect ratio to within one source pixel.
  - What `fill` costs, as the share of the picture that stays visible (the rest is cropped away):
    3:2 landscape on a 16:9 monitor 84 %, 4:3 landscape 75 %; 3:4 portrait on a 16:9 monitor
    **42 %**, a 9:16 phone photo **32 %**. **Open decision:** the default stays `fill`; whether it
    is the right one for the pictures actually in the folder is the owner's call on the reference
    machine. `fit` is one line in the schema (`<default>'fit'</default>` of the `scaling` key,
    `data/*.gschema.xml`) and it is a live setting either way.
- **Pan** (`pan-portrait-images`, **off by default**): with `fill`, a portrait picture whose
  **width is the limiting side** (so that its height overflows the monitor) is scrolled slowly
  from top to bottom, over 90 % of the slide interval, instead of being cropped to its middle.
  That is a portrait picture on a landscape monitor, and also a picture that is narrower than the
  monitor on a portrait monitor (a 1:3 picture on a 9:16 monitor: pan range 1320 px), but not a
  3:4 picture on a 9:16 monitor, whose height limits. Landscape pictures and `fit` are never
  panned. A picture that would need a frame of more than 6 times the monitor's pixels, or taller
  than 16384 px, is cropped instead (on a 1080p monitor the 6x limit binds, on a very large
  one the texture side does; both are tested). If the desktop's "reduce animations" choice is
  on (`gtk-enable-animations` is false), nothing scrolls and the middle of the picture is shown,
  like the centre crop. The controller asks for that choice for every picture (the `animations`
  argument, `preview_window.animations_enabled` in the app) and then makes no tall frame at all:
  the ordinary centre crop covers the same part of the picture. Measured here for a 3000 x 6000
  picture on two 4K monitors, `fill`: the centre crop is a 24 MiB frame, 0.16 s on the worker and
  a peak of 132 to 176 MiB (review, on two machines and two folders; the peak follows the machine
  and the libraries), the panning frame 84 MiB, 0.54 s and 306 MiB. The animation setting is read
  when a picture is prepared; a picture already prepared keeps what it was made for. The switch is
  off because the animation redraws every frame (measured below); the default only changes after a
  battery measurement on the reference laptop
  ([`measurements/pan-battery-protocol.md`](measurements/pan-battery-protocol.md)).
- **Scaler.** `videoscale method=lanczos envelope=3` through GStreamer, which is the only path
  that may be called Lanczos-3, and only as "Lanczos as implemented by videoscale" (MEAS-1,
  section 5 item 5: its taps are quantised, it is not bit-exact Lanczos). Nothing here claims
  anything better than that. If GStreamer cannot be used, `GdkPixbuf` `BILINEAR`, with one
  WARNING per process (`[slideshow] Lanczos scaling through GStreamer is unavailable ...`). A
  picture that already has the target size is not resampled.
- **Decoding** is `GdkPixbuf` (on the MEAS-1 stack: GIF, JPEG, PNG, TIFF; WebP and others only if a
  loader is installed). EXIF orientation is applied (phone photos are stored sideways),
  transparent areas are shown black, an animated GIF shows its first frame.
- **Formats without a loader.** The image source's extension list (`IMAGE_EXTENSIONS`) is not
  narrowed: where a loader exists, a `.webp` or `.bmp` is shown. `scaling.probe_loadable`, which
  `preview_app.build_source` hands to the image source, takes the format the header sniff of the
  image source named (`probe_image` returns it: `jpeg`, `png`, `gif`, `bmp`, `tiff`, `webp`) and
  checks it against `Pixbuf.get_formats()`, the formats of the installed loaders. A picture without
  a loader is skipped with a log line when the folder is scanned (burst-limited like every other
  skip), instead of failing again at every pass. The file is opened once, by `probe_image`, with
  `O_NONBLOCK` and `fstat`. An earlier version then called `Pixbuf.get_file_info(path)`, which
  opens the file again by name with a blocking `open`, and that hangs on a file swapped for a FIFO
  after the first look (measured: the call did not return). Measured here on 303 files, 300 of
  them WebP headers without a loader (gdk-pixbuf 2.42.10, Debian 12, which has no WebP loader): the
  walk keeps 3 and takes 19 ms, against 4 ms and 303 queued with the plain header sniff. The
  probe itself, in a loop over real 640 x 480 JPEG and PNG files: 35 microseconds per file in the
  steady state (the old one: 64 to 103 microseconds for a JPEG and 345 to 385 for a PNG, because
  `get_file_info` reads and parses more of the file). The first call of a process imports the
  GdkPixbuf typelib: 16 to 20 ms (10 ms on a quiet run here), once, on the main loop and outside the
  step budget of the walk. What a failing pass costs depends on the files (review measured 1.56 ms per file
  and pass with a repeated WARNING on its set; with these 40-byte files it was 0.06 ms). The loader
  list of the RHEL machine has not been seen by anyone; what its gdk-pixbuf reads is not claimed
  here.
  **The probe is wider than the call it replaced:** it checks the header sniff and the loader list,
  it does not parse the header. A file whose first bytes are right and whose header is broken
  (`BM` and then noise, a 3-byte JPEG, a PNG cut after its signature, a TIFF header and noise)
  passes the probe (`get_file_info` refused all four, measured), is queued, and is skipped, with a
  log line, when it is decoded: at every pass over the queue again. That is bounded: the controller
  stops after as many failures in a row as the queue holds (section 3), and a failing pass costs
  about 0.06 ms for a small file. Reading the header in the probe would be code of ours that parses
  untrusted bytes, and the FIFO safety would have to be measured again; it is not done.
  `test_the_probe_is_wider_than_get_file_info_...` pins the behaviour.
- **Next picture first.** The next picture is chosen with `advance()` and decoded and scaled on a
  worker thread while the current one is on screen. If it is not ready when the interval ends,
  it appears the moment it is. A single picture is decoded once and reused.
- **Empty source** (`current()` is `None`, which includes an empty or missing folder): a defined
  state, not an error. The windows show "No pictures to show" and the folder searched, on black, one line is logged
  (`[slideshow-dir] no picture to show ...`, WARNING once the folder scan is complete, INFO while
  it still runs), and the preview carries on by itself when a picture turns up. For the preview
  this is on purpose: whoever pressed "Preview" sees why nothing is shown. It is *not* the
  idle path's behaviour: REQ-1 AC-3.7-1 says no slideshow starts there when there is nothing to
  show. The service does what the criterion says: `PreviewSlideshow.start` refuses, with the reason,
  when `current()` is `None` (`docs/service.md`). If the last picture on screen is deleted the windows go
  to the same state; a picture that is deleted while it is on screen stays (its pixels are in
  memory).
- **Input.** The window's own GTK controllers: pointer motion, button press (any button), key
  press, scroll, and a close request on a window (the window was closed from outside, from the
  overview for example). Any one of them, on any monitor's window, stops the preview and closes
  every window. A pointer that simply rests where the window appears is not input: the first
  position the window sees is the baseline, and motion counts once the pointer is 2 px away from
  it. **Open, only a live trial decides:** 2 px may be too little on a touchpad or on a desk that
  shakes. One more fact from the headless compositor: the first pointer event of a window carried
  coordinates of another frame than the later ones (27, 66 first, then 257, 176 for a pointer
  that had moved 1 px), so the baseline of the first event is not reliable there; it was not
  measured on a real GNOME session. The smoke test therefore resets the baseline by hand before it
  checks the threshold (section 8).
- **Transitions** (`transitions`, default Ken Burns; `slideshow_lock/transitions.py` names
  and times them, `slideshow_lock/transition_draw.py` says what each looks like and which comes
  next): when the interval ends, the next picture comes in with a transition instead of a cut. Ten
  are drawn, each over black, the new picture over the old one: `crossfade`, `fade-black`
  (the old picture to black, black to the new one), `slide-in` (the new picture slides in
  from the right over the still old one), `push` (the new picture pushes the old one out to
  the left), `ken-burns` (with the effects the cross-fade, the move
  that gave it its name being every picture's, see below; without them as in 1.0.1, see *Plain*), `zoom` (the old picture grows by 15 %
  and fades while the new one comes in from 85 %), `wipe` (the new picture is uncovered from
  the left), `circle` (from the centre in a growing circle that ends past the corners),
  `blur` (the old picture blurs, at the middle the new one takes over and sharpens) and
  `rotate` (the new picture turns in from -12 degrees, enlarged by 25 %, while it fades in over
  the old one). Progress is eased (smoothstep). The next three paragraphs (the soft edges, the slow
  move, the stronger Ken Burns zoom) are the **effects**, drawn only where a GPU is known to draw
  (**Effects only where a GPU is known**, below). **Soft edges:** where the new picture meets the old
  one there is a band, not a line: `slide-in`, `push`, `wipe`, `circle`, `zoom` and `rotate` fade
  the new picture in over `SOFT_EDGE_SHARE` (12 %) of the window's shorter side. The band narrows
  where the edge has no room (at the window's border at the start and at the end of a run), so the
  first and the last frame are the plain pictures with no band left behind; a `wipe` and a
  `circle` run on until the whole band is past the window, and a `push` lets the new picture come
  in over the last pixels of the old one, so the seam is two pictures dissolving into each other,
  not a dark line. `crossfade`, `fade-black` and `blur` have no edge.
  **With the effects, a picture is never still during its
  interval and under the transition after it** (`_Move`, `transition_draw.base_pose`), under every one of the ten: a picture that
  does not scroll appears enlarged by 14 % (`KEN_BURNS_ZOOM`) and shifted left by 3.5 % of the
  window width (`KEN_BURNS_DRIFT`) if it fills the window, and over the whole time it lives, which
  is its interval plus the longest transition after it (`picture_seconds`), shrinks and drifts
  back at a steady pace until that time is over, and a picture that stays on screen longer than that
  (a folder of one picture) keeps the pose it ended in; the shift is at most half of the
  enlargement, so the picture covers the window at every moment (no black edge). A picture that
  does not fill the window only grows by 4 % (`SMALL_ZOOM`) around the middle, without a shift.
  The move belongs to the picture: the incoming one arrives in motion, the outgoing one moves on
  under the transition (`Draw.pose`, done inside the picture's own place before the transition
  moves, turns and cuts it), and the plain drawing after the transition goes on from the same
  value. It is redrawn 30 times a second (`MOTION_FPS`), and the picture is always drawn through
  the transform (filtered), not 1:1. A scrolling picture keeps its pan and has no such move. No
  move with the desktop's animations off. An empty list, or a list with no valid name,
  is the cut.
  **Effects only where a GPU is known** (`slideshow_lock/effects.py`, `gl_probe.py`). The soft edges,
  the slow move and the Ken Burns zoom of 14 % cost drawing time that a CPU renderer does not have
  (1.0.2 stuttered on a machine like that), so they are drawn only if two layers agree.
  *First, the renderer* (`effects.decide`, at the first picture a window shows): GTK's renderer
  class must be a GPU one (`GskNglRenderer`, `GskGLRenderer`, `GskVulkanRenderer`; not
  `GskCairoRenderer`, not `GSK_RENDERER=cairo`, `LIBGL_ALWAYS_SOFTWARE` or `GALLIUM_DRIVER=llvmpipe|softpipe`
  in the environment) and the OpenGL renderer string must name a GPU. GTK and PyGObject do not give
  that string (`Gsk.Renderer` has only its type name, `Gdk.GLContext` the version and the API), so
  `gl_probe.read_gl_renderer` makes a GDK GL context current for the moment and asks `glGetString(GL_RENDERER)`
  of `libGL.so.1` (or `libGLESv2.so.2`) through ctypes, then clears the context. A string with
  `llvmpipe`, `softpipe`, `swrast`, `lavapipe`, `swiftshader` or `software` is the CPU: this is the Mesa that
  falls back on its own, which GTK reports as an ordinary GL renderer. `SVGA3D`, `virgl`, `VMware`,
  `virtio`, `VirtualBox`, `QXL`, `Bochs`, `Cirrus` or `Parallels` is a virtual GPU, taken for a no too. Only a positive reading is a yes: an
  unknown class, a string that cannot be read (no GL, no library, a failed call) or a window with no
  renderer yet means *plain* (for a window with no renderer yet until it has one; any other answer
  is final for the process).
  *Second, the drawing time* (`Effects.frame`, `FrameTimer`): while the effects are drawn, the interval
  between the frame clock's ticks (the ticks of the slow move and of the transitions) is taken in
  windows of 20 frames, and if the median of a window is over the budget (`FRAME_BUDGET_MS`, 25 ms,
  **provisional until it is measured on both kinds of machine**; `SLIDESHOW_FRAME_BUDGET_MS` in the
  environment replaces it without a new build) the effects are taken away for the rest of the
  process: the slow move stops where it is and the picture stands still (it is drawn 1:1 from the next frame),
  a transition that is running ends as it began, and every picture after it is plain. An interval of
  a second or more is a pause (display off, window hidden), not slow drawing: it is not counted.
  *Plain* is the drawing of 1.0.1 (`transition_draw.compose_plain`): hard edges, a picture that stands
  still (drawn 1:1), no outgoing-picture settling, and `ken-burns` as it was: the new picture
  alone moves, 8 % enlarged and 2 % shifted at the start, over the pan time (90 % of the interval),
  with the cross fade taking the transition's own time. The log has the decision and why
  (`[effects] full effects: ...` or `plain drawing (as in 1.0.1): ...`, at INFO, once), a trip of
  the guard (`plain drawing from now on`, WARNING) and every median (DEBUG). Measured: the string
  is read under a headless compositor without `/dev/dri` on GTK 4.8.3 (renderer class
  `GskGLRenderer`, string `llvmpipe (LLVM 15.0.6, 256 bits)`, which the class alone did not show);
  not measured: a real GPU, the Vulkan renderer, Fedora's and RHEL's GTK, a multi-GPU laptop (the
  context is the display's default one, which is what GTK draws on). The blur's own check
  (`software_gl`, names only, above) was not changed.
  One setting, `transition-duration` (0.2 to 5.0 s, default 1.0 s), is the length of
  every transition. A transition never takes more than half of the interval (a 1 s interval
  cross-fades for 0.5 s; the default 10 s interval leaves the whole 1.0 s) and under 0.2 s it is a
  cut. The stored value is not changed by that cut: the settings window shows the stored one.
  **No transition** for: the first picture, a
  redo after a settings or window size change, the same picture shown again (a folder of one), and
  when the desktop's animations are off (`gtk-enable-animations`, asked for every change of picture
  like the pan; whether the GNOME setting reaches it was not measured). A window that had no
  picture before shows the new one without a transition. Any later `show_frame`, message or close
  ends a running transition at once.
  **Choosing.** The setting is a list of names (`crossfade`, `fade-black`, `slide-in`, `push`,
  `ken-burns`, `zoom`, `wipe`, `circle`, `blur`, `rotate`; an unknown one is left out by the getter
  and logged once). One name: that one. Several: `transition-order` `random` picks one for each
  change, never the one just used; `sequence` goes round in the order above. The choice is made
  once per change of picture and every monitor gets the same one. The settings window's "random"
  stores eight names (everything but `blur` and `ken-burns`, `RANDOM_POOL`) with `random`;
  `--transition random` of `preview_app` does the same for one run.
  **Two are not always what was asked for** (`effective_name`): `ken-burns` needs a picture that
  fills the window and does not scroll (a fit picture, or a panning portrait, gets the cross-fade
  instead, which is what it is anyway now; the picture's own move differs), and the `blur`, which is the costly one (it works on pictures reduced to a quarter of
  their size), is a cross-fade when drawing is known to be in software (Cairo renderer,
  `GSK_RENDERER=cairo`, `LIBGL_ALWAYS_SOFTWARE`, `GALLIUM_DRIVER=llvmpipe|softpipe`; this check goes
  by those names only, so a Mesa that falls back to llvmpipe on its own, a virtual machine
  without a GPU, is not recognised by it and the blur is tried there; the effects above do read the
  OpenGL renderer string). The blur is also a cross-fade, with
  a warning in the log, if the reduced pictures cannot be made.
  How it is drawn: `_Canvas` keeps the old texture; `transition_draw.compose` gives, for a moment
  of the transition, the pictures to paint from the bottom up (which one, opacity, scale, angle,
  shift, clip, circle, blur, soft edge), and the canvas turns each into `Gtk.Snapshot` calls (clip,
  opacity, blur, then shift/rotate/scale around the centre). A soft edge is a `push_mask` with
  an alpha gradient, which needs GTK 4.10: on GTK 4.8 the edges stay as they were cut. A picture of opacity 0 is not painted at all:
  on GTK 4.8.3 a rotated, faded-out node was seen to change the pixels of the picture under it
  (cause not looked into). The canvas has a frame-clock tick of its own and a `TransitionRun`
  whose end is a point in time, not a number of frames, so a slow machine draws fewer frames and
  not a longer transition. The first frame is at progress 0 with the new picture added at one
  pixel and almost no opacity (the new texture is uploaded there, out of sight), the clock starts
  at the second tick, and at the end the transition's tick is removed; the picture goes on with
  the tick of its slow move if the effects are drawn (a scrolling picture, or any picture without the effects, is drawn 1:1 as always, no redraw until the
  next change). A panning old picture stays where it stopped; the new one starts at the top. `--transition
  NAME|random|none` of `preview_app` replaces the setting for one run.
- **Live settings.** The interval re-arms the running timer. `scaling` and `pan-portrait-images`
  redo the picture on screen and the prepared one. `transitions` is read at every change of
  picture and redoes nothing. The folder and the order are applied by
  `source_from_settings`; after an order change the picture that is already prepared is still
  shown next, then the new order.

### 2.1 The idle request of a preview

- **Taken** once the windows are open and a monitor was found (`start_preview`, after
  `controller.start()`), for the first window, with `Gtk.ApplicationInhibitFlags.IDLE` only (no
  logout, switch or suspend flag). With no monitor, or a `start()` that raises, nothing is asked.
- **Given back** by the controller's stop listener, so by every way the preview ends: any input
  on any window, a close from outside, `controller.stop()` (the settings window closing under a
  running preview), and, for `preview_app`, the application's `shutdown` signal, which SIGINT and
  SIGTERM now reach (they quit the application; before, they ended the process at once). Once per
  preview: the cookie is cleared before the call, so a second stop cannot give it back twice, and a
  refused or failing give-back is logged and goes no further.
- **Ends by itself after two minutes.** The preview stops 120 seconds after it started showing
  (`PREVIEW_LIMIT_SECONDS` in `preview_app.py`; a constant, not a setting, and the only time limit).
  The timer is started in `start_preview`, the path both `preview_app` and the settings window use,
  and calls `controller.stop("time limit")`: the windows close, the worker thread closes and the
  request is given back, as for input. Any other end (input, a stop from outside, `shutdown`) takes
  the timer down, so it cannot go off at a preview that has ended. It applies with or without an
  application. Why: this is an app that locks; a preview left running must not keep the desktop's
  idle lock away for good. One exception: a desktop that never answers the request (see "If the
  session manager never answers" below).
- **While a preview holds its request**, the desktop's own idle-based blanking and automatic lock
  do not run, for two minutes at most (not measured on a real GNOME session), except when the
  desktop never answers the request (see "If the session manager never answers" below).
- **If the call raises** (or GTK answers cookie 0), a WARNING says the screen may blank under the
  preview, and the preview runs on. **On Wayland a refusal by the desktop is not detected, and
  that WARNING does not appear for it:** measured on GTK 4.8.3 in the headless Wayland session, a
  desktop with no session manager on the bus, one that answers `Inhibit` with a D-Bus error and
  one that answers cookie 0 all gave `Gtk.Application.inhibit` the cookie 1 (and 2 for a second
  request), as did one that accepted the request: the Wayland backend hands out its own cookies, not
  the desktop's answer. The cookie 0 branch is reachable only with a backend that answers 0 itself
  (reported by a tester for the Broadway backend; not run here). So under a refusing desktop the
  preview runs with the screen unprotected and the application says nothing about it. GTK itself
  does write a line of its own to stderr when the desktop answers `Inhibit` with a D-Bus error
  (`Gtk-WARNING ... Calling org.gnome.SessionManager.Inhibit failed: ...`, seen in the headless
  Wayland session for that one case; the full stderr of the other two cases was not looked at).
  This is a known gap, and the WARNING is no substitute for a check.
- **If the session manager never answers, the two minute limit does not apply.**
  `Gtk.Application.inhibit()` is a synchronous call with no time limit of its own, and it is made
  on the main loop. Measured in the headless Wayland session (GTK 4.8.3) against a fake session
  manager that received `Inhibit` and never answered: the call had not returned after 40 seconds,
  when the probe was ended (one run). A tester reported that it had not returned after 270
  seconds, that the limit timer, input and SIGTERM then had no effect on `preview_app`, and that
  only SIGKILL ended it; in the settings window the whole window freezes. Nothing that runs on the
  main loop can run meanwhile. The case needs a session manager that hangs (a frozen
  `gnome-session`). The effect is a frozen preview or window, not a way around the lock. This is
  a known limit and it is not handled here.
- **A second start on the same application id** (`preview_app` started again while it runs: GTK
  hands it to the running instance as another `activate`) is ignored while the first preview is
  up: one preview, one request, one limit timer. Before, the second `activate` built a second
  controller and a second request, and only the last was given back at shutdown (measured: one
  request was left on the desktop). A start after the first preview has ended starts a new one.
- **Without an application** (`start_preview(settings, source)`) nothing is asked.
- It does not hold back a lock the user asked for, and the preview never locks anyway.
- Next to the service (`docs/service.md`): while a preview holds its request, the service sees an
  inhibitor of another application id (`...Preview` or `...Preferences`), so it starts no idle
  slideshow until the preview has ended. That is the intended order. The other way round, a
  service slideshow that is already showing when a preview takes its request is ended by the
  service (`_on_inhibit_changed`: another application inhibits idle), and the session is locked if
  the grace period is over. In practice that is possible only for a `preview_app` started from a
  command line (the settings window's button is under the slideshow; not tried).

## 3. Pictures that cannot be shown

`ImageScaler.prepare` raises `ImageSkipped` with a one-line reason; the controller logs
`[slideshow-dir] skipping '<path>': <reason>` (WARNING, burst-limited like the image source's own
log: the first 10 per 60 s, then one count line) and goes to the next picture at once.

| Case | How it is detected |
|---|---|
| decoder error (JPEG header followed by garbage, truncated TIFF or GIF, ...) | the decoder's error |
| truncated JPEG, PNG or BMP with a valid header | marker structure: JPEG without end-of-image, PNG without IEND, BMP shorter than its header says. The `GdkPixbuf` loader accepts all three without any error (measured, see section 7) |
| file still being copied | modified less than 2 s ago, or its size or modification time changed while it was read |
| decompression bomb | the loader is told to produce a 0 x 0 picture as soon as the header announces more than 50 megapixels (`MAX_PIXELS`, derived in section 4), before anything is allocated |
| file over 256 MiB, not a regular file (FIFO), unreadable | refused when it is opened; opened `O_NONBLOCK`, so a FIFO cannot hang the worker |

The structure checks (JPEG markers, PNG chunks) stop after 1 000 000 steps and then say "complete":
the decoder decides. Without that, a crafted JPEG of 0xFF fill bytes kept the worker's Python loop
busy for 4.35 s per 32 MiB (about 35 s extrapolated to the 256 MiB file limit; review measured
4.3 s as well); with the cap, 0.13 s. A PNG of empty chunks: 0.61 s per 32 MiB, 0.21 s with the
cap. The check does not hold the GIL for long stretches in between (review: a 1 ms timer on the
main loop saw p99 9.5 ms, against 2.3 ms idle). A truncation beyond the cap is therefore not found by the
structure check; real files are far below it (a 250 MB JPEG has about a million stuffed bytes, a
PNG some thousand chunks). The shipped value is pinned by tests, not only a patched one: a run of
fill bytes costs exactly 1 000 000 steps, and a noisy 1600 x 1200 JPEG cut off at 60 % needs about
3 600 steps to be found incomplete, so a cap of 1 000 would call half a picture complete.

Not detected, both shown as decoded without any error: a JPEG that is damaged inside its
entropy-coded data but structurally complete (libjpeg conceals that and `GdkPixbuf` reports
nothing), and a PNG whose zlib stream is garbage but still decodes (the `png zlib garbage` test
fixture decodes without error, so the PNG case is no better than the JPEG one).

If as many pictures in a row fail as the queue holds, the controller stops trying until the next
interval (one WARNING), keeps what is on screen (or shows the empty-state message if nothing was
shown yet), and tries again at the next interval. No busy loop, no crash. An unexpected exception
inside the scaler is logged and treated as a skip.

## 4. What is on the main loop, and what is not

**Not on the main loop:** reading a picture file, decoding it, scaling it, and copying the finished
pixels into the `GLib.Bytes` that the window wraps into a texture. All of that runs on one worker
thread; the result is handed back with `GLib.idle_add`.

**On the main loop:** wrapping the finished pixels into a texture (no copy, the `GLib.Bytes` is
already there), GTK's upload and drawing, and **the image source**. Its file system calls, by
function (line numbers drift, names do not):

- `_list_dir`: `os.scandir` of each folder, one per step;
- `_handle_entry`: `entry.is_dir()` and `entry.is_file()` of each entry (for a symlink, or
  where the file system gives no entry type, a `stat`);
- `_stat_key`: `os.stat`, from `_claim_dir` and `_reconcile_root`;
- `_on_created` and `_rewatch_ancestor`: `os.path.isdir` and `os.path.isfile`;
- `__init__` and `set_folder`: `os.path.abspath` (string work, and `os.getcwd()` for a relative
  folder; it does not touch the folder, so it cannot block on a mount);
- `probe_image`: `os.open` (`O_NONBLOCK`), `os.fstat`, `os.read` of the 16 header bytes of each new
  file, and `probe_loadable` after it, which does no file access of its own;
- `gio_directory_watcher`: creating the directory monitor (`monitor_directory`) of each folder;
- `_kernel_watch_inodes`: listing `/proc/self/fd` and reading `/proc/self/fdinfo`, after a walk.

The walk is chopped into steps of a time budget (see the image source's document), but a single
`scandir`, `stat`, `open` or monitor creation on a hung network mount blocks inside the kernel,
and then the main loop stops with it. So the claim is: *picture decoding and picture reading are
not on the main loop; the image source's walk and header reads are*. Putting the source's I/O on
its own thread or process was named a precondition of CORE-1 for the safety condition that locking
before sleep never waits for picture I/O. CORE-1 meets the condition the other way round: the sleep
path runs on a thread of its own with its own main context and never waits for the main loop
(`docs/service.md`, tested with a real image source stuck inside `os.scandir`), so the source stays
where it is, and a stuck folder can delay a picture or the slideshow's own stop, not the lock.

**What the worker still does to the main loop:** it holds the GIL while it copies pixel data in
Python (`read_pixel_bytes().get_data()` and the repacking), and that stalls the main loop for as
long as the copy takes. Measured here, a 1 ms timer on the main loop while one picture is prepared
(`fit`, one 1080p monitor): 12 MP 13 ms, 24 MP 65 to 190 ms, 50 MP 150 to 290 ms between runs; review
measured 12 MP 15 to 18 ms, 24 MP 26 to 32 ms, and 1346 ms for an 88 MP picture, which the 50 MP limit
below now refuses. `fill` is cheaper (a crop is copied, not the whole picture): 50 MP 52 ms. On a
quiet machine the same probe gave 12 to 13 ms, 24 to 68 ms and 51 to 56 ms (one cold run 516 ms)
for 12, 24 and 50 MP, the same before and after the memory change below; the figure follows the
machine and what else runs on it, and the earlier range stays the one to expect.

### Memory, and the pixel limit

`MAX_PIXELS` is 50 000 000, derived and not guessed:

- Peak resident growth while one picture was prepared (a new process per run, JPEG and PNG with and
  without alpha, EXIF-rotated JPEG, one 1080p monitor, `fit`, the mode that copies the whole
  picture): 9.0 bytes per source pixel at 36 MP (310 MiB) and at 50 MP (431 MiB), whatever the
  width. A width whose three bytes per pixel are not a multiple of 4 (7071 x 7071, 6325 x 6325) used
  to cost 12.05 bytes per pixel (575 MiB at 50 MP, 13 % over the frame below): the repacking
  joined one string per row, a third copy of the frame at the peak. It now takes the pixbuf's
  bytes and adds the padding its last row lacks, which is one copy less (7071 x 7071: 430 MiB;
  7068 x 7068, a multiple of 4: 430 MiB; 10000 x 5000: 431 MiB). With two 4K-class monitors
  (3840 x 2160 and 2160 x 3840) the growth depends on the shape of the picture: a 4:3 landscape
  one (8164 x 6124, 50 MP) takes 449 MiB, which is 9.4 bytes per pixel (JPEG 448.5, PNG with alpha
  446 to 448; 441 MiB for the EXIF-rotated JPEG), and a square one (7071 x 7071, 50 MP) takes 430 to
  431 MiB, 9.03 bytes per pixel, the same as with two landscape monitors. Review measured 435 MiB
  (9.13 bytes per pixel) for the square picture on the mixed pair; that is 1 % more than the
  430 MiB measured here and was not reproduced (another stack), so the figure follows the
  machine by that much. `fill` 36 MP 259 MiB, 50 MP 359 MiB (7.5 bytes per pixel, a 4:3 landscape
  picture on a 1080p monitor).
- Memory frame: 512 MiB for the decode step. 512 MiB / 9.4 B = 57 MP; rounded down to **50 MP**,
  which leaves 12 % headroom (449 MiB at 50 MP, the worst of the measurements above, for every
  width) in `fit` and `fill`. The frame is a design figure, not a limit the code enforces: only
  `MAX_PIXELS` is.
- **`fit` is not the worst mode.** A panning frame (`--pan`, animations on) is: the decode, the
  whole tall frame and the copies of it are alive together. Measured in fresh processes on two 4K
  monitors, `fill` with the pan on: a 4000 x 12500 picture (50 MP, a frame of 3840 x 12000) 538 to
  542 MiB (three runs, the same at the last three commits of the pull request), which is 11.3
  bytes per source pixel and **5 to 6 % over the 512 MiB frame**, not under it; 5000 x 10000
  (50 MP) 430 MiB; 3000 x 6000 (18 MP) 307 MiB. An earlier version of this section said 526 MiB
  (11.0 bytes per pixel, 2.7 % over); that figure was not reproduced by the probe used now, and
  review measured about 11.9 bytes per pixel. So **512 MiB is not a hard limit** for the pan path:
  it can pass it by some tens of MiB. The limit is not narrowed (`MAX_PIXELS` stays at 50 MP); that
  the pan path can exceed the frame is a fact for whoever decides whether the pan stays on by
  default. The 6x pan limit bounds the frame, not the source.
- The earlier 100 MP would have been about 0.9 to 1.2 GB by the same arithmetic (extrapolated;
  100 MP was not run). `test_the_pixel_limit_is_the_documented_one` only pins the number, so a
  change is a decision. That the cost per pixel holds for every width is measured by
  `test_preparing_a_picture_costs_about_the_same_bytes_per_pixel_for_every_width`, which prepares a
  9 MP picture of a width with and without a row multiple of 4 in a fresh process and reads the
  peak resident size (limit 10.5 bytes per pixel; measured 9.0 to 10.1: review got 9.54 and 10.08 at
  the 3001 side with other libraries, so the limit leaves 4 % above the highest; 12.1 and 12.6
  before the fix); `test_rows_peaks_at_two_copies_...` checks the Python-side peak of the repacking itself.
- The file itself is held in memory while it is decoded, up to `MAX_FILE_BYTES` (256 MiB), so the
  worst case is that on top of the above; a normal 50 MP JPEG is some 25 MB. Lowering
  `MAX_FILE_BYTES` is a separate decision and is not made here.
- **What this does not do:** it bounds the memory of the decode step. It does not protect the
  process against running out of memory. Under a memory limit (cgroup, `RLIMIT_AS`) GLib's
  `failed to allocate` ends the **whole process** with SIGTRAP, and `except Exception` does not
  catch that; nor does it help against the kernel's OOM killer. Decoding in a separate process
  with `RLIMIT_AS` is the answer to that, and is a separate item before CORE-1, not part of this
  card.

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
GSETTINGS_SCHEMA_DIR=data python3 -m slideshow_lock.preview_app [--folder PATH]
```

Without `--folder` the stored picture folder is used. While none was chosen, that is
the system's pictures folder itself (the `XDG_PICTURES_DIR` of
`~/.config/user-dirs.dirs`, `~/Képek` on a Hungarian system), read recursively;
`~/Pictures` if the system has none configured, or if it is the home directory itself. If that folder holds no picture, the
preview shows "No pictures to show" with the path it looked at on the next line, and logs the same path.

Options (`--interval`, `--order`, `--scaling`, `--pan`, `--transition`, `--debug`) apply to that
run only and are never written to the settings. Any key, click, scroll or mouse movement ends it, and so does
the time limit of two minutes (section 2.1).
`start_preview(settings, source, application=None)` in the same module is what the settings window
calls (see `docs/preferences.md`); the service has its own controller wiring. Give it the
`Gtk.Application` and the preview also keeps the desktop's idle delay from blanking the screen
(section 2.1).

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
- Main-loop gaps (a 10 ms timer, software GL, two virtual monitors): the longest gap was 41 to
  110 ms over some twenty runs in the first round, 55 to 240 ms on clean runs of three different
  machines in review. In the three runs where the gaps were sorted by time it fell within 0.6 s
  after a picture appeared (54 to 78 ms, against 28 to 34 ms at all other times), which is texture
  upload and drawing; with the decoding cached the longest gap was still 44 to 54 ms. Picture
  reading, decoding and scaling are not on the main loop (tests), but the texture upload is, and on
  software GL it is not small. On a GPU it was not measured. The smoke test prints this number and
  does not gate on it (section 8).
- A panning frame of 36 MP source at 4K (3840 x 11520, 127 MiB): wrapping it into a `GLib.Bytes`
  on the main loop took 41 to 50 ms; made on the worker it takes no time on the main loop, and the
  worker's own stall of the main loop did not grow (46 to 47 ms in five runs, against 47 to 73 ms
  before). Review measured 300 to 530 ms for this copy on another machine.
- `Gtk.Settings` has the property `gtk-enable-animations` on GTK 4.8.3, true in the headless
  compositor, and it can be set. That the desktop's "reduce animations" choice of a GNOME session
  reaches it was **not measured** (no GNOME session here): the pan check against it is part of the
  manual test on the reference machine.
- `videoscale` Lanczos is not exact on flat areas: a flat 60 came out as 62 and a flat 200 as 206 at
  a 5:1 reduction on this stack (GStreamer 1.22), exact at 6:1. That is the quantised taps MEAS-1
  mentions. Not changed; only an eye on the reference machine can say whether it shows. The
  scaler test therefore measures ringing relative to the flat parts (section 8).
- The first pointer event of a window can carry another coordinate frame than the later ones
  (section 2, "Input").
- `Gtk.Application.inhibit` on GTK 4.8.3 with a Wayland surface and no window logs `Gtk-CRITICAL
  gtk_native_get_surface: assertion 'GTK_IS_NATIVE (self)' failed` and still sends the request; with
  the GTK window of the first preview window the critical error is gone. Against a fake session
  manager (`tests/fake_dbus.py`) the request arrives as `Inhibit(application id, 0, reason, 8)`, with
  the application id `<APP_ID>.Preview` (preview_app) or `<APP_ID>.Preferences` (settings window),
  and the give-back as `Uninhibit(cookie)` with the cookie it was handed
  (`tools/wayland-smoke/smoke_preview_inhibit.py`, 19 checks, SIGTERM during a preview and the limit shortened to 3 s included). **Not measured:** a real GNOME
  session manager (that it accepts the call, lists the inhibitor, and really keeps the screen on);
  whether GTK also takes a Wayland idle inhibitor for the window next to the D-Bus one (it was not
  looked at); what a real session manager does with the inhibitor when the process is killed
  (the fake does not watch bus names).
- **Transitions** (headless mutter, software GL, the stack above; the machine was loaded, so frame
  counts say nothing about a real screen). Two runs. (1) `tools/wayland-smoke/smoke_transitions.py`
  (one window, 640 x 360, the canvas driven by hand with chosen times, what GTK's own renderer
  draws read back as pixels, 86 checks): for every one of the ten the first frame is the old picture
  pixel for pixel, in the middle it is neither picture, after the end the canvas holds no run, no
  old texture and no tick and the pixels are the new picture exactly, and a `show_frame`, a message
  or a close in the middle ends the run at once; the blur was also run with the hardware path forced
  (its reduced pictures and the blur node) and the same checks passed; a Ken Burns picture smaller
  than the window became a cross-fade. (2) The real preview (`smoke_preview.py --transition NAME`,
  interval 3 s, three pictures, all ten names in turn, then `random` and `none`): every later
  picture came in with the chosen transition (`random`: eight names, never the same one twice in a
  row), no GTK or Python warning, and the redraws stopped when the transition was over: 11 to 47
  redraws per picture for the nine fixed-length ones, 82 to 109 for Ken Burns (it never stops
  while the picture is shown), 1 for the cut. In this session `LIBGL_ALWAYS_SOFTWARE` is set, so
  the `blur` ran as a cross-fade; its own drawing was exercised only in (1). **Not measured:** a real
  GPU, GTK 4.16, how any of the ten looks (a human judgement on a real screen), the CPU and battery
  cost on hardware (the plan's relative figures were taken with a prototype, not this code), the
  cost and look of the blur on a GPU, the stutter of the first frame on hardware, more than one
  monitor with a transition, whether the GNOME animation switch reaches `gtk-enable-animations`.
- The tests also ran on a second stack, the CI runner image: GStreamer 1.24.2, gdk-pixbuf 2.42.10,
  PyGObject 3.58, Python 3.12 (the result of the latest run is in the pull request).

## 8. Tests, and what they do not prove

- `tests/test_preview.py`: the controller with fake windows and clock around the real image source
  (140 tests): order, interval (also counted from when a picture appeared, not from the start),
  switching, a late next picture shown the moment it is ready, also when settings or the window size
  change meanwhile, a picture prepared for a placeholder or outdated window size made again (first
  picture, prepared next picture, every window, with and without a size event), live settings, a refresh that is still running when the next picture is
  replaced, damaged pictures with a negative control, failures that are not in a row not adding up,
  burst-limited error lines, empty source, deleted pictures, input on every window and kind, the
  worker result delivered on the main loop only, and the main loop staying free while a worker
  thread decodes (with a negative control showing the measurement catches a block), also for the
  redo after a settings or size change. A redo of the shown picture that fails out is tried again
  once per interval (not in a loop, and the log line that promises it is true), a picture
  prepared for the old mode is never shown when the interval ends before the redo, and the
  frames of a prepared picture that is deleted are not shown under its follower's name (the
  reset in the source-changed handler; the two resets of the prepared frames in `_prefetch` and
  `_swap` cover each other, so each alone can be taken out unseen, both together cannot).
- Transitions: `tests/test_transitions.py` (no GTK: the ten names, the one length, the
  half-of-the-interval cap and the range of the duration), `tests/test_transition_draw.py` (no GTK:
  for each of the ten what is
  painted at progress 0 and 1, that the new picture only comes in, ranges, the first frame, which
  transition is really drawn, the clock of a run and its end by time, and the choice: random never
  twice in a row, sequence order, one name, none, unknown names), the rules of when a picture comes
  in with one in `tests/test_preview.py` (first picture, redo, the same frame, animations off, a
  window with nothing on screen, live setting, one choice for all monitors) and the canvas in
  `tests/test_preview_window_logic.py` (start, an end by time and not by frame count, any later
  `set_frame` ends it, Ken Burns and blur rules, the order of the drawing calls against a recording
  snapshot). That the result looks right is not tested in CI: the pixel check of section 7 is a
  manual run in headless mutter.
- The "never locks" proof has three parts, and each has a limit. (1) A method spy: every call the
  controller makes on the objects handed to it is on a list of picture and timing methods; it sees
  nothing the controller does on its own (a call in `stop()` that goes to a subprocess is invisible
  to it). (2) A code scan of every module of the package that is not on the list of lock-side modules (the
  seven in `LOCK_SIDE_MODULES` in `tests/test_preview.py`, the five of the service plus the two
  entry points (`settings_app.py`, `control.py`), the only ones that name the lock, the session and the bus on purpose; a module goes on that list only by a decision made
  there), `__init__.py` included, plus a check that no scanned module imports one of the seven (a
  list of
  the preview's own modules once let a literal `os.system("true")` in `__init__.py` through both
  layers; measured): every identifier, import and string constant (docstrings excluded) is
  squashed to lower-case letters and digits and must contain none of: a whole word of the lock
  family (`lock`, `unlock`, `screensaver`, `dbus`, `systemd`, `inhibit`, ... and the ways to start
  any program: `system`, `execv`, `execve`, `execvp`, `execvpe`, `execl`, `execle`, `execlp`,
  `execlpe`, `startfile`, `ctypes`, `cdll`), or one of the fragments `screensaver`, `dbus`,
  `setactive`, `loginctl`, `busctl`, `qdbus`, `gdbus`, `subprocess`, `spawn`, `pydbus`, `suspend`,
  `logout`, `systemctl`, `session`, `bus`, `login1`, `logind`, `systemd`, `inhibit`, `popen`, and
  the names of other lockers (`locker` looks redundant next to the others and is not: measured
  with it removed, `light-locker-command` passes the scan): `locker`, `securelock`, `swaylock`, `i3lock`, `xlock`, `xtrlock`,
  `slock`. Eight exact names are allowed in every module (the `SessionSettings` class, the `--help`
  sentence "It never locks the session", the `lock-grace-period-seconds` key with its constant,
  getter and setter, two sentences of the settings window about the grace period), and three names
  of the idle request of section 2.1 are allowed in `preview_app.py` only, by (module, name):
  `inhibit`, `uninhibit` and `ApplicationInhibitFlags`, which are `Gtk.Application`'s own idle
  request and name no bus. A test fails if an allowance is no longer used, and one that the same
  names in `preview.py` or `preferences.py` are findings. A separate check of the syntax tree of
  `preview_app.py` requires exactly one `inhibit` call whose flags are `Gtk.ApplicationInhibitFlags.IDLE`
  and nothing else: another flag (`SWITCH`, `LOGOUT`, `SUSPEND`), a number, a combination, or any other
  mention of `ApplicationInhibitFlags` is a finding, and it has negative controls for those. Thirty real calls (busctl,
  gdbus, qdbus, loginctl, a Gio `call_sync` on the ScreenSaver, systemctl, pydbus, a bus socket, a
  `Logout` call, an `Inhibit` call by name and `inhibit` with all flags, `os.system`, every `os.exec*`, `os.startfile`, `ctypes`, `cdll`, and seven locker
  programs by name) are inserted into `stop()` of the real source in turn, and the scan must find
  each. It is a net, not a proof: a name assembled at runtime (a hex-encoded `os.system`, a name
  taken from a table) passes it. Two such gaps are known for the idle request, both tried by
  adding them to the real `preview_app.py` source: a second call written as
  `getattr(app, 'inhibit')(window, 15, reason)` passes the syntax-tree check, which counts only
  calls of the form `x.inhibit(...)`, and a call with the string `'Inhibit'` as its argument passes
  the name scan, because that name is allowed in this module. The effect of either is one more
  idle inhibitor, not a lock; the checks guard the intent, they do not prove it. (3) A tripwire in `tests/conftest.py`, active in every test unless
  it is marked `spawns_processes` (the tests that carry the mark are on a list in
  `tests/test_tripwire.py`, three now; a new one fails there until the list is changed): `os.system`, `popen`, `fork`, `exec*`, `spawn*`, `posix_spawn*`,
  `subprocess.Popen`, `GLib.spawn_*`, `Gio.bus_*`, `Gio.Subprocess.new` and `newv`,
  `Gio.SubprocessLauncher.spawnv` (`spawn` is not in the typelib) and `Gio.AppInfo.create_from_commandline`,
  `launch_default_for_uri` and `launch_default_for_uri_async` raise when called. It catches a call made
  through one of those names that the scan cannot see (a name assembled and looked up at the time
  of the call, `getattr(os, ...)`) wherever a test runs the line. It does **not** catch `ctypes`,
  a `Gio.AppInfo` object's own `launch` and `launch_uris`, `_posixsubprocess.fork_exec`, or a
  function object taken before the test started. Of 19 mutants that put a call into `stop()`, QA
  measured it 17 caught by the tripwire and the other two (`ctypes`, the key handler) by the scan only. Its negative controls are
  in `tests/test_tripwire.py` (the exec ones name a program that does not exist, so that with the
  tripwire off they fail instead of replacing the test process). What neither (2) nor (3) catches is an assembled name in code that no test
  runs. Measured by putting an `os.system` call into the window code: in `_on_key` it is caught
  (the window-logic tests call that handler), in the click handler's lambda or in `_on_tick` it is
  not (nothing calls them without a display), and the Wayland smoke does not run the tripwire.
  Measured the other way round: 15 lines put into `stop()`, a named call (`os.system`,
  `os.execv`, `os.execvpe`, `os.startfile`, `os.popen`, `subprocess.run`) is found by the scan and
  by the tripwire, a bare name (`ctypes`, `cdll`, a locker's program name) by the scan alone, and
  an assembled one (hex-encoded `os.system`, `"po" + "pen"`, `"sub" + "process"`, `"b" + "us_..."`,
  a `GLib.spawn_` name built from two strings) by the tripwire alone. Measured the third way: a
  name assembled from pieces and bound where no test runs (`getattr(os, "sys" + "tem")`,
  `getattr(Gio, "b" + "us_get_sync")` in the click lambda or in `_on_tick`) passes both. So the
  protection comes from the construction (there is no import and no name in the code that starts
  a program or reaches a bus, and a scan and a tripwire check that on every change); it is not
  a defence against someone who sets out to get round it. The Wayland smoke has no tripwire, so
  nothing sees such a line in a handler that only a display runs. Review showed that the earlier, narrower scan and the method spy let such
  calls through; the same lines are now found.
- `tests/test_scaling.py` (188): the geometry for 72 monitor and picture combinations, each for `fit`
  and `fill`; the pan rule (including the 1:3 picture on a portrait monitor, the 6x frame limit and
  the texture side limit as separate tests, and both limits pinned from both sides with literal
  numbers: exactly 6x pans, 6.001x does not; a frame side of 16384 pans, 16385 does not); JPEG,
  PNG and BMP completeness and the step cap, including the shipped default of a million steps; the
  file checks, including a file rewritten in place to the same size, which only its modification
  time reveals; the pixel limit.
- `tests/test_scaling_gdk.py` (99, every scaling test twice when GStreamer is there: once on the
  Lanczos path, once with GStreamer switched off): real decoding and scaling of real files: sizes,
  pixel positions of `fit` and `fill` crops, odd strides on both paths, EXIF orientation,
  transparency, truncated and bomb files, the fallback. `Frame.method` is only a label of the
  path taken; what the Lanczos path does is measured by a hard edge (40 to 220, 600 px down to
  100): the Lanczos result dips at least 4 levels below the dark side and peaks at least 4 above the
  bright side (measured 6 and 6 here), bilinear and a smaller envelope do not. A test that fails in
  CI when GStreamer is missing keeps the Lanczos path from silently dropping out. The probe for
  pictures without a loader (which opens the file once and never hangs on a FIFO, also one that
  appears after the first look), the repacking of rows for every width and block size, its
  peak memory, and the bytes per pixel of a whole `prepare` in a fresh process for a width with
  and without a row multiple of 4. A noisy photo cut off at 60 % is skipped with the real step
  limit.
- `tests/test_preview_window_logic.py` (19): the input logic of the window (first enter is a
  baseline, a second enter is motion, the 2 px threshold from the baseline, key, scroll, a close
  request) and the canvas deciding between a scrolling frame and its middle when the desktop's
  animation choice changes before a frame is shown (`set_frame`, with the drawing calls replaced),
  without a display: the handlers are plain methods, run on an instance made without the
  GTK constructor. `animations_enabled()` is tested with a faked `Gtk.Settings` (true, false, a
  value that is not a bool, no settings object, a change between two calls, the property name);
  whether the desktop's choice reaches the property is not (manual test). Which controller calls which handler, and the hidden cursor, are the smoke
  tool's.
- `tests/test_preview_app.py` (52): the command line, the settings of one run (an override of
  `false` or `0` still counts), the worker thread closed with the preview, the source with the
  probe, the idle request on every path, the two minute limit (on a fake clock: nothing waits
  for real), and a second start of `main` on the same application id (ignored while the preview is
  up, a new preview after it ended). The module imports GTK 4 without opening a display; CI installs `gir1.2-gtk-4.0` and its
  verify step checks that the GTK 4 typelibs import.
- `tests/test_tripwire.py` (23): the negative controls of the tripwire in `conftest.py` (every kind
  of call it guards raises; an opted-out test can start a program; ordinary calls are not in the
  way) and the list of tests that may opt out.
- `tools/wayland-smoke/run.sh`: the real windows, scaler and source inside a headless mutter with two
  virtual monitors, with real pointer motion, button and scroll events injected through mutter's
  remote-desktop service. It is not part of pytest or CI (it needs a compositor). What each input
  check proves: pointer motion (`--input motion`), left, right and middle button (`button`,
  `rbutton`, `mbutton`) and scroll are real events injected into the compositor and delivered to
  the window; `motion-small` resets the baseline by hand (section 2) and then shows that 1 px more
  does not end the preview and 4 + 3 px does; `key` fires the controller's `key-pressed` signal by
  hand, `close` calls `Gtk.Window.close()` by hand. Those two prove that the controller is wired
  to the preview, **not** that GTK or mutter produce a key event or a close request on their own
  (a headless mutter without a shell gives windows no keyboard focus). `--input none` injects
  nothing and does not park the pointer: it rests wherever the compositor left it, so it shows
  that nothing ends the preview by itself, not that a resting pointer is tolerated (that is
  what the baseline logic is for, and only a live session can test it). `--animations off --pan`
  switches `gtk-enable-animations` off before the windows open and checks that no tall panning
  frame is made (the portrait picture is monitor-sized) and that nothing scrolls and no tick runs.
  `--scaling fit --pan` is refused: the preview scrolls only a filled picture, so the scroll check
  could not hold. Every window's cursor property is checked to be
  `none`, which is the property, not what the compositor draws. The stall of the main loop is
  printed and not gated: it gave 55 to 240 ms on clean runs on three machines, so a threshold
  measured the machine, not the code; that picture work is off the main loop is what
  `test_ac7_*` prove, with a negative control.

Not proven by any of this: how the pictures look, behaviour on a real GPU or with fractional
scaling, real multi-monitor hardware, battery cost of the pan animation, behaviour on RHEL 10.2,
that the GNOME "reduce animations" choice reaches `gtk-enable-animations`. These belong on the
manual test list (DOC-2).
That list is not in this repository yet; the trial steps that exist are in
[`try-it.md`](try-it.md) and under "Trial on a real session" in [`service.md`](service.md).
