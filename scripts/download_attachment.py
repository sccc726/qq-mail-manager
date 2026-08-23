#!/usr/bin/env python3
"""Download attachments from messages addressed by IMAP UID."""
from __future__ import annotations

import os
import pathlib
import re
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
    decoded = []
    for part, charset in decode_header(value):
        decoded.append(part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else part)
    return "".join(decoded)


def safe_filename(filename):
    name = os.path.basename(filename)
    if not name or name.strip(".") == "":
        return "attachment"
    name = re.sub(r'[<>:"/\\|?*]', "_", name).replace("..", "_")
    return name if name and name.strip(".") else "attachment"


def _raw(data, uid):
    response = select_uid_fetch(data, uid)
    return response.raw if response else None


def download_attachments_for_mail(mail, reference, output_dir=".", target_file=None):
    """Fetch a UID and return a reference-bearing per-message outcome."""
    try:
        select_verified_mailref(mail, reference, readonly=True)
        status, data = mail.uid("FETCH", reference.uid, "(BODY.PEEK[])")
        raw = _raw(data, reference.uid) if status == "OK" else None
        if raw is None:
            raise MailRefError("无法获取邮件内容")
        message = BytesParser().parsebytes(raw)
        downloaded, available = [], []
        for part in message.walk():
            filename = part.get_filename()
            if not filename:
                continue
            filename = decode_str(filename)
            available.append(filename)
            if target_file and filename != target_file:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            name = safe_filename(filename)
            destination = pathlib.Path(output_dir) / name
            suffix = 1
            while destination.exists():
                destination = pathlib.Path(output_dir) / f"{pathlib.Path(name).stem}_{suffix}{pathlib.Path(name).suffix}"
                suffix += 1
            destination.write_bytes(payload)
            downloaded.append({"name": destination.name, "size": len(payload), "path": str(destination.resolve())})
        if target_file and not downloaded:
            raise MailRefError(f"未找到附件: {target_file}")
        return {**reference.public_dict(), "status": "success", "downloaded": downloaded,
                "download_count": len(downloaded), "total_size": sum(item["size"] for item in downloaded)}
    except Exception as exc:
        return {**reference.public_dict(), "status": "error", "message": str(exc) or "下载附件失败"}


def download_attachments(email_addr, auth_code, mail_ids, folder, uidvalidity, output_dir=".", target_file=None):
    try:
        references = [MailRef(folder, uidvalidity, uid) for uid in mail_ids]
    except MailRefError as exc:
        return error_result(str(exc), code="invalid_mailref")
    try:
        pathlib.Path(output_dir).mkdir(parents=True, exist_ok=True)
        with imap_connection(Credentials(email_addr, auth_code)) as mail:
            outcomes = [download_attachments_for_mail(mail, reference, output_dir, target_file)
                        for reference in references]
    except Exception:
        return error_result("IMAP连接或认证失败", code="imap_error")
    good = [item for item in outcomes if item["status"] == "success"]
    bad = [item for item in outcomes if item["status"] != "success"]
    return batch_result(succeeded=good, failed=bad, folder=folder, uidvalidity=uidvalidity,
                        results=outcomes, total_downloaded=sum(item.get("download_count", 0) for item in good),
                        total_size=sum(item.get("total_size", 0) for item in good))


def main():
    parser = StructuredArgumentParser(description="下载QQ邮箱邮件附件（UID）")
    parser.add_argument("--mail_ids", required=True, help="UID，多个逗号分隔")
    parser.add_argument("--folder", required=True)
    parser.add_argument("--uidvalidity", required=True)
    parser.add_argument("--dir", default=".")
    parser.add_argument("--file")
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
    return emit_json(download_attachments(credentials.email, credentials.auth_code, mail_ids,
                                          args.folder, args.uidvalidity, args.dir, args.file))


if __name__ == "__main__":
    raise SystemExit(main())
