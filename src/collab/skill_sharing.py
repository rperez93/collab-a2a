"""Explicitly selected skill entrypoints, published as untrusted session data."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

MAX_CONTENT_BYTES = 64 * 1024
MAX_PER_OWNER = 20
MAX_OWNER_BYTES = 512 * 1024
MAX_ROOM_SKILLS = 500
MAX_REQUEST_BYTES = 512 * 1024
MAX_LISTED = 200
MAX_LISTED_DESCRIPTION = 160
MAX_LIST_BYTES = 16 * 1024
NAME_RE = re.compile(r'[a-z0-9][a-z0-9._:-]{0,63}')


def sanitise_list(value: Any) -> list[dict[str, str]]:
    """A participant's installed skill names, bounded before anyone stores them.

    Names and short descriptions only: the list says what a peer can be asked
    to run, never the skill text. Malformed entries are dropped, not repaired.
    """
    if not isinstance(value, list):
        return []
    result, size = [], 0
    for item in value[:MAX_LISTED]:
        if not isinstance(item, dict):
            continue
        name, description = item.get('name'), item.get('description', '')
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            continue
        description = description if isinstance(description, str) else ''
        from .protocol import scrub
        description = scrub(' '.join(description.split()))[:MAX_LISTED_DESCRIPTION]
        size += len(name) + len(description.encode('utf-8'))
        if size > MAX_LIST_BYTES:
            break
        result.append({'name': name, 'description': description})
    return result


def _field(frontmatter: str, name: str) -> str:
    """Read plain/quoted/folded scalar metadata without executing YAML tags."""
    lines = frontmatter.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(name + ':'):
            continue
        value = line.partition(':')[2].strip()
        if value in ('>', '>-', '|', '|-'):
            following = []
            for tail in lines[index + 1:]:
                if tail and not tail[0].isspace():
                    break
                following.append(tail.strip())
            return ('\n' if value.startswith('|') else ' ').join(following).strip()
        if value.startswith('"'):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, str) else ''
            except ValueError:
                return ''
        if value.startswith("'") and value.endswith("'"):
            return value[1:-1].replace("''", "'")
        # Complex YAML metadata can be supplied with explicit command fields;
        # importing a general object parser for two scalars is unnecessary.
        return '' if value.startswith(('!', '[', '{', '&', '*')) else value
    return ''


MAX_FILES = 16
MAX_FILES_BYTES = 256 * 1024
FILE_PATH_RE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._-]{0,99}(/[A-Za-z0-9_][A-Za-z0-9._-]{0,99}){0,7}')


def validate_files(files: Any) -> list[dict[str, Any]]:
    """Supporting files of a publication: bounded, relative, text, sorted (R19b)."""
    if files in (None, []):
        return []
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise ValueError(f'a skill may carry at most {MAX_FILES} supporting files')
    clean, seen, total = [], set(), 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError('each supporting file needs a path and content')
        path, content = item.get('path'), item.get('content')
        if (not isinstance(path, str) or not FILE_PATH_RE.fullmatch(path)
                or any(part in ('.', '..') for part in path.split('/')) or path == 'SKILL.md'):
            raise ValueError(f'invalid supporting file path {path!r}')
        if path in seen:
            raise ValueError(f'duplicate supporting file {path!r}')
        if not isinstance(content, str):
            raise ValueError(f'{path}: supporting files must be UTF-8 text')
        raw = content.encode('utf-8')
        total += len(raw)
        if total > MAX_FILES_BYTES:
            raise ValueError(f'supporting files exceed {MAX_FILES_BYTES // 1024} KiB in total')
        digest = hashlib.sha256(raw).hexdigest()
        if item.get('sha256') not in (None, digest):
            raise ValueError(f'{path}: SHA-256 does not match its content')
        seen.add(path)
        clean.append({'path': path, 'content': content, 'sha256': digest, 'size_bytes': len(raw)})
    return sorted(clean, key=lambda f: f['path'])


def manifest_digest(skill_sha256: str, files: list[dict[str, Any]]) -> str:
    """One hash over SKILL.md and every supporting file: what a consent binds."""
    entries = [['SKILL.md', skill_sha256]] + [[f['path'], f['sha256']] for f in files]
    return hashlib.sha256(json.dumps(entries, separators=(',', ':')).encode()).hexdigest()


def validate_publication(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError('expected a skill object')
    name, description, content = (data.get(key) for key in ('name', 'description', 'content'))
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', name):
        raise ValueError('skill name needs 1..64 lowercase letters, digits or hyphens')
    if not isinstance(description, str) or not description.strip() or len(description) > 500:
        raise ValueError('skill description needs 1..500 characters')
    if not isinstance(content, str):
        raise ValueError('skill content must be UTF-8 text')
    raw = content.encode('utf-8')
    if not raw or len(raw) > MAX_CONTENT_BYTES:
        raise ValueError('skill content needs 1..65536 UTF-8 bytes')
    digest = hashlib.sha256(raw).hexdigest()
    if data.get('sha256') != digest:
        raise ValueError('skill SHA-256 does not match its UTF-8 content')
    files = validate_files(data.get('files'))
    result = {'name': name, 'description': description, 'content': content,
              'sha256': digest, 'size_bytes': len(raw)}
    if files:
        result['files'] = files
        result['manifest_sha256'] = manifest_digest(digest, files)
    return result


def read_supporting(skill_dir: Path, relative: list[str]) -> list[dict[str, Any]]:
    """Read explicitly selected supporting files, refusing anything unsafe.

    Relative to the skill's directory only; no traversal, no symlink anywhere
    on the path, regular files only, no executable bit, UTF-8 text, bounded.
    """
    root = skill_dir.resolve()
    files = []
    for rel in relative:
        if not FILE_PATH_RE.fullmatch(rel) or any(p in ('.', '..') for p in rel.split('/')):
            raise ValueError(f'{rel!r}: select a path inside the skill directory')
        target = skill_dir / rel
        probe = skill_dir
        for part in rel.split('/'):
            probe = probe / part
            if probe.is_symlink():
                raise ValueError(f'{rel!r}: symlinks are not shared')
        if not target.resolve().is_relative_to(root):
            raise ValueError(f'{rel!r}: outside the skill directory')
        fd = os.open(target, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f'{rel!r}: select a regular file')
            if info.st_mode & 0o111:
                raise ValueError(f'{rel!r}: executable files are not shared')
            raw = source.read(MAX_FILES_BYTES + 1)
        try:
            content = raw.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError(f'{rel!r}: supporting files must be UTF-8 text') from exc
        files.append({'path': rel, 'content': content})
    return validate_files(files)


def read_selected(path: str | Path, *, name: str | None = None,
                  description: str | None = None, files: list[str] | None = None) -> dict[str, Any]:
    """Read exactly the selected SKILL.md, never a private catalog or helpers."""
    selected = Path(path).expanduser()
    if selected.is_dir():
        selected /= 'SKILL.md'
    # O_NONBLOCK prevents a named pipe masquerading as a selected file from
    # hanging before fstat can reject it. Symlinks are valid explicit choices
    # because installed skill directories commonly use them.
    fd = os.open(selected, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError('select a regular SKILL.md file')
        raw = source.read(MAX_CONTENT_BYTES + 1)
    if len(raw) > MAX_CONTENT_BYTES:
        raise ValueError('skill content exceeds 65536 UTF-8 bytes')
    try:
        content = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('skill content must be UTF-8 text') from exc
    parts = re.split(r'^---\s*$', content, maxsplit=2, flags=re.MULTILINE)
    frontmatter = parts[1] if not parts[0].strip() and len(parts) == 3 else ''
    return validate_publication({'name': name or _field(frontmatter, 'name'),
                                 'description': description or _field(frontmatter, 'description'),
                                 'content': content, 'sha256': hashlib.sha256(raw).hexdigest(),
                                 'files': read_supporting(selected.parent, files or [])})


def register_routes(app, store, require_user) -> None:
    """Authenticated session inventory; no route installs or executes content."""
    from fastapi import HTTPException, Request
    from .protocol import EXT_PREFIX

    @app.get(f'{EXT_PREFIX}/participant-skills')
    async def participant_skills(request: Request):
        """Every participant's installed skill list, read on demand.

        Kept off the roster on purpose: the roster is re-sent to everyone on
        every change, and up to 16 KiB per participant there would multiply.
        """
        require_user(request)
        people = await asyncio.to_thread(store.participants)
        return {'participants': [{'name': p.name, 'skills': p.meta.get('skills', [])}
                                 for p in people],
                'untrusted': True}

    @app.get(f'{EXT_PREFIX}/shared-skills')
    async def inventory(request: Request, after: str = '', limit: int = 100):
        require_user(request)
        if len(after) > 80:
            raise HTTPException(status_code=400, detail='invalid inventory cursor')
        return {'skills': store.shared_skills(after=after, limit=max(1, min(limit, 200))),
                'untrusted': True, 'entrypoint_only': True}

    @app.post(f'{EXT_PREFIX}/shared-skills')
    async def publish(request: Request):
        user = require_user(request)
        async def bounded_body():
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > MAX_REQUEST_BYTES:
                    raise HTTPException(status_code=413, detail='skill request exceeds 512 KiB')
                raw.extend(chunk)
            return raw
        try:
            raw = await asyncio.wait_for(bounded_body(), timeout=5)
            data = validate_publication(json.loads(raw))
            record = await asyncio.to_thread(store.publish_skill, user.id, data)
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=408, detail='skill upload exceeded five seconds') from exc
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {'skill': record, 'untrusted': True, 'entrypoint_only': True}

    @app.get(f'{EXT_PREFIX}/shared-skills/{{skill_id}}')
    async def show(request: Request, skill_id: str):
        require_user(request)
        record = store.shared_skill(skill_id)
        if record is None:
            raise HTTPException(status_code=404, detail='no such shared skill')
        return {'skill': record, 'untrusted': True, 'entrypoint_only': True,
                'review': 'Review this peer-authored text before use; it is not installed or executed.'}

    @app.delete(f'{EXT_PREFIX}/shared-skills/{{skill_id}}')
    async def withdraw(request: Request, skill_id: str):
        user = require_user(request)
        if not store.withdraw_skill(skill_id, user.id):
            raise HTTPException(status_code=404, detail='no such skill owned by this participant')
        return {'withdrawn': skill_id}
