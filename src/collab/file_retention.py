"""Explicit retention for shared design and review documents."""
from pathlib import Path
from urllib.parse import urlsplit

#: Kept until withdrawn by default: design and review documents only. `.json`
#: and `.txt` were on this list, and those are what agents' routine reports
#: are, so every status dump became permanent (gate review of #197). Anything
#: else can still be kept on purpose with `--keep`.
DOCUMENT_SUFFIXES = frozenset({'.md', '.rst', '.pdf'})

#: KEPT FILES ARE BOUNDED. A kept file is never deleted on receipt or by age,
#: so without a cap one participant, or an agent's loop of reports, could fill
#: the host's disk with files nobody withdraws (gate review of #197). Five
#: maximum-size files (MAX_FILE_BYTES, 10 MiB) per owner, and five owners'
#: worth per session. Over either, an upload is stored as TEMPORARY and the
#: answer says why: the CLI always states `keep`, so the hub cannot tell a
#: default-kept document from a deliberate `--keep`, and refusing would turn an
#: ordinary document upload into an error once the cap filled.
MAX_KEPT_OWNER_BYTES = 50 * 1024 * 1024
MAX_KEPT_SESSION_BYTES = 250 * 1024 * 1024


def keep_document(name: str) -> bool:
    return Path(name).suffix.lower() in DOCUMENT_SUFFIXES


def durable_source(value: str) -> str:
    if not value:
        return ''
    if len(value.encode('utf-8')) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('durable source must be an HTTPS URL of at most 2048 bytes')
    # NO INVISIBLE FORMATTING. U+202E (right-to-left override) makes a shown
    # URL read backwards, so «example.com/evil» can display as another host;
    # every format character (Cf) and line or paragraph separator (Zl, Zp) is
    # refused rather than shown to the people who will follow the link.
    import unicodedata
    if any(unicodedata.category(c) in ('Cf', 'Zl', 'Zp') for c in value):
        raise ValueError('durable source must not contain invisible formatting or separator characters')
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
