"""Bounded local telemetry command, including cleanup of inherited-pipe children."""
from __future__ import annotations
import asyncio
import threading
import contextlib
import os
import selectors
import signal
import subprocess
import time


def run(command: str, *, timeout: float = 20, max_bytes: int = 1024 * 1024, cancel=None, env=None):
    if os.name != 'posix':
        raise OSError('telemetry commands require process-group isolation')
    proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True, env=env)
    chunks = [bytearray(), bytearray()]
    total = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((proc.stdout, proc.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            while selector.get_map():
                if cancel is not None and cancel.is_set():
                    raise subprocess.SubprocessError('telemetry command cancelled')
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
            # A child can close both pipes and keep running. EOF must not
            # replace the cancellation-aware selector with a 20-second wait.
            while proc.poll() is None:
                if cancel is not None and cancel.is_set():
                    raise subprocess.SubprocessError('telemetry command cancelled')
                left = deadline - time.monotonic()
                if left <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                if cancel is not None:
                    cancel.wait(min(.1, left))
                else:
                    time.sleep(min(.1, left))
            code = proc.returncode
            return subprocess.CompletedProcess(command, code, *(bytes(b).decode('utf-8', errors='replace') for b in chunks))
    finally:
        # Always remove descendants, even when the shell already exited.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()


async def run_async(command: str, *, timeout: float = 20, env=None):
    """Cancel the owned process group too, not merely its to_thread waiter.

    The selector wakes at least every 100 ms. Keeping the thread shielded
    until its finally completes prevents a daemon restart leaving a usage
    command and descendants running for the remaining 20-second deadline.
    """
    cancel = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(run, command, timeout=timeout,
                                               cancel=cancel, env=env))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel.set()
        with contextlib.suppress(Exception):
            await task
        raise
