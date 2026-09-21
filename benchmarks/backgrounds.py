"""Bounded decoration CPU/RAM: synthetic images and frames, isolated config only."""
import gc
import json
import os
from pathlib import Path
import resource
import tempfile
import time
import tracemalloc


def main():
    with tempfile.TemporaryDirectory(prefix='collab-background-budget-') as tmp:
        root=Path(tmp)
        os.environ.update(COLLAB_CONFIG=str(root/'config.json'), COLLAB_HOME=str(root/'home'),
                          COLLAB_STATE_DIR=str(root/'state'), COLLAB_PEERS_DIR=str(root/'peers'))
        from PIL import Image
        from collab import config
        from collab.client.background import Background, read_image
        from runtime import rss_kib
        path=root/'wall.png'
        Image.new('RGB',(1920,1080),(70,150,210)).save(path)
        before=rss_kib(); cpu=time.process_time(); wall=time.monotonic()
        read_image(path)
        decode={'wall_seconds':time.monotonic()-wall,'cpu_seconds':time.process_time()-cpu,
                'rss_before_kib':before,'rss_after_kib':rss_kib(),
                'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
        rows=[]
        for mode in ('matrix','image'):
            config.save_config({'watch_background':mode,'watch_background_image':str(path)})
            layer=Background(); layer.prepare({})
            # Warm caches and allocator before measuring retained growth.
            for n in range(100): layer.frame(120,30,now=layer.started+n)
            gc.collect(); tracemalloc.start(); before=tracemalloc.get_traced_memory()[0]
            cpu=time.process_time(); wall=time.monotonic()
            for n in range(2000):
                layer.frame(120,30,now=layer.started+100+n*.5)
            gc.collect()
            current,peak=tracemalloc.get_traced_memory(); tracemalloc.stop()
            rows.append({'mode':mode,'frames':2000,'wall_seconds':time.monotonic()-wall,
                         'cpu_seconds':time.process_time()-cpu,'retained_bytes':current-before,
                         'peak_traced_bytes':peak,'rss_kib':rss_kib()})
        fifo=root/'pipe.png'; os.mkfifo(fifo)
        wall=time.monotonic(); cpu=time.process_time()
        try: read_image(fifo)
        except ValueError: pass
        else: raise AssertionError('FIFO accepted')
        bad={'wall_seconds':time.monotonic()-wall,'cpu_seconds':time.process_time()-cpu}
        print(json.dumps({'decode_1080p':decode,'frames':rows,'fifo_rejection':bad,
                         'limits':'Synthetic generation precedes decode baseline; peak RSS includes fixture creation. Frame CPU includes tracemalloc, excludes curses; real-terminal measurements are separate.'},indent=2))

if __name__=='__main__': main()
