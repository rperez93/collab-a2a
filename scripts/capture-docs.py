#!/usr/bin/env python3
"""Capture the shipped curses UI with synthetic data, never a user's session.

Requires tmux, rich and cairosvg. Run from any directory with the environment
that imports Collab's dependencies. Each capture owns a private tmux socket,
config and state directory; captured ANSI cells are rendered unchanged to PNG.
"""
from pathlib import Path
import json
from datetime import datetime, timezone
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def scene(which):
    from collab import demo
    from collab.client import tui, demo_agent
    # tmux preserves indexed SGR colors but omits OSC palette changes from
    # capture-pane. Save exactly the colors curses installs so exports do not
    # replace custom theme colors with the standard grayscale slots 232–255.
    import curses
    palette = {}
    init_color = curses.init_color

    def record_color(index, red, green, blue):
        init_color(index, red, green, blue)
        palette[str(index)] = [round(c * 255 / 1000) for c in (red, green, blue)]
        target = Path(os.environ['COLLAB_DOC_PALETTE'])
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(palette))
        temporary.replace(target)

    curses.init_color = record_color
    original = demo.snapshot

    def snapshot():
        data = original()
        now = time.time()
        for i, person in enumerate(data['participants']):
            person['color'] = ('#79b8ff', '#56d4dd', '#d2a8ff')[i]
            stats = person['stats']
            stats.update(model='gpt-6-astra' if i == 1 else 'claude-opus-5' if i == 0 else 'claude-sonnet-5',
                         tokens_out=12500, tokens_cached=48000, context_tokens=82000 if i == 0 else 44000,
                         context_limit=200000, cost_usd=2.35 if i == 0 else 1.10,
                         cost_kind='estimated', cost_scope='session', source='demo fixture',
                         observed_at=now-5, model_observed_at=now-5, context_observed_at=now-5,
                         tokens_observed_at=now-5, cost_observed_at=now-5, quota_observed_at=now-5,
                         subagents={'active': 1 if i == 0 else 0, 'total': 2, 'source':'demo fixture','observed_at':now-5})
            for quota in stats.get('quotas', {}).values():
                quota['resets_at'] = datetime.fromtimestamp(now + 7200, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            stats['worker'] = dict(enabled=i != 2, running=i == 0,
                agent='codex' if i == 1 else 'claude', model='gpt-5.6-luna' if i == 1 else 'haiku',
                turns=12, attempts=12, pending=0, errors=0, tokens_in=6200, tokens_out=1300,
                tokens_cached=2800, tokens_cache_write=1200, cost_usd=.04,
                cost_kind='estimated', cost_scope='worker lifetime',
                source='demo fixture', observed_at=now-5, tokens_observed_at=now-5,
                cost_observed_at=now-5, quota_observed_at=now-5,
                quotas={'five_hour':{'used_pct':18,'resets_at':datetime.fromtimestamp(now+7200, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                        'seven_day':{'used_pct':32,'resets_at':datetime.fromtimestamp(now+172800, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}},
                quota_scope='shared_account')
            if i == 0:
                stats['worker'].update(last_model='claude-haiku-4-5', context_pct=3,
                    context_tokens=6000, context_limit=200000, context_observed_at=now-5)
        return data

    demo.snapshot = snapshot
    if which == 'full':
        return demo_agent.run_together()
    if which == 'settings':
        from collab.cli import main
        return main(['config', '--tui'])
    model = demo.model()
    return tui.run(model.profile, view='roster' if which == 'details' else 'both', model=model)


def capture_all():
    from rich.console import Console
    from rich.text import Text
    from rich.terminal_theme import TerminalTheme, DEFAULT_TERMINAL_THEME
    import cairosvg
    # Dimensions belong to the terminal, not a reconstructed mock of the UI.
    shots = [
        ('demo', 'full', 168, 38, 'classic', [], 'Collab · coding agent + live viewer'),
        ('participant-panel', 'both', 90, 34, 'classic', [], 'Collab · compact participants and conversation'),
        ('participant-details', 'details', 120, 32, 'classic', ['Enter'], 'Collab · expanded main and worker details'),
        ('participant-panel-narrow', 'details', 52, 36, 'classic', ['Enter'], 'Collab · narrow participant details'),
        ('worker-telemetry', 'details', 120, 32, 'classic', ['Enter'], 'Collab · separate main and worker telemetry'),
        ('theme-cyberpunk', 'details', 120, 32, 'cyberpunk', ['Enter'], 'Collab · Cyberpunk'),
        ('theme-matrix', 'details', 120, 32, 'matrix', ['Enter'], 'Collab · Matrix'),
        ('theme-matrix-compact', 'both', 90, 34, 'matrix', [], 'Collab · compact Matrix panel'),
        ('settings-panel', 'settings', 100, 34, 'classic', [], 'Collab · worker settings'),
    ]
    with tempfile.TemporaryDirectory(prefix='collab-doc-capture-') as folder:
        home = Path(folder)
        for name, mode, cols, rows, theme, keys, title in shots:
            config = home / (name + '.json')
            config.write_text(json.dumps({'theme':theme, 'watch_roster_size':38,  # percent of window height
                'watch_participant_details':False,
                'watch_participant_fields':['model','context','quota','cost','subagents','worker']}))
            env = {**os.environ, 'COLLAB_CONFIG':str(config), 'COLLAB_HOME':str(home/'state'),
                   'COLLAB_STATE_DIR':str(home/'state'), 'PYTHONPATH':str(ROOT/'src'),
                   'COLLAB_DOC_PALETTE':str(home/(name+'.palette.json')),
                   'TERM':'xterm-256color','NCURSES_NO_UTF8_ACS':'1'}
            env.pop('TMUX', None)
            cmd = ['tmux', '-S', str(home/(name+'.sock'))]
            def call(*args):
                return subprocess.run(cmd+list(args), env=env, check=True,
                    capture_output=True, text=True, timeout=10).stdout
            try:
                call('-f','/dev/null','new-session','-d','-x',str(cols),'-y',str(rows),
                     shlex.join(['env','TERM=xterm-256color',sys.executable,str(Path(__file__).resolve()),'--scene',mode]))
                until = time.monotonic()+10
                marker = 'collab settings' if mode == 'settings' else 'PARTICIPANTS'
                while marker not in call('capture-pane','-p'):
                    if time.monotonic() > until:
                        raise RuntimeError(f'{name}: viewer did not draw')
                    time.sleep(.1)
                if mode == 'settings':
                    call('send-keys','-l','/worker')
                    call('send-keys','Enter')
                if keys:
                    call('send-keys',*keys)
                # The full-window demo reveals its scripted agent output over
                # time; capture after the last line, while the real viewer polls.
                time.sleep(8 if mode == 'full' else .5)
                ansi = call('capture-pane','-p','-e','-N')
                palette_path = home / (name + '.palette.json')
                palette = json.loads(palette_path.read_text()) if palette_path.exists() else {}
                def restore_color(match):
                    role, index = match.groups()
                    rgb = palette.get(index)
                    return (f"{role};2;" + ';'.join(map(str, rgb))) if rgb else match.group()
                ansi = re.sub(r'(38|48);5;(\d+)', restore_color, ansi)
                terminal_theme = TerminalTheme((0, 0, 0), (229, 229, 229),
                                                [DEFAULT_TERMINAL_THEME.ansi_colors[i] for i in range(8)],
                                                [DEFAULT_TERMINAL_THEME.ansi_colors[i] for i in range(8, 16)])
                with open(os.devnull,'w') as sink:
                    console = Console(record=True,width=cols,height=rows,force_terminal=True,
                                      color_system='truecolor',file=sink)
                    console.print(Text.from_ansi(ansi),end='')
                    svg = console.export_svg(title=title, theme=terminal_theme)
                svg = re.sub(r'    @font-face \{.*?\n    \}', '', svg, flags=re.S)
                svg = svg.replace('font-family: Fira Code, monospace;',
                                  'font-family: DejaVu Sans Mono, monospace;')
                cairosvg.svg2png(bytestring=svg.encode(),write_to=str(ROOT/'assets'/f'{name}.png'))
                print(name, f'{cols}x{rows}', flush=True)
            finally:
                subprocess.run(cmd+['kill-server'],env=env,capture_output=True,timeout=10)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--scene']:
        raise SystemExit(scene(sys.argv[2]))
    capture_all()
