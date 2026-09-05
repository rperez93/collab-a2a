"""What one agent found out, kept where the next one will look for it.

A session is a conversation, and a conversation is the wrong shape for a fact.
«The staging bucket needs the eu-west key» is said once, at four in the
afternoon, to whoever happened to be reading — and by five it is a hundred
messages back, invisible to the agent that joins tomorrow and to the agent that
compacted its own context an hour ago. Every session in a repository ends up
rediscovering the same handful of things.

So a learning is written down, and four decisions shape where.

**Outside every repository.** The store is the AGENT's, not the checkout's: one
folder holding what it has learnt about every repository it has worked on,
grouped by repository. Writing into the checkout would put an agent's private
notes into somebody's diff and make the feature a thing to be reviewed; and an
agent that works on ten repositories wants one place to look, not ten.

**Grouped by a key that survives the machine.** Two agents on two laptops with
two different paths are working on ONE repository, and a learning from one is
worth having on the other. The key is the normalised `origin` remote, so
`git@host:a/b.git` and `https://host/a/b` land in the same group.

**A bundle rather than a file.** Each group is a Google Open Knowledge Format
v0.2 bundle, modelled on this repository's own `knowledge/` folder: an index, a
dated log, and one file per learning carrying frontmatter that says who
recorded it and when. That is the shape an agent is already taught to traverse
here, and one file per learning is what makes a slug, a counter and a search
index possible at all.

**A daemon can only ever publish the bundle of the repository its own session
is in.** Not the one a request names — a request cannot name one. The store
holds every repository this agent has touched, and most of them have nothing to
do with the people in this room.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .protocol import KIND_CHAT, scrub, scrub_block

#: The marker on a chat that carries a whole learning, and the one that asks
#: for other people's. Both ride the body: chat is the only kind a client may
#: send, so a new kind was never an option — and the body is where a receiver
#: that understands this looks, while the text is what a receiver that does not
#: still reads as a sentence.
MARKER = "learning"
SYNC_MARKER = "learning_sync"

#: What the text says, so a participant whose client knows nothing about any of
#: this sees what it is rather than a line of context-free prose.
PREFIX = "learning:"
SYNC_TEXT = "learning sync requested"

#: The files a bundle always has, named here because three functions build
#: paths out of them and a typo in one of the three is a second bundle.
INDEX = "index.md"
LOG = "log.md"
DB = ".index.db"

#: A slug is a file name in a folder this module writes to unattended, on the
#: strength of a string that arrived over the network. Lower-case letters,
#: digits and hyphens: nothing that can be `..`, nothing that can be absolute,
#: nothing a case-insensitive filesystem can fold onto a neighbour.
SLUG_OK = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SLUG = 60

#: A ceiling on a learning's body. It is read back into an agent's context and
#: it arrived from somebody else, so both halves of the reason apply: an
#: unbounded one is a tax on every future session, and an unbounded one from a
#: stranger is a disk somebody else can fill.
MAX_BODY = 16 * 1024
MAX_TITLE = 200
MAX_DESCRIPTION = 300
MAX_TAGS = 8
MAX_TAG = 30

#: How many learnings a sync answers with when nobody says.
DEFAULT_WANT = 20
MAX_WANT = 100

#: How often one daemon will answer the same asker. A sync is a burst of direct
#: messages, and an agent that asked twice by accident should not get two.
SYNC_COOLDOWN = 300.0

#: Where the CLI leaves work for the daemon, under the session directory.
SPOOL = "learn/pending"

#: Column weights for the search index: a word in the title is what somebody
#: was looking for, a word in the body might be an aside.
BM25_WEIGHTS = (10.0, 5.0, 5.0, 1.0)


# --- where the store is -------------------------------------------------------

def store_dir() -> Path | None:
    """The folder holding every repository's bundle, or None when it is off.

    Defaults BESIDE THE GLOBAL CONFIG rather than to a fixed path under the
    home directory, which gets both behaviours from one expression: the
    ordinary case is `~/.config/collab/learnings`, and a second profile — or a
    test — that points `COLLAB_CONFIG` somewhere else takes its learnings with
    it. Every other global path here already follows that folder; a store that
    did not would be the one piece of a profile that stayed behind.
    """
    from .config import global_config_path, learnings_dir

    configured = learnings_dir()
    if not configured:
        return None                        # the empty string is «off»
    return Path(configured).expanduser() if configured != "-" \
        else global_config_path().parent / "learnings"


def _git(*args: str, cwd: Path | None = None) -> str:
    try:
        done = subprocess.run(["git", *args], capture_output=True, text=True,
                              timeout=5, cwd=str(cwd) if cwd else None,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def normalise_remote(url: str) -> str:
    """A remote URL reduced to the thing that identifies the repository.

    The same repository is cloned over SSH by one person and over HTTPS by
    another, with a token in the URL by a third and a trailing `.git` by a
    fourth. All four are one repository, and a key that told them apart would
    give four agents four separate stores of the same knowledge — which is
    exactly the failure this feature exists to remove.

    So: the scheme goes, credentials go, the port goes, `.git` goes, the host
    is lower-cased (hostnames are case-insensitive and paths are not), and what
    is left is `host/owner/name`.
    """
    text = scrub(str(url or "")).strip()
    if not text:
        return ""
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text, flags=re.I)
    text = text.split("@", 1)[-1]          # credentials, or the scp-style user
    # `host:owner/name` is git's scp-like form; `host:22/owner/name` is a port.
    if ":" in text:
        host, _, rest = text.partition(":")
        rest = rest.lstrip("/")
        rest = re.sub(r"^\d+/", "", rest)  # a port, not the first path segment
        text = f"{host}/{rest}"
    text = text.rstrip("/")
    if text.lower().endswith(".git"):
        text = text[:-4]
    host, _, path = text.partition("/")
    if not path:
        return ""
    return f"{host.lower()}/{path}"


def repo_key(cwd: Path | None = None) -> str:
    """Which repository a session is in, named so another machine agrees.

    `local/<directory name>` with no remote, and the prefix is not decoration:
    it says the key is this machine's opinion. Two people with a directory
    called `api` and no remote are not working on the same repository, and a
    bare `api` would have claimed they were.
    """
    key = normalise_remote(_git("remote", "get-url", "origin", cwd=cwd))
    if key:
        return key
    root = _git("rev-parse", "--show-toplevel", cwd=cwd)
    where = Path(root) if root else Path(cwd or Path.cwd())
    return "local/" + (_safe_segment(where.name) or "repo")


def _safe_segment(text: str) -> str:
    """One path segment, with everything that is not a name taken out."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", scrub(str(text or ""))).strip("-.")
    return cleaned[:80]


def bundle_dir(key: str, root: Path | None = None) -> Path | None:
    """Where one repository's learnings live. None when the store is off.

    THE KEY IS RE-CLEANED HERE, not merely at the point it was derived. It
    arrives on the wire in a sync answer and out of a config file, and this is
    the function that turns it into a path — so it is the place a `../` has to
    die, rather than one of the several places it could have been caught.
    """
    where = root if root is not None else store_dir()
    if where is None:
        return None
    parts = [_safe_segment(p) for p in str(key).split("/") if p not in ("", ".", "..")]
    parts = [p for p in parts if p]
    if not parts:
        return None
    return Path(where).joinpath(*parts)


# --- one learning -------------------------------------------------------------

@dataclass
class Learning:
    """One thing somebody found out, as it sits on disk and on the wire."""

    slug: str
    title: str
    description: str = ""
    body: str = ""
    tags: list[str] = field(default_factory=list)
    by: str = ""
    at: str = ""
    repo: str = ""
    #: What THIS agent has done with it. `reads` is `collab learn read`;
    #: `uses` is an agent saying out loud that it helped. Kept apart because
    #: they measure different things — see `collab learn used`.
    uses: int = 0
    reads: int = 0
    #: And the best either counter has reached on somebody ELSE's machine, out
    #: of the sync messages. A learning a fresh agent has never opened has two
    #: zeroes of its own and still sorts by what the others found valuable.
    peer_uses: int = 0
    peer_reads: int = 0

    @property
    def score(self) -> tuple[int, int, str]:
        """«Most used», in one place so every listing agrees on the order."""
        return (self.uses + self.peer_uses, self.reads + self.peer_reads, self.at)


def slugify(title: str, taken: Iterable[str] = ()) -> str:
    """A file name from a title, and never a collision.

    A clash is resolved by counting rather than by overwriting: two people can
    learn two different things and call them the same thing, and the second one
    is not a correction of the first.
    """
    base = re.sub(r"[^a-z0-9]+", "-", scrub(str(title or "")).lower()).strip("-")
    base = base[:MAX_SLUG].strip("-") or "learning"
    held = set(taken)
    if base not in held:
        return base
    for n in range(2, 1000):
        candidate = f"{base[:MAX_SLUG - 4]}-{n}".strip("-")
        if candidate not in held:
            return candidate
    return f"{base[:MAX_SLUG - 8]}-{int(time.time())}"


def valid_slug(slug: str) -> bool:
    return bool(slug) and len(slug) <= MAX_SLUG and bool(SLUG_OK.match(slug))


# --- the frontmatter ----------------------------------------------------------
#
# Written and read by hand rather than with a YAML library, for the reason the
# knowledge bundle's own test gives: PyYAML is not a dependency of this project
# and adding one to read four shapes of mapping would be a poor trade. What is
# emitted is the subset every reader accepts, which is also the subset this
# parser can read back.

def _quote(text: str) -> str:
    """A scalar that cannot break the document it is written into."""
    flat = scrub(str(text or "")).replace('"', "'")
    return f'"{flat}"'


def to_markdown(learning: Learning) -> str:
    """One learning as its file: frontmatter, then the body."""
    tags = ", ".join(_quote(t) for t in learning.tags)
    return (
        "---\n"
        "type: Learning\n"
        f"title: {_quote(learning.title)}\n"
        f"description: {_quote(learning.description)}\n"
        f"tags: [{tags}]\n"
        "status: stable\n"
        f"generated: {{ by: {_quote(learning.by)}, at: {_quote(learning.at)} }}\n"
        "verified: []\n"
        "sources: []\n"
        f"repo: {_quote(learning.repo)}\n"
        f"uses: {int(learning.uses)}\n"
        f"reads: {int(learning.reads)}\n"
        f"peer_uses: {int(learning.peer_uses)}\n"
        f"peer_reads: {int(learning.peer_reads)}\n"
        "---\n\n"
        + learning.body.rstrip() + "\n")


_FRONT = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.S)


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def from_markdown(slug: str, text: str) -> Learning | None:
    """Read one back. None for anything that is not one of ours.

    Never raises. These files sit in a folder a person can edit and another
    machine can copy in, and a half-written one must cost that learning rather
    than the listing it appears in.
    """
    match = _FRONT.match(text or "")
    if not match:
        return None
    front: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line or line.startswith((" ", "\t", "#")):
            continue
        key, _, value = line.partition(":")
        front[key.strip()] = value.strip()
    if front.get("type") != "Learning":
        return None

    def number(name: str) -> int:
        try:
            return max(0, int(_unquote(front.get(name, "0")) or 0))
        except (TypeError, ValueError):
            return 0

    generated = front.get("generated", "")
    by = _unquote((re.search(r"by:\s*([^,}]+)", generated) or [None, ""])[1])
    at = _unquote((re.search(r"at:\s*([^,}]+)", generated) or [None, ""])[1])
    tags = [_unquote(t) for t in (front.get("tags", "").strip("[]").split(","))]
    return Learning(
        slug=slug,
        title=_unquote(front.get("title", "")) or slug,
        description=_unquote(front.get("description", "")),
        body=match.group(2).strip(),
        tags=[t for t in tags if t][:MAX_TAGS],
        by=by, at=at, repo=_unquote(front.get("repo", "")),
        uses=number("uses"), reads=number("reads"),
        peer_uses=number("peer_uses"), peer_reads=number("peer_reads"),
    )


# --- the bundle ---------------------------------------------------------------

def learning_path(bundle: Path, slug: str) -> Path | None:
    """The file for one slug, or None when the slug is not one.

    The single door onto a path in this folder, so that «a slug is a name and
    never a route» is enforced once. Checked twice on purpose: the pattern
    rejects the obvious, and resolving against the folder catches whatever a
    filesystem does with the rest — a symlink in the store, a case fold, a
    Windows device name.
    """
    if not valid_slug(slug):
        return None
    path = (Path(bundle) / f"{slug}.md")
    try:
        inside = path.resolve().parent == Path(bundle).resolve()
    except OSError:
        return None
    return path if inside else None


def load(bundle: Path, slug: str) -> Learning | None:
    path = learning_path(bundle, slug)
    if path is None:
        return None
    try:
        return from_markdown(slug, path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def every(bundle: Path) -> list[Learning]:
    """Everything in a bundle, most used first. Empty for a folder that is not one."""
    out: list[Learning] = []
    try:
        found = sorted(Path(bundle).glob("*.md"))
    except OSError:
        return out
    for path in found:
        if path.name in (INDEX, LOG):
            continue
        one = load(bundle, path.stem)
        if one is not None:
            out.append(one)
    out.sort(key=lambda x: x.score, reverse=True)
    return out


def slugs(bundle: Path) -> set[str]:
    try:
        return {p.stem for p in Path(bundle).glob("*.md")
                if p.name not in (INDEX, LOG)}
    except OSError:
        return set()


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".writing.{os.getpid()}.{int(time.time() * 1000)}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def save(bundle: Path, learning: Learning) -> Path | None:
    """Write one learning, then the index that lists it. Returns the file."""
    path = learning_path(bundle, learning.slug)
    if path is None:
        return None
    _write_atomically(path, to_markdown(learning))
    rewrite_index(bundle)
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


def rewrite_index(bundle: Path) -> None:
    """The bundle's index, regenerated from what is actually in the folder.

    Regenerated rather than appended to, which is the opposite of the log below
    and right for the opposite reason: an index is a statement about the
    present, so a stale line in it is a link to a file somebody deleted, while
    a log is a statement about the past and may not be rewritten at all.
    """
    items = every(bundle)
    lines = ["---", 'okf_version: "0.2"', "---", "",
             "# Learnings", ""]
    if items:
        lines += [f"* [{scrub(one.title)}]({one.slug}.md) - "
                  f"{scrub(one.description) or 'no description'}" for one in items]
    else:
        lines.append("* Nothing recorded yet.")
    lines += ["", "# Reserved", "",
              "* [Update log](log.md) - What changed here, newest first.", ""]
    with contextlib.suppress(OSError):
        _write_atomically(Path(bundle) / INDEX, "\n".join(lines))


def append_log(bundle: Path, entry: str) -> None:
    """One dated line in the bundle's log, newest first.

    Newest first because that is what the format says and what a reader wants,
    and it costs a read of the file: an append would be cheaper and would put
    the newest at the bottom, which is where nobody looks.
    """
    day = time.strftime("%Y-%m-%d")
    line = f"* {scrub(entry)}"
    path = Path(bundle) / LOG
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        existing = ""
    if not existing.strip():
        existing = "# Bundle Update Log\n"
    head, _, tail = existing.partition("\n")
    if f"\n## {day}\n" in existing:
        body = existing.replace(f"## {day}\n", f"## {day}\n{line}\n", 1)
    else:
        body = f"{head}\n\n## {day}\n{line}\n{tail.lstrip()}"
    with contextlib.suppress(OSError):
        _write_atomically(path, body)


def bump(bundle: Path, slug: str, field_name: str, by: int = 1) -> Learning | None:
    """Add to one counter and write the file back. Returns what it now says.

    ONE FILE AND ONE ROW, which is why this does not go through `save`. That
    regenerates the bundle's index, and regenerating the index reads every
    learning in the folder — so a `used` on a store of five hundred would open
    five hundred files to record a single number. The index carries a title, a
    slug and a description and no count at all, so a bump cannot change a line
    of it; the only derived thing that moves is the row in the search index,
    which `index_one` replaces on its own.
    """
    one = load(bundle, slug)
    if one is None or field_name not in ("uses", "reads", "peer_uses", "peer_reads"):
        return None
    setattr(one, field_name, max(0, getattr(one, field_name) + by))
    path = learning_path(bundle, one.slug)
    if path is None:
        return None
    _write_atomically(path, to_markdown(one))
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    index_one(bundle, one)
    return one


# --- what travels on the wire -------------------------------------------------

def to_wire(one: Learning) -> dict[str, Any]:
    """A learning as a chat body carries it, with this agent's counts on it.

    The counts go out so a receiver can order an index it has never read. They
    arrive as somebody else's opinion and are stored as `peer_*` rather than as
    the receiver's own, because a count is a record of what THIS agent did and
    a copied one would be a claim about work it never performed.
    """
    return {"slug": one.slug, "title": one.title, "description": one.description,
            "body": one.body, "tags": list(one.tags),
            "generated": {"by": one.by, "at": one.at},
            "repo": one.repo, "uses": one.uses, "reads": one.reads}


def from_wire(payload: Any, repo: str) -> Learning | None:
    """A learning out of a message, scrubbed and bounded. None when it is not one.

    `repo` is the RECEIVER's key and is not read from the payload. A sender can
    say anything about which repository a learning belongs to, and believing it
    would let one participant file knowledge under a repository nobody in the
    room is working on — which is both a lie in somebody's store and a way to
    write outside the folder the receiver expected.
    """
    if not isinstance(payload, dict):
        return None
    slug = scrub(str(payload.get("slug") or "")).strip().lower()
    if not valid_slug(slug):
        return None
    title = scrub(str(payload.get("title") or ""))[:MAX_TITLE].strip()
    if not title:
        return None
    tags = payload.get("tags")
    clean_tags = ([scrub(str(t))[:MAX_TAG].strip() for t in tags[:MAX_TAGS]]
                  if isinstance(tags, list) else [])
    generated = payload.get("generated")
    generated = generated if isinstance(generated, dict) else {}

    def counted(name: str) -> int:
        raw = payload.get(name)
        if isinstance(raw, bool) or not isinstance(raw, int):
            return 0
        return max(0, min(raw, 1_000_000))

    return Learning(
        slug=slug, title=title,
        description=scrub(str(payload.get("description") or ""))[:MAX_DESCRIPTION],
        body=scrub_block(str(payload.get("body") or ""))[:MAX_BODY],
        tags=[t for t in clean_tags if t],
        by=scrub(str(generated.get("by") or ""))[:MAX_TITLE],
        at=scrub(str(generated.get("at") or ""))[:40],
        repo=repo,
        peer_uses=counted("uses"), peer_reads=counted("reads"),
    )


def is_learning(env: Any) -> bool:
    """Is this event one? Asked of the BODY, never of the text.

    Anybody can type a message beginning «learning:», and a message that merely
    looks like one must not be filed as a fact about a repository.
    """
    if getattr(env, "kind", "") != KIND_CHAT:
        return False
    body = getattr(env, "body", None)
    return isinstance(body, dict) and isinstance(body.get(MARKER), dict)


def is_sync_request(env: Any) -> bool:
    if getattr(env, "kind", "") != KIND_CHAT:
        return False
    body = getattr(env, "body", None)
    return isinstance(body, dict) and isinstance(body.get(SYNC_MARKER), dict)


def wanted(env: Any) -> int:
    """How many a sync request asked for, bounded. The repo it names is IGNORED.

    Deliberately not read: the responder answers out of the bundle for its own
    session's repository and nothing else, so a `repo` in the request is a
    field with no reader — which is the only way to be sure it can never become
    one by accident.
    """
    body = getattr(env, "body", None) or {}
    ask = body.get(SYNC_MARKER) if isinstance(body, dict) else None
    raw = (ask or {}).get("want") if isinstance(ask, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_WANT
    return max(1, min(raw, MAX_WANT))


def receive(bundle: Path, one: Learning) -> str:
    """File a learning that arrived. Returns what happened, for the log.

    Three outcomes, and the middle one is the interesting one. A slug nobody
    holds is written. A slug held by an identical body is left alone, with the
    peer counts raised to the best seen — the same learning going round a room
    should not multiply. A slug held by a DIFFERENT body is kept beside it as
    `<slug>-2`, because two people can learn two different things and give them
    the same name, and overwriting would lose one of them silently.
    """
    held = load(bundle, one.slug)
    if held is None:
        save(bundle, one)
        index_one(bundle, one)
        return "added"
    if held.body.strip() == one.body.strip() and held.title == one.title:
        changed = False
        for name in ("peer_uses", "peer_reads"):
            best = max(getattr(held, name), getattr(one, name))
            if best != getattr(held, name):
                setattr(held, name, best)
                changed = True
        if changed:
            save(bundle, held)
            index_one(bundle, held)
        return "known"
    one.slug = slugify(one.slug, slugs(bundle))
    save(bundle, one)
    index_one(bundle, one)
    return "forked"


# --- finding one --------------------------------------------------------------
#
# THE MARKDOWN IS THE SOURCE OF TRUTH AND THE INDEX IS DERIVED. `.index.db` can
# be deleted at any moment and the only cost is one rebuild; nothing is stored
# there that is not in a file beside it. That is what makes it safe to have at
# all — a store somebody copies between machines, edits by hand, or restores
# from a backup arrives with an index describing a bundle that no longer
# exists, and the answer has to be «rebuild», not «be wrong».
#
# It is kept current two ways, because either alone leaves a hole. Every writer
# in this module updates it in the same operation, which keeps the ordinary
# path free. And every reader checks it against a STAMP of the folder — the
# sorted (name, mtime, size) of every learning in it, the technique
# `config.load_config` and `themes` already use — and rebuilds on a mismatch,
# which is what catches the writer that was not this process.

@dataclass(frozen=True)
class Hit:
    """One search result: the learning, and the line that matched."""

    learning: Learning
    where: str = ""                        # title | description | tags | body
    line: str = ""


def index_path(bundle: Path) -> Path:
    return Path(bundle) / DB


def stamp(bundle: Path) -> str:
    """What the folder looks like from outside, without opening anything.

    Name, modification time and size for every learning, and no reads. This is
    the whole of what a search on an unchanged bundle costs, which is the
    point: the alternative is parsing every file to find out whether parsing
    every file was necessary.

    `scandir` and a digest, not `glob` and a list. Both matter at the size
    where this matters at all. `glob` plus `path.stat()` is two trips into the
    kernel per file where `scandir` is one directory read and one stat, and
    building the answer as sixty thousand tuples inside a JSON string costs
    more than the stat calls do — measured over ten thousand learnings, the two
    together were most of a search. A digest compares exactly as well: the
    question asked of it is «is this the same folder», never «what changed».
    """
    import hashlib

    digest = hashlib.blake2b(digest_size=16)
    try:
        rows = []
        with os.scandir(bundle) as entries:
            for entry in entries:
                if not entry.name.endswith(".md") or entry.name in (INDEX, LOG):
                    continue
                info = entry.stat()
                rows.append(f"{entry.name}\0{info.st_mtime_ns}\0{info.st_size}")
    except OSError:
        return ""
    # SORTED, because a directory hands entries back in whatever order it
    # pleases and that order is not stable across a rename. An unsorted digest
    # would rebuild the index at random.
    for row in sorted(rows):
        digest.update(row.encode("utf-8", "replace"))
        digest.update(b"\n")
    return f"{len(rows)}:{digest.hexdigest()}"


def _connect(bundle: Path):
    import sqlite3

    path = index_path(bundle)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _has_fts5(conn) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe "
                     "USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        return True
    except Exception:                                         # noqa: BLE001
        return False


_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS learnings USING fts5(
    slug UNINDEXED, title, description, tags, body,
    tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS counts(
    slug TEXT PRIMARY KEY, uses INTEGER, reads INTEGER,
    peer_uses INTEGER, peer_reads INTEGER, generated_at TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def _rows_for(one: Learning) -> tuple:
    return (one.slug, one.title, one.description, " ".join(one.tags), one.body)


def rebuild_index(bundle: Path) -> str:
    """Throw the index away and build it from the files. Returns the engine."""
    with contextlib.suppress(OSError):
        index_path(bundle).unlink()
    try:
        conn = _connect(bundle)
    except Exception:                                         # noqa: BLE001
        return "scan"
    try:
        if not _has_fts5(conn):
            conn.close()
            with contextlib.suppress(OSError):
                index_path(bundle).unlink()
            return "scan"
        conn.executescript(_SCHEMA)
        for one in every(bundle):
            conn.execute("INSERT INTO learnings VALUES (?,?,?,?,?)", _rows_for(one))
            conn.execute("INSERT OR REPLACE INTO counts VALUES (?,?,?,?,?,?)",
                         (one.slug, one.uses, one.reads, one.peer_uses,
                          one.peer_reads, one.at))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('stamp', ?)",
                     (stamp(bundle),))
        conn.commit()
    except Exception:                                         # noqa: BLE001
        with contextlib.suppress(Exception):
            conn.close()
        with contextlib.suppress(OSError):
            index_path(bundle).unlink()
        return "scan"
    conn.close()
    return "fts5"


def index_one(bundle: Path, one: Learning) -> None:
    """Keep the index level with a file that has just been written.

    Never raises, and a failure is not a fault: the index is derived, so the
    worst case is the next reader finding the stamp out of date and rebuilding.
    """
    try:
        if not index_path(bundle).exists():
            return                          # nothing to keep level yet
        conn = _connect(bundle)
    except Exception:                                         # noqa: BLE001
        return
    try:
        conn.execute("DELETE FROM learnings WHERE slug = ?", (one.slug,))
        conn.execute("INSERT INTO learnings VALUES (?,?,?,?,?)", _rows_for(one))
        conn.execute("INSERT OR REPLACE INTO counts VALUES (?,?,?,?,?,?)",
                     (one.slug, one.uses, one.reads, one.peer_uses,
                      one.peer_reads, one.at))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('stamp', ?)",
                     (stamp(bundle),))
        conn.commit()
    except Exception:                                         # noqa: BLE001
        pass
    with contextlib.suppress(Exception):
        conn.close()


def _current_index(bundle: Path):
    """An open index that matches the folder, or None to fall back to a scan."""
    path = index_path(bundle)
    try:
        if not path.exists():
            if rebuild_index(bundle) != "fts5":
                return None
        conn = _connect(bundle)
    except Exception:                                         # noqa: BLE001
        return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'stamp'").fetchone()
        if row is None or row["value"] != stamp(bundle):
            conn.close()
            if rebuild_index(bundle) != "fts5":
                return None
            conn = _connect(bundle)
        return conn
    except Exception:                                         # noqa: BLE001
        with contextlib.suppress(Exception):
            conn.close()
        return None


def _match_query(words: Sequence[str]) -> str:
    """The words, AND-ed, with the last one open at the end.

    QUOTED BEFORE IT REACHES `MATCH`. Everything here is text somebody typed,
    and FTS5's query language has operators in it — a bare `AND`, a `*`, a
    `"`, a `:` — so an unquoted word is at best a syntax error thrown at
    somebody searching for `NOT` and at worst a query that quietly means
    something wider than they asked.
    """
    quoted = []
    for word in words:
        clean = scrub(str(word)).strip()
        if not clean:
            continue
        quoted.append('"' + clean.replace('"', '""') + '"')
    if not quoted:
        return ""
    quoted[-1] += "*"                       # a half-typed last word still hits
    return " AND ".join(quoted)


def search(bundle: Path, words: Sequence[str] = (), *, tag: str = "",
           limit: int = 20) -> tuple[list[Hit], str]:
    """Find learnings, best first. Returns the hits and which engine answered.

    Ranked by WHERE the words matched before anything else — a word in the
    title is what somebody was looking for and a word in the body might be an
    aside — and only then by what the counts say. Ordering by the counts first
    would answer «the most used learning that happens to mention this», which
    is a different question.
    """
    conn = _current_index(bundle)
    if conn is None:
        return _scan(bundle, words, tag=tag, limit=limit), "scan"
    try:
        return _search_fts(conn, bundle, words, tag=tag, limit=limit), "fts5"
    except Exception:                                         # noqa: BLE001
        return _scan(bundle, words, tag=tag, limit=limit), "scan"
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _search_fts(conn, bundle: Path, words: Sequence[str], *, tag: str,
                limit: int) -> list[Hit]:
    query = _match_query(words)
    weights = ", ".join(str(w) for w in (0.0, *BM25_WEIGHTS))
    want = scrub(str(tag)).strip().lower()
    # RANKED, FILTERED AND CUT BEFORE ANYTHING IS OPENED. The whole reason for
    # an index is that a search does not read the bundle; loading every row to
    # find out which twenty to keep would have read it anyway, and the cost
    # test counts exactly that. `bm25` puts the column weights to work, so a
    # word in a title outranks a word in a body without a second pass, and the
    # counts settle what relevance leaves level.
    order = ("ORDER BY rank, c.uses + c.peer_uses DESC,"
             " c.reads + c.peer_reads DESC, c.generated_at DESC")
    if query:
        sql = (f"SELECT l.slug AS slug, l.tags AS tags,"
               f" bm25(learnings, {weights}) AS rank"
               " FROM learnings l LEFT JOIN counts c ON c.slug = l.slug"
               f" WHERE learnings MATCH ? {order}")
        rows = conn.execute(sql, (query,)).fetchall()
    else:
        sql = ("SELECT l.slug AS slug, l.tags AS tags, 0.0 AS rank"
               " FROM learnings l LEFT JOIN counts c ON c.slug = l.slug"
               + order)
        rows = conn.execute(sql).fetchall()

    wanted = [scrub(str(w)).strip().lower() for w in words if str(w).strip()]
    hits: list[Hit] = []
    for row in rows:
        if want and want not in str(row["tags"] or "").lower().split():
            continue
        one = load(bundle, row["slug"])
        if one is None:
            continue
        hits.append(Hit(one, _where(one, wanted),
                        _matching_line(one.body, wanted)))
        if limit and len(hits) >= limit:
            break
    return hits


#: How much a match is worth by where it landed. Lower sorts first.
_PLACES = {"title": 0, "description": 1, "tags": 1, "body": 2, "": 3}


def _place(where: str) -> int:
    return _PLACES.get(where, 3)


def _where(one: Learning, words: Sequence[str]) -> str:
    """The most important field any of the words appears in."""
    wanted = [scrub(str(w)).strip().lower() for w in words if str(w).strip()]
    if not wanted:
        return ""
    haystacks = (("title", one.title), ("description", one.description),
                 ("tags", " ".join(one.tags)), ("body", one.body))
    for name, text in haystacks:
        low = text.lower()
        if any(w in low for w in wanted):
            return name
    return ""


def _scan(bundle: Path, words: Sequence[str], *, tag: str,
          limit: int) -> list[Hit]:
    """The same answer without an index, for a python built without FTS5.

    Deliberately the same ORDER rather than the same algorithm: bm25 cannot be
    reproduced by reading files, and pretending to would be worse than saying
    which engine answered. What is preserved is the part a reader depends on —
    a title match above a body match, then the counts — so the same search
    returns the same set in nearly the same order on either.
    """
    wanted = [scrub(str(w)).strip().lower() for w in words if str(w).strip()]
    want_tag = scrub(str(tag)).strip().lower()
    hits: list[Hit] = []
    for one in every(bundle):
        if want_tag and want_tag not in [t.lower() for t in one.tags]:
            continue
        where = _where(one, wanted)
        if wanted and not where:
            continue
        hits.append(Hit(one, where, _matching_line(one.body, wanted)))
    hits.sort(key=lambda h: (_place(h.where), *(-n for n in h.learning.score[:2])))
    return hits[:limit] if limit else hits


def _matching_line(body: str, words: Sequence[str]) -> str:
    for line in (body or "").splitlines():
        low = line.lower()
        if any(w in low for w in words):
            return line.strip()[:200]
    return ""


# --- the spool: the agent never waits for any of this -------------------------
#
# No `collab learn` command may make an agent wait on a file write, an index
# update or a publish. A turn is the scarcest thing in this system, and the one
# thing a knowledge feature must not do is cost one every time it is used.
#
# So the command writes one small file and returns, and the daemon — which is
# already running, already outlives the turn, and already has somewhere to put
# a slow operation — does the work. The exception is `read`, which prints from
# the file synchronously because printing it IS the command; only its counter
# is spooled.

def spool_dir(session_dir: Path | str) -> Path:
    return Path(session_dir) / SPOOL


def spool(session_dir: Path | str, op: str, **fields: Any) -> Path | None:
    """Leave one operation for the daemon. Returns the file, or None.

    Written to a temporary name and renamed, so the daemon draining this
    folder can never read a half-written request — it runs every three seconds
    and this runs whenever somebody types, which is exactly the race that shape
    of write exists for.
    """
    where = spool_dir(session_dir)
    stamp = f"{int(time.time() * 1000)}-{os.getpid()}-{op}"
    try:
        where.mkdir(parents=True, exist_ok=True)
        path = where / f"{stamp}.json"
        tmp = where / f".{stamp}.writing"
        tmp.write_text(json.dumps({"op": op, "at": time.time(), **fields}),
                       encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        return None
    return path


def pending(session_dir: Path | str) -> list[Path]:
    """Everything waiting, oldest first — the order it was asked for."""
    try:
        return sorted(p for p in spool_dir(session_dir).glob("*.json"))
    except OSError:
        return []


def read_spooled(path: Path) -> dict[str, Any] | None:
    try:
        found = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None
