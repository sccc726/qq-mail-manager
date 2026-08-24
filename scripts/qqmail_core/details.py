#!/usr/bin/env python3
"""Read message details by stable IMAP UID references."""
from __future__ import annotations

from email.parser import BytesParser
from .config import CredentialError, Credentials, load_credentials
from .connections import imap_connection
from .cli import parse_mailref_csv
from .mailref import MailRef, MailRefError, select_verified_mailref
from .mime import (bodystructure_parts, decode_header_value,
                              decode_text, decode_transfer, extract_body_and_attachments)
from .imap_uid import select_uid_section
from .results import (ArgumentParseError, StructuredArgumentParser,
                                 argument_error_result, batch_result, emit_json,
                                 error_result)


MAX_BODY_BYTES = 64 * 1024


def fetch_single_email(mail, reference: MailRef, parser: BytesParser):
    """Fetch exactly one UID after its selected mailbox is verified."""
    select_verified_mailref(mail, reference, readonly=True)
    status, data = mail.uid("FETCH", reference.uid,
                            "(UID BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO CC DATE)]<0.16384>)")
    header = select_uid_section(data, reference.uid, "HEADER.FIELDS (SUBJECT FROM TO CC DATE)", maximum=16 * 1024) if status == "OK" else None
    if header is None:
        return None
    message = parser.parsebytes(header.raw)
    parts = bodystructure_parts(header.metadata)
    text_parts = [part for part in parts if not part["attachment"] and part["type"] == "text/plain"]
    text_parts += [part for part in parts if not part["attachment"] and part["type"] == "text/html"]
    body = ""
    body_truncated = False
    body_bytes_fetched = 0
    if text_parts:
        section = str(text_parts[0]["section"])
        size = text_parts[0].get("size")
        if not isinstance(size, int) or size < 0:
            raise MailRefError("正文大小无效")
        if size == 0:
            text_parts = []
        else:
            expected = min(size, MAX_BODY_BYTES)
            body_status, body_data = mail.uid("FETCH", reference.uid, f"(UID BODY.PEEK[{section}]<0.{MAX_BODY_BYTES}>)")
            body_response = select_uid_section(body_data, reference.uid, section, maximum=MAX_BODY_BYTES,
                                                expected_length=expected) if body_status == "OK" else None
            if body_response is None:
                raise MailRefError("正文 section FETCH失败")
            body_bytes_fetched = len(body_response.raw)
            body_truncated = size > body_bytes_fetched
            body = decode_text(decode_transfer(body_response.raw, str(text_parts[0].get("encoding") or ""), partial=body_truncated),
                               str(text_parts[0].get("charset") or "utf-8"))
    attachments = [{"name": decode_header_value(part["filename"]) or f"attachment-{part['section']}", "type": part["type"]}
                   for part in parts if part["attachment"]]
    return {
        **reference.public_dict(),
        "subject": decode_header_value(message.get("Subject", "")) or "(无主题)",
        "sender": decode_header_value(message.get("From", "")),
        "to": decode_header_value(message.get("To", "")),
        "cc": decode_header_value(message.get("Cc", "")),
        "date": message.get("Date", ""),
        "body": body,
        "body_truncated": body_truncated,
        "body_bytes_fetched": body_bytes_fetched,
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
    return parse_mailref_csv(args.folder, args.uidvalidity, args.mail_ids)


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
