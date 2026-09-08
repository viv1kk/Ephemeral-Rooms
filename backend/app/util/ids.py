"""Server-generated identifiers. Nothing here is ever accepted from a client."""

from __future__ import annotations

import secrets
import uuid


def new_uuid() -> str:
    return str(uuid.uuid4())


def new_session_token() -> str:
    """128 bits of CSPRNG output, hex-encoded (spec section 4.1)."""
    return secrets.token_hex(16)


def is_uuid(value: object) -> bool:
    """Reject anything that is not a well-formed UUID before it can reach the
    filesystem layer (spec section 21.2)."""
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True
