"""Stable MailRef validation used by the later UID migration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .folders import FolderError, quote_mailbox


MAX_UINT32 = 4_294_967_295


class MailRefError(ValueError):
    """Raised before credentials or network are touched for invalid references."""


def _parse_uint32(value: Any, label: str) -> str:
    if isinstance(value, bool):
        raise MailRefError(f"{label}必须是十进制整数")
    text = str(value) if isinstance(value, int) else value
    if not isinstance(text, str) or not text or not text.isascii() or not text.isdecimal():
        raise MailRefError(f"{label}必须是十进制整数")
    if text[0] == "0" or int(text) > MAX_UINT32:
        raise MailRefError(f"{label}必须介于 1 和 {MAX_UINT32} 之间")
    return text


def parse_uid(value: Any) -> str:
    """Accept only one canonical nonzero UID, never an IMAP sequence-set."""
    return _parse_uint32(value, "UID")


def parse_uidvalidity(value: Any) -> str:
    return _parse_uint32(value, "UIDVALIDITY")


@dataclass(frozen=True)
class MailRef:
    folder: str
    uidvalidity: str
    uid: str

    def __post_init__(self) -> None:
        try:
            quote_mailbox(self.folder)
        except FolderError as exc:
            raise MailRefError(str(exc)) from exc
        object.__setattr__(self, "uidvalidity", parse_uidvalidity(self.uidvalidity))
        object.__setattr__(self, "uid", parse_uid(self.uid))

    @property
    def mail_id(self) -> str:
        return self.uid

    def public_dict(self) -> dict[str, str]:
        return {"folder": self.folder, "uidvalidity": self.uidvalidity, "mail_id": self.uid}


def select_verified_mailref(client: Any, reference: MailRef, *, readonly: bool = True) -> None:
    """Select the folder and stop before UID operations if UIDVALIDITY changed."""
    status, _ = client.select(quote_mailbox(reference.folder), readonly=readonly)
    if status != "OK":
        raise MailRefError(f"无法访问文件夹: {reference.folder}")
    _key, values = client.response("UIDVALIDITY")
    actual = values[0].decode() if values and isinstance(values[0], bytes) else values[0] if values else None
    if parse_uidvalidity(actual) != reference.uidvalidity:
        raise MailRefError("UIDVALIDITY 不匹配，已停止邮件操作")
