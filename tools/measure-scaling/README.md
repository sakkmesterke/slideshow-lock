# Scaling measurement tools (MEAS-1)

Measurement scripts behind `docs/measurements/scaling.md`. They are not part of the product package.

| File | Purpose |
|---|---|
| `scene.py` | deterministic synthetic test scene, renderable at any size |
| `bench.py` | cost and quality of every scaling path (cases A to D), `python3 bench.py A --out out` |
| `prep_c.py`, `load_bench.py`, `rss_one.py` | 24 MP JPEG/PNG decode, scale-on-load, `pamscale`, peak memory |
| `run-cpu.sh` | runs the whole CPU part |
| `capture_ngl.py` | renders the GSK texture path with the window's renderer (needs a compositor) |
| `gl_metrics.py` | quality numbers of the `capture_ngl.py` output (needs `bench.py A` and `B` first) |
| `sheets.py` | builds the comparison PNG sheets from `out/` |
| `target_check.py` | on-target check with a real display (see section 8 of the report) |

Dependencies, all from BaseOS/AppStream: `python3-gobject`, `python3-cairo`, `python3-numpy`,
`gstreamer1-plugins-base` (and `netpbm-progs` for the `pamscale` row of `load_bench.py`).
