"""Synthetic receipt fixtures; these do not demonstrate human approval."""
import hashlib
import secrets
from collab import consent
from collab.remote_requests import binding_digest, digest


def receipt(inbox, offer, target, command, cwd=None):
    token = secrets.token_urlsafe(32)
    with consent._database(inbox.root.parent) as db:
        consent._append(db, 'approved', 'remote', offer['id'], digest(offer),
                        binding_digest(target, command, cwd),
                        hashlib.sha256(token.encode()).hexdigest(),
                        {'channel': 'synthetic-test'})
    return consent.Receipt(token)
