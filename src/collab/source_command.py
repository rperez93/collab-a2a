"""Bounded local telemetry command, including cleanup of inherited-pipe children."""
from __future__ import annotations
import contextlib
import os
import selectors
import signal
import subprocess
import time


def run(command: str, *, timeout: float = 20, max_bytes: int = 1024 * 1024):
    if os.name != 'posix':
        raise OSError('telemetry commands require process-group isolation')
    proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    chunks = [bytearray(), bytearray()]
    total = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((proc.stdout, proc.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                for key, _ in selector.select(min(left, .1)):
                    block = os.read(key.fd, min(65536, max_bytes - total + 1))
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(block)
                    if total > max_bytes:
                        raise subprocess.SubprocessError('telemetry command output exceeds 1 MiB')
                    chunks[key.data].extend(block)
            code = proc.wait(timeout=max(.001, deadline-time.monotonic()))
            return subprocess.CompletedProcess(command, code, *(bytes(b).decode('utf-8', errors='replace') for b in chunks))
    finally:
        # Always remove descendants, even when the shell already exited.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()
