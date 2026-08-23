"""Deterministic, side-effect-free confirmation manifests for future writes."""
from __future__ import annotations

import hashlib
import hmac
import json
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


def _attachment_manifest(path_value: str | Path) -> dict[str, str | int]:
    path = Path(path_value).expanduser().resolve(strict=True)
    contents = path.read_bytes()
    return {"path": str(path), "size": len(contents), "sha256": _digest_bytes(contents)}


def send_manifest(*, account: str, to: Iterable[str] | str, cc: Iterable[str] | str | None,
                  bcc: Iterable[str] | str | None, subject: str, body: str,
                  attachments: Iterable[str | Path] = (), reply_to: MailRef | None = None,
                  html: bool = False) -> dict:
    """Create a deterministic send preview without opening an SMTP connection."""
    manifest = {
        "kind": "send",
        "account": account,
        "to": _address_values(to),
        "cc": _address_values(cc),
        "bcc": _address_values(bcc),
        "subject": subject,
        "body_sha256": _digest_bytes(body.encode("utf-8")),
        "html": bool(html),
        "attachments": [_attachment_manifest(path) for path in attachments],
        "reply_to": reply_to.public_dict() if reply_to else None,
    }
    return {**manifest, "confirmation": _canonical_digest(manifest)}


def confirmation_matches(manifest: dict, confirmation: str) -> bool:
    """Verify a supplied confirmation against all manifest fields except itself."""
    unsigned = {key: value for key, value in manifest.items() if key != "confirmation"}
    return hmac.compare_digest(_canonical_digest(unsigned), confirmation)
