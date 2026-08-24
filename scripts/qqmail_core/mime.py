"""Bounded, defensive MIME helpers shared by mail readers.

The list view deliberately works from IMAP headers and one selected text part;
it must never turn a message listing into an attachment download.
"""
from __future__ import annotations

import re
import base64
import quopri
from urllib.parse import unquote_to_bytes
from email.header import decode_header
from email.message import Message
from typing import Any, Iterator


def decode_header_value(value: Any) -> str:
    """Decode an RFC 2047 header without allowing an unknown charset to fail."""
    if value is None:
        return ""
    result: list[str] = []
    for part, charset in decode_header(value):
        if isinstance(part, bytes):
            try:
                result.append(part.decode(charset or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                result.append(part.decode("utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def decode_text(payload: bytes | str | None, charset: str | None = None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    try:
        return payload.decode(charset or "utf-8", errors="replace")
    except (LookupError, TypeError):
        return payload.decode("utf-8", errors="replace")


def decode_transfer(payload: bytes, encoding: str | None, *, partial: bool = False) -> bytes:
    """Decode one bounded MIME section before applying its declared charset."""
    normalized = (encoding or "").lower()
    if normalized == "base64":
        compact = re.sub(rb"\s+", b"", payload)
        remainder = len(compact) % 4
        if remainder:
            if not partial:
                raise ValueError("base64正文编码截断")
            compact = compact[:len(compact) - remainder]
        return base64.b64decode(compact, validate=True) if compact else b""
    if normalized in {"quoted-printable", "quopri"}:
        payload = _validate_quoted_printable(payload, partial=partial)
        return quopri.decodestring(payload)
    return payload


def _validate_quoted_printable(payload: bytes, *, partial: bool) -> bytes:
    """Validate QP escapes, allowing only a final partial escape for previews."""
    index = 0
    while index < len(payload):
        if payload[index] != ord("="):
            index += 1
            continue
        remaining = len(payload) - index
        if remaining >= 3 and payload[index + 1:index + 3] == b"\r\n":
            index += 3
            continue
        if remaining >= 2 and payload[index + 1:index + 2] == b"\n":
            index += 2
            continue
        if (remaining >= 3 and chr(payload[index + 1]) in "0123456789abcdefABCDEF"
                and chr(payload[index + 2]) in "0123456789abcdefABCDEF"):
            index += 3
            continue
        # A bounded preview may end midway through one otherwise-valid hex or
        # CRLF soft-break escape.  Never discard an invalid escape in the
        # middle of a complete literal.
        tail = payload[index:]
        if partial and (tail == b"=" or
                        (len(tail) == 2 and tail[:1] == b"=" and tail[1:2] in b"0123456789abcdefABCDEF") or
                        tail == b"=\r"):
            return payload[:index]
        raise ValueError("quoted-printable正文编码无效或截断")
    return payload


def extract_body_and_attachments(message: Message) -> tuple[str, list[dict[str, str]]]:
    """Keep the established detail-view body and attachment metadata contract."""
    body, attachments = "", []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        filename = part.get_filename()
        if filename:
            attachments.append({"name": decode_header_value(filename), "type": part.get_content_type()})
        elif not body and part.get_content_type() in {"text/plain", "text/html"}:
            payload = part.get_payload(decode=True)
            if payload is not None:
                body = decode_text(payload, part.get_content_charset())
    return body, attachments


def preview_text(payload: bytes | str | None, charset: str | None = None, *, max_len: int = 150,
                 max_bytes: int = 8192) -> str:
    """Decode only a server-bounded literal and cap the public preview."""
    if isinstance(payload, bytes):
        payload = payload[:max_bytes]
    elif isinstance(payload, str):
        payload = payload.encode("utf-8", errors="replace")[:max_bytes]
    return decode_text(payload, charset)[:max_len]


_BODYSTRUCTURE = re.compile(r"\bBODYSTRUCTURE\s+", re.IGNORECASE)


def _tokens(value: str) -> Iterator[str]:
    """Small IMAP S-expression lexer sufficient for BODYSTRUCTURE responses."""
    index = 0
    while index < len(value):
        char = value[index]
        if char.isspace():
            index += 1
        elif char in "()":
            yield char
            index += 1
        elif char == '"':
            index += 1
            out = []
            while index < len(value) and value[index] != '"':
                if value[index] == "\\" and index + 1 < len(value):
                    index += 1
                out.append(value[index])
                index += 1
            index += index < len(value)
            yield "".join(out)
        else:
            end = index
            while end < len(value) and not value[end].isspace() and value[end] not in "()":
                end += 1
            yield value[index:end]
            index = end


def _parse_sexpr(tokens: Iterator[str]) -> Any:
    values = list(tokens)
    if not values or len(values) > 2_000:
        raise ValueError("BODYSTRUCTURE超出限制")
    root: Any = None
    stack: list[list[Any]] = []
    for token in values:
        if token == "(":
            if len(stack) >= 64:
                raise ValueError("BODYSTRUCTURE嵌套过深")
            node: list[Any] = []
            if stack:
                stack[-1].append(node)
            elif root is not None:
                raise ValueError("BODYSTRUCTURE尾部无效")
            else:
                root = node
            stack.append(node)
        elif token == ")":
            if not stack:
                raise ValueError("BODYSTRUCTURE尾部无效")
            stack.pop()
        elif stack:
            stack[-1].append(token)
        else:
            raise ValueError("BODYSTRUCTURE尾部无效")
    if stack or root is None:
        raise ValueError("不完整的 BODYSTRUCTURE")
    if not isinstance(root, list):
        raise ValueError("BODYSTRUCTURE尾部无效")
    return root


def _bodystructure_value(metadata: bytes | str) -> str | None:
    text = metadata.decode("utf-8", errors="replace") if isinstance(metadata, bytes) else str(metadata)
    match = _BODYSTRUCTURE.search(text)
    if not match:
        return None
    start = text.find("(", match.end())
    if start < 0:
        return None
    depth, quoted, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _is_attachment(node: list[Any]) -> bool:
    # For single-part bodies the disposition appears after the line-count and
    # MD5 slots.  Searching nested extension fields is intentionally tolerant
    # of broken but common server BODYSTRUCTURE variants.
    def strings(item: Any) -> Iterator[str]:
        if isinstance(item, list):
            for child in item:
                yield from strings(child)
        elif isinstance(item, str):
            yield item.upper()
    return "ATTACHMENT" in set(strings(node[7:]))


def plain_text_part(metadata: bytes | str) -> str | None:
    """Return a safe text/plain MIME section from a BODYSTRUCTURE response.

    ``None`` means either no usable plain part or malformed structure.  In both
    cases callers must omit the preview rather than fetching BODY[]/TEXT.
    """
    raw = _bodystructure_value(metadata)
    if not raw or len(raw) > 64 * 1024:
        return None
    try:
        token_values = list(_tokens(raw))
        if len(token_values) > 2_000:
            return None
        root = _parse_sexpr(iter(token_values))
    except (StopIteration, ValueError):
        return None

    def visit(node: Any, path: tuple[int, ...] = ()) -> str | None:
        if not isinstance(node, list) or not node:
            return None
        if isinstance(node[0], list):  # multipart: children precede subtype
            for index, child in enumerate(node):
                if not isinstance(child, list):
                    break
                found = visit(child, path + (index + 1,))
                if found:
                    return found
            return None
        if len(node) >= 2 and str(node[0]).upper() == "TEXT" and str(node[1]).upper() == "PLAIN":
            return None if _is_attachment(node) else ".".join(map(str, path or (1,)))
        return None

    return visit(root)


def preferred_body_part(metadata: bytes | str) -> dict[str, str | int | None] | None:
    parts = bodystructure_parts(metadata)
    for content_type in ("text/plain", "text/html"):
        match = next((part for part in parts if not part["attachment"] and part["type"] == content_type), None)
        if match:
            return match
    return None


def bodystructure_parts(metadata: bytes | str) -> list[dict[str, str | int | None]]:
    """Return bounded metadata for leaf MIME parts; never fetches a literal."""
    raw = _bodystructure_value(metadata)
    if not raw or len(raw) > 64 * 1024:
        return []
    try:
        tokens = list(_tokens(raw))
        if len(tokens) > 2_000:
            return []
        root = _parse_sexpr(iter(tokens))
    except (StopIteration, ValueError):
        return []

    def pairs(value: Any) -> dict[str, str]:
        if not isinstance(value, list):
            return {}
        result: dict[str, str] = {}
        for index in range(0, len(value) - 1, 2):
            key = str(value[index]).upper()
            if key in result:
                # RFC 2231 duplicate segments are ambiguous.  Do not choose
                # one silently, especially for a filename used on disk.
                return {}
            result[key] = str(value[index + 1])
        return result

    def filename_param(values: dict[str, str], *names: str) -> str | None:
        for name in names:
            direct = values.get(name)
            extended = values.get(name + "*")
            segments: dict[int, tuple[str, bool]] = {}
            for key, value in values.items():
                match = re.fullmatch(re.escape(name) + r"\*(\d+)(\*)?", key)
                if match:
                    number = int(match.group(1))
                    if number in segments:
                        return None
                    segments[number] = (value, bool(match.group(2)))
            chunks = None
            if segments:
                indexes = sorted(segments)
                if indexes != list(range(len(indexes))):
                    return None
                chunks = [segments[index] for index in indexes]
                value = "".join(chunk[0] for chunk in chunks)
            else:
                value = extended or direct
            if value is None:
                continue
            if chunks is not None and all(chunk[1] for chunk in chunks):
                if value.count("'") < 2:
                    return None
                charset, _language, value = value.split("'", 2)
                try:
                    return unquote_to_bytes(value).decode(charset or "utf-8", errors="replace")
                except LookupError:
                    return unquote_to_bytes(value).decode("utf-8", errors="replace")
            if extended is not None:
                if "'" in value:
                    charset, _language, value = value.split("'", 2)
                    try:
                        return unquote_to_bytes(value).decode(charset or "utf-8", errors="replace")
                    except LookupError:
                        return unquote_to_bytes(value).decode("utf-8", errors="replace")
                return unquote_to_bytes(value).decode("utf-8", errors="replace")
            if chunks is not None and any(chunk[1] for chunk in chunks):
                # Mixed encoded/unencoded continuations have ambiguous byte
                # semantics.  Reject rather than percent-decoding literal
                # pieces or guessing a character set.
                return None
            return value
        return None

    found: list[dict[str, str | int | None]] = []
    def visit(node: Any, path: tuple[int, ...] = ()) -> None:
        if not isinstance(node, list) or not node:
            return
        if isinstance(node[0], list):
            for index, child in enumerate(node):
                if not isinstance(child, list):
                    break
                visit(child, path + (index + 1,))
            return
        if len(node) < 7:
            return
        params = pairs(node[2] if len(node) > 2 else None)
        major_type = str(node[0]).upper()
        is_text = major_type == "TEXT"
        # RFC 3501 TEXT adds line-count; MESSAGE/rfc822/global additionally
        # carries envelope, nested body and line-count before MD5/disposition.
        disposition_index = 9 if is_text else 11 if major_type == "MESSAGE" else 8
        disposition = node[disposition_index] if len(node) > disposition_index else None
        disposition_type = str(disposition[0]).upper() if isinstance(disposition, list) and disposition else ""
        disposition_params = pairs(disposition[1] if isinstance(disposition, list) and len(disposition) > 1 else None)
        filename = filename_param(disposition_params, "FILENAME") or filename_param(params, "FILENAME", "NAME")
        try:
            size = int(node[6])
        except (TypeError, ValueError):
            size = None
        found.append({"section": ".".join(map(str, path or (1,))),
                      "type": f"{node[0]}/{node[1]}".lower(), "filename": filename,
                      "encoding": str(node[5]).lower(), "charset": params.get("CHARSET"), "size": size,
                      "attachment": bool(filename) or disposition_type == "ATTACHMENT"})
    visit(root)
    return found
