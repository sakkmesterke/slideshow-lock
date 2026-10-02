#!/bin/bash
# Run the whole CPU-side measurement (no display needed). Results land in ./out and ./data.
# Needs: python3-gobject python3-cairo python3-numpy gstreamer1-plugins-base (all AppStream).
set -e
cd "$(dirname "$0")"
mkdir -p out data
for case in A B C D; do
    python3 bench.py "$case" --out out
done
python3 prep_c.py data
python3 load_bench.py data
: > out/rss.jsonl
for path in "pixbuf nearest" "pixbuf bilinear" "pixbuf hyper" "cairo good" "cairo best" \
    "gst bilinear" "gst lanczos env=3" "numpy lanczos3 (own code)"; do
    python3 rss_one.py "$path" >> out/rss.jsonl
done
echo "done: out/results_*.json, data/load_results.json, out/rss.jsonl"
