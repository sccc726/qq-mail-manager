"""Protocol-level helpers shared by the M2 UID-only consumers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


_MAX_PROTOCOL_NUMBER = (1 << 32) - 1
_MAX_GENERIC_NUMBER = (1 << 64) - 1


@dataclass(frozen=True)
class UidFetch:
    """The one literal whose own metadata positively identifies the requested UID."""

    raw: bytes
    metadata: bytes


@dataclass(frozen=True)
class _FetchItem:
    name: str
    value_start: int
    value_end: int
    section: str | None = None
    origin: int | None = None
    literal_length: int | None = None
    explicit_nil: bool = False


def _bytes(value: Any) -> bytes | None:
    return bytes(value) if isinstance(value, (bytes, bytearray)) else None


def _is_fetch_metadata(value: Any) -> bool:
    payload = _bytes(value)
    if payload is None:
        return False
    text = payload.decode("ascii", errors="replace")
    return _fetch_envelope_start(text) is not None


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index] == " ":
        index += 1
    return index


def _read_atom(text: str, index: int) -> tuple[str, int] | None:
    if index >= len(text) or not text[index].isascii() or not text[index].isalpha():
        return None
    end = index + 1
    while (end < len(text) and text[end].isascii() and
           (text[end].isalnum() or text[end] in "_.-")):
        end += 1
    return text[index:end].upper(), end


def _skip_quoted(text: str, index: int) -> int | None:
    if index >= len(text) or text[index] != '"':
        return None
    index += 1
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            index += 2
        elif text[index] == '"':
            return index + 1
        else:
            index += 1
    return None


def _skip_list(text: str, index: int) -> int | None:
    if index >= len(text) or text[index] != "(":
        return None
    depth = 0
    while index < len(text):
        if text[index] == '"':
            index = _skip_quoted(text, index)
            if index is None:
                return None
            continue
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
            if depth < 0:
                return None
        index += 1
    return None


def _bounded_decimal(digits: str, *, maximum: int = _MAX_PROTOCOL_NUMBER) -> int | None:
    """Accept only an unsigned response number within the caller's bound."""
    if (not digits or len(digits) > len(str(maximum)) or not digits.isascii() or
            not digits.isdecimal()):
        return None
    try:
        value = int(digits)
    except ValueError:
        return None
    return value if value <= maximum else None


def _read_decimal_delimited(text: str, index: int, opening: str, closing: str) -> tuple[int, int] | None:
    if index >= len(text) or text[index] != opening:
        return None
    end = text.find(closing, index + 1)
    value = _bounded_decimal(text[index + 1:end]) if end >= 0 else None
    return (value, end + 1) if value is not None else None


def _read_decimal_braces(text: str, index: int) -> tuple[int, int] | None:
    return _read_decimal_delimited(text, index, "{", "}")


def _read_decimal_atom(text: str, index: int, *, maximum: int = _MAX_PROTOCOL_NUMBER) -> tuple[int, int] | None:
    end = index
    while end < len(text) and text[end].isascii() and text[end].isdigit():
        end += 1
    value = _bounded_decimal(text[index:end], maximum=maximum)
    if value is None or (end < len(text) and text[end] not in {" ", ")"}):
        return None
    return value, end


def _read_nz_number(text: str, index: int) -> tuple[int, int] | None:
    """Read an RFC 3501 nz-number without leading zeroes."""
    number = _read_decimal_atom(text, index)
    if number is None or number[0] == 0 or text[index] == "0":
        return None
    return number


def _fetch_envelope_start(text: str) -> int | None:
    """Return the byte after one strictly valid FETCH response root ``("``."""
    index = _skip_space(text, 0)
    sequence = _read_nz_number(text, index)
    if sequence is None:
        return None
    _, end = sequence
    after_space = _skip_space(text, end)
    if after_space == end or after_space >= len(text) or text[after_space] != "(":
        return None
    return after_space + 1


def _envelope_literal_markers(text: str) -> tuple[tuple[int, int, int], ...] | None:
    """Find every unquoted IMAP literal marker, including nested-list ones."""
    markers: list[tuple[int, int, int]] = []
    index = 0
    while index < len(text):
        if text[index] == '"':
            index = _skip_quoted(text, index)
            if index is None:
                return None
            continue
        if text[index] == "{":
            marker = _read_decimal_braces(text, index)
            if marker is None:
                return None
            length, end = marker
            markers.append((index, end, length))
            index = end
            continue
        if text[index] == "}":
            return None
        index += 1
    return tuple(markers)


def _is_literal_free_metadata(parsed: tuple[str, list[_FetchItem]]) -> bool:
    """A flat FETCH metadata record cannot contain a pending literal value."""
    text, items = parsed
    markers = _envelope_literal_markers(text)
    return (markers == () and all(item.section is None or item.explicit_nil for item in items))


def _parse_body_section(text: str, index: int) -> tuple[str, int] | None:
    if index >= len(text) or text[index] != "[":
        return None
    start, nested = index + 1, 0
    index += 1
    while index < len(text):
        if text[index] == '"':
            index = _skip_quoted(text, index)
            if index is None:
                return None
            continue
        if text[index] == "(":
            nested += 1
        elif text[index] == ")":
            nested -= 1
            if nested < 0:
                return None
        elif text[index] == "]" and nested == 0:
            return text[start:index], index + 1
        elif text[index] == "[":
            return None
        index += 1
    return None


def _skip_generic_value(text: str, index: int) -> tuple[int, int | None] | None:
    index = _skip_space(text, index)
    if index >= len(text):
        return None
    if text[index] == '"':
        end = _skip_quoted(text, index)
        return (end, None) if end is not None else None
    if text[index] == "(":
        end = _skip_list(text, index)
        return (end, None) if end is not None else None
    if text[index] == "{":
        marker = _read_decimal_braces(text, index)
        return (marker[1], marker[0]) if marker else None
    if text[index] in "[<>)":
        return None
    if text[index].isascii() and text[index].isdigit():
        decimal = _read_decimal_atom(text, index, maximum=_MAX_GENERIC_NUMBER)
        return (decimal[1], None) if decimal else None
    atom = _read_atom(text, index)
    return (atom[1], None) if atom else None


def _parse_fetch_envelope(payload: bytes) -> tuple[str, list[_FetchItem]] | None:
    """Strictly lex one complete FETCH envelope into top-level data items."""
    text = payload.decode("ascii", errors="replace")
    root = _fetch_envelope_start(text)
    if root is None:
        return None
    items: list[_FetchItem] = []
    index = root
    while True:
        previous = index
        index = _skip_space(text, index)
        if index >= len(text):
            return None
        if items and index == previous and text[index] != ")":
            return None
        if text[index] == ")":
            return (text, items) if all(char == " " for char in text[index + 1:]) else None
        atom = _read_atom(text, index)
        if atom is None:
            return None
        name, end = atom
        value_start = _skip_space(text, end)
        has_value_space = value_start > end
        if name in {"BODY", "BODY.PEEK"} and end < len(text) and text[end] == "[":
            section_value = _parse_body_section(text, end)
            if section_value is None:
                return None
            section, value_end = section_value
            origin = None
            if value_end < len(text) and text[value_end] == "<":
                partial = _read_decimal_delimited(text, value_end, "<", ">")
                if partial is None:
                    return None
                origin, value_end = partial
            after_value = _skip_space(text, value_end)
            marker = _read_decimal_braces(text, after_value) if after_value < len(text) and text[after_value] == "{" else None
            literal_length, explicit_nil = None, False
            if marker is not None:
                if after_value == value_end:
                    return None
                literal_length, value_end = marker
            elif text[after_value:after_value + 3].upper() == "NIL" and (
                    after_value + 3 == len(text) or text[after_value + 3] in {" ", ")"}):
                if after_value == value_end:
                    return None
                value_end, explicit_nil = after_value + 3, True
            items.append(_FetchItem(name, value_start, value_end, section, origin,
                                    literal_length, explicit_nil))
            index = value_end
            continue
        if name == "BODY":
            # RFC 3501's non-extension BODY item carries a parenthesized body
            # structure; only BODY[section] is a literal-bearing section item.
            if not has_value_space or value_start >= len(text) or text[value_start] != "(":
                return None
            value_end = _skip_list(text, value_start)
            if value_end is None:
                return None
            items.append(_FetchItem(name, value_start, value_end))
            index = value_end
            continue
        if name == "BODY.PEEK":
            return None
        if name == "UID":
            decimal = _read_nz_number(text, value_start) if has_value_space else None
            if decimal is None:
                return None
            _, value_end = decimal
        elif name == "INTERNALDATE":
            value_end = _skip_quoted(text, value_start) if has_value_space else None
            if value_end is None:
                return None
        elif name in {"BODYSTRUCTURE", "FLAGS"}:
            value_end = _skip_list(text, value_start) if has_value_space else None
            if value_end is None:
                return None
        elif name == "ENVELOPE":
            value_end = _skip_list(text, value_start) if has_value_space else None
            if value_end is None:
                return None
        elif name == "RFC822.SIZE":
            decimal = _read_decimal_atom(text, value_start) if has_value_space else None
            if decimal is None:
                return None
            _, value_end = decimal
        elif name in {"RFC822", "RFC822.HEADER", "RFC822.TEXT"}:
            if not has_value_space:
                return None
            if text[value_start:value_start + 3].upper() == "NIL" and (
                    value_start + 3 == len(text) or text[value_start + 3] in {" ", ")"}):
                value_end, literal_length, explicit_nil = value_start + 3, None, True
            elif text[value_start:value_start + 1] == '"':
                value_end = _skip_quoted(text, value_start)
                if value_end is None:
                    return None
                literal_length, explicit_nil = None, False
            elif text[value_start:value_start + 1] == "{":
                marker = _read_decimal_braces(text, value_start)
                if marker is None:
                    return None
                literal_length, value_end, explicit_nil = marker[0], marker[1], False
            else:
                return None
            items.append(_FetchItem(name, value_start, value_end, literal_length=literal_length,
                                    explicit_nil=explicit_nil))
            index = value_end
            continue
        else:
            if not has_value_space:
                return None
            generic_value = _skip_generic_value(text, value_start)
            if generic_value is None:
                return None
            value_end, literal_length = generic_value
            items.append(_FetchItem(name, value_start, value_end, literal_length=literal_length))
            index = value_end
            continue
        items.append(_FetchItem(name, value_start, value_end))
        index = value_end


def _protocol_uid(value: bytes) -> str | None:
    """Read exactly one top-level decimal UID data item from FETCH metadata."""
    parsed = _parse_fetch_envelope(value)
    if parsed is None:
        return None
    text, items = parsed
    uids = [text[item.value_start:item.value_end] for item in items if item.name == "UID"]
    return uids[0] if len(uids) == 1 else None


def _metadata_uid(value: Any) -> str | None:
    """Read a UID only from a standalone FETCH metadata record."""
    payload = _bytes(value)
    if payload is None or not _is_fetch_metadata(payload):
        return None
    parsed = _parse_fetch_envelope(payload)
    if parsed is None:
        return None
    if not _is_literal_free_metadata(parsed):
        return None
    return _protocol_uid(payload)


def _has_required_metadata_item(payload: bytes, required_item: str | None) -> bool:
    """Recognize only an expected *top-level* FETCH data item and value."""
    if required_item is None:
        return True
    wanted = required_item.upper()
    if wanted not in {"BODYSTRUCTURE", "INTERNALDATE"}:
        return False
    parsed = _parse_fetch_envelope(payload)
    if parsed is None:
        return False
    text, items = parsed
    if not _is_literal_free_metadata(parsed):
        return False
    required = [item for item in items if item.name == wanted and (
        text[item.value_start:item.value_start + 1] == "(" if wanted == "BODYSTRUCTURE"
        else text[item.value_start:item.value_start + 1] == '"')]
    return len(required) == 1


def _has_top_level_section(payload: bytes, section: str, origin: int | None, *,
                           prefix_length: int, literal_length: int) -> bool:
    """Match the literal-adjacent top-level BODY section item for one tuple."""
    parsed = _parse_fetch_envelope(payload)
    if parsed is None:
        return False
    text, items = parsed
    expected_section = section.upper()
    matching = []
    for item in items:
        if item.name not in {"BODY", "BODY.PEEK"} or item.section is None or item.section.upper() != expected_section:
            continue
        if (origin is None and item.origin not in {None, 0}) or (origin is not None and item.origin != origin):
            continue
        matching.append(item)
    if len(matching) != 1:
        return False
    target = matching[0]
    if target.explicit_nil or target.value_start >= prefix_length or target.value_end > prefix_length:
        return False
    # The requested marker must be the final data item before this tuple's
    # literal.  Later continuation bytes can legally contain UID/closing
    # syntax, but another prefix item (NIL/BODY/etc.) cannot share it.
    markers = _envelope_literal_markers(text)
    if markers is None:
        return False
    if markers:
        if any(item.section is not None and not (item.explicit_nil or item.literal_length is not None)
               for item in items):
            return False
        if target.value_end != prefix_length:
            return False
        return (len(markers) == 1 and target.literal_length == markers[0][2] and
                target.value_end == markers[0][1] and markers[0][1] == prefix_length and
                target.literal_length == literal_length)
    if text[target.value_end:prefix_length] not in {"", ")"}:
        return False
    # Old imaplib/FakeIMAP forms without a {N} marker can only be accepted for
    # one target section immediately before this tuple's literal boundary.
    sections = [item for item in items if item.name in {"BODY", "BODY.PEEK"} and item.section is not None]
    return len(sections) == 1


def _tuple_metadata_literal_candidates(item: Any) -> tuple[tuple[bytes, bytes], ...]:
    """Return both possible imaplib metadata/literal directions for validation."""
    if not isinstance(item, tuple):
        return ()
    byte_parts = [part for part in item if _bytes(part) is not None]
    if len(byte_parts) != 2:
        return ()
    first, second = byte_parts
    # The common metadata-first and server-specific literal-first forms are
    # both supported.  If content resembles a FETCH prefix, position alone is
    # not evidence: the caller must validate each direction completely.
    return ((_bytes(first), _bytes(second)), (_bytes(second), _bytes(first)))


def _metadata_with_continuations(items: list[Any], index: int, prefix: bytes) -> bytes:
    """Retain only flat continuations belonging to the immediately prior FETCH."""
    metadata_parts = [prefix]
    for continuation in items[index + 1:]:
        if isinstance(continuation, tuple):
            break
        payload = _bytes(continuation)
        if payload is None or _is_fetch_metadata(payload):
            break
        metadata_parts.append(payload)
    # imaplib has already separated literal bytes from their protocol prefix;
    # continuation fragments retain their exact wire boundary.  Inserting an
    # SP here would turn a malformed ``...{N}UID`` into a valid next item.
    return b"".join(metadata_parts)


def _select_uid_literal(data: Iterable[Any] | None, requested_uid: str, section: str,
                        origin: int | None, *, maximum: int | None = None,
                        expected_length: int | None = None) -> UidFetch | None:
    """Bind one requested top-level BODY item to exactly one tuple literal."""
    items = list(data or ())
    matches: list[UidFetch] = []
    for index, item in enumerate(items):
        for metadata_part, raw in _tuple_metadata_literal_candidates(item):
            if not _is_fetch_metadata(metadata_part):
                continue
            if maximum is not None and len(raw) > maximum:
                continue
            if expected_length is not None and len(raw) != expected_length:
                continue
            metadata = _metadata_with_continuations(items, index, metadata_part)
            if (_protocol_uid(metadata) == requested_uid and
                    _has_top_level_section(metadata, section, origin,
                                           prefix_length=len(metadata_part), literal_length=len(raw))):
                matches.append(UidFetch(raw=raw, metadata=metadata))
    return matches[0] if len(matches) == 1 else None


def select_uid_fetch(data: Iterable[Any] | None, requested_uid: str) -> UidFetch | None:
    """Select a literal only when its *own* metadata names ``requested_uid``.

    IMAP servers may interleave unsolicited FETCH data.  Never choose the first
    tuple: choose the tuple carrying an exact UID token.  Metadata may be on
    either side of the literal and trailing non-tuple metadata is retained for
    fields such as INTERNALDATE.
    """
    return _select_uid_literal(data, requested_uid, "", None)


def select_uid_metadata(data: Iterable[Any] | None, requested_uid: str, *, required_item: str | None = None) -> bytes | None:
    """Select one literal-free metadata row with its required data item."""
    matches: list[bytes] = []
    for item in data or ():
        values = item if isinstance(item, tuple) else (item,)
        payloads = [payload for value in values if (payload := _bytes(value)) is not None]
        if len(payloads) != 1:  # any second bytes object is an unsolicited literal
            continue
        payload = payloads[0]
        if (_metadata_uid(payload) == requested_uid and
                _has_required_metadata_item(payload, required_item)):
            matches.append(payload)
    return matches[0] if len(matches) == 1 else None


def select_uid_section(data: Iterable[Any] | None, requested_uid: str, section: str,
                       *, origin: int = 0, maximum: int | None = None,
                       expected_length: int | None = None) -> UidFetch | None:
    """Select only the literal for one UID and exactly requested MIME section."""
    return _select_uid_literal(data, requested_uid, section, origin,
                               maximum=maximum, expected_length=expected_length)


def uid_fetch_exists(data: Iterable[Any] | None, requested_uid: str) -> bool:
    """Return whether a FETCH response explicitly names one requested UID."""
    for item in data or ():
        if isinstance(item, tuple):
            values = [payload for value in item if (payload := _bytes(value)) is not None]
            if len(values) != 1:
                continue
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
