#!/usr/bin/env python3
"""Build, preview-confirm, and send QQ mail without implicit transmission."""
from __future__ import annotations

import hashlib
import pathlib
import re
import smtplib
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from email import encoders, message_from_bytes
from email.header import decode_header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, getaddresses

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from qqmail_core.config import CredentialError, Credentials, load_credentials
from qqmail_core.confirmation import CapturedFile, capture_file, confirmation_matches, file_manifest, send_manifest
from qqmail_core.connections import imap_connection, smtp_connection
from qqmail_core.imap_uid import select_uid_fetch
from qqmail_core.mailref import MailRef, MailRefError, select_verified_mailref
from qqmail_core.results import (ArgumentParseError, StructuredArgumentParser, batch_result,
                                 emit_json, error_result)


class SendInputError(ValueError):
    """Invalid outgoing input, always raised before a credential/network step."""


class ReplySourceError(ValueError):
    """The specified reply source could not be safely read."""


@dataclass(frozen=True)
class Recipient:
    address: str
    display: str


@dataclass(frozen=True)
class DraftInput:
    to: str | None
    cc: str | None
    bcc: str | None
    subject: str
    body: str
    html: bool
    attachments: tuple[str, ...]
    subject_file: str | None = None
    body_file: str | None = None
    reply_to: MailRef | None = None
    reply_source_sha256: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    quote_suffix: str = ""


@dataclass(frozen=True)
class Draft:
    manifest: dict
    mime: str
    envelope_recipients: tuple[str, ...]
    body: str


MAX_RECIPIENTS = 100
_MESSAGE_ID = re.compile(r"^<[^<>\s]+>$")
_REFERENCES = re.compile(r"^(?:<[^<>\s]+>)(?:\s+<[^<>\s]+>)*$")
_DOT_ATOM = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_DOMAIN_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_CONFIRMATION = re.compile(r"^[0-9a-f]{64}$")


def decode_str(value: str | None) -> str:
    if not value:
        return ""
    return "".join(data.decode(charset or "utf-8", errors="replace") if isinstance(data, bytes) else data
                   for data, charset in decode_header(value))


def _has_controls(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _valid_address(address: str) -> bool:
    if not address or not address.isascii() or _has_controls(address) or any(character.isspace() for character in address):
        return False
    if address.count("@") != 1:
        return False
    local, domain = address.rsplit("@", 1)
    if len(local) > 64 or len(domain) > 253 or len(address) > 254:
        return False
    return bool(_DOT_ATOM.fullmatch(local) and "." in domain and
                all(_DOMAIN_LABEL.fullmatch(label) for label in domain.split(".")))


def _recipient_key(address: str) -> str:
    """Mailbox equality preserves local-part case and folds ASCII domains only."""
    local, domain = address.rsplit("@", 1)
    return local + "@" + domain.lower()


def _safe_unfold_header(value: str, *, label: str) -> str:
    """Accept only RFC folding (CRLF/LF followed by WSP), never bare controls."""
    if not isinstance(value, str):
        raise ReplySourceError(f"{label}不是文本")
    result: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\r":
            if index + 2 >= len(value) or value[index + 1] != "\n" or value[index + 2] not in " \t":
                raise ReplySourceError(f"{label}包含非法控制字符")
            index += 2
            while index < len(value) and value[index] in " \t":
                index += 1
            result.append(" ")
            continue
        if character == "\n":
            if index + 1 >= len(value) or value[index + 1] not in " \t":
                raise ReplySourceError(f"{label}包含非法控制字符")
            index += 1
            while index < len(value) and value[index] in " \t":
                index += 1
            result.append(" ")
            continue
        if character == "\t":
            result.append(" ")
            index += 1
            continue
        if ord(character) < 32 or ord(character) == 127:
            raise ReplySourceError(f"{label}包含非法控制字符")
        result.append(character)
        index += 1
    return "".join(result)


def _has_empty_mailbox_element(value: str) -> bool:
    """Reject empty comma elements while allowing quoted display-name commas."""
    quoted = escaped = False
    angles = 0
    start = 0
    for index, character in enumerate(value):
        if escaped:
            escaped = False
        elif quoted and character == "\\":
            escaped = True
        elif character == '"' and angles == 0:
            quoted = not quoted
        elif character == "<" and not quoted:
            if angles:
                return True
            angles += 1
        elif character == ">" and not quoted:
            if not angles:
                return True
            angles -= 1
        elif character == "," and not quoted and not angles:
            if not value[start:index].strip():
                return True
            start = index + 1
    return quoted or bool(angles) or not value[start:].strip()


def parse_recipients(value: str | None, *, label: str) -> tuple[Recipient, ...]:
    """Use stdlib mailbox parsing, then conservatively validate each address."""
    if value is None:
        return ()
    if not isinstance(value, str) or not value.strip():
        raise SendInputError(f"{label}不能为空")
    if _has_controls(value) or _has_empty_mailbox_element(value):
        raise SendInputError(f"{label}包含空元素或控制字符")
    recipients: list[Recipient] = []
    for name, address in getaddresses([value]):
        name, address = name.strip(), address.strip()
        if _has_controls(name) or not _valid_address(address):
            raise SendInputError(f"{label}包含非法邮箱地址")
        recipients.append(Recipient(address, formataddr((name, address)) if name else address))
    if not recipients:
        raise SendInputError(f"{label}不能为空")
    return tuple(recipients)


def normalize_recipient_groups(to: str | None, cc: str | None, bcc: str | None) -> tuple[
        tuple[Recipient, ...], tuple[Recipient, ...], tuple[Recipient, ...], tuple[str, ...]]:
    """Deduplicate across To/CC/BCC, retaining the first supplied mailbox."""
    seen: set[str] = set()

    def unique(value: str | None, label: str) -> tuple[Recipient, ...]:
        kept: list[Recipient] = []
        for recipient in parse_recipients(value, label=label):
            if _recipient_key(recipient.address) not in seen:
                seen.add(_recipient_key(recipient.address))
                kept.append(recipient)
        return tuple(kept)

    to_items, cc_items, bcc_items = unique(to, "收件人"), unique(cc, "抄送"), unique(bcc, "密送")
    envelope = tuple(item.address for group in (to_items, cc_items, bcc_items) for item in group)
    if not envelope:
        raise SendInputError("至少需要一个有效收件人")
    if len(envelope) > MAX_RECIPIENTS:
        raise SendInputError(f"收件人数量不能超过 {MAX_RECIPIENTS}")
    return to_items, cc_items, bcc_items, envelope


def _read_text(path_value: str, *, label: str, strip: bool = False) -> str:
    file_manifest(path_value, label=label)
    try:
        value = pathlib.Path(path_value).expanduser().resolve(strict=True).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SendInputError(f"无法读取{label}: {path_value}") from exc
    return value.strip() if strip else value


def _captured_text(path_value: str, *, label: str, strip: bool = False) -> tuple[str, CapturedFile]:
    captured = capture_file(path_value, label=label)
    try:
        text = captured.contents.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SendInputError(f"{label}必须是 UTF-8 文本: {path_value}") from exc
    return (text.strip() if strip else text), captured


def _validate_subject(subject: str) -> str:
    if not subject:
        raise SendInputError("邮件主题不能为空，请通过 --subject 或 --subject-file 提供")
    if "\r" in subject or "\n" in subject:
        raise SendInputError("邮件主题不能包含换行")
    return subject


def _validate_thread_headers(in_reply_to: str | None, references: str | None) -> tuple[str | None, str | None]:
    """Validate the exact thread headers that will be put into MIME."""
    if in_reply_to is not None:
        if not isinstance(in_reply_to, str) or not _MESSAGE_ID.fullmatch(in_reply_to):
            raise SendInputError("In-Reply-To 必须是一个合法 Message-ID")
    if references is not None:
        if not isinstance(references, str) or not _REFERENCES.fullmatch(references):
            raise SendInputError("References 必须是合法 Message-ID 列表")
    return in_reply_to, references


def _validate_account(account: str) -> str:
    if not isinstance(account, str) or not _valid_address(account):
        raise SendInputError("QQ_EMAIL 必须是合法 ASCII 邮箱地址")
    return account


def _validate_confirmation(value: str | None) -> None:
    if value is not None and not _CONFIRMATION.fullmatch(value):
        raise SendInputError("--confirmation 必须是 64 位小写十六进制摘要")


def _parse_attachments(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    paths = tuple(item.strip() for item in value.split(","))
    if any(not item for item in paths):
        raise SendInputError("附件路径不能为空")
    for path in paths:
        file_manifest(path, label="附件")
    return paths


def collect_draft_input(args, *, reply_to: MailRef | None = None) -> DraftInput:
    """Validate command/file input before credentials or any connection."""
    if args.subject is not None and args.subject_file is not None:
        raise SendInputError("--subject 与 --subject-file 不能同时使用")
    if args.body is not None and args.body_file is not None:
        raise SendInputError("--body 与 --body-file 不能同时使用")
    subject = args.subject if args.subject is not None else (_read_text(args.subject_file, label="主题文件", strip=True)
                                                              if args.subject_file else "")
    body = args.body if args.body is not None else (_read_text(args.body_file, label="正文文件")
                                                    if args.body_file else "")
    if subject:
        _validate_subject(subject)
    attachments = _parse_attachments(args.attachments)
    if args.to is not None or args.cc is not None or args.bcc is not None:
        normalize_recipient_groups(args.to, args.cc, args.bcc)
    return DraftInput(args.to, args.cc, args.bcc, subject, body, bool(args.html), attachments,
                      args.subject_file, args.body_file, reply_to)


def get_original_email(email_addr: str, auth_code: str, mail_id: str, folder: str, uidvalidity: str) -> dict:
    """Fetch one reply source by verified folder+UIDVALIDITY+UID."""
    reference = MailRef(folder, uidvalidity, mail_id)
    try:
        with imap_connection(Credentials(email_addr, auth_code)) as mail:
            select_verified_mailref(mail, reference, readonly=True)
            status, data = mail.uid("FETCH", reference.uid, "(BODY.PEEK[])")
        response = select_uid_fetch(data, reference.uid) if status == "OK" else None
        if response is None or not response.raw:
            raise ReplySourceError("无法读取原始邮件")
    except (MailRefError, ReplySourceError):
        raise
    except Exception as exc:
        raise ReplySourceError("无法读取原始邮件") from exc
    message = message_from_bytes(response.raw)
    message_id = _safe_unfold_header(message.get("Message-ID", ""), label="原始邮件 Message-ID")
    references = _safe_unfold_header(message.get("References", ""), label="原始邮件 References")
    if not _MESSAGE_ID.fullmatch(message_id):
        raise ReplySourceError("原始邮件缺少或包含非法 Message-ID")
    if references and not _REFERENCES.fullmatch(references):
        raise ReplySourceError("原始邮件包含非法 References")
    from_value = decode_str(_safe_unfold_header(message.get("From", ""), label="原始邮件发件人"))
    reply_to_value = decode_str(_safe_unfold_header(message.get("Reply-To", ""), label="原始邮件 Reply-To"))
    for label, value in (("原始邮件发件人", from_value), ("原始邮件 Reply-To", reply_to_value)):
        if value:
            parsed = parse_recipients(value, label=label)
            if len(parsed) != 1:
                raise ReplySourceError(f"{label}必须恰好包含一个邮箱地址")
    body_text = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                body_text = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", errors="replace")
                break
    else:
        body_text = (message.get_payload(decode=True) or b"").decode(message.get_content_charset() or "utf-8", errors="replace")
    if len(body_text) > 500:
        body_text = body_text[:500] + "..."
    subject_value = decode_str(_safe_unfold_header(message.get("Subject", ""), label="原始邮件主题"))
    return {"message_id": message_id, "references": references,
            "subject": subject_value, "from": from_value,
            "reply_to": reply_to_value, "body_snippet": body_text,
            "source_sha256": hashlib.sha256(response.raw).hexdigest()}


def _reply_input(source: DraftInput, original: dict, quote: bool) -> DraftInput:
    to = source.to or original["reply_to"] or original["from"]
    subject = source.subject or (original["subject"] if original["subject"].casefold().startswith("re:")
                                 else f"Re: {original['subject']}")
    suffix = ""
    if quote and original["body_snippet"]:
        suffix = "\n\n--- 原始邮件 ---\n发件人: {0}\n主题: {1}\n\n{2}".format(
            original["from"], original["subject"], original["body_snippet"])
    references = " ".join(value for value in (original["references"], original["message_id"]) if value)
    return DraftInput(to, source.cc, source.bcc, subject, source.body, source.html, source.attachments,
                      source.subject_file, source.body_file, source.reply_to, original["source_sha256"],
                      original["message_id"], references, suffix)


def build_draft(account: str, source: DraftInput) -> Draft:
    """Pure construction: normalize, bind files, build MIME; never connects."""
    account = _validate_account(account)
    in_reply_to, references = _validate_thread_headers(source.in_reply_to, source.references)
    subject, subject_capture = (source.subject, None)
    if source.subject_file:
        subject, subject_capture = _captured_text(source.subject_file, label="主题文件", strip=True)
    subject = _validate_subject(subject)
    body, body_capture = (source.body, None)
    if source.body_file:
        body, body_capture = _captured_text(source.body_file, label="正文文件")
    body += source.quote_suffix
    if not body:
        raise SendInputError("邮件正文不能为空，请通过 --body 或 --body-file 提供")
    to, cc, bcc, envelope = normalize_recipient_groups(source.to, source.cc, source.bcc)
    attachment_captures = [capture_file(path, label="附件") for path in source.attachments]
    manifest = send_manifest(account=account, to=[item.display for item in to], cc=[item.display for item in cc],
                             bcc=[item.display for item in bcc], envelope_recipients=envelope, subject=subject,
                             body=body, attachments=(), reply_to=source.reply_to,
                             html=source.html, subject_file=source.subject_file, body_file=source.body_file,
                             reply_source_sha256=source.reply_source_sha256,
                             in_reply_to=in_reply_to, references=references,
                             subject_file_manifest=subject_capture.manifest if subject_capture else None,
                             body_file_manifest=body_capture.manifest if body_capture else None,
                             attachment_manifests=[capture.manifest for capture in attachment_captures])
    message = MIMEMultipart(boundary="qqmail-" + manifest["confirmation"][:32])
    message["From"], message["To"], message["Subject"] = account, ", ".join(item.display for item in to), subject
    if cc:
        message["Cc"] = ", ".join(item.display for item in cc)
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    message.attach(MIMEText(body, "html" if source.html else "plain", "utf-8"))
    for capture in attachment_captures:
        path = pathlib.Path(capture.manifest["path"])
        part = MIMEBase("application", "octet-stream")
        part.set_payload(capture.contents)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=path.name)
        message.attach(part)
    return Draft(manifest, message.as_string(), envelope, body)


def preview_result(draft: Draft) -> dict:
    return {"status": "preview", "message": "草稿已构建；请以 --confirm --confirmation <摘要> 确认发送",
            "to": draft.manifest["to"], "cc": draft.manifest["cc"], "bcc": draft.manifest["bcc"],
            "envelope_recipients": list(draft.envelope_recipients), "subject": draft.manifest["subject"],
            "body_summary": {"length": len(draft.body), "sha256": draft.manifest["body_sha256"], "preview": draft.body[:200]},
            "attachments": draft.manifest["attachments"], "subject_file": draft.manifest["subject_file"],
            "body_file": draft.manifest["body_file"], "reply_to": draft.manifest["reply_to"],
            "confirmation": draft.manifest["confirmation"], "manifest": draft.manifest}


def _safe_smtp_reason(value: str | bytes) -> str:
    """Decode SMTP diagnostics as one safe display line, preserving no controls."""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    return " ".join(("".join(" " if (ord(character) < 32 or ord(character) == 127) else character
                               for character in text)).split())


def _validated_rejections(response, envelope: tuple[str, ...], *, require_complete: bool = False) -> dict[str, tuple[int, str]]:
    """Defensively validate the complete ``sendmail`` refusal partition."""
    if not isinstance(response, Mapping):
        raise ValueError("SMTP 拒收结果不是字典")
    expected = {_recipient_key(address): address for address in envelope}
    rejected: dict[str, tuple[int, str]] = {}
    for address, details in response.items():
        if not isinstance(address, str) or not _valid_address(address):
            raise ValueError("SMTP 拒收结果包含非法收件人")
        key = _recipient_key(address)
        if key not in expected or key in rejected:
            raise ValueError("SMTP 拒收结果与信封收件人不一致")
        if (not isinstance(details, tuple) or len(details) != 2 or isinstance(details[0], bool) or
                not isinstance(details[0], int) or not 400 <= details[0] <= 599 or
                not isinstance(details[1], (str, bytes))):
            raise ValueError("SMTP 拒收详情格式非法")
        rejected[key] = (details[0], _safe_smtp_reason(details[1]))
    if require_complete and set(rejected) != set(expected):
        raise ValueError("SMTPRecipientsRefused 未完整覆盖信封收件人")
    return rejected


def _invalid_smtp_response() -> dict:
    return error_result("SMTP服务器返回无效拒收结果", code="smtp_invalid_response", delivery_indeterminate=True)


def transmit_draft(credentials: Credentials, draft: Draft) -> dict:
    """Open one SMTP connection, make one sendmail call, and classify refusal."""
    send_attempted = False
    all_recipients_refused = False
    try:
        with smtp_connection(credentials) as server:
            send_attempted = True
            rejected = server.sendmail(credentials.email, list(draft.envelope_recipients), draft.mime)
    except smtplib.SMTPRecipientsRefused as exc:
        rejected = exc.recipients
        all_recipients_refused = True
    except smtplib.SMTPAuthenticationError:
        fields = {"delivery_indeterminate": True} if send_attempted else {}
        return error_result("SMTP认证失败，请检查授权码是否正确", code="smtp_authentication_failed", **fields)
    except smtplib.SMTPException:
        fields = {"delivery_indeterminate": True} if send_attempted else {}
        return error_result("SMTP传输失败", code="smtp_transport_failed", **fields)
    except OSError:
        fields = {"delivery_indeterminate": True} if send_attempted else {}
        return error_result("SMTP连接或TLS失败", code="smtp_connection_failed", **fields)
    except Exception:
        fields = {"delivery_indeterminate": True} if send_attempted else {}
        return error_result("SMTP传输失败", code="smtp_transport_failed", **fields)
    try:
        rejected_by_key = _validated_rejections(rejected, draft.envelope_recipients,
                                                require_complete=all_recipients_refused)
    except ValueError:
        return _invalid_smtp_response()
    accepted = [address for address in draft.envelope_recipients if _recipient_key(address) not in rejected_by_key]
    failed = [{"recipient": address, "reason": rejected_by_key[_recipient_key(address)][1][:256],
               "code": str(rejected_by_key[_recipient_key(address)][0])}
              for address in draft.envelope_recipients if _recipient_key(address) in rejected_by_key]
    result = batch_result(succeeded=accepted, failed=failed, recipients=list(draft.envelope_recipients))
    result["message"] = "邮件发送成功" if not failed else ("部分收件人被SMTP服务器拒收" if accepted else "所有收件人均被SMTP服务器拒收")
    result["confirmation"] = draft.manifest["confirmation"]
    return result


def send_email(email_addr, auth_code, to, subject, body, cc=None, bcc=None, html=False, attachments=None,
               in_reply_to=None, references=None, confirmation=None, subject_file=None, body_file=None,
               reply_to=None, reply_source_sha256=None):
    """Compatibility API: omitted confirmation always returns a preview."""
    try:
        _validate_confirmation(confirmation)
        source = DraftInput(to, cc, bcc, subject, body, html, tuple(attachments or ()), subject_file, body_file,
                            reply_to, reply_source_sha256, in_reply_to, references)
        draft = build_draft(email_addr, source)
        if confirmation is None:
            return preview_result(draft)
        if not confirmation_matches(draft.manifest, confirmation):
            return error_result("confirmation 与当前发送清单不匹配", code="confirmation_mismatch")
        return transmit_draft(Credentials(email_addr, auth_code), draft)
    except (SendInputError, ValueError) as exc:
        return error_result(str(exc), code="invalid_send_input")


def test_smtp(credentials: Credentials) -> dict:
    """TLS/auth connectivity test only: this function never invokes sendmail."""
    try:
        _validate_account(credentials.email)
        with smtp_connection(credentials):
            pass
        return {"status": "success", "message": "SMTP TLS与认证测试成功", "smtp_test": True, "sent": False}
    except SendInputError as exc:
        return error_result(str(exc), code="invalid_sender")
    except smtplib.SMTPAuthenticationError:
        return error_result("SMTP认证失败，请检查授权码是否正确", code="smtp_authentication_failed")
    except (smtplib.SMTPException, OSError):
        return error_result("SMTP连接、TLS或认证测试失败", code="smtp_connection_failed")
    except Exception:
        return error_result("SMTP连接、TLS或认证测试失败", code="smtp_connection_failed")


def _parser() -> StructuredArgumentParser:
    parser = StructuredArgumentParser(description="发送/回复QQ邮件")
    parser.add_argument("--test", action="store_true", help="仅测试SMTP TLS与认证，不发送邮件")
    parser.add_argument("--confirm", action="store_true", help="确认按预览清单发送")
    parser.add_argument("--confirmation", help="预览返回的 confirmation 摘要")
    parser.add_argument("--to", help="收件人邮箱（可含显示名，多个逗号分隔）")
    parser.add_argument("--cc", help="抄送（多个逗号分隔）")
    parser.add_argument("--bcc", help="密送（多个逗号分隔，不写入邮件头）")
    parser.add_argument("--reply-to-id", help="回复指定邮件的 UID")
    parser.add_argument("--reply-folder", help="原始邮件所在文件夹（回复时必填）")
    parser.add_argument("--reply-uidvalidity", help="原始邮件的 UIDVALIDITY（回复时必填）")
    parser.add_argument("--reply-quote", action="store_true", help="在正文中引用原邮件片段")
    parser.add_argument("--subject", help="邮件主题")
    parser.add_argument("--subject-file", help="从文件读取邮件主题")
    parser.add_argument("--body", help="邮件正文（纯文本）")
    parser.add_argument("--body-file", help="从文件读取邮件正文")
    parser.add_argument("--html", action="store_true", help="以HTML格式发送正文")
    parser.add_argument("--attachments", help="附件路径（多个逗号分隔）")
    return parser


def _reply_reference(args) -> MailRef | None:
    supplied = any((args.reply_to_id is not None, args.reply_folder is not None,
                    args.reply_uidvalidity is not None, args.reply_quote))
    if not supplied:
        return None
    if not args.reply_to_id or not args.reply_folder or not args.reply_uidvalidity:
        raise MailRefError("回复邮件时 --reply-to-id、--reply-folder 和 --reply-uidvalidity 均为必填参数")
    return MailRef(args.reply_folder, args.reply_uidvalidity, args.reply_to_id)


def _test_has_send_options(args) -> bool:
    """Detect option appearance, including explicitly provided empty strings."""
    return any((args.confirm, args.confirmation is not None, args.to is not None, args.cc is not None,
                args.bcc is not None, args.reply_to_id is not None, args.reply_folder is not None,
                args.reply_uidvalidity is not None, args.reply_quote, args.subject is not None,
                args.subject_file is not None, args.body is not None, args.body_file is not None,
                args.attachments is not None, args.html))


def main() -> int:
    parser = _parser()
    try:
        args = parser.parse_args()
        if args.test:
            if _test_has_send_options(args):
                raise SendInputError("--test 不能与发送、回复或确认参数同时使用")
            return emit_json(test_smtp(load_credentials()))
        if args.confirm != (args.confirmation is not None):
            raise SendInputError("--confirm 必须与 --confirmation 同时提供")
        _validate_confirmation(args.confirmation)
        reference = _reply_reference(args)
        if reference is None and not args.to:
            raise SendInputError("发送邮件需要 --to 参数（或使用 --reply-to-id 回复邮件）")
        source = collect_draft_input(args, reply_to=reference)
        if reference is None:
            # New-mail subject/body are not supplied by the server, so reject
            # their absence before credentials or any network connection.
            _validate_subject(source.subject)
            if not source.body:
                raise SendInputError("邮件正文不能为空，请通过 --body 或 --body-file 提供")
        elif not source.body and not source.body_file and not args.reply_quote:
            raise SendInputError("回复邮件必须提供正文，或使用 --reply-quote")
        elif source.body_file and not source.body and not args.reply_quote:
            raise SendInputError("正文文件不能为空，或使用 --reply-quote")
        credentials = load_credentials()
        _validate_account(credentials.email)
        if reference is not None:
            original = get_original_email(credentials.email, credentials.auth_code, reference.uid, reference.folder,
                                        reference.uidvalidity)
            source = _reply_input(source, original, args.reply_quote)
        draft = build_draft(credentials.email, source)
        if not args.confirm:
            return emit_json(preview_result(draft))
        if not confirmation_matches(draft.manifest, args.confirmation):
            return emit_json(error_result("confirmation 与当前发送清单不匹配", code="confirmation_mismatch"))
        return emit_json(transmit_draft(credentials, draft))
    except (ArgumentParseError, SendInputError) as exc:
        return emit_json(error_result(f"参数错误: {exc}", code="invalid_arguments"))
    except MailRefError as exc:
        return emit_json(error_result(str(exc), code="invalid_mailref"))
    except CredentialError as exc:
        return emit_json(error_result(str(exc), code="missing_credentials"))
    except ReplySourceError as exc:
        return emit_json(error_result(str(exc), code="reply_source_unavailable"))
    except Exception:
        return emit_json(error_result("无法构建发送草稿", code="send_build_failed"))


if __name__ == "__main__":
    raise SystemExit(main())
