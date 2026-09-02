"""The knowledge bundle must stay conformant, and its links must stay resolved.

`knowledge/` is written to be read by an agent that will not open the source to
check it, which is a different risk from `docs/`: a wrong sentence there is
read by a person who can tell. So the properties that make the bundle
trustworthy are asserted rather than maintained by hand.

The OKF spec tolerates a broken cross-link as not-yet-written knowledge. This
bundle does not: every link in it points at something that already exists, and
the point of testing it is that a file renamed six months from now breaks a
test instead of quietly becoming a dead end.

The frontmatter parser here is deliberately small. PyYAML is not a dependency
of this project and adding one to read three shapes of mapping would be a poor
trade, so the test enforces the subset it can read — which keeps the bundle
parseable by anything, rather than only by a full YAML engine.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "knowledge"

RESERVED = {"index.md", "log.md"}

#: §5.4. Absent means `stable`, so the key is optional rather than defaulted.
STATUSES = {"draft", "stable", "deprecated"}

#: §7. `human:` is the prefix a consumer keys trust off, so a bundle that
#: claims one has claimed a person read it.
ACTOR = re.compile(r"^(human:[\w.@-]+|process:[\w.-]+|[\w.-]+/[\w.\[\]-]+)$")

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
FOOTNOTE_REF = re.compile(r"(?<!\])\[\^([^\]]+)\]")
FOOTNOTE_DEF = re.compile(r"^\[\^([^\]]+)\]:", re.MULTILINE)

#: Evidence into this repository, pinned to the revision a claim was checked
#: against. A branch name in place of the sha would be the unpinned reference
#: the bundle exists to argue against, so the sha is matched in full.
REPO_BLOB = re.compile(
    r"^https://github\.com/rperez93/collab-a2a/blob/([0-9a-f]{40})/(\S+)$")

#: Anything else reaching into this repository. `blob/main/...` resolves today
#: and says nothing about which tree a claim was checked against, so it is
#: refused rather than waved through as an ordinary external URL.
REPO_ANY = re.compile(r"^https://github\.com/rperez93/collab-a2a(/|$)")
REPO_ROOT = "https://github.com/rperez93/collab-a2a"

#: What a resource must not be: a path out of the bundle. It resolves only
#: while the bundle sits at this depth in this checkout, which is the form
#: these fields were moved off.
LOOKS_LIKE_A_PATH = re.compile(r"^(\.{1,2}/|/|[\w.-]+/)")


# --- a frontmatter parser small enough to be obviously right ------------------

def _scalar(raw: str):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _split_flow(body: str) -> list[str]:
    """Top-level comma separation, so a nested brace does not split an entry."""
    parts, depth, current = [], 0, ""
    for ch in body:
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current)
    return [p for p in parts if p.strip()]


def _value(raw: str):
    raw = raw.strip()
    if raw.startswith("{") and raw.endswith("}"):
        out = {}
        for item in _split_flow(raw[1:-1]):
            key, sep, val = item.partition(":")
            if not sep:
                raise ValueError(f"not a mapping entry: {item!r}")
            out[key.strip()] = _scalar(val)
        return out
    if raw.startswith("[") and raw.endswith("]"):
        return [_scalar(i) for i in _split_flow(raw[1:-1])]
    return _scalar(raw)


def parse_frontmatter(text: str) -> dict:
    """The YAML subset this bundle is allowed to use. Raises on anything else."""
    if not text.startswith("---\n"):
        raise ValueError("no frontmatter block")
    end = text.find("\n---\n", 3)
    if end == -1:
        raise ValueError("unterminated frontmatter block")
    lines = text[4:end + 1].splitlines()

    data: dict = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line[0] in " \t":
            raise ValueError(f"unexpected indentation: {line!r}")
        key, sep, rest = line.partition(":")
        if not sep:
            raise ValueError(f"not a key: {line!r}")
        key = key.strip()
        i += 1
        if rest.strip():
            data[key] = _value(rest)
            continue
        # A block sequence: entries are flow mappings, or indented key/values.
        entries: list = []
        while i < len(lines) and lines[i].startswith("  "):
            entry_line = lines[i].strip()
            if not entry_line.startswith("- "):
                raise ValueError(f"expected a sequence entry: {lines[i]!r}")
            body = entry_line[2:]
            i += 1
            if body.startswith("{"):
                entries.append(_value(body))
                continue
            first_key, sep2, first_val = body.partition(":")
            if not sep2:
                raise ValueError(f"not a mapping entry: {body!r}")
            entry = {first_key.strip(): _value(first_val)}
            while i < len(lines) and lines[i].startswith("    ") \
                    and not lines[i].strip().startswith("- "):
                sub_key, sep3, sub_val = lines[i].strip().partition(":")
                if not sep3:
                    raise ValueError(f"not a mapping entry: {lines[i]!r}")
                entry[sub_key.strip()] = _value(sub_val)
                i += 1
            entries.append(entry)
        if not entries:
            raise ValueError(f"{key} has no value and no entries")
        data[key] = entries
    return data


def _iso(value: str) -> datetime:
    """Every timestamp in OKF is ISO 8601 with an explicit offset (§5)."""
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def concepts() -> list[Path]:
    return sorted(p for p in BUNDLE.rglob("*.md") if p.name not in RESERVED)


def indexes() -> list[Path]:
    return sorted(BUNDLE.rglob("index.md"))


def _ids(path: Path) -> list[str]:
    return [str(path.relative_to(BUNDLE))]


def test_the_bundle_exists() -> None:
    assert BUNDLE.is_dir(), "the knowledge bundle is missing"
    assert (BUNDLE / "index.md").exists(), "a bundle root should list its contents"
    assert concepts(), "a bundle with no concepts is not a bundle"


@pytest.mark.parametrize("path", concepts(), ids=lambda p: str(p.name))
def test_every_concept_has_parseable_frontmatter_with_a_type(path: Path) -> None:
    """Conformance §11.1 and §11.2 — the only two hard requirements there are."""
    front = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert str(front.get("type", "")).strip(), f"{path.name} declares no type"


@pytest.mark.parametrize("path", concepts(), ids=lambda p: str(p.name))
def test_frontmatter_stays_inside_what_every_yaml_reader_accepts(path: Path) -> None:
    """The parser above is permissive where real YAML is not, and that bit.

    A plain scalar may not contain «: » — YAML reads the second colon as
    another mapping key and rejects the document. The parser here splits on the
    FIRST colon and is perfectly happy, so a title written `Live run: the room
    list` passed every test in this file while PyYAML refused the file outright.
    Eight concepts were written that way before a real engine was pointed at
    them.

    A bundle only its own producer's parser can read is not an exchange format,
    so the subset is asserted rather than assumed.
    """
    text = path.read_text(encoding="utf-8")
    block = text[4:text.find("\n---\n", 3) + 1]
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            stripped = stripped[2:]
        if stripped.startswith("{"):
            continue                       # a flow mapping; colons are structure
        _, sep, value = stripped.partition(":")
        value = value.strip()
        if not sep or not value or value[0] in "\"'{[":
            continue
        assert ": " not in value and not value.endswith(":"), (
            f"{path.name}: {stripped!r} — a plain scalar cannot hold a colon"
            " and a space; quote it or rewrite it"
        )
        assert value[0] not in "*&!%@`>|", (
            f"{path.name}: {stripped!r} — that leading character is a YAML"
            " indicator; quote the value"
        )


@pytest.mark.parametrize("path", concepts(), ids=lambda p: str(p.name))
def test_lifecycle_fields_say_something_a_consumer_can_act_on(path: Path) -> None:
    """`status` and `stale_after` are advisory, so a wrong one is never caught.

    Nothing rejects a bundle for these, which is exactly why they rot: an
    invented status or an unparseable instant costs nothing at consumption time
    and misleads for ever.
    """
    front = parse_frontmatter(path.read_text(encoding="utf-8"))
    if "status" in front:
        assert front["status"] in STATUSES, f"{path.name}: unknown status"
    if "stale_after" in front:
        _iso(front["stale_after"])


@pytest.mark.parametrize("path", concepts(), ids=lambda p: str(p.name))
def test_no_concept_claims_a_human_verified_it(path: Path) -> None:
    """The trust tier is derived from the `human:` prefix, so it is load-bearing.

    Every file here was written and checked by an agent. A `human:` actor would
    move the whole bundle from machine-confirmed to human-reviewed on the
    strength of a string nobody typed on purpose — which is the one claim this
    bundle must not make by accident.
    """
    front = parse_frontmatter(path.read_text(encoding="utf-8"))
    generated = front.get("generated")
    assert isinstance(generated, dict) and generated.get("by"), \
        f"{path.name}: nothing records who produced this"
    actors = [generated["by"]]

    verified = front.get("verified")
    if isinstance(verified, dict):        # §5.2 — a bare mapping is one entry
        verified = [verified]
    for event in verified or []:
        assert event.get("by") and event.get("at"), \
            f"{path.name}: a verification event needs both `by` and `at`"
        _iso(event["at"])
        actors.append(event["by"])

    if generated.get("at"):
        _iso(generated["at"])

    for actor in actors:
        assert ACTOR.match(actor), f"{path.name}: {actor!r} is not an actor (§7)"
        assert not actor.startswith("human:"), (
            f"{path.name}: claims {actor!r} — no person reviewed this bundle"
        )


def _resources(path: Path) -> list[str]:
    """Every path-or-URI-valued field on a concept (§6.2)."""
    front = parse_frontmatter(path.read_text(encoding="utf-8"))
    found = [front["resource"]] if front.get("resource") else []
    for entry in front.get("sources") or []:
        assert entry.get("resource"), \
            f"{path.name}: a source entry needs a resource (§5.1)"
        found.append(entry["resource"])
        if entry.get("last_modified"):
            _iso(entry["last_modified"])
    return found


@pytest.mark.parametrize("path", concepts(), ids=lambda p: str(p.name))
def test_evidence_is_pinned_or_is_honestly_a_scope_descriptor(path: Path) -> None:
    """A resource says what a claim was checked against, so it must not drift.

    These began as relative paths — `../../src/collab/batch.py` — which §6.2
    permits and which resolve perfectly well while the bundle sits here. Two
    things were wrong with them. A bundle is meant to be exchanged, and every
    one of those dangles the moment it is lifted out of the checkout. And a
    path with no revision on it says «check this against that file» while
    meaning «against whatever that file becomes», which is the defect the
    bundle is about, committed in its own frontmatter.

    So evidence into this repository is a URL pinned to a full sha, and
    evidence that is not a file at all — a command that was run — is a scope
    descriptor, which §5.1 provides for and which those always were.
    """
    for resource in _resources(path):
        if m := REPO_BLOB.match(resource):
            assert (ROOT / m.group(2)).exists(), (
                f"{path.name}: {m.group(2)} is no longer in the tree — the pin"
                " is still valid, but the concept describes something that has"
                " moved and needs re-checking"
            )
        elif REPO_ANY.match(resource):
            assert resource == REPO_ROOT, (
                f"{path.name}: {resource!r} reaches into this repository without"
                " a revision — pin it to a full sha, so it says which tree the"
                " claim was checked against"
            )
        elif resource.startswith(("http://", "https://")):
            continue                      # an external source; nothing to check
        else:
            assert not LOOKS_LIKE_A_PATH.match(resource), (
                f"{path.name}: {resource!r} is a path out of the bundle — pin it"
                " to a revision, or say plainly that it is a scope descriptor"
            )


def test_the_whole_bundle_is_pinned_to_one_revision() -> None:
    """Half a bundle pinned to one commit and half to another describes neither.

    Nothing in the format forbids it, and nothing downstream would notice: each
    URL resolves, so a consumer following any single one is satisfied while the
    document as a whole is an account of two different trees.
    """
    shas = {m.group(1) for path in concepts() for r in _resources(path)
            if (m := REPO_BLOB.match(r))}
    assert len(shas) == 1, f"pinned to {len(shas)} different revisions: {shas}"


@pytest.mark.parametrize("path", concepts(), ids=lambda p: str(p.name))
def test_a_footnote_resolves_to_a_source_it_can_be_attributed_to(path: Path) -> None:
    """§5.1 — the label is the join key into `sources`, not decoration.

    Labels are keyed rather than positional because agents rewrite these files;
    a footnote naming an id that is no longer in the list attributes a claim to
    nothing at all, silently.
    """
    text = path.read_text(encoding="utf-8")
    front = parse_frontmatter(text)
    known = {e.get("id") for e in front.get("sources") or [] if e.get("id")}
    body = text[text.find("\n---\n", 3) + 5:]
    used = set(FOOTNOTE_REF.findall(body))
    defined = set(FOOTNOTE_DEF.findall(body))
    for label in used:
        assert label in known, f"{path.name}: footnote [^{label}] names no source"
        assert label in defined, f"{path.name}: footnote [^{label}] is never defined"
    for label in defined:
        assert label in used, f"{path.name}: [^{label}] is defined but never cited"


@pytest.mark.parametrize("path", sorted(BUNDLE.rglob("*.md")), ids=lambda p: str(p.name))
def test_every_link_in_the_bundle_resolves(path: Path) -> None:
    """Broken links are spec-tolerated (§6.1). Ours are not, and this is why.

    Every target here exists today. Asserting it is what stops a rename turning
    a traversable graph into a set of dead ends nobody notices, because nothing
    downstream would complain.
    """
    for target in LINK.findall(path.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        clean = target.split("#", 1)[0]
        if not clean:
            continue
        resolved = (BUNDLE / clean.lstrip("/")) if clean.startswith("/") \
            else (path.parent / clean)
        assert resolved.exists(), f"{path.name}: {target} resolves to nothing"


def test_the_root_index_declares_the_version_it_targets() -> None:
    """§12 — and §8: the bundle root is the ONLY index that may carry any."""
    front = parse_frontmatter((BUNDLE / "index.md").read_text(encoding="utf-8"))
    assert front == {"okf_version": "0.2"}, \
        "the root index may declare okf_version and nothing else"


@pytest.mark.parametrize("path", [p for p in indexes() if p.parent != BUNDLE],
                         ids=lambda p: str(p.parent.name))
def test_a_subdirectory_index_carries_no_frontmatter(path: Path) -> None:
    """§8 — an index is a listing, not a concept."""
    assert not path.read_text(encoding="utf-8").startswith("---")


@pytest.mark.parametrize("path", indexes(), ids=lambda p: str(p.parent.name))
def test_an_index_lists_its_directory_the_way_the_spec_says(path: Path) -> None:
    """§8 — sections of `* [Title](url) - description` bullets.

    The description is not optional here even though the spec would allow it:
    progressive disclosure is the entire purpose of an index, and a bare list
    of titles makes a consumer open every file to find out what is in it.
    """
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.startswith("*")]
    assert lines, f"{path} lists nothing"
    for line in lines:
        assert re.match(r"^\* \[[^\]]+\]\([^)]+\) - \S", line), \
            f"{path}: {line!r} is not a listing entry"
    assert any(ln.startswith("# ") for ln in
               path.read_text(encoding="utf-8").splitlines()), \
        f"{path} groups nothing under a heading"


def test_the_log_is_dated_newest_first() -> None:
    """§9 — ISO 8601 `YYYY-MM-DD` headings, newest first."""
    text = (BUNDLE / "log.md").read_text(encoding="utf-8")
    assert not text.startswith("---"), "a log is not a concept"
    dates = re.findall(r"^## (\S+)$", text, re.MULTILINE)
    assert dates, "the log records nothing"
    for date in dates:
        datetime.strptime(date, "%Y-%m-%d")
    assert dates == sorted(dates, reverse=True), "the log is not newest first"


def test_every_concept_is_reachable_from_the_root() -> None:
    """A concept nothing links to is one an agent traversing this never finds."""
    linked: set[Path] = set()
    for path in BUNDLE.rglob("*.md"):
        for target in LINK.findall(path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            clean = target.split("#", 1)[0]
            resolved = (BUNDLE / clean.lstrip("/")) if clean.startswith("/") \
                else (path.parent / clean)
            resolved = resolved.resolve()
            if resolved.is_dir():
                resolved = resolved / "index.md"
            linked.add(resolved)
    orphans = [str(p.relative_to(BUNDLE)) for p in concepts()
               if p.resolve() not in linked]
    assert not orphans, f"nothing links to {orphans}"
