"""Encrypt secrets at rest (Fernet, key derived from SECRET_KEY)."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from . import config


class CannotDecrypt(Exception):
    """Stored secret was encrypted with a different SECRET_KEY."""


def _fernet() -> Fernet | None:
    if not config.SECRET_KEY:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(config.SECRET_KEY.encode()).digest())
    return Fernet(key)


def seal(value: Any) -> Any:
    """Wrap a value for storage. Without SECRET_KEY it's stored as is (the page warns)."""
    f = _fernet()
    if f is None:
        return value
    return {"enc": f.encrypt(json.dumps(value).encode()).decode()}


def unseal(stored: Any) -> Any:
    """Read a value written by seal() (or an older plain one)."""
    if isinstance(stored, dict) and set(stored) == {"enc"}:
        f = _fernet()
        if f is None:
            raise CannotDecrypt("SECRET_KEY is missing, so saved Tesla data can't be read.")
        try:
            return json.loads(f.decrypt(stored["enc"].encode()))
        except InvalidToken:
            raise CannotDecrypt("SECRET_KEY changed, so saved Tesla data can't be read.") from None
    return stored
