#!/usr/bin/env python3
"""Search QQ Mail with UID SEARCH/FETCH and stable MailRef results."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser

from .config import CredentialError, Credentials, load_credentials
from .connections import imap_connection
from .folders import FolderError, parse_list_response, quote_mailbox
from .imap_uid import select_uid_metadata, select_uid_section
from .mailref import MailRef, MailRefError, parse_uidvalidity
from .mime import decode_header_value, decode_text, decode_transfer, preferred_body_part
from .results import (ArgumentParseError, StructuredArgumentParser,
                                 argument_error_result, emit_json, error_result)

UTC8 = timezone(timedelta(hours=8))
PAGE_SIZE = 15


def parse_date_arg(value):
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%d-%b-%Y")
        except ValueError:
            pass
    raise ValueError("无法解析日期: 支持 YYYY-MM-DD, YYYY/MM/DD, DD-Mon-YYYY")


def parse_recent_arg(value, *, now=None):
    message = "无法解析相对时间: 支持 30m、2h、7d、1w"
    try:
        if not isinstance(value, str):
            raise ValueError(message)
        match = re.fullmatch(r"(\d+)([mhdw])", value.strip().lower())
        if not match:
            raise ValueError(message)
        amount, unit = int(match.group(1)), match.group(2)
        if amount <= 0:
            raise ValueError(message)
        delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount),
                 "d": timedelta(days=amount), "w": timedelta(weeks=amount)}[unit]
        cutoff = (now or datetime.now(UTC8)) - delta
        return (cutoff - timedelta(days=1)).strftime("%d-%b-%Y"), cutoff
    except (ValueError, OverflowError) as exc:
        raise ValueError(message) from exc


def _validate_search_options(recent, limit, offset, *, now=None):
    if offset < 0 or (limit is not None and limit < 0):
        raise ValueError("limit 和 offset 不能为负数")
    if recent is None:
        return None, None
    return parse_recent_arg(recent, now=now)


def _imap_date_datetime(value):
    return datetime.strptime(value, "%d-%b-%Y").replace(tzinfo=UTC8)


def _quoted_term(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}不能为空")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{label}不能包含控制字符")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_search_criteria(query=None, from_addr=None, subject=None, since=None, before=None, seen=None):
    """Build validated IMAP criteria; fuzzy query is run against three fields."""
    filters = []
    if from_addr:
        filters.append("FROM " + _quoted_term(from_addr, "发件人"))
    if subject:
        filters.append("SUBJECT " + _quoted_term(subject, "主题"))
    if since:
        filters.append("SINCE " + since)
    if before:
        filters.append("BEFORE " + before)
    if seen is True:
        filters.append("SEEN")
    elif seen is False:
        filters.append("UNSEEN")
    # ``--from`` has explicit precedence over the broad query per SKILL.md.
    if query and query != "*" and not from_addr:
        term = _quoted_term(query, "查询词")
        return filters, ["SUBJECT " + term, "FROM " + term, "TO " + term]
    return filters or ["ALL"], None


def _folders(mail):
    status, rows = mail.list()
    if status != "OK":
        raise MailRefError("无法枚举文件夹")
    return [parse_list_response(row).wire_name for row in rows or []]


def _uidvalidity(mail):
    _key, values = mail.response("UIDVALIDITY")
    value = values[0].decode() if values and isinstance(values[0], bytes) else values[0] if values else None
    return parse_uidvalidity(value)


def search_in_folder(mail, folder, query=None, from_addr=None, subject=None, since=None, before=None, seen=None):
    status, _ = mail.select(quote_mailbox(folder), readonly=True)
    if status != "OK":
        raise MailRefError(f"无法访问文件夹: {folder}")
    uidvalidity = _uidvalidity(mail)
    base, fuzzy_fields = build_search_criteria(query, from_addr, subject, since, before, seen)
    uid_set = set()
    criteria_groups = ([field, *base] for field in fuzzy_fields) if fuzzy_fields else (base,)
    for criteria in criteria_groups:
        charset = "UTF-8" if any(not value.isascii() for value in criteria) else None
        if charset:
            # IMAP4.uid() concatenates arguments; unlike search(), it does not
            # insert the IMAP CHARSET atom for us.
            status, payload = mail.uid("SEARCH", "CHARSET", charset, *criteria)
        else:
            status, payload = mail.uid("SEARCH", None, *criteria)
        if status != "OK":
            raise MailRefError(f"UID SEARCH失败: {folder}")
        if payload and payload[0]:
            uid_set.update(value.decode() if isinstance(value, bytes) else str(value)
                           for value in payload[0].split())
    return [MailRef(folder, uidvalidity, uid) for uid in uid_set]


def _internaldate(meta):
    text = meta.decode("ascii", errors="replace") if isinstance(meta, bytes) else str(meta)
    match = re.search(r'INTERNALDATE "([^"]+)"', text)
    if not match:
        raise ValueError("UID FETCH响应缺少 INTERNALDATE")
    return datetime.strptime(match.group(1), "%d-%b-%Y %H:%M:%S %z")


def fetch_metadata(mail, reference):
    status, _ = mail.select(quote_mailbox(reference.folder), readonly=True)
    if status != "OK" or _uidvalidity(mail) != reference.uidvalidity:
        raise MailRefError("UIDVALIDITY 不匹配，已停止邮件操作")
    status, data = mail.uid("FETCH", reference.uid, "(UID INTERNALDATE)")
    metadata = select_uid_metadata(data, reference.uid, required_item="INTERNALDATE") if status == "OK" else None
    if metadata is None:
        raise MailRefError("UID FETCH失败")
    return _internaldate(metadata)


def fetch_email_summary(mail, reference):
    status, data = mail.uid("FETCH", reference.uid,
                            "(UID INTERNALDATE BODYSTRUCTURE "
                            "BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)]<0.16384>)")
    response = select_uid_section(data, reference.uid, "HEADER.FIELDS (SUBJECT FROM DATE)", maximum=16 * 1024) if status == "OK" else None
    if response is None:
        raise MailRefError("UID邮件头 FETCH失败")
    internaldate = _internaldate(response.metadata)
    message = BytesParser().parsebytes(response.raw)
    part = preferred_body_part(response.metadata)
    return {**reference.public_dict(), "subject": decode_header_value(message.get("Subject", "")) or "(无主题)",
            "sender": decode_header_value(message.get("From", "")), "date": message.get("Date", ""),
            "preview": "", "internaldate": internaldate.isoformat()}, part


def fetch_preview(mail, reference, part):
    if not part:
        return ""
    # Only final page rows get a preview; this section is selected from
    # BODYSTRUCTURE and never refers to an attachment.
    section = str(part["section"])
    size = part.get("size")
    if not isinstance(size, int) or size < 0:
        raise MailRefError("正文大小无效")
    if size == 0:
        return ""
    expected = min(size, 8192)
    status, data = mail.uid("FETCH", reference.uid, f"(UID BODY.PEEK[{section}]<0.8192>)")
    response = select_uid_section(data, reference.uid, section, maximum=8192, expected_length=expected) if status == "OK" else None
    if response is None:
        raise MailRefError("UID正文片段 FETCH失败")
    return decode_text(decode_transfer(response.raw, str(part.get("encoding") or ""), partial=size > 8192),
                       str(part.get("charset") or "utf-8"))[:150]


def query_emails(email_addr, auth_code, query=None, from_addr=None, subject=None,
                 folder="INBOX", all_folders=False, since=None, before=None,
                 recent=None, seen=None, limit=None, offset=0, now=None):
    try:
        recent_since, recent_cutoff = _validate_search_options(recent, limit, offset, now=now)
        if recent_cutoff is not None:
            if since:
                since = max(_imap_date_datetime(since), _imap_date_datetime(recent_since)).strftime("%d-%b-%Y")
            else:
                since = recent_since
        with imap_connection(Credentials(email_addr, auth_code)) as mail:
            names = _folders(mail) if all_folders else [folder]
            references, failed = [], []
            for name in names:
                try:
                    references.extend(search_in_folder(mail, name, query, from_addr, subject, since, before, seen))
                except Exception as exc:
                    failed.append({"folder": name, "message": str(exc)})
            if failed and not references:
                return {"status": "error", "message": "所有文件夹搜索失败", "code": "imap_search_failed",
                        "failed": failed, "emails": [], "total_matched": 0, "total": 0, "has_more": False}
            total_matched = len(references)
            summaries = []
            for reference in references:
                try:
                    internaldate = fetch_metadata(mail, reference)
                    if not recent_cutoff or internaldate.astimezone(UTC8) >= recent_cutoff:
                        summaries.append((reference, internaldate))
                except Exception as exc:
                    failed.append({**reference.public_dict(), "message": str(exc) or "UID FETCH失败"})
            # Folder + UIDVALIDITY + UID makes both identity and sort ties deterministic.
            summaries.sort(key=lambda pair: (pair[1], pair[0].folder, pair[0].uidvalidity,
                                             int(pair[0].mail_id)), reverse=True)
        # Sort/precisely filter before pagination. ``total_matched`` remains
        # the UID SEARCH cardinality; the displayable count drives paging so a
        # broad SINCE result cannot advertise an empty recent-filtered page.
            total_displayable = len(summaries)
            effective_limit = total_displayable if limit is None else min(limit, total_displayable)
            page_size = min(PAGE_SIZE, max(0, effective_limit - offset))
            page = summaries[offset:offset + page_size]
            displayed = []
            for reference, _date in page:
                try:
                # Re-select / verify the MailRef before every late FETCH.
                    status, _ = mail.select(quote_mailbox(reference.folder), readonly=True)
                    if status != "OK" or _uidvalidity(mail) != reference.uidvalidity:
                        raise MailRefError("UIDVALIDITY 不匹配，已停止邮件操作")
                    item, part = fetch_email_summary(mail, reference)
                    item["preview"] = fetch_preview(mail, reference, part)
                    displayed.append(item)
                except Exception as exc:
                    failed.append({**reference.public_dict(),
                                   "message": str(exc) or "UID正文片段 FETCH失败"})
            has_more = offset + page_size < effective_limit
            result = {"status": "partial" if failed and displayed else "error" if failed and not displayed else "success",
                  "folder": "ALL" if all_folders else folder, "since": since, "before": before,
                  "recent": recent, "seen": seen, "total_matched": total_matched, "total": len(displayed),
                  "total_displayable": total_displayable, "has_more": has_more,
                  "emails": displayed}
            if failed:
                result["failed"] = failed
            if has_more:
                result["next_offset"] = offset + page_size
                result["tip"] = f"还有更多结果，使用 --offset {offset + page_size} 查看下一页"
            if query:
                result["query"] = query
            if from_addr:
                result["from"] = from_addr
            if subject:
                result["subject"] = subject
            return result
    except (ValueError, FolderError, MailRefError) as exc:
        return error_result(str(exc), code="invalid_search")
    except Exception:
        return error_result("IMAP连接或认证失败", code="imap_error")


def main():
    parser = StructuredArgumentParser(description="查询QQ邮箱邮件（UID）")
    parser.add_argument("--query")
    parser.add_argument("--from", dest="from_addr")
    parser.add_argument("--subject")
    parser.add_argument("--folder", default="INBOX")
    parser.add_argument("--all-folders", action="store_true")
    parser.add_argument("--since")
    parser.add_argument("--before")
    parser.add_argument("--recent")
    state = parser.add_mutually_exclusive_group()
    state.add_argument("--seen", action="store_true")
    state.add_argument("--unseen", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    try:
        args = parser.parse_args()
        since = parse_date_arg(args.since) if args.since else None
        before = parse_date_arg(args.before) if args.before else None
        _validate_search_options(args.recent, args.limit, args.offset)
        # Reject injected folder/query data before credentials are read.
        quote_mailbox(args.folder)
        build_search_criteria(args.query, args.from_addr, args.subject, since, before,
                              True if args.seen else False if args.unseen else None)
    except ArgumentParseError as exc:
        return emit_json(argument_error_result(str(exc)))
    except (ValueError, FolderError) as exc:
        return emit_json(error_result(str(exc), code="invalid_search"))
    try:
        credentials = load_credentials()
    except CredentialError as exc:
        return emit_json(error_result(str(exc), code="missing_credentials"))
    return emit_json(query_emails(credentials.email, credentials.auth_code, args.query, args.from_addr,
                                  args.subject, args.folder, args.all_folders, since, before, args.recent,
                                  True if args.seen else False if args.unseen else None, args.limit, args.offset))


if __name__ == "__main__":
    raise SystemExit(main())
