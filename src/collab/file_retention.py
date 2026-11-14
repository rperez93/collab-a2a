"""Explicit retention for shared design and review documents."""
from pathlib import Path
from urllib.parse import urlsplit

DOCUMENT_SUFFIXES = frozenset({'.md', '.rst', '.txt', '.pdf', '.docx', '.odt', '.html', '.json'})


def keep_document(name: str) -> bool:
    return Path(name).suffix.lower() in DOCUMENT_SUFFIXES


def durable_source(value: str) -> str:
    if not value:
        return ''
    if len(value.encode('utf-8')) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('durable source must be an HTTPS URL of at most 2048 bytes')
    try:
        url = urlsplit(value)
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError
    except ValueError:
        raise ValueError('durable source must be an HTTPS URL without embedded credentials') from None
    return value


def _chunks(path):
    """Only bounded regular files can enter or leave the private worker stash."""
    import os
    import stat
    from .protocol import MAX_FILE_BYTES
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ValueError('queued upload must be a regular file within the file limit')
        total = 0
        while block := stream.read(min(65536, MAX_FILE_BYTES - total + 1)):
            total += len(block)
            if total > MAX_FILE_BYTES:
                raise ValueError('queued upload exceeds the file limit')
            yield block


def copy_upload(source, target):
    import hashlib
    digest = hashlib.sha256()
    with Path(target).open('xb') as out:
        for block in _chunks(source):
            out.write(block)
            digest.update(block)
    return digest.hexdigest()


def read_upload(path):
    return b''.join(_chunks(path))


CONFIRM_BYTES = 16 * 1024
CONFIRM_SECONDS = 2.0


def retention_warning(record, keep, source):
    """Only an explicit matching host response confirms durable metadata."""
    if not isinstance(record, dict) or type(record.get('kept')) is not bool or (keep is not None and record.get('kept') is not keep) or (source and record.get('source') != source):
        return 'Upload accepted, but retention/source is unconfirmed. Keep your original; inspect the host before sharing again.'
    return ''
