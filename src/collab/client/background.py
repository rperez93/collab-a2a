"""Bounded decoration for the participant panel, beneath opaque text spans.

No browser, SVG interpreter, graphics protocol or animation thread is needed.
Static image pixels are decoded once per file change and sampled once per size.
Matrix frames share the viewer's existing 4Hz loop; there is no additional timer.
"""
from __future__ import annotations
import io
import os
from pathlib import Path
import stat
import time

# A 1080p photograph fits; huge wallpaper exports must be resized explicitly.
# Bounds cover compressed bytes, decoded pixels, and terminal work separately.
MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 4_000_000
MAX_CELLS = 16_000
# ASCII keeps the rain readable on terminals/fonts without CJK glyphs;
# half-width katakana became missing-glyph boxes in the real capture host.
GLYPHS = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ:;*+'


def read_image(path):
    from PIL import Image
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError('use a regular PNG/JPEG file of at most 8 MiB')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError('image exceeds 8 MiB')
    finally:
        os.close(fd)
    try:
        opened = Image.open(io.BytesIO(data), formats=('PNG', 'JPEG'))
    except Image.DecompressionBombError as exc:
        raise ValueError('resize image to at most 4 million pixels') from exc
    with opened as image:
        if image.width * image.height > MAX_PIXELS:
            raise ValueError('resize image to at most 4 million pixels')
        # Animated PNG deliberately uses frame zero. Only the built-in Matrix
        # effect has an animation path, so arbitrary files cannot arm timers.
        image.thumbnail((320, 320))
        rgba = image.convert('RGBA')
        flat = Image.new('RGBA', rgba.size, (0,0,0,255))
        flat.alpha_composite(rgba)
        # Quantize the source, not each resized frame. The same eight colours
        # then survive arbitrary resizes without allocating new terminal pairs.
        return flat.convert('RGB').quantize(colors=8)


class Background:
    def __init__(self):
        self.key = None
        self.image = None
        self.path_key = None
        self.stat_at = 0.0
        self.frame_key = None
        self.cells = ()
        self.error = ''
        self.started = time.monotonic()

    def prepare(self, theme, *, supported=True):
        from ..runtime_settings import get
        mode = get('watch_background')
        if mode == 'theme':
            mode = theme.get('panel_background', 'none')
        if not supported:
            mode = 'none'
        key = (mode, get('watch_background_image'), get('watch_background_dim'),
               get('watch_background_fps'), get('watch_reduced_motion'))
        changed = key != self.key
        if changed:
            self.key = key
            self.cells = ()
            self.frame_key = None
            self.error = ''
            self.stat_at = 0.0
            if mode != 'image' or (self.path_key and self.path_key[0] != key[1]):
                self.image, self.path_key = None, None
        if mode == 'image' and key[2] < 100:
            changed = self.refresh_image(time.monotonic()) or changed
        return changed

    def refresh_image(self, now):
        if now < self.stat_at:
            return False
        self.stat_at = now + 1
        path = self.key[1]
        try:
            info = Path(path).stat()
            stamp = (path, info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = (path, None)
        if stamp == self.path_key:
            return False
        self.path_key, self.image = stamp, None
        self.frame_key = None
        try:
            self.image = read_image(path)
            self.error = ''
        except (OSError, ValueError, ImportError) as exc:
            self.error = 'background: ' + (str(exc) if isinstance(exc, ValueError)
                                           else 'cannot read PNG/JPEG image')
        return True

    def frame(self, width, height, *, now=None):
        mode, path, dim, fps, reduced = self.key
        if mode == 'none' or dim == 100 or width <= 0 or height <= 0:
            return ()
        now = time.monotonic() if now is None else now
        width = min(width, 240)
        height = min(height, MAX_CELLS // width)
        # Image I/O belongs exclusively to prepare(), before the palette and
        # text caches are rebuilt. A deadline crossing mid-draw must not swap
        # pixels without announcing the colour generation change.
        tick = 0 if reduced else max(0, int((now-self.started)*fps))
        cache = (width, height, dim, tick if mode == 'matrix' else self.path_key)
        if self.frame_key == cache:
            return self.cells
        cells = []
        brightness = (100-dim)/100
        if mode == 'matrix':
            # Sparse columns with different offsets/speeds, no random state or
            # growing trail buffers. Only visible tails exist in each frame.
            for x in range(1, width, 4):
                head = (tick // (1 + x % 3) + x*17) % (height + 12)
                for tail in range(8):
                    y = head-tail
                    if 0 <= y < height:
                        strength = brightness * (1-tail/10)
                        color = '#%02x%02x%02x' % (int(80*strength), int(255*strength), int(125*strength))
                        glyph = GLYPHS[(x*13+y*7+tick//3) % len(GLYPHS)]
                        cells.append((x,y,glyph,color))
        elif self.image is not None:
            from PIL import Image, ImageOps
            # Characters are roughly twice as tall as wide. Fit at that aspect
            # before reducing to terminal cells, preserving the subject's shape.
            fitted = ImageOps.fit(self.image, (width,height*2), method=Image.Resampling.NEAREST)
            indexed = fitted.resize((width,height), Image.Resampling.NEAREST)
            pixels, palette = list(indexed.tobytes()), indexed.getpalette()
            colors = {}
            for index in set(pixels):
                rgb = palette[index*3:index*3+3]
                colors[index] = '#%02x%02x%02x' % tuple(int(c*brightness) for c in rgb)
            cells = [(i%width,i//width,'█',colors[pixel]) for i,pixel in enumerate(pixels)]
        self.frame_key, self.cells = cache, tuple(cells)
        return self.cells

    def paint(self, win, top, height, width, pair_for):
        import curses
        if not getattr(curses, 'COLORS', 0):
            return
        attrs = {}
        for x,y,glyph,color in self.frame(width,height):
            if color not in attrs:
                attrs[color] = curses.color_pair(pair_for(color))
            try:
                win.addstr(top+y, x, glyph, attrs[color])
            except curses.error:
                pass
        if self.error:
            try:
                win.addnstr(top, 0, self.error, max(0,width-1), curses.A_DIM)
            except curses.error:
                pass
