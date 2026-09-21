"""Decoration has a fixed budget and never becomes an input or a busy loop."""
import os
from pathlib import Path
import time
import pytest
from PIL import Image
from collab.client import background as bg
from collab.runtime_settings import set_value


def configured(mode, **settings):
    set_value('watch_background', mode)
    for key, value in settings.items():
        set_value('watch_'+key, value)
    layer = bg.Background()
    layer.prepare({})
    return layer


def test_matrix_is_cached_at_its_rate_and_reduced_motion_freezes_it():
    """Input redraws cannot turn a two-Hz decoration into a high-rate animation."""
    layer = configured('matrix', background_fps=2)
    start = layer.started
    first = layer.frame(120,30,now=start)
    assert first and first is layer.frame(120,30,now=start+.1)
    assert first != layer.frame(120,30,now=start+1)
    set_value('watch_reduced_motion', True)
    layer.prepare({})
    frozen = layer.frame(120,30,now=start+2)
    assert frozen is layer.frame(120,30,now=start+100)
    assert len(layer.frame(10000,10000)) <= bg.MAX_CELLS


@pytest.mark.parametrize('suffix', ['png','jpg'])
def test_images_decode_once_and_refresh_when_the_file_changes(tmp_path, monkeypatch, suffix):
    """Static wallpaper must not decode on every frame, even when resized."""
    path = tmp_path/('wall.'+suffix)
    Image.new('RGB',(320,200),(120,200,255)).save(path)
    calls=[]
    read=bg.read_image
    monkeypatch.setattr(bg,'read_image',lambda p: (calls.append(p),read(p))[1])
    layer = configured('image', background_image=str(path), background_dim=40)
    start=time.monotonic()
    first=layer.frame(80,20,now=start)
    assert len(first)==1600
    assert first is layer.frame(80,20,now=start+.1)
    layer.frame(120,30,now=start+.5)
    assert len(calls)==1
    Image.new('RGB',(320,200),(255,50,0)).save(path)
    layer.stat_at = 0
    assert layer.prepare({})
    assert first != layer.frame(80,20,now=start+2)
    assert len(calls)==2


def test_disabled_and_fully_dimmed_backgrounds_never_open_the_file(monkeypatch):
    """Turning decoration off must remove its I/O and rendering cost."""
    monkeypatch.setattr(bg,'read_image',lambda p: pytest.fail('disabled image was opened'))
    for mode,dim in [('none',0),('image',100)]:
        layer=configured(mode,background_dim=dim,background_image='/missing.png')
        assert not layer.frame(120,30)


def test_invalid_and_oversized_images_fail_once_without_crashing_the_view(tmp_path,monkeypatch):
    """Bad wallpaper cannot allocate its declared dimensions or retry each frame."""
    path=tmp_path/'bad.png'
    path.write_bytes(b'not a PNG')
    layer=configured('image',background_image=str(path))
    assert not layer.frame(120,30)
    assert layer.error
    big=tmp_path/'big.png'
    Image.new('1',(2001,2000)).save(big)
    with pytest.raises(ValueError,match='4 million'):
        bg.read_image(big)
    fifo=tmp_path/'pipe.png'
    os.mkfifo(fifo)
    before=time.monotonic()
    with pytest.raises(ValueError,match='regular'):
        bg.read_image(fifo)
    assert time.monotonic()-before < .5


def test_dimming_changes_only_the_decoration_colors(tmp_path):
    """Dimming is continuous brightness scaling, never foreground text opacity."""
    path=tmp_path/'white.png'
    Image.new('RGB',(20,20),'white').save(path)
    layer=configured('image',background_image=str(path),background_dim=0)
    before=layer.frame(20,10)
    set_value('watch_background_dim',90)
    layer.prepare({})
    after=layer.frame(20,10)
    assert before[0][3]=='#ffffff' and after[0][3]=='#191919'


def test_replacing_an_image_invalidates_the_palette_before_text_is_laid_out(tmp_path):
    """The same pathname with new pixels needs a fresh palette, not stale pairs."""
    path=tmp_path/'wall.png'
    Image.new('RGB',(64,64),'red').save(path)
    layer=configured('image',background_image=str(path))
    first=layer.frame(80,20)
    old_colors={cell[3] for cell in first}
    for size in ((100,25),(40,10),(240,60)):
        assert {cell[3] for cell in layer.frame(*size)} == old_colors
    Image.new('RGB',(64,64),'blue').save(path)
    layer.stat_at=0
    assert layer.prepare({})
    assert old_colors != {cell[3] for cell in layer.frame(80,20)}
    assert not layer.prepare({})


def test_live_image_edits_keep_real_terminal_pairs_and_text_caches_in_sync(tmp_path):
    """Repeated wallpaper edits cannot consume pair IDs or reuse cached text colours."""
    import json,shlex,shutil,subprocess,sys
    if not shutil.which('tmux'):
        pytest.skip('tmux is not installed')
    result=tmp_path/'result.json'
    script=tmp_path/'render.py'
    script.write_text('''
import curses,json,os,time
from pathlib import Path
from PIL import Image
from collab import demo,config
from collab.client import tui
root=Path(os.environ['TEST_ROOT'])
path=root/'wall.png'
config.save_config({'theme':'matrix','watch_background':'image','watch_background_image':str(path)})
m=demo.model(); m.load_initial(); pane=tui.Tui(m)
rows=[]
def run(win):
 tui._init_colors()
 for n in range(40):
  Image.new('RGB',(64,64),((n*17)%256,(n*43)%256,(n*79)%256)).save(path)
  if hasattr(pane,'_panel_background'): pane._panel_background.stat_at=0
  pane._draw(win)
  version=tui._theme_version()
  assert pane._rows_key[4]==version
  assert pane._roster_key[1]==version
  rows.append({'pairs':tui._NEXT_PAIR[0],'colors':len(tui._HEX_SLOTS),'version':version})
  time.sleep(.005)
curses.wrapper(run)
(root/'result.json').write_text(json.dumps(rows))
''')
    env={**os.environ,'TEST_ROOT':str(tmp_path),'COLLAB_CONFIG':str(tmp_path/'config.json'),
         'COLLAB_HOME':str(tmp_path/'home'),'COLLAB_STATE_DIR':str(tmp_path/'state'),
         'COLLAB_PEERS_DIR':str(tmp_path/'peers'),'TERM':'xterm-256color',
         'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
    env.pop('TMUX',None)
    command=['tmux','-S',str(tmp_path/'test.sock')]
    try:
        subprocess.run(command+['-f','/dev/null','new-session','-d','-x','120','-y','40',
                        shlex.join([sys.executable,str(script)])+' 2> '+shlex.quote(str(tmp_path/'out'))],
                        env=env,check=True,capture_output=True,timeout=5)
        deadline=time.monotonic()+10
        while not result.exists() and time.monotonic()<deadline:
            time.sleep(.05)
        assert result.exists(), (tmp_path/'out').read_text()
        rows=json.loads(result.read_text())
        assert len({row['version'] for row in rows})==40
        assert max(row['pairs'] for row in rows)<120
        assert max(row['colors'] for row in rows)<=24
    finally:
        subprocess.run(command+['kill-server'],env=env,capture_output=True,timeout=5)


def test_crossing_the_image_poll_deadline_mid_frame_cannot_swap_pixels(tmp_path):
    """Reload only before palette/layout, never after an image poll deadline races draw."""
    path=tmp_path/'wall.png'
    Image.new('RGB',(32,32),'red').save(path)
    layer=configured('image',background_image=str(path))
    first=layer.frame(80,20)
    Image.new('RGB',(32,32),'blue').save(path)
    layer.stat_at=0
    assert layer.frame(80,20,now=time.monotonic()+2) is first
    assert layer.prepare({})
    assert layer.frame(80,20) != first
