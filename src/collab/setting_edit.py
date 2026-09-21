"""Terminal-editor drafts shared by the CLI and the settings panel.

The editor never receives the config file: it gets one private disposable
draft. Its exit status, bounded output and the setting's validator must all
succeed before either interface can save anything.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
from typing import Any

from . import config

# Match the panel's paste budget, with four bytes per Unicode character. Read
# only one byte beyond this cap so a broken editor cannot exhaust memory.
MAX_DRAFT = 32_000
MAX_BYTES = MAX_DRAFT * 4


def shown(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(part) for part in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def editor_command() -> list[str]:
    preferred = config.setting("editor").read()
    command = preferred or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    argv = shlex.split(command)
    if not argv:
        raise ValueError("Set a terminal editor with collab config editor nano")
    return argv


def editor_draft(item: config.Setting, text: str) -> str:
    # vim, nvim and nano normally terminate a saved line. Model identifiers
    # reject that newline and paths can silently include it; prose must keep
    # it. Remove only the final line ending for single-line settings, leaving
    # embedded newlines for the setting's usual validation to decide about.
    if not item.multiline:
        if text.endswith("\r\n"):
            return text[:-2]
        if text.endswith("\n"):
            return text[:-1]
    return text


def edit_text(text: str) -> str:
    argv = editor_command()
    with tempfile.TemporaryDirectory(prefix="collab-setting-") as directory:
        path = Path(directory) / "value.txt"
        path.touch(mode=0o600)
        path.write_text(text, encoding="utf-8")
        # Inherit the terminal, not pipes: editors need interactive input and
        # can take as long as the person needs. No shell interprets the draft
        # or command arguments, and no output is accumulated while waiting.
        result = subprocess.run([*argv, str(path)], check=False)
        if result.returncode:
            raise ValueError(f"Editor exited with status {result.returncode}; setting unchanged")
        # An editor may replace its file. Refuse symlinks/devices/FIFOs and
        # open nonblocking before fstat, so a FIFO cannot hang this consumer.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("The editor draft must be a regular file")
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError(f"Draft limit: {MAX_DRAFT:,} characters")
        try:
            draft = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("The editor draft must be UTF-8 text") from exc
        if len(draft) > MAX_DRAFT:
            raise ValueError(f"Draft limit: {MAX_DRAFT:,} characters")
        return draft
