# Reproducing the documentation screenshots

The PNGs in `assets/` capture the current curses UI running in real, isolated
tmux terminals. Participant identities, conversation and usage figures are
synthetic fixtures. They do not show a user's session, credentials or account
allowance. The window frame is added when the captured ANSI cells are rendered
to PNG; it does not imply a particular terminal application.

From a checkout with Collab's dependencies available, install the optional
capture dependencies into a separate environment, then run:

```bash
python -m venv --system-site-packages /tmp/collab-capture-venv
source /tmp/collab-capture-venv/bin/activate
python -m pip install rich cairosvg
python scripts/capture-docs.py
```

The host also needs `tmux`, Cairo and the DejaVu Sans Mono font. The script uses
an absolute `PYTHONPATH` for this checkout, a temporary configuration/state
folder and a private tmux socket for each capture. It closes only those private
servers and leaves existing terminals and Collab sessions alone. It preserves
trailing blank cells and records the palette installed by curses, so custom
theme backgrounds and colours survive the ANSI-to-PNG export.

| Image | Terminal size | View |
| --- | --- | --- |
| `demo.png` | 168×38 | Coding agent and combined viewer |
| `participant-panel.png` | 90×34 | Collapsed participants and conversation |
| `participant-details.png` | 120×32 | Expanded participant, main and worker columns |
| `participant-panel-narrow.png` | 52×36 | Expanded participant in a narrow pane |
| `worker-telemetry.png` | 120×32 | Independent main and worker telemetry |
| `theme-cyberpunk.png` | 120×32 | Expanded Cyberpunk panel |
| `theme-matrix.png` | 120×32 | Expanded Matrix panel |
| `theme-matrix-compact.png` | 90×34 | Collapsed Matrix panel and conversation |
| `theme-image.png` | 120×32 | Dimmed synthetic PNG behind participant details |
| `settings-panel.png` | 100×34 | Worker controls with Reset to default visible |
| `settings-editor.png` | 100×20 | External-editor or inline text choice |

Combined views give the roster 38 percent of the terminal height. Matrix and
image captures use 65 percent background dimming (the default is 85). The image
fixture is generated locally by the capture script. Chat uses the four-line
«show more» default.

Review the generated images for wrapping, clipping, correct glyphs and readable
colours before committing them. The script changes screenshots only; it does
not alter the application's demo data or install Collab.
