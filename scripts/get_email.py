#!/usr/bin/env python3
"""Read message details by stable IMAP UID references."""
from __future__ import annotations

import pathlib
import sys
from email.header import decode_header
from email.parser import BytesParser

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from qqmail_core.config import CredentialError, Credentials, load_credentials
from qqmail_core.connections import imap_connection
from qqmail_core.imap_uid import select_uid_fetch
from qqmail_core.mailref import MailRef, MailRefError, select_verified_mailref
from qqmail_core.results import (ArgumentParseError, StructuredArgumentParser,
                                 argument_error_result, batch_result, emit_json,
                                 error_result)


def decode_str(value):
    if value is None:
        return ""
    result = []
    for part, charset in decode_header(value):
        if isinstance(part, bytes):
            try:
                result.append(part.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                result.append(part.decode("utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def extract_body_and_attachments(message):
    body, attachments = "", []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        filename = part.get_filename()
        if filename:
            attachments.append({"name": decode_str(filename), "type": part.get_content_type()})
        elif not body and part.get_content_type() in {"text/plain", "text/html"}:
            payload = part.get_payload(decode=True)
            if payload is not None:
                body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return body, attachments


def _raw_from_fetch(data, uid):
    response = select_uid_fetch(data, uid)
    return response.raw if response else None


def fetch_single_email(mail, reference: MailRef, parser: BytesParser):
    """Fetch exactly one UID after its selected mailbox is verified."""
    select_verified_mailref(mail, reference, readonly=True)
    status, data = mail.uid("FETCH", reference.uid, "(BODY.PEEK[])")
    raw = _raw_from_fetch(data, reference.uid) if status == "OK" else None
    if raw is None:
        return None
    message = parser.parsebytes(raw)
    body, attachments = extract_body_and_attachments(message)
    return {
        **reference.public_dict(),
        "subject": decode_str(message.get("Subject", "")) or "(无主题)",
        "sender": decode_str(message.get("From", "")),
        "to": decode_str(message.get("To", "")),
        "cc": decode_str(message.get("Cc", "")),
        "date": message.get("Date", ""),
        "body": body,
        "attachments": attachments,
    }


def get_emails(email_addr, auth_code, mail_ids, folder="INBOX", uidvalidity=None):
    """Get one or more messages; ``mail_ids`` are decimal UIDs, never sequences."""
    try:
        references = [MailRef(folder, uidvalidity, uid) for uid in mail_ids]
    except MailRefError as exc:
        return error_result(str(exc), code="invalid_mailref")
    succeeded, failed = [], []
    try:
        with imap_connection(Credentials(email_addr, auth_code)) as mail:
            parser = BytesParser()
            for reference in references:
                try:
                    detail = fetch_single_email(mail, reference, parser)
                    if detail is None:
                        raise MailRefError("无法获取邮件")
                    succeeded.append(detail)
                except Exception as exc:
                    failed.append({**reference.public_dict(), "message": str(exc) or "无法获取邮件"})
        return batch_result(succeeded=succeeded, failed=failed, folder=folder,
                            uidvalidity=references[0].uidvalidity, emails=succeeded,
                            fetched=len(succeeded))
    except Exception:
        return error_result("IMAP连接或认证失败", code="imap_error")


def _references_from_args(args):
    values = [value.strip() for value in args.mail_ids.split(",") if value.strip()]
    if not values:
        raise MailRefError("邮件编号不能为空")
    return [MailRef(args.folder, args.uidvalidity, value) for value in values]


def main():
    parser = StructuredArgumentParser(description="获取QQ邮箱邮件详情（UID）")
    parser.add_argument("--mail_ids", required=True, help="UID，多个逗号分隔")
    parser.add_argument("--folder", required=True, help="邮件所在文件夹")
    parser.add_argument("--uidvalidity", required=True, help="搜索结果中的 UIDVALIDITY")
    try:
        args = parser.parse_args()
        references = _references_from_args(args)  # Validate before credentials or network.
    except ArgumentParseError as exc:
        return emit_json(argument_error_result(str(exc)))
    except MailRefError as exc:
        return emit_json(error_result(str(exc), code="invalid_mailref"))
    try:
        credentials = load_credentials()
    except CredentialError as exc:
        return emit_json(error_result(str(exc), code="missing_credentials"))
    result = get_emails(credentials.email, credentials.auth_code,
                        [reference.uid for reference in references], args.folder,
                        references[0].uidvalidity)
    return emit_json(result)


if __name__ == "__main__":
    raise SystemExit(main())
