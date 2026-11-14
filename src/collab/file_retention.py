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


def retention_warning(record, keep, source):
    """Only an explicit matching host response confirms durable metadata."""
    if not isinstance(record, dict) or type(record.get('kept')) is not bool or (keep is not None and record.get('kept') is not keep) or (source and record.get('source') != source):
        return 'Upload accepted, but retention/source is unconfirmed. Keep your original; inspect the host before sharing again.'
    return ''
