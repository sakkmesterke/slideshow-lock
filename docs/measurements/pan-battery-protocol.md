# Pan animation: battery measurement protocol (outline)

Status: protocol outline for the reference laptop. Nothing in it has been measured. DOC-2 takes it
over and turns it into the manual test list entry. The result decides whether the default of
`pan-portrait-images` may ever change from off; that decision is the project owner's, not the
measurement's.

## 1. Question

How much more energy does the slideshow use with the pan animation on than with the same
pictures standing still, on the same laptop, over the same time?

Why it needs measuring: with the animation on, the window is redrawn on every frame of the
monitor (about 100 redraws per 2 s picture were counted in the headless run, against 1 to 2 for
a still picture). What that costs in watts depends on the GPU and the display, which only the
reference laptop can say.

## 2. What must be identical in both runs

Same laptop, same battery, same session, same monitor setup (note the number and size of
monitors), same brightness (fixed by hand, no auto-brightness), same picture folder, same slide
interval, same duration, same room, same Wi-Fi and Bluetooth state, no other application open,
notifications off, nothing else touching the keyboard or mouse (any input ends the preview).

Only one thing differs: `--pan` (or the `pan-portrait-images` setting) on or off.

## 3. Picture folder

A folder with portrait pictures only (for example twenty phone photos), because only they can
pan. With a mixed folder the animated share of the time would change with the mix.

## 4. Procedure

1. Charge to 100 %, unplug the charger, wait 2 minutes, let the battery settle.
2. Note the date, the laptop model, `rpm -q gtk4 gdk-pixbuf2 gstreamer1-plugins-base`, the
   renderer (`GSK_DEBUG=renderer` prints it), the monitor setup, the brightness.
3. Read the energy counter: `cat /sys/class/power_supply/BAT0/energy_now` (or `charge_now` if the
   battery exposes charge instead of energy) and `energy_full`. Note the time.
4. Start the preview with the pictures and walk away. Without pan:
   `python3 -m slideshow_lock.preview_app --folder DIR --interval 20 --scaling fill`
   With pan: the same command with `--pan` added. (Start it with a delay, for example
   `sleep 30; python3 -m ...` in a terminal you then leave alone, so that no input follows the
   start.)
5. After 30 minutes, end the preview with one key press, and read the energy counter and the time
   again.
6. Repeat alternately, so that drift in the battery and in the room cancels out: still, pan, still,
   pan, still, pan (three of each). Let the laptop rest on AC for the same time between runs, or
   recharge to the same level.

## 5. What to record

| Run | Mode | Start energy | End energy | Minutes | Mean power (W) | Notes |
|---|---|---|---|---|---|---|
| 1 | still | | | | | |
| 2 | pan | | | | | |
| 3 | still | | | | | |
| 4 | pan | | | | | |
| 5 | still | | | | | |
| 6 | pan | | | | | |

Mean power in watts is the energy difference (in microwatt-hours from `energy_now`, divided by
1 000 000 for Wh) divided by the hours; `power_now` can be sampled every minute as a cross-check.
Report the mean and the spread of the three runs per mode, not a single run. If the three runs of
one mode differ by more than the difference between the modes, the measurement says nothing yet:
repeat it.

Optionally, in the same runs: the GPU and CPU load (`top -b -n 5 -d 60` filtered to the preview
process), and the frame statistics printed by MEAS-1's `target_check.py` for the same monitor.

## 6. Reporting

The table above, the notes of step 2, and one sentence: "pan costs X W more (X % of the still
slideshow) on this laptop". The default stays off unless the project owner decides otherwise on the
basis of that sentence.
