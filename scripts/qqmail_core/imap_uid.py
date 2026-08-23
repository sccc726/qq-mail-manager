"""Protocol-level helpers shared by the M2 UID-only consumers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


_UID_TOKEN = re.compile(r"(?:^|[ (])UID\s+(\d+)(?:$|[ )])", re.IGNORECASE)
_FETCH_PREFIX = re.compile(r"^\s*\d+\s+\(", re.ASCII)


@dataclass(frozen=True)
class UidFetch:
    """The one literal whose own metadata positively identifies the requested UID."""

    raw: bytes
    metadata: bytes


def _bytes(value: Any) -> bytes | None:
    return bytes(value) if isinstance(value, (bytes, bytearray)) else None


def _is_fetch_metadata(value: Any) -> bool:
    payload = _bytes(value)
    if payload is None:
        return False
    text = payload.decode("ascii", errors="replace")
    return bool(_FETCH_PREFIX.match(text))


def _protocol_uid(value: bytes) -> str | None:
    match = _UID_TOKEN.search(value.decode("ascii", errors="replace"))
    return match.group(1) if match else None


def _metadata_uid(value: Any) -> str | None:
    """Read a UID only from a standalone FETCH metadata record."""
    payload = _bytes(value)
    return _protocol_uid(payload) if payload is not None and _is_fetch_metadata(payload) else None


def select_uid_fetch(data: Iterable[Any] | None, requested_uid: str) -> UidFetch | None:
    """Select a literal only when its *own* metadata names ``requested_uid``.

    IMAP servers may interleave unsolicited FETCH data.  Never choose the first
    tuple: choose the tuple carrying an exact UID token.  Metadata may be on
    either side of the literal and trailing non-tuple metadata is retained for
    fields such as INTERNALDATE.
    """
    items = list(data or [])
    for index, item in enumerate(items):
        if not isinstance(item, tuple):
            continue
        byte_parts = [part for part in item if _bytes(part) is not None]
        prefix_parts = [part for part in byte_parts if _is_fetch_metadata(part)]
        literal_parts = [part for part in byte_parts if part not in prefix_parts]
        if not prefix_parts or not literal_parts:
            continue
        # A literal can precede or follow its tuple's metadata.  Continuations
        # immediately after it are protocol metadata, never email content.
        metadata_parts = list(prefix_parts)
        for continuation in items[index + 1:]:
            if isinstance(continuation, tuple):
                break
            payload = _bytes(continuation)
            if payload is not None:
                metadata_parts.append(payload)
        metadata = b" ".join(metadata_parts)
        if _protocol_uid(metadata) == requested_uid:
            return UidFetch(raw=literal_parts[0], metadata=metadata)
    return None


def uid_fetch_exists(data: Iterable[Any] | None, requested_uid: str) -> bool:
    """Return whether a FETCH response explicitly names one requested UID."""
    for item in data or ():
        if isinstance(item, tuple):
            values = item
        else:
            # UID FETCH <uid> (UID) is commonly a flat imaplib bytes row.
            values = (item,)
        if any(_metadata_uid(value) == requested_uid for value in values):
            return True
    return False


def normalize_capabilities(values: Iterable[Any]) -> set[str]:
    """Normalize capability atoms returned as bytes or strings."""
    normalized = set()
    for value in values:
        text = value.decode("ascii", errors="replace") if isinstance(value, bytes) else str(value)
        normalized.update(atom.upper() for atom in text.split())
    return normalized


def refresh_capabilities(client: Any) -> set[str]:
    """Ask the authenticated server for current capabilities before mutation."""
    status, values = client.capability()
    if status != "OK":
        return set()
    capabilities = normalize_capabilities(values or ())
    try:
        client.capabilities = tuple(sorted(capabilities))
    except Exception:
        pass
    return capabilities
