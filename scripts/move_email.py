#!/usr/bin/env python3
"""Preview and safely move/delete messages by UID."""
from __future__ import annotations

import pathlib
import sys
from email.header import decode_header
from email.parser import BytesParser

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from qqmail_core.config import CredentialError, Credentials, load_credentials
from qqmail_core.confirmation import confirmation_matches, move_manifest
from qqmail_core.connections import imap_connection
from qqmail_core.imap_uid import refresh_capabilities, select_uid_fetch
from qqmail_core.folders import FolderError, choose_trash_folder, parse_list_response, quote_mailbox
from qqmail_core.mailref import MailRef, MailRefError, select_verified_mailref
from qqmail_core.results import (ArgumentParseError, StructuredArgumentParser,
                                 argument_error_result, batch_result, emit_json,
                                 error_result)


def decode_str(value):
    return "".join(part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else part
                   for part, charset in decode_header(value or ""))


def _trash_folder(mail):
    status, rows = mail.list()
    if status != "OK":
        raise MailRefError("无法枚举文件夹以确定垃圾箱")
    trash = choose_trash_folder(parse_list_response(row) for row in rows or [])
    if not trash:
        raise MailRefError("未找到垃圾箱，请使用 --dst_folder")
    return trash.wire_name


def _raw(data, uid):
    response = select_uid_fetch(data, uid)
    return response.raw if response else None


def preview_emails(mail, references):
    previews, failed = [], []
    for reference in references:
        try:
            select_verified_mailref(mail, reference, readonly=True)
            status, data = mail.uid("FETCH", reference.uid, "(BODY.PEEK[])")
            raw = _raw(data, reference.uid) if status == "OK" else None
            if raw is None:
                raise MailRefError("UID FETCH失败")
            message = BytesParser().parsebytes(raw)
            previews.append({**reference.public_dict(), "subject": decode_str(message.get("Subject")) or "(无主题)",
                             "sender": decode_str(message.get("From")), "date": message.get("Date", "")})
        except Exception as exc:
            failed.append({**reference.public_dict(), "message": str(exc) or "无法预览邮件"})
    return previews, failed


def _move_one(mail, reference, destination, use_move):
    """Return a precise per-message state after UID MOVE or safe UIDPLUS fallback."""
    select_verified_mailref(mail, reference, readonly=False)
    if use_move:
        try:
            status, _ = mail.uid("MOVE", reference.uid, quote_mailbox(destination))
        except Exception as exc:
            return {**reference.public_dict(), "status": "error", "message": str(exc) or "UID MOVE异常",
                    "final_state": "indeterminate_after_move_exception"}
        if status == "OK":
            return {**reference.public_dict(), "status": "success", "final_state": "moved"}
        return {**reference.public_dict(), "status": "error", "message": "UID MOVE失败",
                "final_state": "source_unchanged"}
    try:
        status, _ = mail.uid("COPY", reference.uid, quote_mailbox(destination))
    except Exception as exc:
        return {**reference.public_dict(), "status": "error", "message": str(exc) or "UID COPY异常",
                "final_state": "indeterminate_after_copy_exception"}
    if status != "OK":
        return {**reference.public_dict(), "status": "error", "message": "UID COPY失败",
                "final_state": "source_unchanged"}
    try:
        status, _ = mail.uid("STORE", reference.uid, "+FLAGS", "(\\Deleted)")
    except Exception as exc:
        return {**reference.public_dict(), "status": "error", "message": str(exc) or "UID STORE异常",
                "final_state": "copied_destination_source_state_unknown"}
    if status != "OK":
        return {**reference.public_dict(), "status": "error", "message": "UID STORE失败",
                "final_state": "copied_destination_source_unchanged"}
    try:
        status, _ = mail.uid("EXPUNGE", reference.uid)
    except Exception as exc:
        return {**reference.public_dict(), "status": "error", "message": str(exc) or "UID EXPUNGE异常",
                # An exception can arrive after the server applied UID EXPUNGE.
                # Do not assert that the source remains merely marked deleted.
                "final_state": "copied_destination_source_expunge_indeterminate"}
    if status != "OK":
        return {**reference.public_dict(), "status": "error", "message": "UID EXPUNGE失败",
                "final_state": "copied_destination_source_marked_deleted"}
    return {**reference.public_dict(), "status": "success", "final_state": "moved"}


def move_emails(email_addr, auth_code, mail_ids, src_folder="INBOX", dst_folder=None, delete=False,
                confirm=False, uidvalidity=None, confirmation=None):
    try:
        references = list({ref.uid: ref for ref in (MailRef(src_folder, uidvalidity, value) for value in mail_ids)}.values())
        if not references:
            raise MailRefError("邮件编号不能为空")
        if confirm and not confirmation:
            raise MailRefError("--confirm 必须同时提供 --confirmation")
        if delete and dst_folder:
            raise MailRefError("--delete 与 --dst_folder 不能同时使用")
        if delete and not dst_folder:
            # It is intentionally deferred until inside the connection, but no mutation happens first.
            pass
        elif not dst_folder:
            raise MailRefError("请指定 --dst_folder 或 --delete")
        if dst_folder == src_folder:
            raise MailRefError("源文件夹和目标文件夹不能相同")
        if dst_folder:
            quote_mailbox(dst_folder)
    except (MailRefError, FolderError) as exc:
        return error_result(str(exc), code="invalid_mailref")
    try:
        with imap_connection(Credentials(email_addr, auth_code)) as mail:
            destination = _trash_folder(mail) if delete else dst_folder
            if destination == src_folder:
                return error_result("源文件夹和目标文件夹不能相同", code="same_source_destination")
            manifest = move_manifest(action="delete" if delete else "move", source_folder=src_folder,
                                     destination_folder=destination, references=references)
            previews, preview_failed = preview_emails(mail, references)
            if not confirm:
                status = "preview" if not preview_failed else "partial" if previews else "error"
                return {"status": status, "action": "删除" if delete else "移动", "src_folder": src_folder,
                        "dst_folder": destination, "uidvalidity": uidvalidity, "emails": previews,
                        "failed": preview_failed, "confirmation": manifest["confirmation"], "manifest": manifest,
                        "message": "预览完成；请以 --confirm --confirmation <摘要> 确认"}
            if preview_failed:
                return {"status": "error", "message": "预览未完整成功，已停止移动", "code": "preview_failed",
                        "failed": preview_failed, "emails": previews}
            if not confirmation or not confirmation_matches(manifest, confirmation):
                return error_result("confirmation 与当前操作清单不匹配", code="confirmation_mismatch")
            capabilities = refresh_capabilities(mail)
            if "MOVE" not in capabilities and "UIDPLUS" not in capabilities:
                return error_result("服务器不支持安全 UID 移动", code="safe_move_unsupported")
            status, _ = mail.select(quote_mailbox(destination), readonly=True)
            if status != "OK":
                return error_result("无法访问目标文件夹，已停止移动", code="destination_unavailable")
            use_move = "MOVE" in capabilities
            outcomes = []
            for reference in references:
                try:
                    outcomes.append(_move_one(mail, reference, destination, use_move))
                except Exception as exc:
                    outcomes.append({**reference.public_dict(), "status": "error", "message": str(exc),
                                     "final_state": "source_unchanged"})
        good = [item for item in outcomes if item["status"] == "success"]
        bad = [item for item in outcomes if item["status"] != "success"]
        return batch_result(succeeded=good, failed=bad, action="删除" if delete else "移动",
                            src_folder=src_folder, dst_folder=destination, uidvalidity=uidvalidity,
                            emails=previews, results=outcomes, confirmation=manifest["confirmation"])
    except Exception:
        return error_result("IMAP连接或认证失败", code="imap_error")


def main():
    parser = StructuredArgumentParser(description="移动或删除QQ邮箱邮件（UID）")
    parser.add_argument("--mail_ids", required=True)
    parser.add_argument("--src_folder", required=True)
    parser.add_argument("--uidvalidity", required=True)
    parser.add_argument("--dst_folder")
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--confirmation")
    try:
        args = parser.parse_args()
        values = [value.strip() for value in args.mail_ids.split(",") if value.strip()]
        if not values:
            raise MailRefError("邮件编号不能为空")
        [MailRef(args.src_folder, args.uidvalidity, value) for value in values]
        if args.delete and args.dst_folder:
            raise MailRefError("--delete 与 --dst_folder 不能同时使用")
        if not args.delete and not args.dst_folder:
            raise MailRefError("请指定 --dst_folder 或 --delete")
        if args.confirm and not args.confirmation:
            raise MailRefError("--confirm 必须同时提供 --confirmation")
        if args.dst_folder:
            quote_mailbox(args.dst_folder)
    except ArgumentParseError as exc:
        return emit_json(argument_error_result(str(exc)))
    except (MailRefError, FolderError) as exc:
        return emit_json(error_result(str(exc), code="invalid_mailref"))
    try:
        credentials = load_credentials()
    except CredentialError as exc:
        return emit_json(error_result(str(exc), code="missing_credentials"))
    return emit_json(move_emails(credentials.email, credentials.auth_code, values, args.src_folder,
                                 args.dst_folder, args.delete, args.confirm, args.uidvalidity,
                                 args.confirmation))


if __name__ == "__main__":
    raise SystemExit(main())
