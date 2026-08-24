"""IMAP folder names, LIST parsing, and special-use mailbox selection."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Iterable


class FolderError(ValueError):
    """Raised for unsafe or malformed folder input."""


@dataclass(frozen=True)
class Folder:
    wire_name: str
    display_name: str
    attributes: tuple[str, ...] = ()
    delimiter: str | None = None
    raw: str = ""

    @property
    def is_trash(self) -> bool:
        return any(attribute.casefold() == "\\trash" for attribute in self.attributes)

    def public_dict(self) -> dict[str, str]:
        """The established list_folders fields, kept intentionally unchanged."""
        return {"name": self.wire_name, "display": self.display_name, "raw": self.raw}


def _reject_controls(value: str, label: str = "文件夹名") -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise FolderError(f"{label}不能为空")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise FolderError(f"{label}不能包含控制字符")
    return value


def decode_modified_utf7(value: str) -> str:
    """Decode RFC 3501 modified UTF-7, retaining malformed fragments verbatim."""
    if not value:
        return value
    result: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "&":
            result.append(value[index])
            index += 1
            continue
        end = value.find("-", index + 1)
        if end < 0:
            result.append(value[index:])
            break
        encoded = value[index + 1:end]
        if not encoded:
            result.append("&")
        else:
            try:
                padded = encoded.replace(",", "/") + "=" * (-len(encoded) % 4)
                result.append(base64.b64decode(padded, validate=True).decode("utf-16-be"))
            except (ValueError, UnicodeError):
                result.append(value[index:end + 1])
        index = end + 1
    return "".join(result)


def encode_modified_utf7(value: str) -> str:
    """Encode a display name into the IMAP wire form without relying on imaplib internals."""
    _reject_controls(value)
    result: list[str] = []
    non_ascii: list[str] = []

    def flush() -> None:
        if non_ascii:
            payload = "".join(non_ascii).encode("utf-16-be")
            encoded = base64.b64encode(payload).decode("ascii").rstrip("=").replace("/", ",")
            result.append("&" + encoded + "-")
            non_ascii.clear()

    for char in value:
        if "\x20" <= char <= "\x7e" and char != "&":
            flush()
            result.append(char)
        elif char == "&":
            flush()
            result.append("&-")
        else:
            non_ascii.append(char)
    flush()
    return "".join(result)


def quote_mailbox(wire_name: str) -> str:
    """Return one safe IMAP mailbox argument, quoting spaces, quotes and backslashes."""
    _reject_controls(wire_name)
    if "\r" in wire_name or "\n" in wire_name:
        raise FolderError("文件夹名不能包含换行")
    escaped = wire_name.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def _parse_quoted(value: str, index: int) -> tuple[str, int]:
    if index >= len(value) or value[index] != '"':
        raise FolderError("LIST 响应缺少引号字符串")
    index += 1
    chars: list[str] = []
    while index < len(value):
        char = value[index]
        if char == '"':
            return "".join(chars), index + 1
        if char == "\\":
            index += 1
            if index >= len(value):
                raise FolderError("LIST 响应中的转义不完整")
            chars.append(value[index])
        else:
            chars.append(char)
        index += 1
    raise FolderError("LIST 响应中的引号字符串未结束")


def _parse_mailbox_name(value: str, index: int) -> tuple[str, int]:
    """Parse a LIST mailbox name as quoted string or atom; literals stay unsupported."""
    if index >= len(value):
        raise FolderError("LIST 响应缺少邮箱名")
    if value[index] == '"':
        return _parse_quoted(value, index)
    if value[index] == "{":
        raise FolderError("LIST literal 邮箱名暂不支持")
    end = index
    while end < len(value) and not value[end].isspace():
        end += 1
    atom = value[index:end]
    if not atom:
        raise FolderError("LIST 响应缺少邮箱名")
    if any(char in '(){ %*"\\]' for char in atom):
        raise FolderError("LIST 响应中的邮箱 atom 非法")
    _reject_controls(atom)
    return atom, end


def parse_list_response(response: bytes | str) -> Folder:
    """Parse a single IMAP LIST response into a wire/display folder model."""
    raw = response.decode("utf-8", errors="replace") if isinstance(response, bytes) else str(response)
    if not raw.startswith("("):
        raise FolderError("LIST 响应缺少属性列表")
    close = raw.find(")")
    if close < 0:
        raise FolderError("LIST 响应属性列表未结束")
    attributes = tuple(part for part in raw[1:close].split() if part)
    index = close + 1
    while index < len(raw) and raw[index].isspace():
        index += 1
    if raw[index:index + 3].upper() == "NIL":
        delimiter, index = None, index + 3
    else:
        delimiter, index = _parse_quoted(raw, index)
    while index < len(raw) and raw[index].isspace():
        index += 1
    wire_name, index = _parse_mailbox_name(raw, index)
    if raw[index:].strip():
        raise FolderError("LIST 响应包含未解析内容")
    _reject_controls(wire_name)
    return Folder(wire_name=wire_name, display_name=decode_modified_utf7(wire_name),
                  attributes=attributes, delimiter=delimiter, raw=raw)


_TRASH_FALLBACK_NAMES = frozenset({"trash", "deleted messages", "deleted items", "垃圾箱", "已删除邮件"})


def _is_controlled_trash_fallback(folder: Folder) -> bool:
    """Accept a finite root/INBOX leaf name, never an arbitrary nested leaf."""
    for value in (folder.wire_name, folder.display_name):
        components = value.split(folder.delimiter) if folder.delimiter else [value]
        if len(components) == 1:
            leaf = components[0]
        elif len(components) == 2 and components[0].casefold() == "inbox":
            leaf = components[1]
        else:
            continue
        if leaf.casefold() in _TRASH_FALLBACK_NAMES:
            return True
    return False


def choose_trash_folder(folders: Iterable[Folder]) -> Folder | None:
    """Prefer SPECIAL-USE \\Trash; use a finite, deterministic name fallback only."""
    choices = list(folders)
    for folder in choices:
        if folder.is_trash:
            return folder
    fallbacks = [folder for folder in choices if _is_controlled_trash_fallback(folder)]
    return fallbacks[0] if len(fallbacks) == 1 else None
