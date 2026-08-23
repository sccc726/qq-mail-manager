"""Deterministic, side-effect-free confirmation manifests for future writes."""
from __future__ import annotations

import hashlib
import hmac
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .mailref import MailRef


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_digest(manifest: dict) -> str:
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _digest_bytes(payload)


def _ordered_refs(references: Iterable[MailRef]) -> list[dict[str, str]]:
    unique = {(ref.folder, ref.uidvalidity, ref.uid): ref for ref in references}
    return [unique[key].public_dict() for key in sorted(unique)]


def move_manifest(*, action: str, source_folder: str, destination_folder: str,
                  references: Iterable[MailRef]) -> dict:
    """Create a canonical move/delete preview manifest and its confirmation digest."""
    if action not in {"move", "delete"}:
        raise ValueError("action must be move or delete")
    references = list(references)
    if not references or any(reference.folder != source_folder for reference in references):
        raise ValueError("移动清单中的邮件必须全部属于源文件夹")
    manifest = {
        "kind": "move",
        "action": action,
        "source_folder": source_folder,
        "destination_folder": destination_folder,
        "mailrefs": _ordered_refs(references),
    }
    return {**manifest, "confirmation": _canonical_digest(manifest)}


def _address_values(values: Iterable[str] | str | None) -> list[str]:
    if values is None:
        return []
    raw = values.split(",") if isinstance(values, str) else values
    return [str(value).strip() for value in raw if str(value).strip()]


@dataclass(frozen=True)
class CapturedFile:
    """One immutable file snapshot used for both confirmation and MIME."""

    manifest: dict[str, str | int]
    contents: bytes


def capture_file(path_value: str | Path, *, label: str = "文件") -> CapturedFile:
    """Return the complete, canonical identity of a regular input file.

    Resolving before recording deliberately makes confirmations independent of
    the current directory.  Hashing is the authoritative change detector;
    size and mtime make previews useful to humans and catch common changes
    before a hash needs to be inspected.
    """
    try:
        path = Path(path_value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label}不存在或无法解析: {path_value}") from exc
    try:
        info = path.stat()
    except OSError as exc:
        raise ValueError(f"无法读取{label}: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label}必须是普通文件: {path}")
    try:
        contents = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"无法读取{label}: {path}") from exc
    try:
        after = path.stat()
    except OSError as exc:
        raise ValueError(f"读取{label}期间文件发生变化: {path}") from exc
    if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(contents) != after.st_size:
        raise ValueError(f"读取{label}期间文件发生变化: {path}")
    return CapturedFile({
        "path": str(path),
        "type": "regular",
        "size": len(contents),
        "mtime_ns": after.st_mtime_ns,
        "sha256": _digest_bytes(contents),
    }, contents)


def file_manifest(path_value: str | Path, *, label: str = "文件") -> dict[str, str | int]:
    """Compatibility metadata accessor; use ``capture_file`` when consuming data."""
    return capture_file(path_value, label=label).manifest


def send_manifest(*, account: str, to: Iterable[str] | str, cc: Iterable[str] | str | None,
                  bcc: Iterable[str] | str | None, subject: str, body: str,
                  attachments: Iterable[str | Path] = (), reply_to: MailRef | None = None,
                  html: bool = False, subject_file: str | Path | None = None,
                  body_file: str | Path | None = None,
                  reply_source_sha256: str | None = None,
                  envelope_recipients: Iterable[str] | None = None,
                  in_reply_to: str | None = None,
                  references: str | None = None,
                  subject_file_manifest: dict[str, str | int] | None = None,
                  body_file_manifest: dict[str, str | int] | None = None,
                  attachment_manifests: Iterable[dict[str, str | int]] | None = None) -> dict:
    """Create a deterministic send preview without opening an SMTP connection."""
    manifest = {
        "kind": "send",
        "account": account,
        "to": _address_values(to),
        "cc": _address_values(cc),
        "bcc": _address_values(bcc),
        "envelope_recipients": _address_values(envelope_recipients),
        "subject": subject,
        "body_sha256": _digest_bytes(body.encode("utf-8")),
        "html": bool(html),
        "subject_file": subject_file_manifest if subject_file_manifest is not None else (
            file_manifest(subject_file, label="主题文件") if subject_file else None),
        "body_file": body_file_manifest if body_file_manifest is not None else (
            file_manifest(body_file, label="正文文件") if body_file else None),
        "attachments": list(attachment_manifests) if attachment_manifests is not None else [
            file_manifest(path, label="附件") for path in attachments],
        "reply_to": reply_to.public_dict() if reply_to else None,
        "reply_source_sha256": reply_source_sha256,
        "in_reply_to": in_reply_to,
        "references": references,
    }
    return {**manifest, "confirmation": _canonical_digest(manifest)}


def confirmation_matches(manifest: dict, confirmation: str) -> bool:
    """Verify a supplied confirmation against all manifest fields except itself."""
    unsigned = {key: value for key, value in manifest.items() if key != "confirmation"}
    return hmac.compare_digest(_canonical_digest(unsigned), confirmation)
