"""Writes ``tests/compose_1_0_1.json``: what 1.0.1 drew, for ``tests/test_transition_plain.py``.

Run it on a checkout of the ``v1.0.1`` tag (not on this tree: the file is what that release did),
with PyGObject and GTK 4 installed:

    git worktree add --detach ../slideshow-lock-1.0.1 v1.0.1
    python3 tools/dump_compose_1_0_1.py ../slideshow-lock-1.0.1 tests/compose_1_0_1.json

For every transition (and a name that is none of them), two window sizes and many moments of
progress it records ``compose`` (and ``first_frame``) of that release, with the ``fade_share`` only
Ken Burns reads, and the names of the ``Gtk.Snapshot`` calls that release's canvas makes for those
``Draw`` values.
"""

import json
import sys

PROGRESS = [0.0, 0.01, 0.1, 0.25, 0.37, 0.5, 0.63, 0.75, 0.9, 0.99, 1.0]


def main(repo, target):
    sys.path.insert(0, repo)
    import gi

    gi.require_version("Gtk", "4.0")
    from slideshow_lock import preview_window, transition_draw
    from slideshow_lock.transitions import ALL_TRANSITIONS

    class Texture:
        def __init__(self, width, height):
            self.size = (width, height)

        def get_width(self):
            return self.size[0]

        def get_height(self):
            return self.size[1]

    class Snapshot:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            return lambda *args: self.calls.append(name)

    def calls_of(draws, width, height):
        canvas = preview_window._Canvas.__new__(preview_window._Canvas)
        canvas._scale = lambda: 1.0
        canvas._old_texture = Texture(int(width), int(height))
        canvas._texture = Texture(int(width), int(height))
        canvas._old_offset = canvas._offset = (0, 0)
        canvas._reduced = {}
        snapshot = Snapshot()
        for draw in draws:
            canvas._paint(snapshot, draw, width, height)
        return snapshot.calls

    rows = []
    for name in list(ALL_TRANSITIONS) + ["nonsense"]:
        for width, height in ((1920.0, 1080.0), (320.0, 180.0)):
            for share in (1.0, 0.3) if name == "ken-burns" else (1.0,):
                for progress in PROGRESS + ["first"]:
                    if progress == "first":
                        draws = transition_draw.first_frame(name, width, height, share)
                    else:
                        draws = transition_draw.compose(name, progress, width, height, share)
                    rows.append(
                        {
                            "name": name,
                            "size": [width, height],
                            "fade_share": share,
                            "progress": progress,
                            "draws": [list(draw) for draw in draws],
                            "calls": calls_of(draws, width, height),
                        }
                    )
    with open(target, "w") as handle:
        json.dump(rows, handle, separators=(",", ":"))
    print(len(rows), "rows")


if __name__ == "__main__":
    main(*sys.argv[1:3])
