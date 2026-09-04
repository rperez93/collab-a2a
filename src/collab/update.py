"""Checking whether a newer collab has been released.

Two agents on different versions can disagree about the wire format, so the
moment to notice is when someone starts or joins a session — not at some random
later point. The check is cached, best-effort, and never blocks the thing you
actually asked for: no network, no GitHub, or no answer in time all mean "carry
on".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .config import global_config_path

RELEASES_API = "https://api.github.com/repos/rperez93/collab-a2a/releases/latest"
REPO_URL = "https://github.com/rperez93/collab-a2a"

#: The name on PyPI, which is not the import name.
PACKAGE = "collab-a2a"

#: Installations that upgrade themselves without being asked. Each replaces
#: only files it owns, and re-running the command is a no-op — so the question
#: «may I?» has no answer worth waiting for. A checkout is not on this list and
#: should not be: it is a working copy, and pulling into one is the user's
#: decision to make, not a side effect of starting a session.
_SELF_UPGRADING = frozenset({"pip", "pipx", "uv"})

#: Long enough that starting sessions all day costs one request.
CACHE_SECONDS = 6 * 3600
TIMEOUT = 4.0


def cache_path() -> Path:
    return global_config_path().parent / "update-check.json"


def _parse(version: str) -> tuple[int, ...]:
    cleaned = version.strip().lstrip("vV")
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts or [0])


def is_newer(candidate: str, current: str) -> bool:
    return _parse(candidate) > _parse(current)


@dataclass
class UpdateInfo:
    current: str
    latest: str = ""
    available: bool = False
    checked_at: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "current": self.current,
            "latest": self.latest,
            "available": self.available,
            "checked_at": self.checked_at,
            "error": self.error,
        }


def read_cache() -> UpdateInfo | None:
    p = cache_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    info = UpdateInfo(
        current=str(data.get("current") or __version__),
        latest=str(data.get("latest") or ""),
        available=bool(data.get("available")),
        checked_at=float(data.get("checked_at") or 0),
        error=str(data.get("error") or ""),
    )
    # A cached answer about an older build says nothing about this one.
    if info.current != __version__ and info.latest:
        info.available = is_newer(info.latest, __version__)
        info.current = __version__
    return info


def _write_cache(info: UpdateInfo) -> None:
    p = cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(info.to_dict()))
    except OSError:
        pass


def check(*, force: bool = False, timeout: float = TIMEOUT) -> UpdateInfo:
    """Return what we know about newer releases, consulting the cache first."""
    if os.environ.get("COLLAB_NO_UPDATE_CHECK") == "1":
        return UpdateInfo(current=__version__, error="disabled")

    cached = read_cache()
    if cached and not force and (time.time() - cached.checked_at) < CACHE_SECONDS:
        return cached

    info = UpdateInfo(current=__version__, checked_at=time.time())
    # IMPORTED HERE, ON THE ONE PATH THAT GOES TO GITHUB. cli.py imports this
    # module for every command, and httpx at the top of it was 80 ms of a
    # 180 ms `import collab.cli` — paid by `recv`, `send`, `status` and every
    # other command that never opens a connection. The cache and the version
    # arithmetic above need none of it.
    import httpx

    try:
        r = httpx.get(RELEASES_API, timeout=timeout,
                      headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 200:
            info.latest = str(r.json().get("tag_name") or "").lstrip("vV")
            info.available = bool(info.latest) and is_newer(info.latest, __version__)
        else:
            info.error = f"github returned {r.status_code}"
    except (httpx.HTTPError, ValueError) as exc:
        # Offline, rate-limited, behind a proxy — none of it is our problem.
        info.error = str(exc)[:120]
        if cached:
            info.latest, info.available = cached.latest, cached.available

    _write_cache(info)
    return info


def repo_dir() -> Path | None:
    """The checkout this collab runs from, if it is one we could update."""
    here = Path(__file__).resolve()
    candidate = here.parent.parent.parent  # src/collab/update.py -> repo root
    return candidate if (candidate / "install.sh").exists() else None


@dataclass(frozen=True)
class Install:
    """How this collab got here, and therefore how it is upgraded.

    There are two supported ways in and they update by different commands.
    Running the wrong one does not fail cleanly: `git pull` in a directory that
    is not a checkout reports nothing to pull, and `pip install --upgrade` from
    inside a clone upgrades the *installed* copy while the clone the user is
    editing stays where it was. Both leave somebody convinced they are on a
    version they are not, which is the failure this whole module exists to
    prevent — so the question is answered from the filesystem rather than
    assumed.
    """

    kind: str                       # checkout | pipx | uv | pip | unknown
    where: Path | None
    #: What a person should run. Also what `apply` runs, except for a checkout,
    #: which is two commands and is handled on its own path.
    command: list[str]

    @property
    def can_apply(self) -> bool:
        return self.kind != "unknown"

    def describe(self) -> str:
        return " ".join(self.command) if self.command else f"reinstall from {REPO_URL}"


def installed_as() -> Install:
    """Work out which of the two installations this is.

    A checkout is decided by `install.sh` sitting above the package, which is
    the same test `repo_dir` has always used. Everything else is a wheel in an
    environment, and the only question left is which tool owns that environment
    — pipx and uv put their environments in known places and have their own
    upgrade verbs, and using pip inside one of those would work once and then
    be undone by the tool the next time it touched it.
    """
    repo = repo_dir()
    if repo is not None:
        return Install("checkout", repo, ["git", "pull", "&&", "./install.sh"])

    prefix = Path(sys.prefix).resolve()
    parts = prefix.parts
    if "pipx" in parts:
        return Install("pipx", prefix, ["pipx", "upgrade", PACKAGE])
    # uv tool environments live under .../uv/tools/<name>; the directory named
    # `tools` next to `uv` is what distinguishes one from an ordinary venv uv
    # happens to have created, which pip upgrades perfectly well.
    if "uv" in parts and "tools" in parts:
        return Install("uv", prefix, ["uv", "tool", "upgrade", PACKAGE])
    if _is_installed():
        return Install("pip", prefix,
                       [sys.executable, "-m", "pip", "install", "--upgrade",
                        PACKAGE])
    return Install("unknown", None, [])


def _is_installed() -> bool:
    """Is there a distribution behind this import, or is it a loose tree?

    Running from a source tree that is neither a checkout nor an install —
    someone's `PYTHONPATH`, a vendored copy — has no upgrade command that could
    be right, and inventing one would overwrite something they arranged
    deliberately.
    """
    try:
        from importlib import metadata

        metadata.version(PACKAGE)
        return True
    except Exception:                                   # noqa: BLE001
        return False


def apply_update() -> tuple[bool, str]:
    """Upgrade by whichever route this collab arrived through."""
    how = installed_as()
    if how.kind == "checkout":
        return _update_checkout(how.where)              # type: ignore[arg-type]
    if not how.can_apply:
        return False, (
            "collab is not running from a checkout or an installed package, so"
            " it cannot update itself.\n"
            f"Install it with: pip install --upgrade {PACKAGE}\n"
            f"or from source: {REPO_URL}"
        )
    try:
        done = subprocess.run(how.command, capture_output=True, text=True,
                              timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{how.describe()}: {exc}"
    if done.returncode != 0:
        # PEP 668 lands here on a system Python, and its own message names the
        # flag or the virtual environment that would fix it. Passing it through
        # is better than paraphrasing a policy we do not set.
        return False, (done.stderr or done.stdout).strip()[-2000:]
    return True, (done.stdout or "").strip()[-2000:]


def _update_checkout(repo: Path) -> tuple[bool, str]:
    try:
        pull = subprocess.run(["git", "-C", str(repo), "pull", "--ff-only"],
                              capture_output=True, text=True, timeout=120)
        if pull.returncode != 0:
            return False, (pull.stderr or pull.stdout).strip()
        install = subprocess.run([str(repo / "install.sh")], cwd=str(repo),
                                 capture_output=True, text=True, timeout=600)
        if install.returncode != 0:
            return False, (install.stderr or install.stdout).strip()[-2000:]
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return True, (pull.stdout or "").strip()


def prompt_and_maybe_update(info: UpdateInfo, *, assume_yes: bool = False) -> bool:
    """Offer the update when a human is there to answer. Returns True if applied.

    A non-interactive caller — which is most agents — is told and left alone
    rather than having its session start blocked on a question nobody will see.
    """
    if not info.available:
        return False

    banner = (f"collab {info.latest} is available (you have {info.current})")
    how = installed_as()

    # A PACKAGE UPGRADES ITSELF; A CLONE IS ASKED FIRST.
    #
    # The two are not the same risk. `pip install --upgrade` replaces files
    # this install owns and nothing else, and re-running it changes nothing —
    # so stopping to ask buys the user only the chance to say no to something
    # they will have to do anyway. A checkout is somebody's working copy: it
    # may hold edits, a branch, a rebase halfway through, and `git pull` into
    # that is not ours to decide. So the clone keeps the behaviour it has
    # always had, and the wheel stops making people type what we could run.
    if how.kind in _SELF_UPGRADING:
        print(f"  {banner} — updating…")
        done, output = apply_update()
        if done:
            print(f"  updated to {info.latest} — re-run your command to use it")
            return True
        # Not fatal, and deliberately not a prompt either: the session the user
        # actually asked for is still perfectly able to run on this version.
        print(f"  update failed, carrying on: {output.splitlines()[-1][:200]}"
              if output else "  update failed, carrying on")
        return False

    if not assume_yes and not (sys.stdin.isatty() and sys.stdout.isatty()):
        # THE COMMAND THIS INSTALL ACTUALLY TAKES. This line used to name a
        # checkout unconditionally, so an agent that had installed from PyPI
        # was told to `cd` into a directory it does not have and pull a
        # repository it never cloned.
        where = f"cd {how.where} && " if how.kind == "checkout" else ""
        print(f"  {banner} — update with: {where}{how.describe()}")
        return False

    if not assume_yes:
        print(f"  {banner}")
        try:
            answer = input("  update now? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if answer not in ("y", "yes"):
            return False

    print("  updating…")
    ok, output = apply_update()
    if not ok:
        print(f"  update failed: {output}")
        return False
    print(f"  updated to {info.latest} — re-run your command to use it")
    return True
