"""Measure real curses sidebar, roster and chat in isolated private tmux servers.

Use the same script with each version's absolute PYTHONPATH for comparisons.
Synthetic input exercises rendering, windowing and fresh stats at 5 messages/s;
HTTP and provider costs are measured separately by runtime.py. No live state.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

from runtime import process_sample


def pane(view, busy):
    from collab import demo
    from collab.client import tui
    from collab.protocol import Envelope
    m = demo.model()
    original_refresh = m.refresh_side
    started = time.monotonic()
    last = 0

    def refresh():
        original_refresh()
        m.snapshot['fetched_at'] = time.time()
        if busy:
            for p in m.snapshot['participants']:
                p['stats'] = {'model': 'synthetic', 'tokens_in': int(time.monotonic()-started)*100,
                              'context_pct': 25, 'observed_at': time.time(),
                              'worker': {'model': 'synthetic-worker', 'tokens_out': 200,
                                         'observed_at': time.time()}}
    def poll(follow=True):
        nonlocal last
        tick = int((time.monotonic()-started)*5)
        added = max(0, tick-last)
        if busy and added:
            for _ in range(min(added, 20)):
                seq = m._inbox.events[-1].seq + 1
                m._inbox.events.append(Envelope(seq=seq, kind='chat', sender='jarvis',
                    text='Synthetic update: the test is running. ' + 'bounded rendering '*12))
            m._inbox.events[:] = m._inbox.events[-1000:]
            if follow:
                m.load_tail()
        last = tick
        return added if busy else 0
    m.refresh_side = refresh
    m.poll_events = poll
    refresh()
    return tui.run(m.profile, view=view, model=m)


def measure(root, view, busy, seconds):
    key = f'{view}-{"busy" if busy else "idle"}'
    command = ['tmux', '-S', str(root / (key+'.sock'))]
    env = dict(os.environ, TERM='xterm-256color')
    env.pop('TMUX', None)
    def call(*args):
        return subprocess.run(command+list(args), env=env, check=True, capture_output=True,
                              text=True, timeout=5).stdout.strip()
    try:
        args = [sys.executable, str(Path(__file__).resolve()), '--pane', view]
        if busy:
            args += ['--busy']
        call('-f', '/dev/null', 'new-session', '-d', '-x', '120', '-y', '40', 'exec '+shlex.join(args))
        deadline = time.monotonic()+10
        while 'edith' not in call('capture-pane', '-p'):
            if time.monotonic() > deadline:
                raise RuntimeError('Panel did not render: '+call('capture-pane', '-p'))
            time.sleep(.1)
        pid = int(call('display-message', '-p', '#{pane_pid}'))
        server = int(call('display-message', '-p', '#{pid}'))
        time.sleep(2)
        initial, tmux_initial = process_sample(pid), process_sample(server)
        began = time.monotonic()
        samples = []
        while time.monotonic()-began < seconds:
            time.sleep(min(1, max(0, seconds-(time.monotonic()-began))))
            sample = process_sample(pid)
            assert sample['identity'] == initial['identity']
            samples.append(sample)
        wall = time.monotonic()-began
        cpu = samples[-1]['cpu_seconds']-initial['cpu_seconds']
        return {'view': view, 'busy': busy, 'wall_seconds': round(wall,3),
                'cpu_seconds': round(cpu,3), 'one_core_percent': round(cpu/wall*100,3),
                'rss_initial_kib': initial['rss_kib'], 'rss_final_kib': samples[-1]['rss_kib'],
                'rss_peak_kib': max(s['rss_kib'] for s in samples),
                'fd_initial': initial['open_fds'], 'fd_final': samples[-1]['open_fds'],
                'tmux_cpu_seconds': round(process_sample(server)['cpu_seconds']-tmux_initial['cpu_seconds'],3)}
    finally:
        subprocess.run(command+['kill-server'], env=env, capture_output=True, timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=15)
    parser.add_argument('--pane', choices=['both','roster','chat'])
    parser.add_argument('--busy', action='store_true')
    parser.add_argument('--theme', choices=['classic','matrix','cyberpunk'])
    parser.add_argument('--background', choices=['none','matrix','image'], default='none')
    parser.add_argument('--views', nargs='+', choices=['both','roster','chat'], default=['both','roster','chat'])
    args = parser.parse_args()
    if args.pane:
        return pane(args.pane, args.busy)
    if not 1 <= args.seconds <= 60:
        parser.error('seconds must be 1..60')
    with tempfile.TemporaryDirectory(prefix='collab-panels-') as temp:
        root = Path(temp)
        os.environ.update(COLLAB_CONFIG=str(root/'config.json'), COLLAB_HOME=str(root/'home'),
                          COLLAB_STATE_DIR=str(root/'state'), COLLAB_PEERS_DIR=str(root/'peers'),
                          COLLAB_NO_UPDATE_CHECK='1')
        cfg = {'watch_participant_details': True, 'watch_background':args.background,
               'theme':args.theme or ('matrix' if args.background == 'matrix' else 'classic')}
        if args.background == 'image':
            from PIL import Image
            picture = Image.new('RGB',(1920,1080),(80,140,220))
            picture.save(root/'wall.png')
            cfg['watch_background_image'] = str(root/'wall.png')
        (root/'config.json').write_text(json.dumps(cfg))
        from collab import __version__
        rows = [measure(root,view,busy,args.seconds) for busy in (False,True) for view in args.views]
        print(json.dumps({'version': __version__, 'background':args.background, 'theme':cfg['theme'], 'panels': rows,
            'limits': 'Linux /proc, real 120x40 curses, 2s warmup, one-core CPU. Both includes sidebar. Synthetic bounded history; busy input 5 messages/s and changing stats. Excludes network, daemon and providers; no live session.'}, indent=2))

if __name__ == '__main__':
    main()
