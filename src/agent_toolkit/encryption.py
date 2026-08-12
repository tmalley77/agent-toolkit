"""
Field-level encryption for PII stored in the database.

Requires DB_ENCRYPTION_KEY in .env — generate one with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

If the key is not set, enc() stores plaintext and a warning is logged once.
dec() handles mixed encrypted/plaintext rows gracefully (for migration).
"""
import os
import logging
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_fernet = None
_warned = False


def _get_fernet() -> Fernet | None:
    global _fernet, _warned
    if _fernet is not None:
        return _fernet
    key = os.getenv("DB_ENCRYPTION_KEY")
    if not key:
        if not _warned:
            logger.warning("DB_ENCRYPTION_KEY not set — PII fields stored unencrypted")
            _warned = True
        return None
    _fernet = Fernet(key.encode())
    return _fernet


def enc(value: str | None) -> str | None:
    """Encrypt a string value. Returns None if input is None."""
    if value is None:
        return None
    f = _get_fernet()
    if f is None:
        return value
    return f.encrypt(value.encode()).decode()


def dec(value: str | None) -> str | None:
    """Decrypt a string value. Falls back to plaintext only for pre-migration rows
    (InvalidToken means the row was never encrypted). Any other decryption error is
    re-raised so callers don't silently receive garbage or ciphertext."""
    if value is None:
        return None
    f = _get_fernet()
    if f is None:
        return value
    try:
        return f.decrypt(value.encode()).decode()
    except InvalidToken:
        return value
