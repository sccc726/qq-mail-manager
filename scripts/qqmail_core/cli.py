"""Shared static CLI validation helpers.

These helpers deliberately perform no credential lookup or I/O, so every
caller can reject malformed MailRef CSV values before a network-capable path.
"""
from __future__ import annotations

from .mailref import MailRef, MailRefError


def parse_mailref_csv(folder: str, uidvalidity: str, values: str) -> list[MailRef]:
    """Parse the public ``--mail_ids`` CSV into validated UID references."""
    if not isinstance(values, str):
        raise MailRefError("邮件编号不能为空")
    if any(ord(character) < 32 or ord(character) == 127 for character in values):
        raise MailRefError("邮件编号不能包含控制字符")
    items = [item.strip() for item in values.split(",")]
    if not items or any(not item for item in items):
        raise MailRefError("邮件编号列表不能包含空项")
    return [MailRef(folder, uidvalidity, item) for item in items]
