"""Map a LiveKit room to a synthetic account.

Primary: room metadata JSON ``{"account_id": "globex"}``.
Fallback: an identity prefix, e.g. ``acct-globex__user-42`` -> ``globex``
          (separator from ``ACCOUNT_IDENTITY_PREFIX_SEP``, default ``__``).

This is entirely a Portfolio Signal convention; nothing here is LiveKit-specific.
"""

from __future__ import annotations

import json
import os


def _sep() -> str:
    return os.environ.get("ACCOUNT_IDENTITY_PREFIX_SEP", "__")


def account_from_metadata(metadata: str | None) -> str | None:
    """Read account_id from a room (or participant) metadata JSON string."""
    if not metadata:
        return None
    try:
        data = json.loads(metadata)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        value = data.get("account_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def account_from_identity(identity: str | None) -> str | None:
    """Fallback: take the piece before the separator, stripping an optional ``acct-`` prefix."""
    if not identity:
        return None
    sep = _sep()
    if sep not in identity:
        return None
    head = identity.split(sep, 1)[0].strip()
    if head.startswith("acct-"):
        head = head[len("acct-") :]
    return head or None


def resolve_account_id(
    room_metadata: str | None = None,
    identities: list[str] | None = None,
) -> str | None:
    """Best available account_id: metadata first, then the first identity that yields one."""
    account = account_from_metadata(room_metadata)
    if account:
        return account
    for identity in identities or []:
        account = account_from_identity(identity)
        if account:
            return account
    return None
