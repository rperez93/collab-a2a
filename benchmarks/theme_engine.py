"""Bounded theme/cache and settings refresh costs, without a real terminal.

Pair writes are counted in memory; real ncurses parsing and keyboard/mouse are
covered by test_theme_panel_in_tmux.py. This isolates parser/cache CPU and heap.
"""
import curses
import gc
import json
import os
from pathlib import Path
import tempfile
import time
import tracemalloc


def measure(action, count):
    gc.collect()
    before = tracemalloc.get_traced_memory()[0]
    snapshot = tracemalloc.take_snapshot()
    tracemalloc.reset_peak()
    wall, cpu = time.monotonic(), time.process_time()
    for index in range(count):
        action(index)
    elapsed, consumed = time.monotonic() - wall, time.process_time() - cpu
    gc.collect()
    current, peak = tracemalloc.get_traced_memory()
    growth = tracemalloc.take_snapshot().compare_to(snapshot, "lineno")
    return {"iterations": count, "wall_seconds": elapsed, "cpu_seconds": consumed,
            "retained_python_bytes_delta": current - before, "peak_python_bytes": peak,
            "largest_growth": [str(row) for row in growth[:5]]}


def main():
    with tempfile.TemporaryDirectory(prefix="collab-theme-benchmark-") as temp:
        root = Path(temp)
        os.environ.update(COLLAB_CONFIG=str(root / "config.json"), COLLAB_HOME=str(root / "home"),
                          COLLAB_STATE_DIR=str(root / "state"), COLLAB_AGENT_ID="theme-benchmark")
        from collab import config, themes
        from collab.client import tui
        from collab.client.settings_tui import SettingsEditor
        config.save_config({"theme": "benchmark"})
        # The config reader deliberately rereads a just-written file until its
        # timestamp settles. Theme-file edits below do not edit this config;
        # measure their steady path instead of a setup-time settling window.
        settled = time.time() - 10
        os.utime(root / "config.json", (settled, settled))
        folder = root / "themes"
        folder.mkdir()
        path = folder / "benchmark.md"
        path.write_text(themes.template("benchmark", themes.resolve("cyberpunk")))
        curses.COLORS, curses.COLOR_PAIRS = 256, 256
        curses.can_change_color = lambda: False
        writes = [0]
        def pair(*args):
            writes[0] += 1
        curses.init_pair = pair
        tui._THEME_POLLING[0] = True
        tui._deal_colours(256)
        tui._apply_theme_palette()
        editor = SettingsEditor()
        tracemalloc.start()
        for _ in range(100):
            tui._apply_theme_palette()
            editor.refresh()
        before = writes[0]
        idle = measure(lambda _: tui._apply_theme_palette(), 10000)
        idle["palette_pair_writes"] = writes[0] - before
        settings = measure(lambda _: editor.refresh(), 10000)
        settings_followup = measure(lambda _: editor.refresh(), 10000)
        before = writes[0]
        def edit(index):
            path.write_bytes(f'---\nbackground: #020905\nforeground: #{index % 256:02x}ffaa\n---\n'.encode())
            tui._THEME_CACHE["check_at"] = 0
            tui._apply_theme_palette()
        edits = measure(edit, 500)
        edits.update(palette_pair_writes=writes[0] - before,
                     dynamic_pair_cache=len(tui._PAIRS_BY_COLOUR), hex_cache=len(tui._HEX_SLOTS),
                     theme_folder_cache=len(themes._MD_CACHE))
        tracemalloc.stop()
        print(json.dumps({"idle_palette": idle, "idle_settings": settings,
                          "idle_settings_followup": settings_followup, "theme_edits": edits}, indent=2))


if __name__ == "__main__":
    main()
