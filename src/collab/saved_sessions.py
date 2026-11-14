"""Saved sessions in the selected participant namespace, never a peer's state."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import SessionProfile, collab_home, repo_for_home
from .server.session import HubConfig, hosted_sessions, session_summary


def working_directory(record: SessionProfile | HubConfig) -> Path:
    """Legacy records use their workspace binding, not the resumer's cwd."""
    raw = record.working_path
    path = Path(raw) if raw else repo_for_home(record.home)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError(f"saved working directory is unavailable: {path}; restore it before resuming")
    return path.resolve()


def saved_sessions(home: Path | None = None) -> list[dict[str, Any]]:
    """Public listing fields only: no invites, tokens, or remote requests."""
    base = Path(home or collab_home()).resolve()
    hosts = {cfg.session_id: cfg for cfg in hosted_sessions(base)}
    sessions = base / "sessions"
    if not sessions.is_dir():
        return []
    found = []
    for directory in sessions.iterdir():
        if not directory.is_dir():
            continue
        profile = SessionProfile.load_from(directory)
        cfg = hosts.get(directory.name)
        if profile is None and cfg is None:
            continue
        record = cfg or profile
        assert record is not None
        path = record.working_path or str(repo_for_home(base))
        row = {"session_id": directory.name, "title": cfg.title if cfg else "",
               "role": "host" if cfg or (profile and profile.is_host) else "guest",
               "name": profile.name if profile else cfg.host_name,
               "working_path": path, "home": str(base),
               "path_exists": Path(path).is_absolute() and Path(path).is_dir()}
        if cfg:
            row.update(session_summary(cfg))
        stamps = [p.stat().st_mtime for p in (directory / "hub.json", directory / "profile.json")
                  if p.is_file()]
        found.append((max(stamps, default=0), row))
    try:
        current = (base / "current").read_text().strip()
    except OSError:
        current = ""
    rows = [row for _, row in sorted(found, key=lambda item: item[0], reverse=True)]
    for index, row in enumerate(rows):
        row["current"] = row["session_id"] == current
        row["most_recent"] = index == 0
    return rows
