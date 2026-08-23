#!/usr/bin/env python3
"""Mark messages read/unread through UID STORE only."""
from __future__ import annotations

import pathlib
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from qqmail_core.config import CredentialError, Credentials, load_credentials
from qqmail_core.connections import imap_connection
from qqmail_core.imap_uid import uid_fetch_exists
from qqmail_core.mailref import MailRef, MailRefError, select_verified_mailref
from qqmail_core.results import (ArgumentParseError, StructuredArgumentParser,
                                 argument_error_result, batch_result, emit_json,
                                 error_result)


def mark_emails(email_addr, auth_code, mail_ids, action, folder="INBOX", uidvalidity=None):
    try:
        references = [MailRef(folder, uidvalidity, item) for item in mail_ids]
    except MailRefError as exc:
        return error_result(str(exc), code="invalid_mailref")
    operation = "+FLAGS" if action == "read" else "-FLAGS"
    action_text = "已读" if action == "read" else "未读"
    succeeded, failed = [], []
    try:
        with imap_connection(Credentials(email_addr, auth_code)) as mail:
            for reference in references:
                try:
                    select_verified_mailref(mail, reference, readonly=False)
                    fetch_status, fetch_data = mail.uid("FETCH", reference.uid, "(UID)")
                    if fetch_status != "OK" or not uid_fetch_exists(fetch_data, reference.uid):
                        raise MailRefError("UID不存在或无法验证")
                    status, _store_data = mail.uid("STORE", reference.uid, operation, "(\\Seen)")
                    if status != "OK":
                        raise MailRefError("UID STORE失败")
                    succeeded.append(reference.public_dict())
                except Exception as exc:
                    failed.append({**reference.public_dict(), "message": str(exc) or "UID STORE失败"})
    except Exception:
        return error_result("IMAP连接或认证失败", code="imap_error")
    return batch_result(succeeded=succeeded, failed=failed, folder=folder, uidvalidity=uidvalidity,
                        action=action_text)


def main():
    parser = StructuredArgumentParser(description="标记QQ邮箱邮件为已读或未读（UID）")
    parser.add_argument("--mail_ids", required=True, help="UID，多个逗号分隔")
    parser.add_argument("--action", required=True, choices=["read", "unread"])
    parser.add_argument("--folder", required=True)
    parser.add_argument("--uidvalidity", required=True)
    try:
        args = parser.parse_args()
        mail_ids = [item.strip() for item in args.mail_ids.split(",") if item.strip()]
        if not mail_ids:
            raise MailRefError("邮件编号不能为空")
        [MailRef(args.folder, args.uidvalidity, item) for item in mail_ids]
    except ArgumentParseError as exc:
        return emit_json(argument_error_result(str(exc)))
    except MailRefError as exc:
        return emit_json(error_result(str(exc), code="invalid_mailref"))
    try:
        credentials = load_credentials()
    except CredentialError as exc:
        return emit_json(error_result(str(exc), code="missing_credentials"))
    return emit_json(mark_emails(credentials.email, credentials.auth_code, mail_ids, args.action,
                                 args.folder, args.uidvalidity))


if __name__ == "__main__":
    raise SystemExit(main())
