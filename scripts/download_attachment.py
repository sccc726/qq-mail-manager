#!/usr/bin/env python3
"""Download MIME attachments addressed by IMAP UID without unsafe local writes."""
from __future__ import annotations

import base64
import errno
import hashlib
import io
import os
import pathlib
import quopri
import re
import secrets
import sys
import unicodedata

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from qqmail_core.config import CredentialError, Credentials, load_credentials
from qqmail_core.connections import imap_connection
from qqmail_core.imap_uid import select_uid_metadata, select_uid_section
from qqmail_core.mailref import MailRef, MailRefError, select_verified_mailref
from qqmail_core.mime import bodystructure_parts, decode_header_value
from qqmail_core.results import (ArgumentParseError, StructuredArgumentParser,
                                 argument_error_result, batch_result, emit_json,
                                 error_result)


# These limits keep a mistaken or hostile message from consuming arbitrary disk.
# They are module constants so local callers and offline tests can configure them.
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_ATTACHMENT_WIRE_BYTES = MAX_ATTACHMENT_BYTES * 2
MAX_DOWNLOAD_WIRE_BYTES = MAX_DOWNLOAD_BYTES * 2
_RESERVED_WINDOWS_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                           *(f"LPT{i}" for i in range(1, 10))}
MAX_FILENAME_UTF16_UNITS = 180


class AttachmentLimitError(ValueError):
    pass


def _truncate_utf16(value: str, units: int) -> str:
    encoded = value.encode("utf-16-le")[:max(0, units) * 2]
    return encoded.decode("utf-16-le", errors="ignore")


def safe_filename(filename):
    """Turn an untrusted MIME filename into one Windows-safe basename."""
    name = unicodedata.normalize("NFC", decode_header_value(filename)).replace("\x00", "")
    name = "".join(char for char in name if unicodedata.category(char) != "Cf")
    # Slash, backslash, drive/UNC prefixes and ADS separators are all data, not
    # path syntax.  This deliberately preserves a useful basename where safe.
    name = re.split(r"[\\/]+", name)[-1]
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).rstrip(". ")
    name = re.sub(r"\.{2,}", "_", name)
    if not name or name.strip(". ") == "":
        return "attachment"
    stem = name.split(".", 1)[0].rstrip(". ").upper()
    if stem in _RESERVED_WINDOWS_NAMES:
        name = "_" + name
    # Windows counts UTF-16 code units, not Python code points.  Keep room for
    # deterministic collision suffixes without splitting a surrogate pair.
    if len(name.encode("utf-16-le")) // 2 > MAX_FILENAME_UTF16_UNITS:
        suffix = pathlib.PurePath(name).suffix
        suffix = _truncate_utf16(suffix, MAX_FILENAME_UTF16_UNITS)
        suffix_units = len(suffix.encode("utf-16-le")) // 2
        if suffix_units >= MAX_FILENAME_UTF16_UNITS:
            name = suffix
        else:
            name = _truncate_utf16(name[:len(name) - len(pathlib.PurePath(name).suffix)],
                                   MAX_FILENAME_UTF16_UNITS - suffix_units) + suffix
    return name or "attachment"


def validate_output_dir(value) -> pathlib.Path:
    """Normalize a freely chosen local output directory without trusted roots."""
    if not isinstance(value, (str, os.PathLike)) or not os.fspath(value) or "\x00" in os.fspath(value):
        raise MailRefError("下载目录无效")
    try:
        path = pathlib.Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise MailRefError("下载目录无效") from exc
    if path.exists() and not path.is_dir():
        raise MailRefError("下载目录不是目录")
    return path


def _validate_target_file(value):
    if value is not None and (not isinstance(value, str) or not value or "\x00" in value):
        raise MailRefError("附件名称无效")
    return value


def prepare_output_dir(value) -> pathlib.Path:
    path = validate_output_dir(value)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MailRefError("下载目录不可用") from exc
    if not path.is_dir():
        raise MailRefError("下载目录不是目录")
    return path


class _LimitedWriter:
    def __init__(self, handle, limit: int):
        self.handle, self.limit, self.count = handle, limit, 0

    def write(self, data: bytes) -> int:
        if self.count + len(data) > self.limit:
            raise AttachmentLimitError(f"附件超过大小上限 ({self.limit} 字节)")
        self.count += len(data)
        return self.handle.write(data)


def _exclusive_temp(directory: pathlib.Path) -> tuple[pathlib.Path, int]:
    for _ in range(100):
        candidate = directory / (".qqmail-" + secrets.token_hex(16) + ".part")
        try:
            fd = os.open(candidate, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        return candidate, fd
    raise OSError("无法创建唯一临时附件文件")


def _decode_to_file(payload: bytes, fd: int, limit: int, encoding: str) -> int:
    """Decode using, but never closing, the caller-owned destination fd."""
    source = bytes(payload)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            writer = _LimitedWriter(handle, limit)
            stream = io.BytesIO(source)
            if encoding == "base64":
                carry = b""
                terminated = False
                while block := stream.read(64 * 1024):
                    compact = carry + b"".join(block.split())
                    if terminated and compact:
                        raise ValueError("base64附件 padding 后含有数据")
                    usable = len(compact) - (len(compact) % 4)
                    if usable:
                        quartet_data = compact[:usable]
                        writer.write(base64.b64decode(quartet_data, validate=True))
                        terminated = b"=" in quartet_data
                    carry = compact[usable:]
                if carry:
                    if terminated:
                        raise ValueError("base64附件 padding 后含有数据")
                    writer.write(base64.b64decode(carry, validate=True))
            elif encoding in {"quoted-printable", "quopri"}:
                position = 0
                while position < len(source):
                    if source[position:position + 1] != b"=":
                        position += 1
                        continue
                    following = source[position + 1:position + 3]
                    if following == b"\r\n":
                        position += 3
                    elif source[position + 1:position + 2] == b"\n":
                        position += 2
                    elif len(following) == 2 and re.fullmatch(rb"[0-9A-Fa-f]{2}", following):
                        position += 3
                    else:
                        raise ValueError("quoted-printable附件编码无效")
                carry = b""
                while block := stream.read(64 * 1024):
                    combined = carry + block
                    usable = max(0, len(combined) - 3)
                    # Never hand quopri a prefix ending in an incomplete =XX
                    # escape or soft break; retain it for the next fixed block.
                    if usable and combined[usable - 1:usable] == b"=":
                        usable -= 1
                    elif usable >= 2 and combined[usable - 2:usable - 1] == b"=":
                        usable -= 2
                    if usable:
                        writer.write(quopri.decodestring(combined[:usable]))
                    carry = combined[usable:]
                if carry:
                    decoded = quopri.decodestring(carry)
                    if carry.endswith(b"=") or re.search(rb"=[0-9A-Fa-f]$", carry):
                        raise ValueError("quoted-printable附件编码截断")
                    writer.write(decoded)
            else:
                while block := stream.read(64 * 1024):
                    writer.write(block)
            handle.flush()
            os.fsync(handle.fileno())
        return writer.count
    except Exception:
        raise


def _same_identity(first, second) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _unlink_if_identity(path: pathlib.Path, expected) -> None:
    """Remove only the file created by this operation, never a raced-in path."""
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        return
    if _same_identity(current, expected):
        try:
            path.unlink()
        except OSError:
            pass


def _commit_without_overwrite(temporary: pathlib.Path, directory: pathlib.Path, requested: str, fd: int) -> tuple[pathlib.Path, str | None]:
    source_identity = os.fstat(fd)
    path_identity = os.stat(temporary, follow_symlinks=False)
    if (source_identity.st_dev, source_identity.st_ino) != (path_identity.st_dev, path_identity.st_ino):
        raise OSError("临时附件文件身份发生变化")
    base = pathlib.Path(requested)
    for index in range(10_000):
        if index == 0:
            name = requested
        else:
            ending = f"_{index}{base.suffix}"
            name = _truncate_utf16(base.stem, MAX_FILENAME_UTF16_UNITS - len(ending.encode("utf-16-le")) // 2) + ending
        destination = directory / name
        try:
            # link() is atomic and refuses an existing target, unlike replace().
            os.link(temporary, destination)
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                continue
            if exc.errno not in {errno.EPERM, errno.EOPNOTSUPP, getattr(errno, "ENOTSUP", errno.EOPNOTSUPP)}:
                raise OSError("无法安全提交附件文件") from exc
            # FAT/SMB can lack hard links.  Reserve a destination exclusively;
            # this remains no-overwrite but is visibly non-atomic while copied.
            # Keep that descriptor through content and identity verification so
            # a pathname swap cannot be reported as a successful download.
            destination_fd = source_fd = None
            destination_identity = None
            try:
                destination_fd = os.open(destination, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            try:
                destination_identity = os.fstat(destination_fd)
                source_fd = os.dup(fd)
                os.lseek(source_fd, 0, os.SEEK_SET)
                source_hash = hashlib.sha256()
                with os.fdopen(destination_fd, "wb", closefd=False) as output, \
                        os.fdopen(source_fd, "rb", closefd=False) as source:
                    while block := source.read(64 * 1024):
                        source_hash.update(block)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                closing_fd = source_fd
                source_fd = None
                os.close(closing_fd)
                if not _same_identity(os.fstat(destination_fd), destination_identity):
                    raise OSError("附件发布身份校验失败")
                os.lseek(destination_fd, 0, os.SEEK_SET)
                destination_hash = hashlib.sha256()
                while block := os.read(destination_fd, 64 * 1024):
                    destination_hash.update(block)
                if destination_hash.digest() != source_hash.digest():
                    raise OSError("附件发布内容校验失败")
                # Closing before the final pathname check catches the common
                # close-to-stat replacement race.  If a filesystem cannot
                # preserve this identity, fail closed rather than misreport.
                closing_fd = destination_fd
                destination_fd = None
                os.close(closing_fd)
                published = os.stat(destination, follow_symlinks=False)
                if not _same_identity(published, destination_identity):
                    raise OSError("附件发布身份校验失败")
            except Exception:
                # Relinquish the bookkeeping before close(): on platforms
                # where a close error can follow an actual release, retrying
                # the integer could close an unrelated reused descriptor.
                leaked_fds = (destination_fd, source_fd)
                destination_fd = source_fd = None
                for leaked_fd in leaked_fds:
                    if leaked_fd is not None:
                        try:
                            os.close(leaked_fd)
                        except OSError:
                            pass
                if destination_identity is not None:
                    _unlink_if_identity(destination, destination_identity)
                raise
            return destination, None
        published = os.stat(destination, follow_symlinks=False)
        if not _same_identity(published, source_identity):
            _unlink_if_identity(destination, source_identity)
            raise OSError("附件发布身份校验失败")
        return destination, None
    raise OSError("同名附件过多，无法安全保存")


def _save_part(payload: bytes, encoding: str, directory: pathlib.Path, name: str, remaining: int) -> tuple[pathlib.Path, int, str | None]:
    limit = min(MAX_ATTACHMENT_BYTES, remaining)
    if limit < 0 or (limit == 0 and payload):
        raise AttachmentLimitError(f"本次下载超过总大小上限 ({MAX_DOWNLOAD_BYTES} 字节)")
    temporary, fd = _exclusive_temp(directory)
    published = False
    destination = None
    size = 0
    try:
        size = _decode_to_file(payload, fd, limit, encoding)
        destination, _warning = _commit_without_overwrite(temporary, directory, name, fd)
        published = True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # A successful publication is authoritative; best-effort cleanup
            # must never turn it into an error or trigger a retry.
            if published:
                warning = "临时文件清理失败；已安全发布附件"
            else:
                warning = None
        else:
            warning = None
    if destination is None:
        raise OSError("附件发布失败")
    return destination, size, warning


def download_attachments_for_mail(mail, reference, output_dir=".", target_file=None, budget=None):
    """Fetch a full message only for an explicit attachment download request."""
    try:
        directory = prepare_output_dir(output_dir)
        select_verified_mailref(mail, reference, readonly=True)
        status, data = mail.uid("FETCH", reference.uid, "(UID BODYSTRUCTURE)")
        structure = select_uid_metadata(data, reference.uid, required_item="BODYSTRUCTURE") if status == "OK" else None
        if structure is None:
            raise MailRefError("无法获取邮件结构")
        parts = [part for part in bodystructure_parts(structure) if part["attachment"]]
        downloaded, failed, available, total_size = [], [], [], 0
        if budget is None:
            budget = {"remaining": MAX_DOWNLOAD_BYTES, "wire_remaining": MAX_DOWNLOAD_WIRE_BYTES}
        for part in parts:
            filename = decode_header_value(part["filename"]) or f"attachment-{part['section']}"
            available.append(filename)
            if target_file and filename != target_file:
                continue
            try:
                section = str(part["section"])
                wire_size = part.get("size")
                if not isinstance(wire_size, int) or wire_size < 0 or wire_size > MAX_ATTACHMENT_WIRE_BYTES:
                    raise AttachmentLimitError("附件 wire 大小未知或超过上限")
                if wire_size > budget["wire_remaining"]:
                    raise AttachmentLimitError("本次下载超过 wire 总大小上限")
                if wire_size and budget["remaining"] <= 0:
                    raise AttachmentLimitError("本次下载超过总大小上限")
                if wire_size == 0:
                    payload = b""
                elif "payload" in part:
                    payload = part["payload"]
                    payload = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload or b"")
                else:
                    budget["wire_remaining"] -= wire_size
                    fetch_status, fetch_data = mail.uid("FETCH", reference.uid,
                                                        f"(UID BODY.PEEK[{section}]<0.{wire_size}>)")
                    response = select_uid_section(fetch_data, reference.uid, section, maximum=wire_size,
                                                  expected_length=wire_size) if fetch_status == "OK" else None
                    if response is None or len(response.raw) != wire_size:
                        raise MailRefError("附件 section FETCH失败")
                    payload = response.raw
                destination, size, warning = _save_part(payload, str(part["encoding"]), directory, safe_filename(filename),
                                                        budget["remaining"])
                total_size += size
                budget["remaining"] -= size
                item = {"name": destination.name, "size": size, "path": str(destination.resolve())}
                if warning:
                    item["cleanup_warning"] = warning
                downloaded.append(item)
            except Exception as exc:
                failed.append({"name": filename, "message": str(exc) or "附件保存失败"})
        if target_file and not downloaded and not failed:
            raise MailRefError(f"未找到附件: {target_file}")
        status = "partial" if downloaded and failed else "error" if failed else "success"
        return {**reference.public_dict(), "status": status, "downloaded": downloaded,
                "download_count": len(downloaded), "total_size": total_size,
                "attachment_failed": failed, "available": available}
    except Exception as exc:
        return {**reference.public_dict(), "status": "error", "message": str(exc) or "下载附件失败"}


def download_attachments(email_addr, auth_code, mail_ids, folder, uidvalidity, output_dir=".", target_file=None):
    try:
        references = [MailRef(folder, uidvalidity, uid) for uid in mail_ids]
        directory = prepare_output_dir(output_dir)
        _validate_target_file(target_file)
    except (MailRefError, OSError) as exc:
        return error_result(str(exc), code="invalid_download_path")
    try:
        with imap_connection(Credentials(email_addr, auth_code)) as mail:
            budget = {"remaining": MAX_DOWNLOAD_BYTES, "wire_remaining": MAX_DOWNLOAD_WIRE_BYTES}
            outcomes = [download_attachments_for_mail(mail, reference, directory, target_file, budget)
                        for reference in references]
    except Exception:
        return error_result("IMAP连接或认证失败", code="imap_error")
    good = [item for item in outcomes if item["status"] in {"success", "partial"}]
    bad = [item for item in outcomes if item["status"] == "error"]
    result = batch_result(succeeded=good, failed=bad, folder=folder, uidvalidity=uidvalidity,
                        results=outcomes, total_downloaded=sum(item.get("download_count", 0) for item in outcomes),
                        total_size=sum(item.get("total_size", 0) for item in outcomes))
    if any(item["status"] == "partial" for item in outcomes):
        result["status"] = "partial"
    return result


def main():
    parser = StructuredArgumentParser(description="下载QQ邮箱邮件附件（UID；默认单文件50MiB、单次总计100MiB）")
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
        prepare_output_dir(args.dir)
        _validate_target_file(args.file)
    except MailRefError as exc:
        return emit_json(error_result(str(exc), code="invalid_download_path"))
    try:
        credentials = load_credentials()
    except CredentialError as exc:
        return emit_json(error_result(str(exc), code="missing_credentials"))
    return emit_json(download_attachments(credentials.email, credentials.auth_code, mail_ids,
                                          args.folder, args.uidvalidity, args.dir, args.file))


if __name__ == "__main__":
    raise SystemExit(main())
