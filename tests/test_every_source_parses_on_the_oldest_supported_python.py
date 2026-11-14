"""Every source file must parse on the oldest Python collab declares support for.

The suite runs on a newer interpreter, so a construct only Python 3.12 accepts
(PEP 701 f-strings: the same quote nested inside a replacement field, or a
replacement field spanning lines) passes every test here and then makes
`collab` itself a SyntaxError on 3.10 — the whole CLI, not one command. That
shipped in cli.py's room-file message and was found only when a participant
ran the minimum-version integration tests by hand. `ast.parse(feature_version=)`
does not catch it (the tokenizer, not the grammar, changed), so this asks the
real interpreter.
"""
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _floor():
    text = (ROOT / "pyproject.toml").read_text()
    major, minor = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"', text).groups()
    return f"python{major}.{minor}"


FLOOR = _floor()


def _runnable(name):
    # A pyenv shim is on PATH whether or not that version is active, and then
    # answers «command not found»: on PATH is not the same as installed.
    if shutil.which(name) is None:
        return False
    try:
        return subprocess.run([name, "-c", "pass"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _runnable(FLOOR), reason=f"{FLOOR} is not runnable here")
def test_every_source_file_parses_on_the_declared_minimum_python():
    sources = sorted(str(p) for p in (ROOT / "src").rglob("*.py"))
    script = ("import ast, sys\n"
              "bad = []\n"
              "for path in sys.argv[1:]:\n"
              "    try:\n"
              "        ast.parse(open(path, encoding='utf-8').read(), path)\n"
              "    except SyntaxError as exc:\n"
              "        bad.append(f'{path}:{exc.lineno}: {exc.msg}')\n"
              "print('\\n'.join(bad))\n"
              "sys.exit(1 if bad else 0)\n")
    done = subprocess.run([FLOOR, "-I", "-c", script, *sources], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, f"does not parse on {FLOOR}:\n{done.stdout}{done.stderr}"
