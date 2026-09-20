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
    return {'name': name, 'description': description, 'content': content,
            'sha256': digest, 'size_bytes': len(raw)}


def read_selected(path: str | Path, *, name: str | None = None,
                  description: str | None = None) -> dict[str, Any]:
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
                                 'content': content, 'sha256': hashlib.sha256(raw).hexdigest()})


def register_routes(app, store, require_user) -> None:
    """Authenticated session inventory; no route installs or executes content."""
    from fastapi import HTTPException, Request
    from .protocol import EXT_PREFIX

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
