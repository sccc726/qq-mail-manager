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
    items = [item.strip() for item in values.split(",") if item.strip()]
    if not items:
        raise MailRefError("邮件编号不能为空")
    return [MailRef(folder, uidvalidity, item) for item in items]
